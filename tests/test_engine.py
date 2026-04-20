"""Unit tests for autoreview.engine (no network)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from autoreview.engine import (
    BATCH_SIZE_ALL,
    DEFAULT_OUTPUT_NAME,
    VENICE_MODEL_CHOICES,
    VENICE_MODEL_GROUPS,
    ReviewState,
    _format_run_banner,
    _is_substantive_review_text,
    _resolve_report_path,
    _review_context_hint,
    compute_fingerprint,
    discover_files,
    effective_batch_size,
    estimate_project_spend_usd,
    filter_paths,
    json_to_markdown,
    merge_completion_usage,
    parse_review_json,
    read_file_limited,
    validate_review_dict,
    validate_review_payload,
    venice_category_for_model,
    venice_model_categories,
    venice_models_for_category,
)


def test_venice_model_groups_cover_flat_list() -> None:
    flat = set(VENICE_MODEL_CHOICES)
    grouped = {m for _, models in VENICE_MODEL_GROUPS for m in models}
    assert flat == grouped
    assert len(VENICE_MODEL_CHOICES) == len(flat)
    for cat in venice_model_categories():
        for mid in venice_models_for_category(cat):
            assert venice_category_for_model(mid) == cat


@pytest.mark.parametrize(
    "batch,pending,expected",
    [
        (0, 7, 7),
        (BATCH_SIZE_ALL, 100, 100),
        (10, 100, 10),
        (50, 3, 3),
    ],
)
def test_effective_batch_size_parametrized(batch: int, pending: int, expected: int) -> None:
    assert effective_batch_size(batch, pending) == expected


def test_effective_batch_size_rejects_negative() -> None:
    with pytest.raises(ValueError):
        effective_batch_size(-1, 5)


def test_resolve_report_path_explicit(tmp_path: Path) -> None:
    custom = tmp_path / "my_report.md"
    assert _resolve_report_path(tmp_path, custom, None) == custom.resolve()


def test_resolve_report_path_explicit_overrides_state(tmp_path: Path) -> None:
    st = ReviewState(output_name="from_state.md")
    override = tmp_path / "override.md"
    assert _resolve_report_path(tmp_path, override, st) == override.resolve()


def test_resolve_report_path_from_state(tmp_path: Path) -> None:
    st = ReviewState(output_name="custom_report.md")
    assert _resolve_report_path(tmp_path, None, st) == (tmp_path / "custom_report.md").resolve()


def test_resolve_report_path_default_without_state(tmp_path: Path) -> None:
    assert _resolve_report_path(tmp_path, None, None) == (tmp_path / DEFAULT_OUTPUT_NAME).resolve()


def test_compute_fingerprint_stable() -> None:
    paths = ["a.py", "b/c.py"]
    fp1 = compute_fingerprint(paths, (), ())
    fp2 = compute_fingerprint(paths, (), ())
    assert fp1 == fp2
    fp3 = compute_fingerprint(list(reversed(paths)), (), ())
    assert fp1 == fp3


def test_discover_skips_meta_files(tmp_path: Path) -> None:
    (tmp_path / "real.py").write_text("x = 1\n")
    (tmp_path / ".env").write_text("SECRET=x\n")
    (tmp_path / "VENICE_CODE_REVIEW.md").write_text("# report\n")
    found = discover_files(tmp_path)
    assert "real.py" in found
    assert ".env" not in found
    assert "VENICE_CODE_REVIEW.md" not in found


def test_discover_skips_tool_cache_paths(tmp_path: Path) -> None:
    (tmp_path / "good.py").write_text("x = 1\n")
    rc = tmp_path / ".ruff_cache"
    rc.mkdir()
    (rc / ".gitignore").write_text("*\n")
    (rc / "CACHEDIR.TAG").write_text("Signature: 8a477f597d28d172789f06886806bc55\n")
    found = discover_files(tmp_path)
    assert "good.py" in found
    assert not any(".ruff_cache" in p for p in found)
    assert not any(p.endswith("CACHEDIR.TAG") for p in found)


def test_discover_excludes_markdown_and_doc_trees_by_default(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("x=1\n")
    (tmp_path / "README.md").write_text("# hi\n")
    dd = tmp_path / "docs"
    dd.mkdir()
    (dd / "guide.md").write_text("x")
    (dd / "helper.py").write_text("def f(): pass")
    found = discover_files(tmp_path)
    assert "app.py" in found
    assert "README.md" not in found
    assert "docs/guide.md" not in found
    assert "docs/helper.py" not in found


def test_discover_includes_markdown_when_opt_in(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x")
    (tmp_path / "b.MD").write_text("#")
    found = discover_files(tmp_path, apply_default_doc_excludes=False)
    assert "a.py" in found
    assert "b.MD" in found


def test_filter_paths_include_exclude(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "x.js").write_text("x")
    paths = ["a.py", "node_modules/x.js"]
    out = filter_paths(paths, tmp_path, include=("*.py",), exclude=())
    assert out == ["a.py"]
    out2 = filter_paths(paths, tmp_path, include=(), exclude=("node_modules/*",))
    assert "node_modules/x.js" not in out2


def test_parse_review_json_fenced() -> None:
    raw = '```json\n{"security": "ok", "code_quality": "", "structure": "", "performance": "", "testing_observability": "", "suggestions": []}\n```'
    d = parse_review_json(raw)
    assert d["security"] == "ok"


def test_parse_review_json_embedded() -> None:
    raw = 'Here you go:\n{"security": "x", "code_quality": "", "structure": "", "performance": "", "testing_observability": "", "suggestions": []}\nThanks'
    d = parse_review_json(raw)
    assert d["security"] == "x"


def test_parse_review_json_invalid_raises() -> None:
    with pytest.raises(json.JSONDecodeError):
        parse_review_json("this is not valid json")


def test_is_substantive_review_text() -> None:
    assert not _is_substantive_review_text("")
    assert not _is_substantive_review_text("none")
    assert not _is_substantive_review_text("  OK. ")
    assert not _is_substantive_review_text("N/A")
    assert _is_substantive_review_text("x" * 200)
    assert _is_substantive_review_text("Parameterize queries to mitigate SQL injection risk.")


def test_format_run_banner() -> None:
    s = _format_run_banner("kimi-k2-5", "10", ["a.ts", "b/c.ts"])
    assert "## Autoreview run" in s
    assert "`kimi-k2-5`" in s
    assert "Batch:** 10" in s
    assert "`a.ts`" in s
    assert "Files this run (2)" in s
    tail = _format_run_banner("m", "all pending files", [f"f{i}.ts" for i in range(25)])
    assert "+5 more" in tail


def test_review_context_hint() -> None:
    assert _review_context_hint("src/lib.py") == ""
    h = _review_context_hint("tests/unit/test_foo.py")
    assert h and "test" in h.lower()
    assert "workflow" in _review_context_hint(".github/workflows/ci.yml").lower()
    assert "secret" in _review_context_hint("config/app.json").lower()


def test_json_to_markdown_placeholder_when_only_filler() -> None:
    md = json_to_markdown("a.py", {"security": "ok", "suggestions": []})
    assert "No substantive feedback" in md


def test_json_to_markdown() -> None:
    data = {
        "security": "Validate redirects against an allowlist.",
        "code_quality": "ok",
        "structure": "",
        "performance": "",
        "testing_observability": "",
        "suggestions": [{"severity": "low", "detail": "Rename loop index for clarity."}],
    }
    md = json_to_markdown("src/x.py", data)
    assert "src/x.py" in md
    assert "Rename loop" in md
    assert "### Code quality" not in md
    assert "allowlist" in md


def test_read_file_limited(tmp_path: Path) -> None:
    p = tmp_path / "t.txt"
    p.write_text("hello", encoding="utf-8")
    text, skip = read_file_limited(p, 100)
    assert text == "hello"
    assert skip is None


def test_read_file_binary_skip(tmp_path: Path) -> None:
    p = tmp_path / "b.bin"
    p.write_bytes(b"\x00\x01\x02")
    text, skip = read_file_limited(p, 100)
    assert text is None
    assert skip is not None


def test_read_file_oversized_skips_without_reading_entire_file(tmp_path: Path) -> None:
    p = tmp_path / "big.txt"
    p.write_bytes(b"x" * (20_000))
    text, skip = read_file_limited(p, 10_000)
    assert text is None
    assert skip is not None
    assert "10000" in skip


def test_validate_review_dict_normalizes() -> None:
    d = validate_review_dict(
        {
            "security": "Audit trust boundaries for this handler.",
            "suggestions": [{"severity": "HIGH", "detail": "  nit  "}],
        }
    )
    assert d["suggestions"][0]["severity"] == "high"
    assert d["suggestions"][0]["detail"] == "nit"
    assert "code_quality" in d


def test_validate_review_dict_strips_filler_ok() -> None:
    d = validate_review_dict({"security": "ok", "code_quality": "none"})
    assert d["security"] == ""
    assert d["code_quality"] == ""


def test_validate_review_dict_caps_suggestions_by_severity() -> None:
    d = validate_review_dict(
        {
            "security": "x",
            "suggestions": [
                {"severity": "low", "detail": "drop1"},
                {"severity": "high", "detail": "keepH1"},
                {"severity": "medium", "detail": "keepM1"},
                {"severity": "low", "detail": "drop2"},
                {"severity": "high", "detail": "keepH2"},
                {"severity": "medium", "detail": "keepM2"},
                {"severity": "low", "detail": "drop3"},
            ],
        }
    )
    assert len(d["suggestions"]) == 5
    sevs = [x["severity"] for x in d["suggestions"]]
    assert sevs.count("high") == 2
    assert sevs.count("medium") == 2
    assert sevs.count("low") == 1


def test_validate_review_payload_pydantic() -> None:
    d = validate_review_payload(
        {
            "security": "Check CSRF on mutating routes.",
            "suggestions": [{"severity": "HIGH", "detail": "  nit  "}],
        }
    )
    assert d["suggestions"][0]["severity"] == "high"
    assert d["suggestions"][0]["detail"] == "nit"
    assert d["code_quality"] == ""


def test_merge_and_estimate_usage() -> None:
    st = ReviewState()
    merge_completion_usage(st, "m1", {"prompt_tokens": 1_000_000, "completion_tokens": 500_000})
    pm = {"m1": (1.0, 2.0)}
    est = estimate_project_spend_usd(pm, st)
    assert est is not None
    assert abs(est - (1.0 * 1.0 + 0.5 * 2.0)) < 1e-9
    merge_completion_usage(st, "m1", {"prompt_tokens": 0, "completion_tokens": 0})
    assert estimate_project_spend_usd(pm, st) == est
