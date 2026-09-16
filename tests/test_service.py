import json
from unittest.mock import AsyncMock

import pytest
from conftest import commit_job, feedback_job

from feedback_hub.models import CommitAnalysis, Fix, Match


async def test_unique_qq_priority_original_and_matching(service, ai, db):
    await service.process_feedback(feedback_job(service))
    ai.match.return_value = Match(issue_id=1, confidence=0.98, reason="同根因")
    await service.process_feedback(feedback_job(service, "2"))
    await service.process_feedback(feedback_job(service, "3", "23456"))
    issue = db.issues()[0]
    assert (issue["reporters"], issue["reports"]) == (2, 3)
    assert (
        db.one("SELECT original FROM reports WHERE id=1")["original"] == "设备苹果 浏览器 Safari 第三关黑屏"
    )
    assert (
        service.ingest(qq="12345", group="88888", bot="11111", message_id="1", text="duplicate", segments=[])
        == "duplicate"
    )


@pytest.mark.parametrize("field", ["device_quote", "browser_quote", "meaningful"])
async def test_incomplete_or_hallucinated_environment_rejected(service, ai, db, field):
    value = False if field == "meaningful" else "not-in-original"
    ai.triage.return_value = ai.triage.return_value.model_copy(update={field: value})
    await service.process_feedback(feedback_job(service))
    assert not db.rows("SELECT * FROM reports")
    assert "请补充" in db.one("SELECT payload FROM jobs WHERE kind='send'")["payload"]
    assert not db.blocked("12345")


async def test_faq_and_custom_prompt(service, ai, db, settings):
    db.execute("INSERT INTO faqs(question,answer) VALUES('第三关黑屏','关闭省电模式')")
    ai.match.return_value = Match(faq_id=1, confidence=0.99, reason="已有明确解决步骤")
    settings.feedback_faq_prompt = "FAQ: {answer} {url}"
    await service.process_feedback(feedback_job(service))
    assert not db.rows("SELECT * FROM reports")
    assert "关闭省电模式" in db.one("SELECT payload FROM jobs WHERE kind='send'")["payload"]


async def test_admin_comment_on_subsequent_feedback(service, ai, db):
    await service.process_feedback(feedback_job(service))
    service.comment(1, "暂不支持此浏览器，请换 Edge", "deferred", "10000")
    ai.match.return_value = Match(issue_id=1, confidence=0.99, reason="同根因")
    await service.process_feedback(feedback_job(service, "2", "23456"))
    reply = db.one("SELECT payload FROM jobs WHERE kind='send' ORDER BY id DESC")["payload"]
    assert "暂不支持此浏览器" in reply
    assert db.one("SELECT status FROM issues")["status"] == "deferred"


async def test_auto_ban_permanent_and_superuser_exemption(service, ai, db):
    ai.triage.return_value = ai.triage.return_value.model_copy(update={"kind": "abuse", "confidence": 0.999})
    await service.process_feedback(feedback_job(service))
    assert db.blocked("12345")
    assert (
        service.ingest(
            qq="12345", group="88888", bot="11111", message_id="2", text="苹果 Safari 黑屏", segments=[]
        )
        == "ignored"
    )
    await service.process_feedback(feedback_job(service, "3", "10000"))
    assert not db.blocked("10000")
    service.unban("12345", "10000")
    assert not db.blocked("12345")


async def test_ambiguous_abuse_not_banned(service, ai, db):
    ai.triage.return_value = ai.triage.return_value.model_copy(update={"kind": "abuse", "confidence": 0.9})
    await service.process_feedback(feedback_job(service))
    assert not db.blocked("12345")


async def test_delete_only_owned_records_recalculates_counts(service, ai, db):
    await service.process_feedback(feedback_job(service))
    ai.match.return_value = Match(issue_id=1, confidence=0.99, reason="同根因")
    await service.process_feedback(feedback_job(service, "2", "23456"))
    assert service.delete_reports("12345", [2], "10000") == 0
    assert service.delete_reports("12345", [1], "10000") == 1
    assert db.issues()[0]["reporters"] == 1
    assert service.delete_reports("23456", None, "10000") == 1
    assert db.issues()[0]["status"] == "archived"


async def test_deletion_during_ai_does_not_reinsert(service, ai, db):
    job = feedback_job(service)
    result = ai.triage.return_value

    async def analyze(_):
        service.delete_reports("12345", None, "10000")
        return result

    ai.triage.side_effect = analyze
    await service.process_feedback(job)
    assert not db.rows("SELECT * FROM reports")


async def setup_fix(service, ai, db, incomplete=False, quote="-bad\n+good", confidence=0.99):
    await service.process_feedback(feedback_job(service))
    github = AsyncMock()
    github.detail.return_value = {
        "url": "https://github.com/owner/game/commit/" + "a" * 40,
        "files": [{"filename": "game.js", "patch": "@@ test\n-bad\n+good"}],
        "incomplete": incomplete,
    }
    ai.commit.return_value = CommitAnalysis(
        summary="修复第三关黑屏",
        fixes=[
            Fix(
                issue_id=1,
                confidence=confidence,
                explanation="修正渲染初始化",
                file="game.js",
                patch_quote=quote,
            )
        ],
    )
    return github


async def test_commit_resolves_shared_issue_mentions_once(service, ai, db):
    github = await setup_fix(service, ai, db)
    ai.match.return_value = Match(issue_id=1, confidence=0.99, reason="同根因")
    await service.process_feedback(feedback_job(service, "2"))
    await service.process_feedback(feedback_job(service, "3", "23456", "99999"))
    job = commit_job(db)
    await service.analyze_commit(job, github)
    await service.analyze_commit(job, github)
    assert db.one("SELECT status FROM issues")["status"] == "resolved"
    sends = db.rows("SELECT * FROM jobs WHERE dedup LIKE 'resolved:%'")
    assert len(sends) == 2
    assert {json.loads(s["payload"])["qq"] for s in sends} == {"12345", "23456"}
    assert all("尚未确认部署上线" in s["payload"] for s in sends)


@pytest.mark.parametrize(
    "incomplete,quote,confidence",
    [(True, "-bad\n+good", 0.99), (False, "invented", 0.99), (False, "-bad\n+good", 0.8)],
)
async def test_uncertain_fixes_require_review(service, ai, db, incomplete, quote, confidence):
    github = await setup_fix(service, ai, db, incomplete, quote, confidence)
    await service.analyze_commit(commit_job(db), github)
    assert db.one("SELECT status FROM issues")["status"] == "open"
    assert db.one("SELECT state FROM proposals")["state"] == "pending"
    assert not db.rows("SELECT * FROM jobs WHERE dedup LIKE 'resolved:%'")


async def test_banned_reporter_not_notified(service, ai, db):
    github = await setup_fix(service, ai, db)
    service.ban("12345", "管理员操作", "10000")
    await service.analyze_commit(commit_job(db), github)
    assert not db.rows("SELECT * FROM jobs WHERE dedup LIKE 'resolved:%'")


async def test_issue_changed_while_ai_running_requires_review(service, ai, db):
    github = await setup_fix(service, ai, db)
    result = ai.commit.return_value

    async def analyzing(*_):
        service.comment(1, "仍有其它症状待确认", "deferred", "10000")
        return result

    ai.commit.side_effect = analyzing
    await service.analyze_commit(commit_job(db), github)
    assert db.one("SELECT state FROM proposals")["state"] == "pending"


async def test_out_of_batch_issue_id_rejected(service, ai, db):
    github = await setup_fix(service, ai, db)
    ai.commit.return_value.fixes[0].issue_id = 999
    await service.analyze_commit(commit_job(db), github)
    assert not db.rows("SELECT * FROM proposals")


async def test_match_searches_beyond_first_page(service, ai, db):
    for i in range(65):
        db.execute(
            "INSERT INTO issues(title,category,summary,created,updated) VALUES(?,?,?,?,?)",
            (f"问题{i}", "其他", "描述", i, i),
        )

    async def matching(report, issues, faqs):
        return Match(
            issue_id=65 if any(i["id"] == 65 for i in issues) else None, confidence=0.99, reason="匹配"
        )

    ai.match.side_effect = matching
    assert await service.find_match({"text": "test"}) == (65, None)
    assert ai.match.call_count == 3
