import hashlib
import hmac
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from feedback_hub.config import Repo
from feedback_hub.github import GitHub, valid_signature
from feedback_hub.web import create_router


def test_hmac_authentic_and_tampered():
    body, secret = b'{"test":true}', "x" * 40
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert valid_signature(body, secret, sig)
    assert not valid_signature(body + b" ", secret, sig)
    assert not valid_signature(body, "", sig)
    assert not valid_signature(body, secret, "")


async def test_poll_baseline_catchup_dedup_multiple_repos(db, settings):
    github = GitHub(db, settings, AsyncMock())
    repo = Repo(name="o/r")
    github.get = AsyncMock(return_value=([{"sha": "old"}], {}))
    await github.sync(repo)
    assert not db.rows("SELECT * FROM jobs")
    github.get.return_value = ([{"sha": "new2"}, {"sha": "new1"}, {"sha": "old"}], {})
    await github.sync(repo)
    await github.sync(repo)
    jobs = db.rows("SELECT * FROM jobs ORDER BY id")
    assert [json.loads(j["payload"])["sha"] for j in jobs] == ["new1", "new2"]
    assert db.one("SELECT cursor FROM repo_state")["cursor"] == "new2"
    await github.sync(Repo(name="o/other"))
    assert len(db.rows("SELECT * FROM repo_state")) == 2


async def test_paginated_history_fixed_head_and_atomic_cursor(db, settings):
    github = GitHub(db, settings, AsyncMock())
    repo = Repo(name="o/r")
    db.execute("INSERT INTO repo_state(repo_key,cursor) VALUES(?,?)", (repo.key, "old"))
    first = [{"sha": f"new{i}"} for i in range(100)]
    github.get = AsyncMock(side_effect=[(first, {}), ([{"sha": "old"}], {})])
    await github.sync(repo)
    assert github.get.call_args_list[1].args[1]["sha"] == "new0"
    assert len(db.rows("SELECT * FROM jobs")) == 100
    assert db.one("SELECT cursor FROM repo_state")["cursor"] == "new0"


async def test_force_push_never_silently_moves_cursor(db, settings):
    github = GitHub(db, settings, AsyncMock())
    repo = Repo(name="o/r")
    db.execute("INSERT INTO repo_state(repo_key,cursor) VALUES(?,?)", (repo.key, "old"))
    github.get = AsyncMock(return_value=([{"sha": "unrelated"}], {}))
    with pytest.raises(RuntimeError, match="force-push"):
        await github.sync(repo)
    assert db.one("SELECT cursor FROM repo_state")["cursor"] == "old"
    assert not db.rows("SELECT * FROM jobs")


async def test_first_webhook_uses_before_for_catchup(db, settings):
    github = GitHub(db, settings, AsyncMock())
    github.get = AsyncMock(return_value=([{"sha": "new"}, {"sha": "old"}], {}))
    await github.sync(Repo(name="o/r"), "old")
    assert len(db.rows("SELECT * FROM jobs")) == 1


async def test_truncated_diff_disables_auto_resolution(db, settings):
    github = GitHub(db, settings, AsyncMock())
    github.get = AsyncMock(
        return_value=(
            {
                "commit": {"message": "fix"},
                "files": [
                    {
                        "filename": "a.js",
                        "status": "modified",
                        "patch": "-x\n+y",
                        "additions": 3,
                        "deletions": 1,
                    }
                ],
            },
            {},
        )
    )
    assert (await github.detail("o/r", "a" * 40))["incomplete"] is True


async def test_rate_limit_backoff(db, settings):
    def respond(request):
        return httpx.Response(429, headers={"retry-after": "120"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        github = GitHub(db, settings, client)
        with pytest.raises(httpx.HTTPStatusError):
            await github.get("/repos/o/r/commits")
        with pytest.raises(RuntimeError, match="退避"):
            await github.get("/repos/o/r/commits")


@pytest.fixture
async def web(db, settings, service):
    settings.feedback_repos = [Repo(name="o/r")]
    app = FastAPI()
    app.include_router(
        create_router(SimpleNamespace(db=db, settings=settings, service=service, monitor_enabled=True))
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client


async def test_web_protected_api_and_static_page(web):
    assert (await web.get("/feedback/api/overview")).status_code == 401
    assert (await web.get("/feedback/")).status_code == 200
    assert "frame-ancestors 'none'" in (await web.get("/feedback/")).headers["content-security-policy"]
    assert (await web.get("/feedback/assets/app.js")).status_code == 200
    r = await web.get("/feedback/api/overview", headers={"Authorization": "Bearer " + "a" * 40})
    assert r.status_code == 200
    assert r.json()["issues"] == 0
    assert "admin_token" not in r.text


async def test_web_admin_mutations_and_superuser_protection(web, db):
    web.headers["Authorization"] = "Bearer " + "a" * 40
    assert (
        await web.post("/feedback/api/blacklist", json={"qq": "12345", "reason": "test"})
    ).status_code == 200
    assert db.blocked("12345")
    assert (
        await web.post("/feedback/api/blacklist", json={"qq": "10000", "reason": "test"})
    ).status_code == 400
    assert (await web.delete("/feedback/api/blacklist/12345")).status_code == 200
    assert not db.blocked("12345")
    assert (await web.post("/feedback/api/reports/delete", json={"qq": "12345"})).status_code == 400
    assert (
        await web.post("/feedback/api/reports/delete", json={"qq": "12345", "all": True, "ids": [1]})
    ).status_code == 400
    assert (
        await web.post("/feedback/api/faqs", json={"question": "黑屏", "answer": "清理缓存"})
    ).status_code == 200
    assert len((await web.get("/feedback/api/faqs")).json()) == 1


async def test_webhook_signature_allowlist_and_delivery_dedup(web, db):
    def headers(body, delivery="delivery-1"):
        return {
            "X-GitHub-Event": "push",
            "X-GitHub-Delivery": delivery,
            "X-Hub-Signature-256": "sha256=" + hmac.new(b"b" * 40, body, hashlib.sha256).hexdigest(),
        }

    data = {"repository": {"full_name": "o/r"}, "ref": "refs/heads/main", "before": "a" * 40}
    body = json.dumps(data).encode()
    path = "/feedback/github/webhook"
    assert (await web.post(path, content=body)).status_code == 403
    assert (await web.post(path, content=body, headers=headers(body))).json() == {"accepted": True}
    assert (await web.post(path, content=body, headers=headers(body))).json() == {"accepted": False}
    data["ref"] = "refs/heads/unwatched"
    body2 = json.dumps(data).encode()
    assert (await web.post(path, content=body2, headers=headers(body2, "delivery-2"))).json() == {
        "ignored": True
    }
    assert len(db.rows("SELECT * FROM jobs WHERE kind='hook'")) == 1
