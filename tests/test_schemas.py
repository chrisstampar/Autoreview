"""Tests for autoreview.schemas (Pydantic review payload)."""
from __future__ import annotations

import logging

import pytest

from autoreview.schemas import ReviewPayload, SuggestionItem


def test_suggestion_severity_case_and_invalid() -> None:
    assert SuggestionItem(severity="HIGH").severity == "high"
    assert SuggestionItem(severity="Low").severity == "low"
    assert SuggestionItem(severity="bogus").severity == "medium"
    assert SuggestionItem(severity=None).severity == "medium"


def test_suggestion_detail_strip_and_none() -> None:
    assert SuggestionItem(detail="  x  ").detail == "x"
    assert SuggestionItem(detail=None).detail == ""


def test_review_payload_coerce_text_str_and_none() -> None:
    m = ReviewPayload.model_validate(
        {
            "security": "ok",
            "code_quality": None,
            "structure": "",
        }
    )
    assert m.security == "ok"
    assert m.code_quality == ""
    assert m.structure == ""


def test_review_payload_coerce_text_number_to_str() -> None:
    m = ReviewPayload.model_validate({"security": 42, "performance": 3.14})
    assert m.security == "42"
    assert m.performance == "3.14"


def test_review_payload_coerce_text_non_str_logs_debug(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="autoreview.schemas")
    m = ReviewPayload.model_validate({"security": object()})
    assert "object at" in m.security
    assert "Review field expected str" in caplog.text


def test_review_payload_extra_keys_ignored() -> None:
    m = ReviewPayload.model_validate({"security": "x", "unknown_field": 1, "foo": "bar"})
    assert m.security == "x"
    assert not hasattr(m, "unknown_field")


def test_review_payload_suggestions_from_dicts() -> None:
    m = ReviewPayload.model_validate(
        {
            "suggestions": [
                {"severity": "HIGH", "detail": "  z  "},
                {"severity": "nope", "detail": "y"},
            ]
        }
    )
    assert len(m.suggestions) == 2
    assert m.suggestions[0].severity == "high"
    assert m.suggestions[0].detail == "z"
    assert m.suggestions[1].severity == "medium"
    assert m.suggestions[1].detail == "y"


def test_to_report_dict_shape() -> None:
    m = ReviewPayload(
        security="a",
        suggestions=[SuggestionItem(severity="low", detail="b")],
    )
    d = m.to_report_dict()
    assert d["security"] == "a"
    assert d["suggestions"] == [{"severity": "low", "detail": "b"}]
