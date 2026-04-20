<p align="center">
  <img src="assets/app_icon_256.png?v=fit-lens" alt="Autoreview" width="128" height="128">
</p>

# Autoreview

Batch [Venice AI](https://venice.ai) code review for a whole project: each run processes up to **N** files, saves progress under `<project>/.venice_review/`, and appends to **`VENICE_CODE_REVIEW.md`** until every file is done. Use the **CLI** or a **dark-themed Tk GUI**. Optional **macOS `.app`** build via `./build_app.sh`.

Source: [github.com/chrisstampar/Autoreview](https://github.com/chrisstampar/Autoreview)

## Setup

Requires **Python 3.10+** (see `pyproject.toml`).

```bash
cd /path/to/autoreview
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,gui]"
```

Extras: **`[dev]`** — pytest, coverage, ruff, PyInstaller. **`[gui]`** — Pillow for `scripts/make_icon.py`. Runtime (`pip install -e .`) does not require Pillow; the Tk GUI uses the stdlib only.

## API key

- Set **`VENICE_API_KEY`** in the environment, **or**
- Run once without it: you’ll be prompted and the key is stored in the **system keychain** once (service `autoreview-venice`, reused for every project folder).

## CLI

```bash
# From the project you want reviewed (or pass --root)
python -m autoreview --root /path/to/repo --batch-size 10
# Entire remaining folder in one run (same as GUI “All”):
python -m autoreview --root /path/to/repo --batch-size 0

# Preview files only (no API calls)
python -m autoreview --root /path/to/repo --dry-run

# Start over: clears .venice_review state AND deletes the existing markdown report
# so the next run does not duplicate sections (then re-reviews everything).
python -m autoreview --root /path/to/repo --reset-progress
```

Console scripts (after `pip install -e .`): `autoreview`, `autoreview-gui`.

### Report file (`VENICE_CODE_REVIEW.md` or `--output`)

- **Continuing a review:** New sections are **appended** to the report. The file is **not** overwritten. Only files still **pending** in `.venice_review/state.json` are sent to the API.
- **Done:** When nothing is left pending, another run only says “Nothing pending” and leaves the report as-is.
- **Full redo:** Use **`--reset-progress`**, which clears state **and deletes** the existing report file before writing a fresh one (avoids duplicated `## \`path\`` blocks).

**Report shape:** Each **invocation** (each batch you run) is preceded by a **`## Autoreview run`** block with a **timestamp**, **model**, **batch size label**, and the **list of paths scheduled for that run**—so appended content does not turn into an unlabeled pile of passes. Under that, each reviewed file becomes a `## \`relative/path\`` section with optional subsections (Security, Code quality, Structure, Performance, Testing, Suggestions). **Empty or generic filler** (e.g. “no issues”, “N/A”, “OK”) is dropped so only substantive text appears; if nothing remains, the report shows a short *no substantive feedback* line. The **Suggestions** list is limited to **at most five** items per file (strongest severities kept first), and the model is instructed to tie each suggestion to a **named symbol or concrete line-visible behavior** in that file. A horizontal rule separates files. The top **# Venice code review** header (root + model snapshot) is written only when the report file is first created (later runs append new **Autoreview run** sections).

`VENICE_CODE_REVIEW.md` is never included in discovery, so the report is not reviewed as source.

**Discovery caveats:** With **git**, only tracked files are listed (`git ls-files`). Untracked files are skipped unless you add them. **Symlinks** to files are followed as normal paths; unusual layouts (nested repos, submodule checkouts) may require adjusting `--include` / `--exclude` or using non-git discovery (no `.git` → directory walk).

**Skipped paths (token savings):** Files under common tool/cache/build directories are never reviewed (e.g. `.ruff_cache`, `.pytest_cache`, `.mypy_cache`, `__pycache__`, `node_modules`, `.venv`, `dist`, `build`, `.git`, `.cursor`, `.next`, `.turbo`, `vendor`, Rust `target`, etc.), and the cache marker file **`CACHEDIR.TAG`** is skipped. Root `.gitignore` and real source are still included.

**Documentation-heavy paths (default):** To focus on code, Autoreview **does not** review **Markdown / reStructuredText** (`.md`, `.mdx`, `.mdown`, `.markdown`, `.rst`) or anything under a **`doc`**, **`docs`**, or **`documentation`** directory segment. To include those, use **`--review-markdown`** or set **`AUTOREVIEW_REVIEW_MARKDOWN=1`**, or enable **Include markdown & docs folders** in the GUI.

The GUI model list is curated from [Venice text models](https://docs.venice.ai/models/overview) (code- and reasoning-capable picks). Set `VENICE_MODEL` to any valid [chat model id](https://docs.venice.ai/api-reference/endpoint/models/list) if yours is not in the dropdown.

## GUI

```bash
autoreview-gui
# or
python -m autoreview.gui
```

Browse for a folder, pick a **category** (Recommended, Fast coding, Long context, etc.) and **model**, set batch size, optionally check **Include markdown & docs folders**, then **Run review**. Repeat until the log says all files are done.

**Usage & spend (GUI):** Below the subtitle, a line shows **cumulative prompt/completion tokens** for Autoreview runs on that folder (stored in `.venice_review/state.json`). With an API key, it also shows an **estimated USD** from `GET /models` pricing.

## macOS app bundle

```bash
./build_app.sh                    # builds dist/Autoreview.app in this repo
./build_app.sh --install          # same, then copies to /Applications/Autoreview.app
```

Or copy manually: `cp -R dist/Autoreview.app /Applications/`

First launch may require **Right-click → Open** if Gatekeeper complains about an unsigned developer build.

## Target repo `.gitignore`

Add:

```gitignore
.venice_review/
```

Optionally ignore `VENICE_CODE_REVIEW.md` if it contains sensitive snippets. **`.env`** (basename) is never sent to Venice during discovery.

## Security

- **Never commit** Venice API keys, `.env` files, or review reports that contain secrets. This repository’s `.gitignore` excludes common cases (e.g. `.env`, `VENICE_CODE_REVIEW.md`); keep using env vars or your OS keychain for keys.
- **Code you review** is sent to Venice’s API under your account; scope and network policies are your responsibility.

## Trust boundary

Sending source files to Venice is your decision; treat it like pasting code into any cloud API.

## Icons

Regenerate `assets/app_icon_256.png` and `Autoreview.icns` with:

```bash
python scripts/make_icon.py
```

## Tests

```bash
pytest -q
# Coverage (optional)
pytest -q --cov=autoreview --cov-report=term-missing
```

Lint (optional): `ruff check autoreview tests`

## Maintainer setup (GitHub)

After merging this repo, configure the remote (requires [GitHub CLI](https://cli.github.com/) `gh auth login` once, or use the web UI).

| Goal | What to do |
|------|------------|
| **Repository description** | Repo → **⚙ Settings** (or **About** → ✎). Paste a short description (≤350 chars), e.g.: *Batch Venice AI code review — resumable batches, one markdown report, CLI + dark GUI, optional macOS app. Skips caches and env files by default. MIT.* |
| **Topics** | On the repo main page: **About** → ⚙ → add topics such as `venice-ai`, `code-review`, `python`, `tkinter`, `cli`, `pyinstaller`, `developer-tools`. |
| **Dependabot** | [`.github/dependabot.yml`](.github/dependabot.yml) enables weekly PRs for **pip** and **GitHub Actions**. Merge it, then watch the **Pull requests** tab. |
| **Release** | Tag **`v0.1.0`** is published on `main`. In **Releases** → **Draft a new release**, choose tag `v0.1.0`, title `Autoreview 0.1.0`, add notes, publish. Or: `gh release create v0.1.0 --verify-tag --generate-notes` |
| **`gh` one-liners** | `gh repo edit chrisstampar/Autoreview --description "Batch Venice AI code review — resumable batches, markdown report, CLI + GUI, optional macOS app. MIT."` and `gh repo edit chrisstampar/Autoreview --add-topic venice-ai --add-topic code-review --add-topic python --add-topic tkinter` |
| **Branch protection** | **Settings** → **Rules** → **Rulesets** (or **Branches** → **Branch protection**): protect **`main`**, enable **Require status checks to pass**, and select the **CI** workflow jobs (e.g. `test` on Python 3.10 and 3.12) after at least one successful run. Skip “required reviewers” if you’re the only committer. |

## License

MIT — see [`LICENSE`](LICENSE).

## Troubleshooting

| Issue | What to try |
|--------|-------------|
| **429 / rate limits** | The engine retries with backoff; wait and run again with a smaller `--batch-size` or add `--delay-ms`. |
| **Keychain prompt fails** | Install `keyring` (`pip install keyring`) or set `VENICE_API_KEY` in the environment. |
| **GUI fonts look wrong** | The app picks SF Pro / Segoe UI / DejaVu by platform; install a standard UI font or run from a terminal to see errors. |
| **Stop in GUI** | **Stop** requests cancellation before the next file; the current API call always finishes. Progress is saved in `.venice_review/`. |
| **CLI Ctrl+C / SIGTERM** | Same cooperative cancel as the GUI: first interrupt stops before the next file (in-flight HTTP may still finish). Exit code **130** when cancelled. |
| **State save frequency** | Default: flush every 5 completed paths. Set `AUTOREVIEW_STATE_SAVE_EVERY=1` for every file (more IO). |
| **Slow / stuck file reads** | Per-file read uses a thread with timeout (default **120s**). Set `AUTOREVIEW_READ_TIMEOUT_SEC` to adjust (1–3600). |
| **PyInstaller build** | Run `./build_app.sh` on macOS only; use `./build_app.sh --fast` to skip wiping `dist/` between iterations. |
| **`VENICE_CODE_REVIEW.md` unchanged on another run** | Expected if **nothing is pending** (every discovered file is already in `state.json`) — the log explains this; the report is only appended when new files are reviewed. Also: a batch where **every path was skipped** (binary / too large / timeout) adds no sections. **Reload** the file in your editor. If you once used **`--output`**, later CLI runs must use the **same** `--output`, or omit it and let Autoreview use `output_name` from `.venice_review/state.json`. Full redo: `--reset-progress`. |
