import hashlib
import hmac
import time

import httpx

from .config import Repo, Settings
from .db import Database


def valid_signature(body: bytes, secret: str, signature: str) -> bool:
    if not secret or not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


class GitHub:
    def __init__(self, db: Database, settings: Settings, client: httpx.AsyncClient):
        self.db, self.settings, self.client = db, settings, client
        self.backoff_until = 0.0

    async def get(self, path: str, params: dict | None = None):
        if time.time() < self.backoff_until:
            raise RuntimeError("GitHub 限流退避中")
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if token := self.settings.feedback_github_token.get_secret_value():
            headers["Authorization"] = "Bearer " + token
        response = await self.client.get("https://api.github.com" + path, params=params, headers=headers)
        if response.status_code in {403, 429}:
            try:
                delay = float(response.headers.get("retry-after", "60"))
                reset = float(response.headers.get("x-ratelimit-reset", "0"))
                self.backoff_until = max(time.time() + delay, reset)
            except ValueError:
                self.backoff_until = time.time() + 60
        response.raise_for_status()
        return response.json(), response.headers

    async def sync(self, repo: Repo, initial_before: str = ""):
        state = self.db.one("SELECT * FROM repo_state WHERE repo_key=?", (repo.key,))
        cursor = state["cursor"] if state else ""
        if not cursor and initial_before and set(initial_before) != {"0"}:
            cursor = initial_before
        first, _ = await self.get(f"/repos/{repo.name}/commits", {"sha": repo.branch, "per_page": 100})
        if not first:
            return
        head = first[0]["sha"]
        if not cursor:
            # First polling run establishes a baseline without announcing repository history.
            self.db.execute(
                "INSERT INTO repo_state(repo_key,cursor,checked) VALUES(?,?,?) "
                "ON CONFLICT(repo_key) DO UPDATE SET cursor=excluded.cursor,checked=excluded.checked,error=''",
                (repo.key, head, time.time()),
            )
            return
        commits, found, page = [], False, first
        # Fix the head SHA for pagination, otherwise pushes during scanning can skip/duplicate commits.
        for number in range(1, self.settings.feedback_history_pages + 1):
            if number > 1:
                page, _ = await self.get(
                    f"/repos/{repo.name}/commits", {"sha": head, "per_page": 100, "page": number}
                )
            for commit in page:
                if commit["sha"] == cursor:
                    found = True
                    break
                commits.append(commit)
            if found or len(page) < 100:
                break
        if not found:
            raise RuntimeError("未找到旧游标：可能 force-push 或超出扫描上限；需管理员检查并重置基线")
        with self.db.transaction():
            for commit in reversed(commits):
                self.db.enqueue(
                    "commit",
                    f"commit:{repo.key}:{commit['sha']}",
                    {
                        "repo": repo.name,
                        "branch": repo.branch,
                        "sha": commit["sha"],
                        "groups": repo.announce_groups,
                    },
                )
            self.db.execute(
                "INSERT INTO repo_state(repo_key,cursor,checked) VALUES(?,?,?) "
                "ON CONFLICT(repo_key) DO UPDATE SET cursor=excluded.cursor,checked=excluded.checked,error=''",
                (repo.key, head, time.time()),
            )

    async def detail(self, repo: str, sha: str):
        data, headers = await self.get(f"/repos/{repo}/commits/{sha}", {"per_page": 100})
        files = data.get("files", [])
        incomplete = 'rel="next"' in headers.get("link", "")
        # Bounded model input. Missing, binary, or truncated patches disable automatic resolution.
        used, selected = 0, []
        for item in files:
            patch = item.get("patch", "")
            if not patch:
                incomplete = True
            additions = sum(line.startswith("+") for line in patch.splitlines())
            deletions = sum(line.startswith("-") for line in patch.splitlines())
            if additions != item.get("additions", additions) or deletions != item.get("deletions", deletions):
                incomplete = True
            if used + len(patch) > 45000:
                incomplete = True
                break
            selected.append({"filename": item["filename"], "status": item["status"], "patch": patch})
            used += len(patch)
        return {
            "sha": sha,
            "repo": repo,
            "message": data["commit"]["message"][:6000],
            "url": f"https://github.com/{repo}/commit/{sha}",
            "files": selected,
            "incomplete": incomplete,
        }
