from pathlib import Path

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator


class Repo(BaseModel):
    name: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    branch: str = "main"
    poll_seconds: int = Field(default=300, ge=30)
    announce_groups: list[str] = Field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.name}@{self.branch}"


class Settings(BaseModel):
    feedback_groups: set[str] = Field(default_factory=set)
    feedback_database: Path = Path("data/feedback.sqlite3")
    feedback_ai_base_url: str = "https://api.openai.com/v1"
    feedback_ai_key: SecretStr = SecretStr("")
    feedback_ai_model: str = ""
    feedback_vision_enabled: bool = False
    feedback_vision_base_url: str = ""
    feedback_vision_key: SecretStr = SecretStr("")
    feedback_vision_model: str = ""
    feedback_vision_models: list[str] = Field(default_factory=list)
    feedback_vision_max_images: int = Field(default=3, ge=1, le=5)
    feedback_vision_max_bytes: int = Field(default=5_000_000, ge=1024, le=20_000_000)
    feedback_vision_image_hosts: set[str] = {"multimedia.nt.qq.com", "gchat.qpic.cn", "c2cpicdw.qpic.cn"}
    feedback_admin_token: SecretStr = SecretStr("")
    feedback_webhook_secret: SecretStr = SecretStr("")
    feedback_github_token: SecretStr = SecretStr("")
    feedback_repos: list[Repo] = Field(default_factory=list)
    feedback_faq_prompt: str = "这个问题已有明确解决方法，请先查看常见问题：{url}\n{answer}"
    feedback_faq_url: str = "请联系管理员获取常见问题"
    feedback_auto_resolve: bool = True
    feedback_resolve_threshold: float = Field(default=0.95, ge=0.8, le=1)
    feedback_auto_ban: bool = True
    feedback_ban_threshold: float = Field(default=0.98, ge=0.9, le=1)
    feedback_listen_all: bool = False
    feedback_keywords: list[str] = [
        "卡顿",
        "黑屏",
        "白屏",
        "闪退",
        "报错",
        "bug",
        "掉帧",
        "加载",
        "无法",
        "失败",
    ]
    feedback_cooldown_seconds: int = Field(default=30, ge=0)
    feedback_send_cooldown_seconds: int = Field(default=10, ge=0, le=3600)
    feedback_max_message_chars: int = Field(default=6000, ge=100, le=20000)
    feedback_max_pending: int = Field(default=1000, ge=10)
    feedback_history_pages: int = Field(default=100, ge=1, le=1000)

    @field_validator("feedback_admin_token", "feedback_webhook_secret")
    @classmethod
    def strong_secrets(cls, value: SecretStr) -> SecretStr:
        if value.get_secret_value() and len(value.get_secret_value()) < 32:
            raise ValueError("管理令牌和 webhook secret 至少 32 个字符")
        return value

    @model_validator(mode="after")
    def unique_repos(self):
        if len({r.key for r in self.feedback_repos}) != len(self.feedback_repos):
            raise ValueError("仓库和分支配置重复")
        return self
