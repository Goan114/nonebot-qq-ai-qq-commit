from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Triage(StrictModel):
    kind: Literal["feedback", "irrelevant", "abuse"]
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(max_length=1000)
    device: str = Field(default="", max_length=120)
    browser: str = Field(default="", max_length=120)
    device_quote: str = Field(default="", max_length=200)
    browser_quote: str = Field(default="", max_length=200)
    meaningful: bool = False
    title: str = Field(default="", max_length=120)
    category: str = Field(default="其他", max_length=60)
    summary: str = Field(default="", max_length=1000)


class Match(StrictModel):
    issue_id: int | None = None
    faq_id: int | None = None
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(max_length=1000)


class Fix(StrictModel):
    issue_id: int
    confidence: float = Field(ge=0, le=1)
    explanation: str = Field(min_length=1, max_length=1500)
    file: str = Field(max_length=500)
    patch_quote: str = Field(max_length=3000)


class CommitAnalysis(StrictModel):
    summary: str = Field(min_length=1, max_length=1500)
    fixes: list[Fix] = Field(default_factory=list, max_length=20)
