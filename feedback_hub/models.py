from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class VisionAnalysis(StrictModel):
    related: bool
    confidence: float = Field(ge=0, le=1)
    summary: str = Field(min_length=1, max_length=1500)
    visible_text: str = Field(default="", max_length=3000)
    observations: list[str] = Field(default_factory=list, max_length=10)
    limitations: str = Field(default="", max_length=1000)

    @field_validator("observations")
    @classmethod
    def bounded_observations(cls, values):
        if any(len(value) > 500 for value in values):
            raise ValueError("截图观察条目过长")
        return values


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
    announce: bool
    summary: str = Field(min_length=1, max_length=1500)
    fixes: list[Fix] = Field(default_factory=list, max_length=20)
