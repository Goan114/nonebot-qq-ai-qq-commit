import json
import time

from .ai import AI
from .config import Settings
from .db import Database


class Service:
    def __init__(self, db: Database, settings: Settings, ai: AI, superusers: set[str] | None = None):
        self.db, self.settings, self.ai = db, settings, ai
        self.superusers = superusers or set()

    def notify(
        self,
        key: str,
        group: str,
        text: str,
        qq: str = "",
        bot: str = "",
        issue_id: int | None = None,
        report_id: int | None = None,
    ):
        self.db.enqueue(
            "send",
            key,
            {
                "group": group,
                "text": text,
                "qq": qq,
                "bot": bot,
                "issue_id": issue_id,
                "report_id": report_id,
            },
        )

    def reply(self, job: dict, text: str, suffix: str = "reply"):
        p = job["data"]
        self.notify(f"{job['dedup']}:{suffix}", p["group"], text, p["qq"], p["bot"])

    def ingest(
        self,
        *,
        qq: str,
        group: str,
        bot: str,
        message_id: str,
        text: str,
        segments: list,
        explicit: bool = False,
    ) -> str:
        if group not in self.settings.feedback_groups or self.db.blocked(qq):
            return "ignored"
        if (
            not explicit
            and not self.settings.feedback_listen_all
            and not any(k.casefold() in text.casefold() for k in self.settings.feedback_keywords)
        ):
            return "ignored"
        if not text.strip():
            return "ignored"
        if len(text) > self.settings.feedback_max_message_chars:
            return "消息过长，请精简后提交。"
        key = f"feedback:{bot}:{group}:{message_id}"
        if self.db.one("SELECT id FROM jobs WHERE dedup=?", (key,)):
            return "duplicate"
        latest = self.db.one(
            "SELECT created FROM jobs WHERE kind='feedback' AND "
            "json_extract(payload,'$.qq')=? ORDER BY id DESC LIMIT 1",
            (qq,),
        )
        if latest and time.time() - latest["created"] < self.settings.feedback_cooldown_seconds:
            return "提交过于频繁，请稍后重试。" if explicit else "ignored"
        pending = self.db.one("SELECT COUNT(*) n FROM jobs WHERE kind='feedback' AND state='pending'")["n"]
        if pending >= self.settings.feedback_max_pending:
            return "反馈队列已满，请稍后重试。"
        self.db.enqueue(
            "feedback",
            key,
            {
                "qq": qq,
                "group": group,
                "bot": bot,
                "message_id": message_id,
                "text": text,
                "segments": segments,
                "explicit": explicit,
            },
        )
        return "queued"

    async def find_match(self, report: dict):
        # Batch across every issue/FAQ, not only the most popular first page.
        issues = self.db.rows("""SELECT i.id,i.title,i.summary,i.category,i.status,i.comment,
            GROUP_CONCAT(DISTINCT r.device || '/' || r.browser) AS environments
            FROM issues i LEFT JOIN reports r ON r.issue_id=i.id
            WHERE i.status IN ('open','deferred') GROUP BY i.id""")
        faqs = self.db.rows("SELECT id,question,answer FROM faqs WHERE enabled=1")
        best_issue, best_faq = (None, 0.0), (None, 0.0)
        for start in range(0, max(len(issues), len(faqs)), 30):
            chunk_i, chunk_f = issues[start : start + 30], faqs[start : start + 30]
            match = await self.ai.match(report, chunk_i, chunk_f)
            if match.issue_id in {i["id"] for i in chunk_i} and match.confidence > best_issue[1]:
                best_issue = (match.issue_id, match.confidence)
            if match.faq_id in {f["id"] for f in chunk_f} and match.confidence > best_faq[1]:
                best_faq = (match.faq_id, match.confidence)
        return (
            best_issue[0] if best_issue[1] >= 0.90 else None,
            best_faq[0] if best_faq[1] >= 0.95 else None,
        )

    async def process_feedback(self, job: dict):
        p = job["data"]
        if self.db.blocked(p["qq"]):
            self.db.done(job["id"])
            return
        result = await self.ai.triage(p["text"])
        issue_id, faq_id = None, None
        if result.kind == "feedback":
            issue_id, faq_id = await self.find_match({"original": p["text"], **result.model_dump()})
        # An administrator may ban this QQ while awaiting AI.
        with self.db.transaction():
            current_job = self.db.one("SELECT state FROM jobs WHERE id=?", (job["id"],))
            if current_job and current_job["state"] != "pending":
                return
            if self.db.blocked(p["qq"]):
                self.db.done(job["id"])
                return
            if result.kind == "abuse":
                if (
                    self.settings.feedback_auto_ban
                    and result.confidence >= self.settings.feedback_ban_threshold
                    and p["qq"] not in self.superusers
                ):
                    self.ban(p["qq"], "AI: " + result.reason, "AI")
                elif p["explicit"]:
                    self.reply(job, "此入口仅接受游戏问题反馈，请包含设备、浏览器和具体异常。")
                self.db.audit("AI", "abuse_decision", {"qq": p["qq"], **result.model_dump()})
            elif result.kind == "irrelevant":
                if p["explicit"]:
                    self.reply(job, "未识别到游戏问题，请说明设备、浏览器、操作和异常现象。")
            elif faq_id and (faq := self.db.one("SELECT * FROM faqs WHERE id=? AND enabled=1", (faq_id,))):
                template = self.settings.feedback_faq_prompt
                # Literal replacements: admin templates cannot trigger Python format evaluation.
                self.reply(
                    job,
                    template.replace("{url}", self.settings.feedback_faq_url)
                    .replace("{answer}", faq["answer"])
                    .replace("{question}", faq["question"]),
                )
            else:
                missing = []
                if not result.device or not result.device_quote or result.device_quote not in p["text"]:
                    missing.append("设备类型（如安卓/华为/苹果）")
                if not result.browser or not result.browser_quote or result.browser_quote not in p["text"]:
                    missing.append("浏览器（如 Edge/Safari）")
                if not result.meaningful or not result.title or not result.summary:
                    missing.append("具体的游戏异常现象及操作步骤")
                issue = self.db.one(
                    "SELECT * FROM issues WHERE id=? AND status IN ('open','deferred')", (issue_id,)
                )
                if missing:
                    text = "暂未收录，请补充：" + "、".join(missing) + "。请在一条消息中重新提交完整反馈。"
                    if issue and issue["comment"]:
                        text += "\n管理员说明：" + issue["comment"]
                    self.reply(job, text)
                else:
                    now = time.time()
                    if not issue:
                        issue_id = self.db.execute(
                            "INSERT INTO issues(title,category,summary,created,updated) VALUES(?,?,?,?,?)",
                            (result.title, result.category, result.summary, now, now),
                        ).lastrowid
                    report_id = self.db.execute(
                        """INSERT INTO reports
                        (issue_id,qq,group_id,bot_id,message_id,original,segments,device,browser,created)
                        VALUES(?,?,?,?,?,?,?,?,?,?)""",
                        (
                            issue_id,
                            p["qq"],
                            p["group"],
                            p["bot"],
                            p["message_id"],
                            p["text"],
                            json.dumps(p["segments"], ensure_ascii=False),
                            result.device,
                            result.browser,
                            now,
                        ),
                    ).lastrowid
                    self.db.execute("UPDATE issues SET updated=? WHERE id=?", (now, issue_id))
                    count = self.db.one(
                        "SELECT COUNT(DISTINCT qq) n FROM reports WHERE issue_id=?", (issue_id,)
                    )["n"]
                    response = f"已记录反馈 #{issue_id}：{result.title}。已有 {count} 位成员反馈同类问题。"
                    if issue and issue["comment"]:
                        response += "\n管理员说明：" + issue["comment"]
                    self.notify(
                        f"{job['dedup']}:reply",
                        p["group"],
                        response,
                        p["qq"],
                        p["bot"],
                        issue_id=issue_id,
                        report_id=report_id,
                    )
            self.db.done(job["id"])

    def ban(self, qq: str, reason: str, actor: str):
        if qq in self.superusers:
            raise ValueError("不能拉黑机器人超级用户")
        self.db.execute(
            "INSERT OR REPLACE INTO blacklist(qq,reason,actor,created) VALUES(?,?,?,?)",
            (qq, reason, actor, time.time()),
        )
        # Cancel pending replies so unbanning later cannot leak old queued responses.
        self.db.execute(
            "UPDATE jobs SET state='cancelled' WHERE kind IN ('send','feedback') "
            "AND state IN ('pending','failed') AND json_extract(payload,'$.qq')=?",
            (qq,),
        )
        self.db.audit(actor, "ban", {"qq": qq, "reason": reason})

    def unban(self, qq: str, actor: str):
        self.db.execute("DELETE FROM blacklist WHERE qq=?", (qq,))
        self.db.audit(actor, "unban", {"qq": qq})

    def delete_reports(self, qq: str, ids: list[int] | None, actor: str) -> int:
        if ids == []:
            raise ValueError("反馈 ID 列表不能为空；全部删除请明确指定 all")
        with self.db.transaction():
            args: list = [qq]
            where = "qq=?"
            if ids is not None:
                where += " AND id IN (" + ",".join("?" for _ in ids) + ")"
                args.extend(ids)
            reports = self.db.rows("SELECT id,issue_id FROM reports WHERE " + where, args)
            count = self.db.execute("DELETE FROM reports WHERE " + where, args).rowcount
            for report in reports:
                self.db.execute(
                    "UPDATE jobs SET state='cancelled' WHERE kind='send' AND "
                    "state IN ('pending','failed') AND json_extract(payload,'$.report_id')=?",
                    (report["id"],),
                )
                remaining = self.db.one(
                    "SELECT id FROM reports WHERE issue_id=? LIMIT 1", (report["issue_id"],)
                )
                if not remaining:
                    self.db.execute(
                        "UPDATE issues SET status='archived',updated=? WHERE id=? AND status!='resolved'",
                        (time.time(), report["issue_id"]),
                    )
            if ids is None:
                self.db.execute(
                    "UPDATE jobs SET state='cancelled' WHERE kind='feedback' AND "
                    "state IN ('pending','failed') AND json_extract(payload,'$.qq')=?",
                    (qq,),
                )
            self.db.audit(actor, "delete_reports", {"qq": qq, "ids": ids, "count": count})
        return count

    def comment(self, issue_id: int, comment: str, status: str, actor: str):
        if status not in {"open", "deferred", "archived"}:
            raise ValueError("状态应为 open/deferred/archived")
        if not self.db.execute(
            "UPDATE issues SET comment=?,status=?,updated=? WHERE id=? AND status!='resolved'",
            (comment, status, time.time(), issue_id),
        ).rowcount:
            raise ValueError("问题不存在或已解决；已解决问题需先重新打开")
        self.db.audit(actor, "comment", {"issue_id": issue_id, "comment": comment, "status": status})

    def reopen(self, issue_id: int, actor: str):
        if not self.db.execute(
            "UPDATE issues SET status='open',resolution='',updated=? WHERE id=?", (time.time(), issue_id)
        ).rowcount:
            raise ValueError("问题不存在")
        self.db.execute(
            "UPDATE jobs SET state='cancelled' WHERE kind='send' AND "
            "state IN ('pending','failed') AND json_extract(payload,'$.issue_id')=? "
            "AND dedup LIKE 'resolved:%'",
            (issue_id,),
        )
        self.db.audit(actor, "reopen", {"issue_id": issue_id})

    def resolve(self, proposal_id: int, actor: str):
        proposal = self.db.one("SELECT * FROM proposals WHERE id=?", (proposal_id,))
        if not proposal or proposal["state"] != "pending":
            raise ValueError("修复建议不存在或已处理")
        issue = self.db.one("SELECT * FROM issues WHERE id=?", (proposal["issue_id"],))
        if not issue or issue["status"] == "resolved":
            self.db.execute("UPDATE proposals SET state='superseded' WHERE id=?", (proposal_id,))
            return
        url = f"https://github.com/{proposal['repo']}/commit/{proposal['sha']}"
        branch = json.loads(proposal["evidence"]).get("branch", "")
        branch_note = f"修复分支：{branch}\n" if branch else ""
        text = (
            f"你反馈的问题 #{issue['id']}「{issue['title']}」已在上游代码中修复。\n"
            f"{proposal['explanation']}\n{branch_note}提交：{url}\n尚未确认部署上线，请以游戏实际更新为准。"
        )
        self.db.execute(
            "UPDATE issues SET status='resolved',resolution=?,updated=? WHERE id=?",
            (text, time.time(), issue["id"]),
        )
        self.db.execute("UPDATE proposals SET state='accepted' WHERE id=?", (proposal_id,))
        self.db.execute(
            "UPDATE proposals SET state='superseded' WHERE issue_id=? AND state='pending'", (issue["id"],)
        )
        # One mention per QQ per group, regardless of repeated submissions or multiple bot accounts.
        for report in self.db.rows(
            "SELECT qq,group_id,MIN(bot_id) bot_id FROM reports WHERE issue_id=? GROUP BY qq,group_id",
            (issue["id"],),
        ):
            if not self.db.blocked(report["qq"]):
                self.notify(
                    f"resolved:{proposal_id}:{report['group_id']}:{report['qq']}",
                    report["group_id"],
                    text,
                    report["qq"],
                    report["bot_id"],
                    issue["id"],
                )
        self.db.audit(actor, "resolve", {"proposal_id": proposal_id, "issue_id": issue["id"]})

    def announce_commit(self, payload: dict, summary: str, url: str):
        branch = payload.get("branch", "")
        target = f"{payload['repo']}@{branch}" if branch else payload["repo"]
        for group in payload.get("groups", []):
            if group in self.settings.feedback_groups:
                self.notify(
                    f"commit:{target}:{payload['sha']}:{group}",
                    group,
                    f"仓库更新 · {target}\n{summary}\n{url}",
                )

    async def analyze_commit(self, job: dict, github):
        p = job["data"]
        cached = self.db.one("SELECT * FROM commits WHERE repo=? AND sha=?", (p["repo"], p["sha"]))
        if cached:
            # The same SHA can arrive on another watched branch after a merge.
            # Reuse its analysis, but deliver each branch's announcement independently.
            with self.db.transaction():
                self.announce_commit(p, cached["summary"], cached["url"])
                self.db.done(job["id"])
            return
        commit = await github.detail(p["repo"], p["sha"])
        issues = self.db.rows("""SELECT i.id,i.title,i.summary,i.updated,
            GROUP_CONCAT(DISTINCT r.device || '/' || r.browser) AS environments
            FROM issues i JOIN reports r ON r.issue_id=i.id
            WHERE i.status IN ('open','deferred') GROUP BY i.id""")
        snapshots = {i["id"]: i for i in issues}
        analyses = []
        for start in range(0, max(1, len(issues)), 20):
            batch = issues[start : start + 20]
            analysis = await self.ai.commit(commit, batch)
            # A model cannot reference an issue from outside its supplied batch.
            analysis.fixes = [fix for fix in analysis.fixes if fix.issue_id in {i["id"] for i in batch}]
            analyses.append(analysis)
        with self.db.transaction():
            self.db.execute(
                "INSERT INTO commits(repo,sha,summary,url,created) VALUES(?,?,?,?,?)",
                (p["repo"], p["sha"], analyses[0].summary, commit["url"], time.time()),
            )
            self.announce_commit(p, analyses[0].summary, commit["url"])
            patches = {f["filename"]: f.get("patch", "") for f in commit["files"]}
            for analysis in analyses:
                for fix in analysis.fixes:
                    current = self.db.one("SELECT * FROM issues WHERE id=?", (fix.issue_id,))
                    if not current or current["status"] not in {"open", "deferred"}:
                        continue
                    valid_quote = (
                        bool(fix.patch_quote.strip())
                        and fix.file in patches
                        and fix.patch_quote in patches[fix.file]
                        and any(
                            line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
                            for line in fix.patch_quote.splitlines()
                        )
                    )
                    evidence = {
                        "branch": p.get("branch", ""),
                        "file": fix.file,
                        "patch_quote": fix.patch_quote,
                        "verified": valid_quote,
                        "incomplete_diff": commit["incomplete"],
                    }
                    self.db.execute(
                        """INSERT OR IGNORE INTO proposals
                        (issue_id,repo,sha,confidence,explanation,evidence,created) VALUES(?,?,?,?,?,?,?)""",
                        (
                            fix.issue_id,
                            p["repo"],
                            p["sha"],
                            fix.confidence,
                            fix.explanation,
                            json.dumps(evidence, ensure_ascii=False),
                            time.time(),
                        ),
                    )
                    proposal = self.db.one(
                        "SELECT id FROM proposals WHERE issue_id=? AND repo=? AND sha=?",
                        (fix.issue_id, p["repo"], p["sha"]),
                    )
                    if (
                        self.settings.feedback_auto_resolve
                        and valid_quote
                        and not commit["incomplete"]
                        and fix.confidence >= self.settings.feedback_resolve_threshold
                        and current["updated"] == snapshots[fix.issue_id]["updated"]
                    ):
                        self.resolve(proposal["id"], "AI")
            self.db.done(job["id"])
