"""Core: file discovery, persisted batch state, Venice API, markdown report."""
from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import os
import random
import re
import subprocess
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from openai import APIError, APITimeoutError, OpenAI, RateLimitError
from pydantic import ValidationError

from autoreview.schemas import ReviewPayload

logger = logging.getLogger(__name__)

# OpenAI client expects base URL without trailing slash (paths like /chat/completions are appended).
VENICE_BASE_URL = os.environ.get("VENICE_BASE_URL", "https://api.venice.ai/api/v1").rstrip("/")
DEFAULT_MODEL = os.environ.get("VENICE_MODEL", "kimi-k2-5")

# Curated for the GUI from Venice text models (see https://docs.venice.ai/models/overview ).
# Use env VENICE_MODEL for any id not listed here (GUI shows it under “Custom (env)”).
VENICE_MODEL_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Recommended", ("kimi-k2-5", "zai-org-glm-5", "deepseek-v3.2")),
    ("Fast coding / agents", ("z-ai-glm-5-turbo", "kimi-k2-thinking")),
    (
        "Dedicated coders",
        (
            "qwen3-coder-480b-a35b-instruct",
            "qwen3-coder-480b-a35b-instruct-turbo",
            "openai-gpt-53-codex",
            "openai-gpt-52-codex",
        ),
    ),
    ("Long context", ("claude-sonnet-4-6", "claude-opus-4-7", "grok-4-20")),
    ("Reasoning-heavy", ("arcee-trinity-large-thinking", "kimi-k2")),
    (
        "General instruct",
        (
            "qwen3-5-397b-a17b",
            "qwen3-5-35b-a3b",
            "qwen-3-6-plus",
            "mistral-small-2603",
            "minimax-m27",
        ),
    ),
)


def venice_model_categories() -> tuple[str, ...]:
    return tuple(c for c, _ in VENICE_MODEL_GROUPS)


def venice_models_for_category(category: str) -> tuple[str, ...]:
    for c, models in VENICE_MODEL_GROUPS:
        if c == category:
            return models
    return ()


def venice_category_for_model(model_id: str) -> str | None:
    for c, models in VENICE_MODEL_GROUPS:
        if model_id in models:
            return c
    return None


VENICE_MODEL_CHOICES: tuple[str, ...] = tuple(m for _, models in VENICE_MODEL_GROUPS for m in models)

# Persist state every N completed files (skips count). Set AUTOREVIEW_STATE_SAVE_EVERY=1 for old behavior.
def _state_save_interval() -> int:
    raw = os.environ.get("AUTOREVIEW_STATE_SAVE_EVERY", "5").strip()
    try:
        n = int(raw)
    except ValueError:
        return 5
    return max(1, min(n, 10_000))

# batch_size == 0 means “process every pending file in this run” (entire remaining folder).
BATCH_SIZE_ALL = 0
# Per-file read cap (avoids accidental huge reads into memory).
MIN_FILE_BYTES = 1024
MAX_FILE_BYTES_CAP = 50 * 1024 * 1024
STATE_VERSION = 1
STATE_DIRNAME = ".venice_review"
DEFAULT_OUTPUT_NAME = "VENICE_CODE_REVIEW.md"

# Directory name segments to prune during walk and to drop from git-discovered paths.
# Covers caches, venvs, build output, IDE, and package manager noise (saves API tokens).
_TOOL_AND_CACHE_DIR_NAMES = frozenset(
    {
        ".git",
        ".venice_review",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".hypothesis",
        ".nox",
        ".cache",
        ".cursor",
        ".idea",
        ".vscode",
        ".turbo",
        ".next",
        ".nuxt",
        ".output",
        ".parcel-cache",
        "dist",
        "build",
        ".eggs",
        ".tox",
        "vendor",
        ".cargo",
        "target",
    }
)


def _should_skip_noise_path(rel: str) -> bool:
    """Skip paths under tool/cache dirs or known non-source files (applies to git + walk)."""
    parts = rel.replace("\\", "/").split("/")
    for part in parts:
        if part in _TOOL_AND_CACHE_DIR_NAMES:
            return True
        if part.endswith(".egg-info"):
            return True
    base = parts[-1] if parts else ""
    if base == "CACHEDIR.TAG":
        return True
    return False


# Typical documentation trees (path segment match, any depth).
_DOC_TREE_DIR_NAMES = frozenset({"doc", "docs", "documentation"})


def _matches_default_doc_exclude(rel: str) -> bool:
    """True if path looks like prose/docs rather than source (when default doc filter is on)."""
    parts = rel.replace("\\", "/").split("/")
    for part in parts:
        if part.lower() in _DOC_TREE_DIR_NAMES:
            return True
    name = Path(rel).name.lower()
    return name.endswith((".md", ".mdx", ".mdown", ".markdown", ".rst"))

ProgressCallback = Callable[[str, dict], None] | None


def _sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def normalize_root(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def state_dir(root: Path) -> Path:
    return root / STATE_DIRNAME


def state_path(root: Path) -> Path:
    return state_dir(root) / "state.json"


def default_report_path(root: Path) -> Path:
    return root / DEFAULT_OUTPUT_NAME


@dataclass
class ReviewState:
    version: int = STATE_VERSION
    root_abs: str = ""
    fingerprint: str = ""
    completed_paths: list[str] = field(default_factory=list)
    output_name: str = DEFAULT_OUTPUT_NAME
    # Per Venice model id: tokens accumulated for this project (all autoreview runs).
    usage_by_model: dict[str, dict[str, int]] = field(default_factory=dict)

    def to_json(self) -> dict:
        return {
            "version": self.version,
            "root_abs": self.root_abs,
            "fingerprint": self.fingerprint,
            "completed_paths": sorted(set(self.completed_paths)),
            "output_name": self.output_name,
            "usage_by_model": {
                k: {"prompt": int(v["prompt"]), "completion": int(v["completion"])}
                for k, v in sorted(self.usage_by_model.items())
            },
        }

    @classmethod
    def from_json(cls, data: dict) -> ReviewState:
        ubm: dict[str, dict[str, int]] = {}
        raw = data.get("usage_by_model")
        if isinstance(raw, dict):
            for mid, u in raw.items():
                if isinstance(u, dict):
                    ubm[str(mid)] = {
                        "prompt": int(u.get("prompt", 0)),
                        "completion": int(u.get("completion", 0)),
                    }
        return cls(
            version=int(data.get("version", STATE_VERSION)),
            root_abs=str(data.get("root_abs", "")),
            fingerprint=str(data.get("fingerprint", "")),
            completed_paths=list(data.get("completed_paths", [])),
            output_name=str(data.get("output_name", DEFAULT_OUTPUT_NAME)),
            usage_by_model=ubm,
        )


def _resolve_report_path(root: Path, output_path: Path | None, state: ReviewState | None) -> Path:
    """Report file to write to. Explicit ``output_path`` wins; else reuse ``state.output_name`` from a prior run."""
    if output_path is not None:
        return output_path.resolve()
    if state is not None:
        name = (state.output_name or "").strip()
        if name:
            return (root / Path(name).name).resolve()
    return default_report_path(root).resolve()


def compute_fingerprint(rel_paths: list[str], include: tuple[str, ...], exclude: tuple[str, ...]) -> str:
    payload = "\n".join(sorted(rel_paths)) + "\n---\n" + "|".join(include) + "\n" + "|".join(exclude)
    return _sha256_text(payload)


def load_state(root: Path) -> ReviewState | None:
    p = state_path(root)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return ReviewState.from_json(data)
    except (OSError, json.JSONDecodeError, TypeError, KeyError):
        return None


def save_state(root: Path, state: ReviewState) -> None:
    d = state_dir(root)
    d.mkdir(parents=True, exist_ok=True)
    tmp = state_path(root).with_suffix(".tmp")
    tmp.write_text(json.dumps(state.to_json(), indent=2), encoding="utf-8")
    tmp.replace(state_path(root))


def discover_via_git(root: Path) -> list[str] | None:
    git_dir = root / ".git"
    if not git_dir.exists():
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", "--cached", "--exclude-standard"],
            capture_output=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    raw = proc.stdout or b""
    names = [x.decode("utf-8", errors="replace") for x in raw.split(b"\0") if x]
    out: list[str] = []
    for name in names:
        p = root / name
        if p.is_file():
            out.append(name.replace("\\", "/"))
    return sorted(out)


def _should_skip_dir(name: str) -> bool:
    if name in _TOOL_AND_CACHE_DIR_NAMES:
        return True
    if name.endswith(".egg-info"):
        return True
    return False


def discover_via_walk(root: Path) -> list[str]:
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        # prune dirs in place
        dirnames[:] = [d for d in sorted(dirnames) if not _should_skip_dir(d)]
        for fn in sorted(filenames):
            full = Path(dirpath) / fn
            if not full.is_file():
                continue
            try:
                rel = full.relative_to(root)
            except ValueError:
                continue
            out.append(str(rel).replace("\\", "/"))
    return sorted(out)


def filter_paths(
    paths: list[str],
    root: Path,
    include: tuple[str, ...],
    exclude: tuple[str, ...],
) -> list[str]:
    def inc_ok(rel: str) -> bool:
        if not include:
            return True
        return any(fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(Path(rel).name, p) for p in include)

    def not_excluded(rel: str) -> bool:
        if not exclude:
            return True
        return not any(fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(Path(rel).name, p) for p in exclude)

    result: list[str] = []
    for rel in paths:
        if not inc_ok(rel):
            continue
        if not not_excluded(rel):
            continue
        p = root / rel
        if p.is_file():
            result.append(rel)
    return sorted(result)


# Basenames to skip in every review (meta; secrets; not app source).
_SKIP_REVIEW_BASENAMES = frozenset(
    {
        ".env",
        DEFAULT_OUTPUT_NAME,  # do not send the generated report back through the API
    }
)


def discover_files(
    root: Path,
    include: tuple[str, ...] = (),
    exclude: tuple[str, ...] = (),
    *,
    apply_default_doc_excludes: bool = True,
) -> list[str]:
    """List files to review. By default, skips markdown/rst and paths under doc/docs/documentation.

    Set ``apply_default_doc_excludes=False`` (CLI ``--review-markdown``, env ``AUTOREVIEW_REVIEW_MARKDOWN``)
    to review those like any other file.
    """
    paths = discover_via_git(root)
    if paths is None:
        paths = discover_via_walk(root)
    paths = filter_paths(paths, root, include, exclude)
    paths = [
        p
        for p in paths
        if Path(p).name not in _SKIP_REVIEW_BASENAMES
        and not _should_skip_noise_path(p)
        and not (apply_default_doc_excludes and _matches_default_doc_exclude(p))
    ]
    return sorted(paths)


def looks_binary(sample: bytes) -> bool:
    if b"\0" in sample[:8192]:
        return True
    return False


def _read_file_bytes_capped(path: Path, max_bytes: int) -> bytes:
    with path.open("rb") as f:
        return f.read(max_bytes + 1)


def read_file_limited(path: Path, max_bytes: int) -> tuple[str | None, str | None]:
    """Return (text, None) or (None, skip_reason). Reads at most ``max_bytes + 1`` bytes from disk."""
    try:
        timeout = float(os.environ.get("AUTOREVIEW_READ_TIMEOUT_SEC", "120").strip() or "120")
    except ValueError:
        timeout = 120.0
    timeout = max(1.0, min(timeout, 3600.0))
    try:
        with ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(_read_file_bytes_capped, path, max_bytes)
            raw = fut.result(timeout=timeout)
    except FuturesTimeout:
        return None, "skipped (read timed out)"
    except OSError as e:
        return None, f"unreadable: {e}"
    if len(raw) > max_bytes:
        return None, f"skipped (> {max_bytes} bytes)"
    chunk = raw[:8192]
    if looks_binary(chunk):
        return None, "skipped (binary)"
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = raw.decode("utf-8", errors="replace")
        except Exception:
            return None, "skipped (encoding)"
    return text, None


_REVIEW_DIMENSION_KEYS: tuple[str, ...] = (
    "security",
    "code_quality",
    "structure",
    "performance",
    "testing_observability",
)

# Short, generic dismissals (after normalize) — not shown in reports.
_GENERIC_DISMISSAL_PHRASES: frozenset[str] = frozenset(
    {
        "-",
        "—",
        "n/a",
        "n/a.",
        "na",
        "nil",
        "no",
        "no.",
        "none",
        "none.",
        "nope",
        "not applicable",
        "noted",
        "ok",
        "ok.",
        "okay",
        "okay.",
        "no concerns",
        "no issues",
        "no issues.",
        "no problem",
        "no problems",
        "none noted",
        "nothing to note",
        "nothing to report",
        "looks fine",
        "looks good",
        "looks okay",
        "all good",
        "fine",
        "good",
    }
)


def _is_substantive_review_text(text: str) -> bool:
    """True if text is worth showing (filters empty lines and generic one-line dismissals)."""
    if not isinstance(text, str):
        return False
    s = text.strip()
    if not s:
        return False
    if len(s) > 160:
        return True
    low = " ".join(s.lower().split())
    low = low.rstrip(".!…")
    if low in _GENERIC_DISMISSAL_PHRASES:
        return False
    return True


# Enforced after scrubbing; prompt asks the model to stay within this cap and anchor each item.
MAX_SUGGESTIONS_PER_FILE = 5
_SUGGESTION_SEVERITY_RANK: dict[str, int] = {"high": 0, "medium": 1, "low": 2}


def _cap_suggestions_by_severity(suggestions: list[dict]) -> list[dict]:
    """Keep the strongest items when the model returns too many."""
    if len(suggestions) <= MAX_SUGGESTIONS_PER_FILE:
        return suggestions
    ranked = sorted(
        suggestions,
        key=lambda x: _SUGGESTION_SEVERITY_RANK.get(str(x.get("severity", "medium")).lower(), 1),
    )
    return ranked[:MAX_SUGGESTIONS_PER_FILE]


def _scrub_review_dict(data: dict) -> dict:
    """Drop filler dimension text and suggestions that are empty or generic dismissals."""
    out: dict = {}
    for key in _REVIEW_DIMENSION_KEYS:
        v = data.get(key, "")
        s = v if isinstance(v, str) else str(v)
        s = s.strip()
        out[key] = s if _is_substantive_review_text(s) else ""
    cleaned: list[dict] = []
    sug = data.get("suggestions") or []
    if isinstance(sug, list):
        for item in sug:
            if not isinstance(item, dict):
                continue
            sev = str(item.get("severity", "medium")).lower()
            if sev not in ("high", "medium", "low"):
                sev = "medium"
            det = item.get("detail", "")
            det = det if isinstance(det, str) else str(det)
            det = det.strip()
            if not _is_substantive_review_text(det):
                continue
            cleaned.append({"severity": sev, "detail": det})
    out["suggestions"] = _cap_suggestions_by_severity(cleaned)
    return out


def _review_context_hint(relative_path: str) -> str:
    """Optional, path-specific focus line for the user message (not repeated in JSON keys)."""
    norm = relative_path.replace("\\", "/").lower()
    name = Path(relative_path).name.lower()
    suf = Path(relative_path).suffix.lower()

    if ".github/workflows/" in norm and suf in (".yml", ".yaml", ""):
        return (
            "Prioritize workflow correctness, use of secrets, pinning third-party actions, "
            "and least-privilege permissions."
        )
    if name in ("dockerfile",) or norm.endswith("/dockerfile"):
        return (
            "Prioritize image security (base image, packages), non-root users, "
            "and avoiding leaked secrets in layers."
        )
    if name == "makefile" or name.startswith("makefile."):
        return "Prioritize build safety, reproducibility, and avoiding destructive or surprising commands."

    if suf in (".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".properties"):
        return (
            "Prioritize schema/correctness, validation, and avoiding accidental exposure of secrets or credentials."
        )
    if suf == ".sql":
        return "Prioritize SQL correctness, injection risks, and migration safety."

    if suf in (".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java", ".kt", ".swift"):
        in_test_area = (
            "/tests/" in norm
            or "/test/" in norm
            or "/__tests__/" in norm
            or name.startswith("test_")
            or name.endswith("_test.py")
            or name.endswith(".test.ts")
            or name.endswith(".test.js")
            or name.endswith(".test.tsx")
            or name.endswith("_test.go")
            or name == "conftest.py"
        )
        if in_test_area:
            return (
                "Prioritize test correctness, meaningful assertions, fixtures, isolation, "
                "and flaky or slow patterns."
            )

    return ""


SYSTEM_PROMPT = """You are an expert code reviewer. Analyze ONLY the file provided by the user.

Respond with a single JSON object (no markdown fences, no commentary) using exactly these keys:
{
  "security": string,
  "code_quality": string,
  "structure": string,
  "performance": string,
  "testing_observability": string,
  "suggestions": [ {"severity": "high"|"medium"|"low", "detail": string} ]
}

Rules (strict):
- Use the empty string "" for any dimension that does not need a specific observation for THIS file. Do not pad with filler such as "no issues", "looks fine", "N/A", "none", or "OK".
- If you have nothing actionable or specific to say in a dimension, leave it as "".
- When you do write a dimension, keep it to at most two sentences unless you are describing a critical problem that needs more detail.
- Put concrete, actionable items in "suggestions" with an appropriate severity. Use an empty array [] if there is nothing actionable.
- Do not repeat the same point across multiple dimensions; mention it once in the most fitting field or as a single suggestion.
- Tailor content to this file's path and source; avoid generic boilerplate that could apply to any file.

Product judgment (avoid autoreviewer noise):
- Do not suggest refactors or API churn (e.g. sync vs promises, minor style) for rare paths, one-off startup, or code that is clearly cold unless there is a concrete bug or security risk. If the cost to the team outweighs the benefit, say nothing.
- Respect common, acceptable tradeoffs: test ergonomics (e.g. double-underscore helpers in tests), naming that reads well to authors, and patterns that are normal in this ecosystem. Pedantry about style or naming is not a "finding" unless it causes real confusion, bugs, or conflicts with project rules.
- Cryptography, timing guarantees, constant-time comparisons, and subtle security claims: do not assert a defect without reasoning from this file. Prefer: what to verify against the full call chain, what to document (threat model, guarantees), or what might still leak—phrased as worth confirming, not as certainty unless the code is plainly wrong.
- Before claiming async/await races, missing await, or timing bugs: check the actual signature and call style in this file (e.g. sync vs async functions). Do not template-findings from filenames; omit the suggestion if the code cannot exhibit the alleged bug.
- Severity "high" is for issues that are realistically exploitable or broken on normal call paths. If a concern is defense-in-depth (e.g. duplicate check elsewhere in the route layer) or only matters for hypothetical direct callers / future misuse, prefer severity "medium" and say so—do not describe current production URLs as immediately exploitable when guards higher in the stack already enforce invariants.

Suggestions list (anchor + cap):
- Include at most 5 entries in "suggestions" (fewer is fine). If you have more than 5 ideas, keep only the highest-impact ones.
- Every "detail" must point at something concrete in THIS file: name a function, class, variable, import, or describe specific control-flow or observable line behavior readers can find in the snippet. Do not add generic framework or "best practice" advice unless it is clearly tied to named code here.
- Vague middleware/architecture tips with no symbol or line anchor should be omitted."""


def build_user_message(relative_path: str, content: str) -> str:
    hint = _review_context_hint(relative_path)
    extra = ""
    if hint:
        extra = f"\nContext: {hint}\n"
    return (
        f"File path: `{relative_path}`\n"
        f"{extra}\n"
        "Source:\n```\n"
        + content
        + "\n```\n"
    )


def _strip_json_fence(text: str) -> str:
    text = text.strip()
    m = re.match(r"^```(?:json)?\s*([\s\S]*?)\s*```$", text)
    if m:
        return m.group(1).strip()
    return text


def parse_review_json(content: str) -> dict:
    raw = _strip_json_fence(content)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", raw)
        if m:
            return json.loads(m.group(0))
        raise


def validate_review_dict(data: dict) -> dict:
    """Ensure expected keys, normalized suggestions, and scrub non-substantive filler."""
    out: dict = {}
    for key in _REVIEW_DIMENSION_KEYS:
        v = data.get(key, "")
        out[key] = v if isinstance(v, str) else str(v)
    sug = data.get("suggestions") or []
    if not isinstance(sug, list):
        sug = []
    cleaned: list[dict] = []
    for item in sug:
        if not isinstance(item, dict):
            continue
        sev = str(item.get("severity", "medium")).lower()
        if sev not in ("high", "medium", "low"):
            sev = "medium"
        det = item.get("detail", "")
        det = det if isinstance(det, str) else str(det)
        cleaned.append({"severity": sev, "detail": det.strip()})
    out["suggestions"] = cleaned
    return _scrub_review_dict(out)


def validate_review_payload(data: dict) -> dict:
    """Validate and normalize LLM JSON with Pydantic; fall back to :func:`validate_review_dict` if invalid."""
    try:
        d = ReviewPayload.model_validate(data).to_report_dict()
        return _scrub_review_dict(d)
    except ValidationError as e:
        logger.warning("Review JSON failed schema validation; using legacy normalizer: %s", e)
        return validate_review_dict(data)


def json_to_markdown(relative_path: str, data: dict) -> str:
    data = _scrub_review_dict(dict(data))
    lines = [f"## `{relative_path}`\n"]
    any_block = False
    for key, title in [
        ("security", "Security"),
        ("code_quality", "Code quality"),
        ("structure", "Structure / architecture"),
        ("performance", "Performance"),
        ("testing_observability", "Testing / observability"),
    ]:
        val = data.get(key, "")
        if isinstance(val, str) and _is_substantive_review_text(val):
            lines.append(f"### {title}\n\n{val.strip()}\n")
            any_block = True
    sug = data.get("suggestions") or []
    if isinstance(sug, list) and sug:
        lines.append("### Suggestions\n\n")
        for item in sug:
            if not isinstance(item, dict):
                continue
            sev = str(item.get("severity", "medium")).lower()
            det = str(item.get("detail", "")).strip()
            if not _is_substantive_review_text(det):
                continue
            lines.append(f"- **{sev}**: {det}\n")
            any_block = True
        lines.append("")
    if not any_block:
        lines.append("_No substantive feedback for this file._\n")
    lines.append("\n---\n")
    return "\n".join(lines)


def make_openai_client(api_key: str) -> OpenAI:
    return OpenAI(
        api_key=api_key,
        base_url=VENICE_BASE_URL,
        timeout=120.0,
        max_retries=0,
    )


def completion_usage_from_response(resp: object) -> dict[str, int]:
    """Prompt/completion token counts from a chat completion response (OpenAI-compatible)."""
    u = getattr(resp, "usage", None)
    if u is None:
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    return {
        "prompt_tokens": int(getattr(u, "prompt_tokens", None) or 0),
        "completion_tokens": int(getattr(u, "completion_tokens", None) or 0),
        "total_tokens": int(getattr(u, "total_tokens", None) or 0),
    }


def _extract_llm_pricing_usd_per_million(model_obj: object) -> tuple[float, float] | None:
    """Return (input_usd_per_million, output_usd_per_million) from a Venice /models list item."""
    dump = model_obj.model_dump() if hasattr(model_obj, "model_dump") else {}
    if not isinstance(dump, dict):
        return None
    if dump.get("type") != "text":
        return None
    spec = dump.get("model_spec")
    if not isinstance(spec, dict):
        mspec = getattr(model_obj, "model_spec", None)
        if hasattr(mspec, "model_dump"):
            spec = mspec.model_dump()
        elif isinstance(mspec, dict):
            spec = mspec
        else:
            spec = {}
    pr = spec.get("pricing") if isinstance(spec, dict) else None
    if not isinstance(pr, dict):
        pr = dump.get("pricing")
    if not isinstance(pr, dict):
        return None
    inp = pr.get("input") or {}
    out = pr.get("output") or {}
    if not isinstance(inp, dict) or not isinstance(out, dict):
        return None
    iu = inp.get("usd")
    ou = out.get("usd")
    if iu is None or ou is None:
        return None
    return float(iu), float(ou)


def fetch_text_models_pricing_usd(client: OpenAI) -> dict[str, tuple[float, float]]:
    """Map model id → (USD per 1M input tokens, USD per 1M output tokens)."""
    out: dict[str, tuple[float, float]] = {}
    try:
        page = client.models.list()
        for m in page.data:
            p = _extract_llm_pricing_usd_per_million(m)
            if p:
                out[m.id] = p
    except Exception as e:
        logger.warning("Could not list models for pricing: %s", e)
    return out


def merge_completion_usage(state: ReviewState, model: str, usage: dict[str, int]) -> None:
    pt = int(usage.get("prompt_tokens") or 0)
    ct = int(usage.get("completion_tokens") or 0)
    if pt == 0 and ct == 0:
        return
    b = state.usage_by_model.setdefault(model, {"prompt": 0, "completion": 0})
    b["prompt"] += pt
    b["completion"] += ct


def total_usage_tokens(state: ReviewState) -> tuple[int, int]:
    pt = sum(u["prompt"] for u in state.usage_by_model.values())
    ct = sum(u["completion"] for u in state.usage_by_model.values())
    return pt, ct


def estimate_project_spend_usd(
    pricing_map: dict[str, tuple[float, float]],
    state: ReviewState,
) -> float | None:
    """Estimated USD using ``/models`` pricing; None if no usage or no pricing."""
    if not state.usage_by_model:
        return None
    total = 0.0
    found = False
    for mid, u in state.usage_by_model.items():
        p = pricing_map.get(mid)
        if not p:
            continue
        found = True
        inp_m, out_m = p
        total += (u["prompt"] / 1_000_000.0) * inp_m + (u["completion"] / 1_000_000.0) * out_m
    if not found:
        return None
    return total


def project_usage_display_text(root: Path, api_key: str | None) -> str:
    """One-line summary for GUI: tokens + optional USD estimate (requires API key)."""
    st = load_state(root)
    if not st or not st.usage_by_model:
        return "This project: no API usage recorded yet."
    pt, ct = total_usage_tokens(st)
    if not api_key or not api_key.strip():
        return f"This project: {pt:,} prompt + {ct:,} completion tokens (set API key to estimate USD)."
    try:
        client = make_openai_client(api_key.strip())
        pm = fetch_text_models_pricing_usd(client)
        usd = estimate_project_spend_usd(pm, st)
    except Exception as e:
        logger.warning("Usage display failed: %s", e)
        return f"This project: {pt:,} prompt + {ct:,} completion tokens (could not estimate USD)."
    if usd is None:
        return f"This project: {pt:,} prompt + {ct:,} completion tokens (pricing unavailable for some models)."
    return f"This project: est. ~${usd:.4f} USD · {pt:,} prompt + {ct:,} completion tokens"


def completion_with_retry(
    client: OpenAI,
    model: str,
    messages: list[dict],
    *,
    max_attempts: int = 5,
    base_delay: float = 1.0,
) -> tuple[str, dict[str, int]]:
    """Non-streaming completion; retries on 429 and transient 5xx. Returns (text, token_usage)."""
    last_err: Exception | None = None
    for attempt in range(max_attempts):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=0.2,
            )
            choice = resp.choices[0]
            content = choice.message.content
            usage = completion_usage_from_response(resp)
            if content is None:
                return "", usage
            return content, usage
        except RateLimitError as e:
            last_err = e
            logger.warning(
                "Venice API rate limited (attempt %s/%s): %s",
                attempt + 1,
                max_attempts,
                e,
            )
        except APIError as e:
            code = getattr(e, "status_code", None) or getattr(e, "code", None)
            if code in (429, 500, 502, 503, 504):
                last_err = e
                logger.warning(
                    "Venice API error (attempt %s/%s) status=%s: %s",
                    attempt + 1,
                    max_attempts,
                    code,
                    e,
                )
            else:
                raise
        except APITimeoutError as e:
            last_err = e
            logger.warning(
                "Venice API timeout (attempt %s/%s): %s",
                attempt + 1,
                max_attempts,
                e,
            )
        delay = base_delay * (2**attempt) + random.uniform(0, 0.5)
        time.sleep(min(delay, 60.0))
    if last_err:
        raise last_err
    raise RuntimeError("completion_with_retry: exhausted")


@dataclass
class RunResult:
    processed: int = 0
    remaining: int = 0
    complete: bool = False
    cancelled: bool = False
    output_path: Path | None = None
    log_lines: list[str] = field(default_factory=list)
    fingerprint_warning: str | None = None
    # Token usage (this run vs project lifetime in .venice_review/state.json).
    usage_prompt_tokens_run: int = 0
    usage_completion_tokens_run: int = 0
    usage_prompt_tokens_total: int = 0
    usage_completion_tokens_total: int = 0
    usage_estimated_usd_project: float | None = None


def _append_report(path: Path, text: str, header_if_new: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(header_if_new + "\n\n" + text, encoding="utf-8")
    else:
        with path.open("a", encoding="utf-8") as f:
            f.write("\n" + text)


def _format_run_banner(model: str, batch_note: str, rel_paths: list[str], *, max_list: int = 20) -> str:
    """Markdown block prepended once per batch run so appended reports stay separated by time and file set."""
    ts = datetime.now().astimezone().replace(microsecond=0).isoformat()
    n = len(rel_paths)
    show = rel_paths[:max_list]
    extra = n - len(show)
    if n == 0:
        paths_line = "(none)"
    else:
        paths_line = ", ".join(f"`{r}`" for r in show)
        if extra > 0:
            paths_line += f" … (+{extra} more)"
    return (
        f"## Autoreview run — {ts}\n\n"
        f"- **Model:** `{model}`\n"
        f"- **Batch:** {batch_note}\n"
        f"- **Files this run ({n}):** {paths_line}\n"
    )


def effective_batch_size(batch_size: int, pending_count: int) -> int:
    """Map request to a concrete slice length. ``batch_size == 0`` → all pending."""
    if batch_size < 0:
        raise ValueError("batch_size must be >= 0 (use 0 for all pending files in one run)")
    if pending_count < 0:
        raise ValueError("pending_count invalid")
    if batch_size == BATCH_SIZE_ALL:
        return pending_count
    return min(batch_size, pending_count)


def run_review_batch(
    root: Path,
    api_key: str,
    *,
    batch_size: int = 10,
    model: str = DEFAULT_MODEL,
    include: tuple[str, ...] = (),
    exclude: tuple[str, ...] = (),
    max_file_bytes: int = 512_000,
    delay_ms: int = 0,
    dry_run: bool = False,
    reset_progress: bool = False,
    output_path: Path | None = None,
    progress: ProgressCallback = None,
    cancel_event: threading.Event | None = None,
    review_markdown: bool = False,
) -> RunResult:
    """
    Process up to ``batch_size`` pending files; persist state under .venice_review/.
    Use ``batch_size=0`` to process every pending file in this run (full remaining folder).

    By default, markdown/rst and paths under ``doc`` / ``docs`` / ``documentation`` are skipped;
    pass ``review_markdown=True`` (or CLI ``--review-markdown``) to include them.
    """
    result = RunResult()
    root = normalize_root(root)
    if not root.is_dir():
        raise FileNotFoundError(f"Not a directory: {root}")

    max_file_bytes = max(MIN_FILE_BYTES, min(max_file_bytes, MAX_FILE_BYTES_CAP))

    discovered = discover_files(
        root,
        include=include,
        exclude=exclude,
        apply_default_doc_excludes=not review_markdown,
    )
    fp = compute_fingerprint(discovered, include, exclude)

    state = load_state(root)
    out = _resolve_report_path(root, output_path, state)
    result.output_path = out
    if state is None or reset_progress:
        state = ReviewState(root_abs=str(root), fingerprint=fp, completed_paths=[], output_name=out.name)
    else:
        if state.fingerprint != fp:
            result.fingerprint_warning = (
                "Discovery fingerprint changed (new files or filters). "
                "Merging: keeping completed paths that still exist; new paths become pending."
            )
            if progress:
                progress("warning", {"message": result.fingerprint_warning})
        # merge completed with still-valid paths
        valid = set(discovered)
        state.completed_paths = [p for p in state.completed_paths if p in valid]
        state.fingerprint = fp
        state.root_abs = str(root)
        state.output_name = out.name

    pending = [p for p in discovered if p not in set(state.completed_paths)]
    result.remaining = len(pending)

    n_this_run = effective_batch_size(batch_size, len(pending))
    batch_note = "all pending files" if batch_size == BATCH_SIZE_ALL else str(batch_size)

    if dry_run:
        est = n_this_run
        result.log_lines.append(f"[dry-run] Would review {est} file(s) this batch; {len(pending)} pending total.")
        result.log_lines.append(f"[dry-run] Output: {out}")
        for rel in pending[:n_this_run]:
            p = root / rel
            sz = p.stat().st_size if p.is_file() else 0
            result.log_lines.append(f"  {rel} ({sz} bytes)")
        rest = len(pending) - est
        if rest > 0:
            result.log_lines.append(f"  ... {rest} more file(s) pending in later batches")
        pt, ct = total_usage_tokens(state)
        result.usage_prompt_tokens_total = pt
        result.usage_completion_tokens_total = ct
        return result

    if not pending:
        result.complete = True
        result.log_lines.append(
            "Nothing pending; review complete for this project. "
            f"The report file was not modified: `{out}` "
            "(every discovered file is already marked reviewed in .venice_review/state.json). "
            "To re-review from scratch, use --reset-progress."
        )
        pt, ct = total_usage_tokens(state)
        result.usage_prompt_tokens_total = pt
        result.usage_completion_tokens_total = ct
        try:
            _c = make_openai_client(api_key)
            _pm = fetch_text_models_pricing_usd(_c)
            result.usage_estimated_usd_project = estimate_project_spend_usd(_pm, state)
        except Exception:
            result.usage_estimated_usd_project = None
        save_state(root, state)
        return result

    # Fresh full re-run: avoid duplicating sections by appending to an old report.
    if reset_progress and out.exists():
        out.unlink()
        result.log_lines.append(f"Removed previous report (fresh start with --reset-progress): {out}")

    to_process = pending[:n_this_run]
    client = make_openai_client(api_key)
    pricing_map = fetch_text_models_pricing_usd(client)
    run_prompt = 0
    run_completion = 0

    header_if_new = "\n".join(
        [
            "# Venice code review",
            "",
            f"- Root: `{root}`",
            f"- Model: `{model}`",
            f"- Batch size this run: {batch_note}",
            "",
            "> Generated by autoreview. Feed this file to your editor AI to triage changes.",
            "",
        ]
    )

    save_every = _state_save_interval()
    since_save = 0
    run_banner_appended = False

    def flush_state() -> None:
        nonlocal since_save
        save_state(root, state)
        since_save = 0

    for rel in to_process:
        if cancel_event is not None and cancel_event.is_set():
            if since_save > 0:
                flush_state()
            result.cancelled = True
            result.log_lines.append("Stopped by user; progress saved.")
            if progress:
                progress("cancelled", {})
            break

        path = root / rel
        text, skip = read_file_limited(path, max_file_bytes)
        if text is None:
            msg = f"Skip `{rel}`: {skip}"
            result.log_lines.append(msg)
            if progress:
                progress("skip", {"path": rel, "reason": skip or "unknown"})
            state.completed_paths.append(rel)
            since_save += 1
            if since_save >= save_every:
                flush_state()
            continue

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_message(rel, text)},
        ]
        if progress:
            progress("file_start", {"path": rel})
        try:
            raw_reply, comp_usage = completion_with_retry(client, model, messages)
            merge_completion_usage(state, model, comp_usage)
            run_prompt += int(comp_usage.get("prompt_tokens") or 0)
            run_completion += int(comp_usage.get("completion_tokens") or 0)
            data = validate_review_payload(parse_review_json(raw_reply))
        except json.JSONDecodeError as e:
            result.log_lines.append(f"JSON parse error for `{rel}`: {e}; storing raw response in report.")
            data = validate_review_payload(
                {
                    "security": "",
                    "code_quality": "",
                    "structure": "",
                    "performance": "",
                    "testing_observability": f"(parse error) {e}",
                    "suggestions": [{"severity": "medium", "detail": raw_reply[:2000]}],
                }
            )
        except Exception as e:
            result.log_lines.append(f"API error for `{rel}`: {e}")
            if progress:
                progress("error", {"path": rel, "error": str(e)})
            raise

        section = json_to_markdown(rel, data)
        if not run_banner_appended:
            chunk = _format_run_banner(model, batch_note, to_process) + "\n" + section
            _append_report(out, chunk, header_if_new)
            run_banner_appended = True
        else:
            _append_report(out, section, "")
        state.completed_paths.append(rel)
        since_save += 1
        if since_save >= save_every:
            flush_state()
        result.processed += 1
        if progress:
            progress("file_done", {"path": rel})
            pt, ct = total_usage_tokens(state)
            usd = estimate_project_spend_usd(pricing_map, state)
            if usd is None:
                msg = (
                    f"This project: {pt:,} prompt + {ct:,} completion tokens "
                    f"(this run +{run_prompt:,} / +{run_completion:,})."
                )
            else:
                msg = (
                    f"This project: est. ~${usd:.4f} USD · {pt:,} prompt + {ct:,} completion · "
                    f"this run +{run_prompt:,} / +{run_completion:,}"
                )
            progress("usage", {"message": msg})

        if delay_ms > 0:
            time.sleep(delay_ms / 1000.0)

    if since_save > 0:
        flush_state()

    if to_process and result.processed == 0 and not result.cancelled:
        result.log_lines.append(
            "No new sections were written to the report this run: every path in this batch was skipped "
            "(binary, over the size limit, read timeout, or encoding). Those paths are still marked complete."
        )

    still = [p for p in discovered if p not in set(state.completed_paths)]
    result.remaining = len(still)
    result.complete = result.remaining == 0 and not result.cancelled
    result.log_lines.append(f"Processed {result.processed} file(s); {result.remaining} remaining.")
    pt, ct = total_usage_tokens(state)
    result.usage_prompt_tokens_total = pt
    result.usage_completion_tokens_total = ct
    result.usage_prompt_tokens_run = run_prompt
    result.usage_completion_tokens_run = run_completion
    result.usage_estimated_usd_project = estimate_project_spend_usd(pricing_map, state)
    if result.usage_estimated_usd_project is not None:
        result.log_lines.append(
            f"Estimated project spend: ~${result.usage_estimated_usd_project:.4f} USD "
            f"({pt:,} prompt + {ct:,} completion tokens)."
        )
    elif pt or ct:
        result.log_lines.append(f"Token usage (this project): {pt:,} prompt + {ct:,} completion.")
    return result
