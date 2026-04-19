"""Pydantic models for Venice review JSON (OpenAI chat response body)."""
from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

logger = logging.getLogger(__name__)

__all__ = ["ReviewPayload", "SuggestionItem"]


class SuggestionItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    severity: Literal["high", "medium", "low"] = "medium"
    detail: str = ""

    @field_validator("severity", mode="before")
    @classmethod
    def normalize_severity(cls, v: object) -> str:
        if v is None:
            return "medium"
        s = str(v).lower().strip()
        if s in ("high", "medium", "low"):
            return s
        return "medium"

    @field_validator("detail", mode="before")
    @classmethod
    def normalize_detail(cls, v: object) -> str:
        if v is None:
            return ""
        return str(v).strip()


class ReviewPayload(BaseModel):
    """Expected JSON object from the model (system prompt contract).

    Dimensions are often empty strings; the engine strips generic filler before rendering.
    """

    model_config = ConfigDict(extra="ignore")

    security: str = ""
    code_quality: str = ""
    structure: str = ""
    performance: str = ""
    testing_observability: str = ""
    suggestions: list[SuggestionItem] = Field(default_factory=list)

    @field_validator(
        "security",
        "code_quality",
        "structure",
        "performance",
        "testing_observability",
        mode="before",
    )
    @classmethod
    def coerce_text(cls, v: object) -> str:
        if v is None:
            return ""
        if isinstance(v, str):
            return v
        logger.debug("Review field expected str; coercing %s", type(v).__name__)
        return str(v)

    def to_report_dict(self) -> dict:
        return {
            "security": self.security,
            "code_quality": self.code_quality,
            "structure": self.structure,
            "performance": self.performance,
            "testing_observability": self.testing_observability,
            "suggestions": [{"severity": s.severity, "detail": s.detail} for s in self.suggestions],
        }
