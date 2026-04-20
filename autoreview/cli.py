"""Command-line interface for autoreview."""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
from pathlib import Path

from autoreview import __version__
from autoreview.engine import DEFAULT_MODEL, MAX_FILE_BYTES_CAP, RunResult, normalize_root, run_review_batch
from autoreview.keychain import get_api_key, prompt_and_store_key

logger = logging.getLogger(__name__)


def _validate_glob_patterns(patterns: tuple[str, ...]) -> None:
    for p in patterns:
        if "\n" in p or "\r" in p:
            raise ValueError(f"Invalid glob (embedded newline): {p!r}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="autoreview",
        description="Batch Venice AI code review → markdown report (with chunked progress).",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument(
        "--root",
        type=str,
        default=".",
        help="Project root to review (default: current directory).",
    )
    p.add_argument(
        "--batch-size",
        type=int,
        default=10,
        metavar="N",
        help="Max files per run; use 0 for all pending files at once (default: 10).",
    )
    p.add_argument("--model", type=str, default=None, help="Venice model id (default: env VENICE_MODEL or kimi-k2-5).")
    p.add_argument("--output", type=str, default=None, help="Report path (default: <root>/VENICE_CODE_REVIEW.md).")
    p.add_argument("--delay-ms", type=int, default=0, help="Pause between API calls in milliseconds.")
    p.add_argument(
        "--max-file-bytes",
        type=int,
        default=512_000,
        help=f"Skip files larger than this (default: 512000; clamped to {MAX_FILE_BYTES_CAP // (1024 * 1024)} MiB max).",
    )
    p.add_argument("--include", action="append", default=[], help="Glob; include only matching paths (repeatable).")
    p.add_argument("--exclude", action="append", default=[], help="Glob; exclude matching paths (repeatable).")
    p.add_argument(
        "--reset-progress",
        action="store_true",
        help="Clear .venice_review state and delete the existing report file before re-reviewing from scratch.",
    )
    p.add_argument("--dry-run", action="store_true", help="List files; do not call API.")
    p.add_argument(
        "--review-markdown",
        action="store_true",
        help="Include .md/.rst and paths under doc/docs/documentation (default is to skip them).",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = normalize_root(args.root)
    out = Path(args.output).resolve() if args.output else None

    if not root.is_dir():
        print(f"Error: not a directory: {root}", file=sys.stderr)
        return 1

    key = get_api_key(root)
    if not key and not args.dry_run:
        try:
            print("No VENICE_API_KEY in environment; enter key to store in the system keychain (reused for all projects).")
            key = prompt_and_store_key(root)
        except (EOFError, KeyboardInterrupt):
            print("Aborted.", file=sys.stderr)
            return 130
        except Exception as e:
            print(f"Error: could not obtain API key: {e}", file=sys.stderr)
            return 1

    if args.dry_run:
        key = key or "dummy"

    model = args.model or os.environ.get("VENICE_MODEL", DEFAULT_MODEL)

    include = tuple(args.include or ())
    exclude = tuple(args.exclude or ())

    try:
        _validate_glob_patterns(include)
        _validate_glob_patterns(exclude)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    if args.batch_size < 0:
        print("Error: --batch-size must be >= 0 (0 = all pending in one run).", file=sys.stderr)
        return 1

    env_review_md = os.environ.get("AUTOREVIEW_REVIEW_MARKDOWN", "").strip().lower() in ("1", "true", "yes")
    review_markdown = bool(args.review_markdown) or env_review_md

    cancel_event = threading.Event()

    def _on_interrupt(_signum: int, _frame: object | None) -> None:
        cancel_event.set()

    prev_int = signal.signal(signal.SIGINT, _on_interrupt)
    prev_term: object | None = None
    if hasattr(signal, "SIGTERM"):
        prev_term = signal.signal(signal.SIGTERM, _on_interrupt)

    try:
        try:
            result: RunResult = run_review_batch(
                root,
                key,
                batch_size=args.batch_size,
                model=model,
                include=include,
                exclude=exclude,
                max_file_bytes=args.max_file_bytes,
                delay_ms=max(0, args.delay_ms),
                dry_run=args.dry_run,
                reset_progress=args.reset_progress,
                output_path=out,
                cancel_event=cancel_event,
                review_markdown=review_markdown,
            )
        except KeyboardInterrupt:
            print("\nInterrupted.", file=sys.stderr)
            return 130
        except Exception as e:
            logger.error("Review run failed: %s", e)
            print(f"Error: {e}", file=sys.stderr)
            return 1
    finally:
        signal.signal(signal.SIGINT, prev_int)
        if prev_term is not None:
            signal.signal(signal.SIGTERM, prev_term)

    if result.fingerprint_warning:
        print(result.fingerprint_warning)
    for line in result.log_lines:
        print(line)
    if result.output_path:
        print(f"Report: {result.output_path}")
    if result.usage_prompt_tokens_total or result.usage_completion_tokens_total:
        if result.usage_estimated_usd_project is not None:
            print(
                f"Usage (this project): est. ~${result.usage_estimated_usd_project:.4f} USD · "
                f"{result.usage_prompt_tokens_total:,} prompt + "
                f"{result.usage_completion_tokens_total:,} completion tokens."
            )
        else:
            print(
                f"Usage (this project): {result.usage_prompt_tokens_total:,} prompt + "
                f"{result.usage_completion_tokens_total:,} completion tokens."
            )
    if result.cancelled:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
