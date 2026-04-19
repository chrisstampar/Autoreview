# Agent session context

Purpose: quick handoff for new sessions/agents working on this repo.

## `.venice_review/` (inside the **target** project being reviewed)

| File | Purpose |
|------|---------|
| `state.json` | `ReviewState`: completed paths, discovery fingerprint, output filename, `usage_by_model` token totals per Venice model id |
| *(tmp)* | Atomic `state.json.tmp` during save |

Not SQLite — plain JSON. Safe to delete the whole folder to reset progress (or use `--reset-progress`, which also removes the markdown report when starting fresh).

## Key paths

| Path | Role |
|------|------|
| `autoreview/engine.py` | Discovery, Venice API, state under `.venice_review/`, markdown report |
| `autoreview/schemas.py` | Pydantic models for LLM review JSON |
| `autoreview/cli.py` | CLI (`python -m autoreview`) |
| `autoreview/gui.py` | Dark Tk GUI (`autoreview-gui`) |
| `autoreview/keychain.py` | `VENICE_API_KEY` or keyring `autoreview-venice` / username `global` (one key app-wide; legacy per-folder entries are promoted to global on first use) |
| `scripts/make_icon.py` | PNG + iconset + `.icns` (needs `[gui]` / Pillow) |
| `build_app.sh` | PyInstaller → `dist/Autoreview.app` |

## Commands

| Command | Purpose |
|---------|---------|
| `pip install -e ".[dev,gui]"` | Dev + icon assets (pytest, ruff, cov, PyInstaller, Pillow) |
| `pytest -q` | Unit tests |
| `ruff check autoreview tests` | Lint |
| `python -m autoreview --root . --dry-run` | List files without API |
| `./build_app.sh` | Build macOS app |

## Configuration notes

- **`VENICE_MODEL_GROUPS`**: a Python constant in `autoreview/engine.py` (curated GUI list), not an environment variable. Override the model with env **`VENICE_MODEL`** or the GUI dropdown.
- **`AUTOREVIEW_REVIEW_MARKDOWN`**: if `1` / `true` / `yes`, same as CLI **`--review-markdown`** (include `.md`/`.rst` and `doc`/`docs`/`documentation` paths in discovery).
- **Rate limits:** Venice may return 429; the engine retries with exponential backoff. Reduce `--batch-size` or add `--delay-ms` if needed.

## Deferred (not planned for now)

- macOS codesign/notarization; PyInstaller `optimize` / narrower `collect_all`; parallel async file reviews; pip hash locks; `python-magic` for file typing. Revisit only if distribution, bundle size, or edge-case discovery become a problem.

## Discovery (noise paths)

- **`_TOOL_AND_CACHE_DIR_NAMES`** + **`_should_skip_noise_path`**: after git/walk + filters, drop any path under those dirs (e.g. `.ruff_cache`, caches, `node_modules`, `target`, …) and **`CACHEDIR.TAG`**. Saves tokens vs reviewing cache metadata.
- **Default doc filter** (`apply_default_doc_excludes`, on unless `review_markdown` / `--review-markdown` / `AUTOREVIEW_REVIEW_MARKDOWN`): skip `.md`/`.mdx`/`.rst`-style names and paths with segment `doc`, `docs`, or `documentation`.

## GitHub / release prep

- Root **`LICENSE`** (MIT), **`.gitattributes`**, **`.github/workflows/ci.yml`**, expanded **`.gitignore`**. README section **Pushing to GitHub** has init/push steps. **`Repository`** in `pyproject.toml`: https://github.com/chrisstampar/Autoreview

## Recent work

- **2026-04-19**: Initial implementation: chunked batch review, keychain, CLI + dark GUI, PyInstaller packaging, custom icon, tests.
- **2026-04-20**: GUI Category + Model comboboxes; batch **All** = engine `0`; CLI `--batch-size 0`; discovery skips `AGENT_SESSION_CONTEXT.md`; `.gitignore` includes `.env` patterns.
- **2026-04-19 (review follow-up)**: Batched state saves, cancel/stop, SIGINT, usage estimates, `read_file_limited` timeouts (`AUTOREVIEW_READ_TIMEOUT_SEC`), Pydantic `schemas.py` + `validate_review_payload`, immutable `COLORS` (`MappingProxyType`), `_WorkerResultSlot` instead of list for worker results, keychain `lru_cache` on path hash, `__main__.py` ImportError handling, `__all__` in `__init__.py`, `pyproject` `[gui]` extra (Pillow), dev adds ruff + pytest-cov, README report format + git/submodule discovery caveats + troubleshooting; `build_app.sh` + `requirements-dev` aligned; tests: parametrized batch size, invalid JSON, Pydantic payload.
- **2026-04-19**: `tests/test_schemas.py` — `SuggestionItem` / `ReviewPayload` validators, `to_report_dict`, `extra="ignore"`, debug log on non-str coercion. Trimmed “Venice list prices / actual charges” copy in `engine.py` (`project_usage_display_text`, progress `usage`, log line), `cli.py`, `README.md`.
- **2026-04-19**: Keychain API key is **app-wide** (`autoreview-venice` / `global`); legacy per-folder keys are promoted to global on first read; `set_api_key` also deletes legacy for current root. README + GUI copy updated.
- **2026-04-19**: Discovery skips **`.env`** (basename) alongside `AGENT_SESSION_CONTEXT.md` / report file; test + README updated.
- **2026-04-19**: Report path resolution: `_resolve_report_path` uses `state.output_name` when `--output` omitted (fixes appending to wrong file after `CLI --output`). Clearer logs when no pending work or all skips; README troubleshooting row.
- **2026-04-19**: Default discovery excludes markdown/rst and `doc`/`docs`/`documentation` trees; opt-in via `--review-markdown`, `AUTOREVIEW_REVIEW_MARKDOWN`, GUI checkbox. `_matches_default_doc_exclude` in `engine.py`.
- **2026-04-19**: Review prompt asks for **empty dimensions** instead of filler; **`_review_context_hint`** by path (workflows, Docker, Makefile, config extensions, SQL, tests); **`_scrub_review_dict`** / **`_is_substantive_review_text`** strip generic one-liners; **`json_to_markdown`** omits empty sections and shows a placeholder when nothing substantive remains.
- **2026-04-19**: GitHub prep: **`LICENSE`**, **`.gitattributes`**, **`.github/workflows/ci.yml`**, `.gitignore` tweaks (`.ruff_cache/`, IDE swap), README **Pushing to GitHub**, optional **`Repository`** comment in `pyproject.toml`.
- **2026-04-19**: `SYSTEM_PROMPT` extended with **product judgment**: skip low-ROI churn on cold paths; avoid pedantic test/style nits vs real issues; crypto/security subtlety → verify/document, not assert defects without reasoning.
- **2026-04-19**: Suggestions: prompt **cap 5** + **anchor to symbol/line behavior**; **`MAX_SUGGESTIONS_PER_FILE`** + **`_cap_suggestions_by_severity`** in `engine.py` enforce cap (high → medium → low).
- **2026-04-20**: Report: **`_format_run_banner`** prepends once per batch (`## Autoreview run` + ISO timestamp + model + batch + file list). Prompt: check **sync vs async** before race/await findings; **high vs medium** for defense-in-depth vs reachable exploitation.
