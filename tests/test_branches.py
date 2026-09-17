import hashlib
import hmac
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from conftest import feedback_job
from fastapi import FastAPI

from feedback_hub.config import Repo
from feedback_hub.github import GitHub
from feedback_hub.models import CommitAnalysis, Fix
from feedback_hub.web import create_router


async def test_same_sha_on_two_branches_independent_jobs_and_cursors(db, settings):
    github = GitHub(db, settings, AsyncMock())
    main = Repo(name="o/r", branch="main", announce_groups=["88888"])
    release = Repo(name="o/r", branch="release/1.0", poll_seconds=120, announce_groups=["99999"])
    github.get = AsyncMock(return_value=([{"sha": "old"}], {}))
    await github.sync(main)
    await github.sync(release)
    github.get.return_value = ([{"sha": "new"}, {"sha": "old"}], {})
    await github.sync(release)
    assert db.one("SELECT cursor FROM repo_state WHERE repo_key=?", (main.key,))["cursor"] == "old"
    await github.sync(main)
    await github.sync(release)
    await github.sync(main)
    payloads = [json.loads(j["payload"]) for j in db.rows("SELECT * FROM jobs ORDER BY id")]
    assert len(payloads) == 2
    assert payloads[0]["branch"] == "release/1.0"
    assert payloads[0]["groups"] == ["99999"]
    assert payloads[1]["branch"] == "main"
    assert payloads[1]["groups"] == ["88888"]
    assert github.get.call_args_list[1].args[1]["sha"] == "release/1.0"


async def test_cross_branch_announcements_reuse_ai_without_duplicate_fix(db, settings, service, ai):
    await service.process_feedback(feedback_job(service))
    ai.commit.return_value = CommitAnalysis(
        announce=True,
        summary="修复黑屏",
        fixes=[
            Fix(
                issue_id=1,
                confidence=0.99,
                explanation="修正渲染初始化",
                file="game.js",
                patch_quote="-bad\n+good",
            )
        ],
    )
    github = AsyncMock()
    github.detail.return_value = {
        "url": "https://github.com/o/r/commit/new",
        "incomplete": False,
        "files": [{"filename": "game.js", "patch": "-bad\n+good"}],
    }
    for branch, groups in [("develop", ["88888"]), ("main", ["88888", "99999"])]:
        key = f"commit:o/r@{branch}:new"
        db.enqueue("commit", key, {"repo": "o/r", "branch": branch, "sha": "new", "groups": groups})
        job = db.one("SELECT * FROM jobs WHERE dedup=?", (key,))
        job["data"] = json.loads(job["payload"])
        await service.analyze_commit(job, github)
        await service.analyze_commit(job, github)
    assert ai.commit.await_count == 1
    assert github.detail.await_count == 1
    sends = db.rows("SELECT payload FROM jobs WHERE kind='send' AND dedup LIKE 'commit:%'")
    assert len(sends) == 3
    payloads = [json.loads(row["payload"]) for row in sends]
    assert sum("【r @ main 更新】" in p["text"] for p in payloads) == 2
    assert sum("【r @ develop 更新】" in p["text"] for p in payloads) == 1
    fixed = db.rows("SELECT payload FROM jobs WHERE dedup LIKE 'resolved:%'")
    assert len(fixed) == 1
    assert "修复分支：develop" in fixed[0]["payload"]


@pytest.mark.parametrize(
    "branch,accepted", [("main", True), ("develop", True), ("release/1.0", True), ("other", False)]
)
async def test_webhook_routes_exact_configured_branch(db, settings, service, branch, accepted):
    settings.feedback_repos = [Repo(name="o/r", branch=b) for b in ("main", "develop", "release/1.0")]
    app = FastAPI()
    app.include_router(
        create_router(SimpleNamespace(db=db, settings=settings, service=service, monitor_enabled=True))
    )
    body = json.dumps(
        {"repository": {"full_name": "o/r"}, "ref": "refs/heads/" + branch, "before": "a" * 40}
    ).encode()
    headers = {
        "X-GitHub-Event": "push",
        "X-GitHub-Delivery": "event-1",
        "X-Hub-Signature-256": "sha256=" + hmac.new(b"b" * 40, body, hashlib.sha256).hexdigest(),
    }
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/feedback/github/webhook", content=body, headers=headers)
    assert response.status_code == 200
    if accepted:
        assert response.json() == {"accepted": True}
        assert json.loads(db.one("SELECT payload FROM jobs")["payload"])["repo_key"] == f"o/r@{branch}"
    else:
        assert response.json() == {"ignored": True}
        assert not db.rows("SELECT * FROM jobs")
