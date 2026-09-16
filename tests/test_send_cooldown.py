from unittest.mock import AsyncMock

import pytest

from feedback_hub.config import Settings
from feedback_hub.runtime import Runtime


@pytest.fixture
async def runtime(tmp_path):
    run = Runtime(
        Settings(
            feedback_database=tmp_path / "cooldown.sqlite",
            feedback_groups={"88888", "99999"},
            feedback_send_cooldown_seconds=10,
        ),
        set(),
        AsyncMock(),
    )
    yield run
    await run.stop()


def enqueue(runtime, key, group="88888"):
    runtime.service.notify(key, group, "消息")
    return runtime.db.one("SELECT * FROM jobs WHERE dedup=?", (key,))


async def test_same_group_waits_without_consuming_retry(runtime, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("feedback_hub.runtime.time.time", lambda: now[0])
    first, second = enqueue(runtime, "one"), enqueue(runtime, "two")
    await runtime.process(first)
    await runtime.process(second)
    assert runtime.sender.await_count == 1
    row = runtime.db.one("SELECT * FROM jobs WHERE id=?", (second["id"],))
    assert row["state"] == "pending" and row["attempts"] == 0 and row["next_at"] == 1010
    now[0] = 1010
    await runtime.process(row)
    assert runtime.sender.await_count == 2
    assert runtime.db.one("SELECT state FROM jobs WHERE id=?", (second["id"],))["state"] == "done"


async def test_other_groups_not_blocked_by_backlog(runtime, monkeypatch):
    monkeypatch.setattr("feedback_hub.runtime.time.time", lambda: 1000)
    first = enqueue(runtime, "first")
    enqueue(runtime, "queued")
    other = enqueue(runtime, "other", "99999")
    await runtime.process(first)
    due = runtime.db.one(
        "SELECT * FROM jobs WHERE kind='send' AND state='pending' AND next_at<=1000 ORDER BY id LIMIT 1"
    )
    assert due["id"] == other["id"]
    await runtime.process(due)
    assert runtime.sender.await_count == 2


async def test_new_jobs_respect_current_cooldown(runtime, monkeypatch):
    monkeypatch.setattr("feedback_hub.runtime.time.time", lambda: 1000)
    await runtime.process(enqueue(runtime, "first"))
    await runtime.process(enqueue(runtime, "arrived_later"))
    assert runtime.sender.await_count == 1


async def test_failed_send_paces_attempts_and_preserves_backoff(runtime, monkeypatch):
    monkeypatch.setattr("feedback_hub.runtime.time.time", lambda: 1000)
    runtime.settings.feedback_send_cooldown_seconds = 60
    runtime.sender.side_effect = RuntimeError("offline")
    first, second = enqueue(runtime, "failed"), enqueue(runtime, "waiting")
    with pytest.raises(RuntimeError):
        await runtime.process(first)
    runtime.db.fail_job(first, "offline")
    await runtime.process(second)
    assert runtime.sender.await_count == 1
    row = runtime.db.one("SELECT * FROM jobs WHERE id=?", (first["id"],))
    assert row["next_at"] == 1060 and row["attempts"] == 1


async def test_cooldown_survives_restart(tmp_path, monkeypatch):
    monkeypatch.setattr("feedback_hub.runtime.time.time", lambda: 1000)
    config = Settings(feedback_database=tmp_path / "restart.sqlite", feedback_groups={"88888"})
    first = Runtime(config, set(), AsyncMock())
    await first.process(enqueue(first, "before"))
    await first.stop()
    restarted = Runtime(config, set(), AsyncMock())
    try:
        await restarted.process(enqueue(restarted, "after"))
        restarted.sender.assert_not_awaited()
    finally:
        await restarted.stop()


async def test_zero_disables_extra_cooldown(runtime, monkeypatch):
    monkeypatch.setattr("feedback_hub.runtime.time.time", lambda: 1000)
    runtime.settings.feedback_send_cooldown_seconds = 0
    await runtime.process(enqueue(runtime, "one"))
    await runtime.process(enqueue(runtime, "two"))
    assert runtime.sender.await_count == 2


def test_repeated_immediate_notices_are_coalesced(service, db):
    for i in range(20):
        service.notice("12345", "88888", "11111", str(i), "提交过于频繁")
    assert len(db.rows("SELECT * FROM jobs WHERE kind='send'")) == 1
    service.notice("23456", "88888", "11111", "other", "提交过于频繁")
    assert len(db.rows("SELECT * FROM jobs WHERE kind='send'")) == 2


async def test_successful_slow_send_cools_from_completion(runtime, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("feedback_hub.runtime.time.time", lambda: now[0])

    async def slow_send(_):
        now[0] = 1005

    runtime.sender.side_effect = slow_send
    await runtime.process(enqueue(runtime, "slow"))
    second = enqueue(runtime, "second")
    now[0] = 1010
    await runtime.process(second)
    assert runtime.sender.await_count == 1
    assert runtime.db.one("SELECT next_at FROM jobs WHERE id=?", (second["id"],))["next_at"] == 1015
