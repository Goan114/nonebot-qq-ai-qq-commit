import asyncio
import json
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from conftest import commit_job, feedback_job
from fastapi import FastAPI

from feedback_hub.config import Settings
from feedback_hub.db import Database
from feedback_hub.models import VisionAnalysis
from feedback_hub.vision import Vision, VisionDisabled
from feedback_hub.web import create_router

IMAGE = {"type": "image", "data": {"url": "https://gchat.qpic.cn/screenshot.png"}}


def result(related=True):
    return VisionAnalysis(
        related=related,
        confidence=0.99,
        summary="战斗界面黑屏",
        visible_text="渲染初始化失败",
        observations=["有报错弹窗"],
        limitations="静态截图不能验证帧率",
    )


def enable(service, settings):
    settings.feedback_vision_model = "vision-a"
    settings.feedback_vision_models = ["vision-a", "vision-b"]
    settings.feedback_vision_key = Settings(feedback_vision_key="test-key").feedback_vision_key
    service.set_vision(True, "vision-a", "10000")
    service.vision = AsyncMock()
    service.vision.analyze.return_value = (result(), "vision-a")


def pictured_job(service, mid="1", **kwargs):
    job = feedback_job(service, mid, **kwargs)
    job["data"]["segments"].append(IMAGE)
    return job


async def test_disabled_never_calls_vision(service, db):
    service.vision = AsyncMock()
    await service.process_feedback(pictured_job(service))
    service.vision.analyze.assert_not_awaited()
    assert db.one("SELECT id FROM reports")
    assert not db.rows("SELECT * FROM screenshots")


async def test_related_screenshot_classification_persistence_and_commit_context(service, settings, db, ai):
    enable(service, settings)
    await service.process_feedback(pictured_job(service))
    assert ai.triage.await_count == 2
    assert ai.triage.call_args.kwargs["vision"]["visible_text"] == "渲染初始化失败"
    screenshot = db.one("SELECT * FROM screenshots")
    assert screenshot["model"] == "vision-a" and screenshot["report_id"] == 1
    assert db.issues()[0]["reporters"] == 1
    github = AsyncMock()
    github.detail.return_value = {"url": "https://github.com/o/r/commit/a", "files": [], "incomplete": True}
    await service.analyze_commit(commit_job(db), github)
    assert ai.commit.call_args.args[1][0]["screenshots"][0]["visible_text"] == "渲染初始化失败"


async def test_unrelated_screenshot_not_used_for_classification(service, settings, db, ai):
    enable(service, settings)
    service.vision.analyze.return_value = (result(False), "vision-a")
    await service.process_feedback(pictured_job(service))
    assert ai.triage.await_count == 1
    assert db.one("SELECT id FROM screenshots")
    assert service.screenshot_context(1) == []


async def test_irrelevant_text_does_not_send_images(service, settings, ai):
    enable(service, settings)
    ai.triage.return_value = ai.triage.return_value.model_copy(update={"kind": "irrelevant"})
    await service.process_feedback(pictured_job(service))
    service.vision.analyze.assert_not_awaited()


async def test_vision_cannot_replace_required_text_fields(service, settings, ai, db):
    enable(service, settings)
    ai.triage.return_value = ai.triage.return_value.model_copy(update={"device_quote": ""})
    await service.process_feedback(pictured_job(service))
    assert not db.rows("SELECT * FROM reports")
    assert not db.rows("SELECT * FROM screenshots")


async def test_too_many_images_rejected_before_download(settings):
    settings.feedback_vision_max_images = 1
    vision = Vision(settings, None, lambda: {"enabled": True})
    vision.fetch_image = AsyncMock()
    with pytest.raises(ValueError, match="数量"):
        await vision.analyze("test", [IMAGE, IMAGE])
    vision.fetch_image.assert_not_awaited()


async def test_delete_during_supplement_does_not_restore_screenshot(service, settings, db):
    enable(service, settings)
    await service.process_feedback(feedback_job(service))
    assert (
        service.ingest(
            qq="12345",
            group="88888",
            bot="11111",
            message_id="2",
            text="/反馈补图 1",
            segments=[IMAGE],
            explicit=True,
            issue_id=1,
        )
        == "queued"
    )
    job = db.one("SELECT * FROM jobs WHERE kind='feedback' ORDER BY id DESC")
    job["data"] = json.loads(job["payload"])

    async def analyze(*_):
        service.delete_reports("12345", [1], "10000")
        return result(), "vision-a"

    service.vision.analyze.side_effect = analyze
    await service.process_feedback(job)
    assert not db.rows("SELECT * FROM reports")
    assert not db.rows("SELECT * FROM screenshots")


async def test_image_only_message_ignored(service, settings):
    enable(service, settings)
    assert (
        service.ingest(qq="12345", group="88888", bot="11111", message_id="1", text="", segments=[IMAGE])
        == "ignored"
    )


async def test_visual_prompt_cannot_cause_ban(service, settings, ai, db):
    enable(service, settings)
    original = ai.triage.return_value
    ai.triage.side_effect = [original, original.model_copy(update={"kind": "abuse", "confidence": 1})]
    await service.process_feedback(pictured_job(service))
    assert not db.blocked("12345")
    assert db.one("SELECT id FROM reports")


async def test_provider_failure_falls_back_to_text(service, settings, db):
    enable(service, settings)
    service.vision.analyze.side_effect = httpx.ReadTimeout("no response")
    await service.process_feedback(pictured_job(service))
    assert db.one("SELECT id FROM reports")
    assert not db.rows("SELECT * FROM screenshots")
    assert "截图分析失败" in db.one("SELECT payload FROM jobs WHERE kind='send'")["payload"]


async def test_supplement_ownership_and_does_not_increase_counts(service, settings, db):
    enable(service, settings)
    settings.feedback_cooldown_seconds = 30
    await service.process_feedback(feedback_job(service))
    kwargs = {
        "group": "88888",
        "bot": "11111",
        "message_id": "2",
        "text": "/反馈补图 1",
        "segments": [IMAGE],
        "explicit": True,
        "issue_id": 1,
    }
    assert "只能" in service.ingest(qq="23456", **kwargs)
    assert service.ingest(qq="12345", **kwargs) == "queued"
    assert "提交过于频繁" in service.ingest(qq="12345", **{**kwargs, "message_id": "3"})
    job = db.one("SELECT * FROM jobs WHERE kind='feedback' ORDER BY id DESC")
    job["data"] = json.loads(job["payload"])
    await service.process_feedback(job)
    assert db.issues()[0]["reports"] == 1
    assert db.one("SELECT id FROM screenshots")
    service.delete_reports("12345", [1], "10000")
    assert not db.rows("SELECT * FROM screenshots")


async def test_disable_during_analysis_discards_result(service, settings, db):
    enable(service, settings)

    async def analyze(*_):
        service.set_vision(False, None, "10000")
        raise VisionDisabled()

    service.vision.analyze.side_effect = analyze
    await service.process_feedback(pictured_job(service))
    assert db.one("SELECT id FROM reports")
    assert not db.rows("SELECT * FROM screenshots")


async def test_vision_web_controls_authenticated_and_models_allowlisted(service, db, settings):
    enable(service, settings)
    app = FastAPI()
    app.include_router(
        create_router(SimpleNamespace(service=service, db=db, settings=settings, monitor_enabled=False))
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.put("/feedback/api/vision", json={"enabled": False})).status_code == 401
        client.headers["Authorization"] = "Bearer " + "a" * 40
        changed = await client.put("/feedback/api/vision", json={"enabled": True, "model": "vision-b"})
        assert changed.json()["model"] == "vision-b"
        assert "key" not in changed.json()
        assert (
            await client.put("/feedback/api/vision", json={"enabled": True, "model": "unknown"})
        ).status_code == 400
        await client.put("/feedback/api/vision", json={"enabled": False})
        assert not service.vision_state()["enabled"]


async def test_multimodal_request_uses_separate_provider_and_selected_model(service, settings):
    enable(service, settings)
    settings.feedback_vision_base_url = "https://vision.example/v1"
    service.set_vision(True, "vision-b", "10000")
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": result().model_dump_json()}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        vision = Vision(settings, client, service.vision_state)
        vision.fetch_image = AsyncMock(return_value="data:image/png;base64,ZmFrZQ==")
        analysis, model = await vision.analyze("苹果 Safari 黑屏", [IMAGE])
        assert model == "vision-b" and analysis.related
        req = seen[0]
        assert str(req.url) == "https://vision.example/v1/chat/completions"
        assert req.headers["Authorization"] == "Bearer test-key"
        body = json.loads(req.content)
        assert body["model"] == "vision-b"
        assert body["messages"][1]["content"][1]["type"] == "image_url"
        assert "gchat.qpic.cn" not in req.content.decode()
        service.set_vision(False, None, "10000")
        with pytest.raises(VisionDisabled):
            await vision.analyze("test", [IMAGE])
        assert len(seen) == 1


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://127.0.0.1/x",
        "https://evil.example/x",
        "https://gchat.qpic.cn.evil.example/x",
        "https://user:pass@gchat.qpic.cn/x",
    ],
)
async def test_unsafe_image_sources_rejected(settings, url):
    vision = Vision(settings, None, lambda: {"enabled": True})
    with pytest.raises(ValueError):
        await vision.fetch_image({"type": "image", "data": {"url": url}})


async def test_private_dns_rejected(settings, monkeypatch):
    monkeypatch.setattr(
        asyncio.get_running_loop(),
        "getaddrinfo",
        AsyncMock(return_value=[(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 443))]),
    )
    vision = Vision(settings, None, lambda: {"enabled": True})
    with pytest.raises(ValueError, match="公网"):
        await vision.fetch_image(IMAGE)


@pytest.mark.parametrize(
    "body,status",
    [
        (b"\x89PNG\r\n\x1a\nhello", 200),
        (b"not-an-image", 200),
        (b"\x89PNG\r\n\x1a\n" + b"x" * 2048, 200),
        (b"", 302),
    ],
)
async def test_image_download_limits_and_no_credentials(settings, monkeypatch, body, status):
    settings.feedback_vision_max_bytes = 1024
    monkeypatch.setattr(
        asyncio.get_running_loop(),
        "getaddrinfo",
        AsyncMock(return_value=[(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("8.8.8.8", 443))]),
    )
    original_client = httpx.AsyncClient
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(status, content=body, headers={"location": "http://127.0.0.1/private"})

    def client(**kwargs):
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
        return original_client(transport=httpx.MockTransport(respond), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    vision = Vision(settings, None, lambda: {"enabled": True})
    if body == b"\x89PNG\r\n\x1a\nhello":
        assert (await vision.fetch_image(IMAGE)).startswith("data:image/png;base64,")
    else:
        with pytest.raises((ValueError, httpx.HTTPStatusError)):
            await vision.fetch_image(IMAGE)
    assert len(requests) == 1 and "authorization" not in requests[0].headers


def test_v1_database_upgrades_without_losing_records(tmp_path):
    path = tmp_path / "old.sqlite"
    db = Database(path)
    db.execute("INSERT INTO faqs(question,answer) VALUES('test','answer')")
    db.execute("DROP TABLE screenshots")
    db.execute("DROP TABLE runtime_settings")
    db.execute("PRAGMA user_version=1")
    db.close()
    db = Database(path)
    assert db.one("SELECT answer FROM faqs")["answer"] == "answer"
    assert db.one("PRAGMA user_version")["user_version"] == 2
    db.execute("INSERT INTO runtime_settings(key,value) VALUES('vision_enabled','true')")
    db.close()
    db = Database(path)
    from feedback_hub.service import Service

    assert Service(db, Settings(), None).vision_state()["enabled"]
    db.close()
