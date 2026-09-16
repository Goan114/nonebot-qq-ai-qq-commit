import json
import re
import secrets
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field

from .github import valid_signature

QQ = Annotated[str, Field(pattern=r"^[0-9]{5,20}$")]


class BanBody(BaseModel):
    qq: QQ
    reason: str = Field(min_length=1, max_length=1000)


class DeleteBody(BaseModel):
    qq: QQ
    ids: list[int] | None = None
    all: bool = False


class CommentBody(BaseModel):
    comment: str = Field(max_length=2000)
    status: Literal["open", "deferred", "archived"] = "deferred"


class FAQBody(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    answer: str = Field(min_length=1, max_length=4000)
    enabled: bool = True


class DecisionBody(BaseModel):
    action: Literal["accept", "reject"]


class RepoBody(BaseModel):
    repo_key: str


class VisionBody(BaseModel):
    enabled: bool
    model: str | None = Field(default=None, max_length=200)


def create_router(runtime):
    router = APIRouter(prefix="/feedback")
    db, service, config = runtime.db, runtime.service, runtime.settings

    async def auth(request: Request):
        expected = config.feedback_admin_token.get_secret_value()
        value = request.headers.get("authorization", "")
        if not expected or not secrets.compare_digest(value, "Bearer " + expected):
            raise HTTPException(401, "需要机器人管理令牌", headers={"WWW-Authenticate": "Bearer"})
        return "web-admin"

    api = APIRouter(prefix="/api", dependencies=[Depends(auth)])

    @router.get("/", response_class=HTMLResponse)
    async def index():
        return HTMLResponse(
            Path(__file__).with_name("static").joinpath("index.html").read_text("utf-8"),
            headers={
                "Cache-Control": "no-store",
                "X-Frame-Options": "DENY",
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; "
                "style-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'",
            },
        )

    @router.get("/assets/{name}")
    async def asset(name: str):
        if name not in {"app.js", "style.css"}:
            raise HTTPException(404)
        mime = "text/javascript" if name.endswith("js") else "text/css"
        return Response(Path(__file__).with_name("static").joinpath(name).read_text("utf-8"), media_type=mime)

    @api.get("/overview")
    async def overview():
        return {
            "issues": db.one("SELECT COUNT(*) n FROM issues")["n"],
            "reports": db.one("SELECT COUNT(*) n FROM reports")["n"],
            "reporters": db.one("SELECT COUNT(DISTINCT qq) n FROM reports")["n"],
            "statuses": db.rows("SELECT status,COUNT(*) n FROM issues GROUP BY status"),
            "jobs": db.rows("SELECT kind,state,COUNT(*) n FROM jobs GROUP BY kind,state"),
            "monitor_enabled": runtime.monitor_enabled,
            "ai_configured": bool(config.feedback_ai_key.get_secret_value() and config.feedback_ai_model),
            "auto_resolve": config.feedback_auto_resolve,
            "vision": service.vision_state(),
            "repos": [
                {
                    **r.model_dump(),
                    "key": r.key,
                    "state": db.one("SELECT * FROM repo_state WHERE repo_key=?", (r.key,)),
                }
                for r in config.feedback_repos
            ],
        }

    @api.get("/issues")
    async def issues(status: str = "", limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
        return {
            "items": db.issues(status, limit, offset),
            "total": db.one("SELECT COUNT(*) n FROM issues WHERE ?='' OR status=?", (status, status))["n"],
        }

    @api.put("/vision")
    async def configure_vision(body: VisionBody):
        try:
            service.set_vision(body.enabled, body.model, "web-admin")
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return service.vision_state()

    @api.get("/issues/{issue_id}")
    async def detail(issue_id: int, offset: int = Query(0, ge=0)):
        issue = db.one("SELECT * FROM issues WHERE id=?", (issue_id,))
        if not issue:
            raise HTTPException(404)
        return {
            "issue": issue,
            "reports": db.rows(
                "SELECT * FROM reports WHERE issue_id=? ORDER BY id DESC LIMIT 100 OFFSET ?",
                (issue_id, offset),
            ),
            "total": db.one("SELECT COUNT(*) n FROM reports WHERE issue_id=?", (issue_id,))["n"],
            "screenshots": db.rows(
                "SELECT s.id,s.report_id,s.original,s.model,s.analysis,s.created,r.qq "
                "FROM screenshots s JOIN reports r ON r.id=s.report_id WHERE r.id IN "
                "(SELECT id FROM reports WHERE issue_id=? ORDER BY id DESC LIMIT 100 OFFSET ?) "
                "ORDER BY s.id DESC",
                (issue_id, offset),
            ),
        }

    @api.put("/issues/{issue_id}/comment")
    async def comment(issue_id: int, body: CommentBody):
        try:
            service.comment(issue_id, body.comment, body.status, "web-admin")
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"ok": True}

    @api.post("/issues/{issue_id}/reopen")
    async def reopen(issue_id: int):
        try:
            service.reopen(issue_id, "web-admin")
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        return {"ok": True}

    @api.get("/blacklist")
    async def blacklist():
        return db.rows("SELECT * FROM blacklist ORDER BY created DESC")

    @api.post("/blacklist")
    async def ban(body: BanBody):
        try:
            with db.transaction():
                service.ban(body.qq, body.reason, "web-admin")
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"ok": True}

    @api.delete("/blacklist/{qq}")
    async def unban(qq: str):
        service.unban(qq, "web-admin")
        return {"ok": True}

    @api.post("/reports/delete")
    async def delete(body: DeleteBody):
        if body.all == bool(body.ids):
            raise HTTPException(400, "必须且只能指定 all=true 或非空 ids")
        return {"deleted": service.delete_reports(body.qq, None if body.all else body.ids, "web-admin")}

    @api.get("/faqs")
    async def faqs():
        return db.rows("SELECT * FROM faqs ORDER BY id")

    @api.post("/faqs")
    async def add_faq(body: FAQBody):
        faq_id = db.execute(
            "INSERT INTO faqs(question,answer,enabled) VALUES(?,?,?)",
            (body.question, body.answer, int(body.enabled)),
        ).lastrowid
        db.audit("web-admin", "add_faq", {"id": faq_id})
        return {"id": faq_id}

    @api.put("/faqs/{faq_id}")
    async def update_faq(faq_id: int, body: FAQBody):
        if not db.execute(
            "UPDATE faqs SET question=?,answer=?,enabled=? WHERE id=?",
            (body.question, body.answer, int(body.enabled), faq_id),
        ).rowcount:
            raise HTTPException(404)
        db.audit("web-admin", "update_faq", {"id": faq_id})
        return {"ok": True}

    @api.get("/proposals")
    async def proposals(state: str = "pending", offset: int = Query(0, ge=0)):
        return db.rows(
            "SELECT p.*,i.title FROM proposals p JOIN issues i ON i.id=p.issue_id "
            "WHERE p.state=? ORDER BY p.id DESC LIMIT 100 OFFSET ?",
            (state, offset),
        )

    @api.post("/proposals/{proposal_id}")
    async def decide(proposal_id: int, body: DecisionBody):
        with db.transaction():
            if body.action == "accept":
                try:
                    service.resolve(proposal_id, "web-admin")
                except ValueError as exc:
                    raise HTTPException(400, str(exc)) from exc
            else:
                if not db.execute(
                    "UPDATE proposals SET state='rejected' WHERE id=? AND state='pending'", (proposal_id,)
                ).rowcount:
                    raise HTTPException(400, "建议不存在或已处理")
                db.audit("web-admin", "reject_proposal", {"id": proposal_id})
        return {"ok": True}

    @api.get("/commits")
    async def commits(offset: int = Query(0, ge=0)):
        return db.rows("SELECT * FROM commits ORDER BY created DESC LIMIT 100 OFFSET ?", (offset,))

    @api.get("/jobs")
    async def jobs(offset: int = Query(0, ge=0)):
        return db.rows(
            "SELECT id,kind,state,attempts,error,created,next_at FROM jobs "
            "WHERE state IN ('pending','failed') ORDER BY state,id LIMIT 100 OFFSET ?",
            (offset,),
        )

    @api.post("/jobs/{job_id}/retry")
    async def retry(job_id: int):
        if not db.execute(
            "UPDATE jobs SET state='pending',attempts=0,next_at=0,error='' WHERE id=? AND state='failed'",
            (job_id,),
        ).rowcount:
            raise HTTPException(400, "只能重试失败任务")
        db.audit("web-admin", "retry", {"id": job_id})
        return {"ok": True}

    @api.post("/repos/reset")
    async def reset_repo(body: RepoBody):
        if body.repo_key not in {r.key for r in config.feedback_repos}:
            raise HTTPException(404)
        db.execute("DELETE FROM repo_state WHERE repo_key=?", (body.repo_key,))
        db.execute(
            "UPDATE jobs SET state='cancelled' WHERE kind='hook' AND state IN ('pending','failed') "
            "AND json_extract(payload,'$.repo_key')=?",
            (body.repo_key,),
        )
        db.audit("web-admin", "reset_repo_baseline", body.repo_key)
        return {"ok": True, "message": "下次轮询将以当前 HEAD 建立基线，期间旧提交不会补发"}

    @api.get("/audit")
    async def audit(offset: int = Query(0, ge=0)):
        return db.rows("SELECT * FROM audit ORDER BY id DESC LIMIT 100 OFFSET ?", (offset,))

    @router.post("/github/webhook")
    async def webhook(request: Request):
        if not runtime.monitor_enabled or not config.feedback_webhook_secret.get_secret_value():
            raise HTTPException(503, "Webhook 未启用")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 2_000_000:
                raise HTTPException(413, "Webhook 超过 2MB；轮询仍可补偿")
        if not valid_signature(
            bytes(body),
            config.feedback_webhook_secret.get_secret_value(),
            request.headers.get("x-hub-signature-256", ""),
        ):
            raise HTTPException(403, "签名无效")
        event = request.headers.get("x-github-event", "")
        if event == "ping":
            return {"ok": True}
        if event != "push":
            return {"ignored": True}
        delivery = request.headers.get("x-github-delivery", "")
        if not re.fullmatch(r"[A-Za-z0-9-]{1,100}", delivery):
            raise HTTPException(400, "缺少有效 delivery ID")
        try:
            data = json.loads(body)
            repo_name, ref = data["repository"]["full_name"], data["ref"]
            before = data.get("before", "")
            if before and not re.fullmatch(r"[0-9a-f]{40,64}", before):
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            raise HTTPException(400, "无效 push payload") from None
        repo = next(
            (r for r in config.feedback_repos if r.name == repo_name and ref == f"refs/heads/{r.branch}"),
            None,
        )
        if not repo or data.get("deleted"):
            return {"ignored": True}
        added = db.enqueue("hook", "hook:" + delivery, {"repo_key": repo.key, "before": before})
        return {"accepted": added}

    router.include_router(api)
    return router
