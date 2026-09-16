import json
from unittest.mock import AsyncMock

import pytest

from feedback_hub.config import Settings
from feedback_hub.db import Database
from feedback_hub.models import CommitAnalysis, Match, Triage
from feedback_hub.service import Service


@pytest.fixture
def db():
    database = Database(":memory:")
    yield database
    database.close()


@pytest.fixture
def settings():
    return Settings(
        feedback_groups={"88888", "99999"},
        feedback_cooldown_seconds=0,
        feedback_admin_token="a" * 40,
        feedback_webhook_secret="b" * 40,
    )


@pytest.fixture
def ai():
    mock = AsyncMock()
    mock.triage.return_value = Triage(
        kind="feedback",
        confidence=0.99,
        reason="有效反馈",
        device="苹果",
        browser="Safari",
        device_quote="苹果",
        browser_quote="Safari",
        meaningful=True,
        title="第三关黑屏",
        category="渲染",
        summary="第三关黑屏",
    )
    mock.match.return_value = Match(confidence=0.1, reason="无匹配")
    mock.commit.return_value = CommitAnalysis(summary="修复第三关黑屏")
    return mock


@pytest.fixture
def service(db, settings, ai):
    return Service(db, settings, ai, {"10000"})


def feedback_job(service, mid="1", qq="12345", group="88888", text="设备苹果 浏览器 Safari 第三关黑屏"):
    assert (
        service.ingest(
            qq=qq,
            group=group,
            bot="11111",
            message_id=mid,
            text=text,
            segments=[{"type": "text", "data": {"text": text}}],
            explicit=True,
        )
        == "queued"
    )
    job = service.db.one("SELECT * FROM jobs WHERE dedup=?", (f"feedback:11111:{group}:{mid}",))
    job["data"] = json.loads(job["payload"])
    return job


def commit_job(db, repo="owner/game", sha="a" * 40):
    db.enqueue("commit", f"commit:{repo}:{sha}", {"repo": repo, "sha": sha, "groups": ["88888"]})
    job = db.one("SELECT * FROM jobs WHERE dedup=?", (f"commit:{repo}:{sha}",))
    job["data"] = json.loads(job["payload"])
    return job
