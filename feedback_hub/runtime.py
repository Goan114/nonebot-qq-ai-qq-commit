import asyncio
import json
import logging
import time

import httpx

from .ai import AI
from .config import Settings
from .db import Database
from .github import GitHub
from .service import Service

log = logging.getLogger("feedback_hub")


class Runtime:
    def __init__(self, settings: Settings, superusers: set[str], sender):
        self.settings = settings
        self.db = Database(settings.feedback_database)
        self.client = httpx.AsyncClient(timeout=30, follow_redirects=False)
        self.service = Service(self.db, settings, AI(settings, self.client), superusers)
        self.github = GitHub(self.db, settings, self.client)
        self.sender = sender
        self.monitor_enabled = False
        self.tasks: list[asyncio.Task] = []

    async def start(self):
        self.tasks = [
            asyncio.create_task(self.work("feedback")),
            asyncio.create_task(self.work("commit")),
            asyncio.create_task(self.work("send")),
            asyncio.create_task(self.poll()),
        ]

    async def stop(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.client.aclose()
        self.db.close()

    def active(self, job):
        row = self.db.one("SELECT state FROM jobs WHERE id=?", (job["id"],))
        return row and row["state"] == "pending"

    async def process(self, job):
        job["data"] = json.loads(job["payload"])
        if job["kind"] == "feedback":
            await self.service.process_feedback(job)
        elif job["kind"] == "commit":
            await self.service.analyze_commit(job, self.github)
        else:
            p = job["data"]
            qq = p.get("qq", "")
            if p["group"] not in self.settings.feedback_groups or (qq and self.db.blocked(qq)):
                self.db.done(job["id"])
                return
            if (
                p.get("issue_id")
                and qq
                and not self.db.one(
                    "SELECT id FROM reports WHERE issue_id=? AND qq=? AND group_id=?",
                    (p["issue_id"], qq, p["group"]),
                )
            ):
                self.db.done(job["id"])
                return
            await self.sender(p)
            self.db.done(job["id"])

    async def work(self, kind: str):
        while True:
            if kind == "commit" and not self.monitor_enabled:
                await asyncio.sleep(1)
                continue
            job = self.db.one(
                "SELECT * FROM jobs WHERE kind=? AND state='pending' AND next_at<=? ORDER BY id LIMIT 1",
                (kind, time.time()),
            )
            if not job:
                await asyncio.sleep(1)
                continue
            try:
                await self.process(job)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 -- worker must isolate all task failures
                # HTTP errors can contain sensitive request URLs. Store only class/status, never tokens/body.
                status = getattr(getattr(exc, "response", None), "status_code", "")
                error = f"{type(exc).__name__} {status}".strip()
                log.warning("Job %s (%s) failed: %s", job["id"], kind, error)
                if self.active(job):
                    self.db.fail_job(job, error)
            await asyncio.sleep(0.5 if kind == "send" else 0)

    async def poll(self):
        due: dict[str, float] = {}
        while True:
            if self.monitor_enabled:
                for repo in self.settings.feedback_repos:
                    hook = self.db.one(
                        "SELECT * FROM jobs WHERE kind='hook' AND state='pending' "
                        "AND json_extract(payload,'$.repo_key')=? AND next_at<=? "
                        "ORDER BY id LIMIT 1",
                        (repo.key, time.time()),
                    )
                    if not hook and time.time() < due.get(repo.key, 0):
                        continue
                    try:
                        before = json.loads(hook["payload"]).get("before", "") if hook else ""
                        await self.github.sync(repo, before)
                        if hook:
                            self.db.done(hook["id"])
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001 -- one repository must not stop others
                        error = str(exc) if type(exc) is RuntimeError else type(exc).__name__
                        self.db.execute(
                            "INSERT INTO repo_state(repo_key,error,checked) VALUES(?,?,?) "
                            "ON CONFLICT(repo_key) DO UPDATE SET error=excluded.error,checked=excluded.checked",
                            (repo.key, error[:500], time.time()),
                        )
                        if hook:
                            self.db.fail_job(hook, error)
                        log.warning("Repository %s: %s", repo.key, error[:500])
                    due[repo.key] = time.time() + repo.poll_seconds
            await asyncio.sleep(2)
