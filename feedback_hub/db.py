import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS issues (
 id INTEGER PRIMARY KEY, title TEXT NOT NULL, category TEXT NOT NULL, summary TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'open', comment TEXT NOT NULL DEFAULT '',
 created REAL NOT NULL, updated REAL NOT NULL, resolution TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS reports (
 id INTEGER PRIMARY KEY, issue_id INTEGER NOT NULL REFERENCES issues(id), qq TEXT NOT NULL,
 group_id TEXT NOT NULL, bot_id TEXT NOT NULL, message_id TEXT NOT NULL,
 original TEXT NOT NULL, segments TEXT NOT NULL, device TEXT NOT NULL, browser TEXT NOT NULL,
 created REAL NOT NULL, UNIQUE(bot_id, group_id, message_id)
);
CREATE INDEX IF NOT EXISTS reports_issue ON reports(issue_id, qq);
CREATE INDEX IF NOT EXISTS reports_qq ON reports(qq);
CREATE TABLE IF NOT EXISTS blacklist (
 qq TEXT PRIMARY KEY, reason TEXT NOT NULL, actor TEXT NOT NULL, created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS faqs (
 id INTEGER PRIMARY KEY, question TEXT NOT NULL, answer TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS jobs (
 id INTEGER PRIMARY KEY, kind TEXT NOT NULL, dedup TEXT NOT NULL UNIQUE,
 payload TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
 next_at REAL NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '', created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(kind, state, next_at);
CREATE TABLE IF NOT EXISTS repo_state (
 repo_key TEXT PRIMARY KEY, cursor TEXT NOT NULL DEFAULT '', checked REAL NOT NULL DEFAULT 0,
 error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS commits (
 repo TEXT NOT NULL, sha TEXT NOT NULL, summary TEXT NOT NULL, url TEXT NOT NULL,
 created REAL NOT NULL, PRIMARY KEY(repo, sha)
);
CREATE TABLE IF NOT EXISTS proposals (
 id INTEGER PRIMARY KEY, issue_id INTEGER NOT NULL REFERENCES issues(id), repo TEXT NOT NULL,
 sha TEXT NOT NULL, confidence REAL NOT NULL, explanation TEXT NOT NULL, evidence TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'pending', created REAL NOT NULL, UNIQUE(issue_id, repo, sha)
);
CREATE TABLE IF NOT EXISTS audit (
 id INTEGER PRIMARY KEY, actor TEXT NOT NULL, action TEXT NOT NULL, detail TEXT NOT NULL, created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS runtime_settings (
 key TEXT PRIMARY KEY, value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS screenshots (
 id INTEGER PRIMARY KEY, report_id INTEGER NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
 job_id INTEGER NOT NULL UNIQUE, original TEXT NOT NULL, segments TEXT NOT NULL,
 model TEXT NOT NULL, analysis TEXT NOT NULL, created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS screenshots_report ON screenshots(report_id);
PRAGMA user_version=2;
"""


class Database:
    """Short synchronous transactions. Run a single bot process against this database."""

    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=5000")
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version > 2:
            raise RuntimeError("数据库版本高于当前程序，拒绝降级打开")
        self.conn.executescript(SCHEMA)

    @contextmanager
    def transaction(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.conn.rollback()
            raise
        else:
            self.conn.commit()

    def execute(self, sql: str, args=()):
        return self.conn.execute(sql, args)

    def rows(self, sql: str, args=()) -> list[dict]:
        return [dict(row) for row in self.execute(sql, args)]

    def one(self, sql: str, args=()) -> dict | None:
        row = self.execute(sql, args).fetchone()
        return dict(row) if row else None

    def audit(self, actor: str, action: str, detail: Any):
        self.execute(
            "INSERT INTO audit(actor,action,detail,created) VALUES(?,?,?,?)",
            (actor, action, json.dumps(detail, ensure_ascii=False), time.time()),
        )

    def blocked(self, qq: str) -> bool:
        return self.one("SELECT qq FROM blacklist WHERE qq=?", (qq,)) is not None

    def enqueue(self, kind: str, dedup: str, payload: dict) -> bool:
        return (
            self.execute(
                "INSERT OR IGNORE INTO jobs(kind,dedup,payload,created) VALUES(?,?,?,?)",
                (kind, dedup, json.dumps(payload, ensure_ascii=False), time.time()),
            ).rowcount
            == 1
        )

    def issues(self, status: str = "", limit: int = 100, offset: int = 0):
        return self.rows(
            """SELECT i.*, COUNT(DISTINCT r.qq) AS reporters, COUNT(r.id) AS reports
            FROM issues i LEFT JOIN reports r ON r.issue_id=i.id
            WHERE (?='' OR i.status=?) GROUP BY i.id
            ORDER BY reporters DESC, i.created ASC LIMIT ? OFFSET ?""",
            (status, status, limit, offset),
        )

    def fail_job(self, job: dict, error: str):
        attempts = job["attempts"] + 1
        self.execute(
            "UPDATE jobs SET state=?,attempts=?,next_at=MAX(next_at,?),error=? WHERE id=?",
            (
                "failed" if attempts >= 8 else "pending",
                attempts,
                time.time() + min(3600, 5 * 2**attempts),
                error[:500],
                job["id"],
            ),
        )

    def done(self, job_id: int):
        self.execute("UPDATE jobs SET state='done',error='' WHERE id=?", (job_id,))

    def close(self):
        self.conn.close()
