"""Dark-themed Tkinter UI for autoreview."""
from __future__ import annotations

import os
import queue
import sys
import threading
import tkinter as tk
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from types import MappingProxyType

from autoreview import __version__
from autoreview.engine import (
    BATCH_SIZE_ALL,
    DEFAULT_MODEL,
    VENICE_MODEL_GROUPS,
    RunResult,
    normalize_root,
    project_usage_display_text,
    run_review_batch,
    venice_category_for_model,
)
from autoreview.keychain import get_api_key, set_api_key


@dataclass
class _WorkerResultSlot:
    """Holds the latest :class:`~autoreview.engine.RunResult` from the worker thread (single slot)."""

    value: RunResult | None = None


def _font_title() -> tuple[str, int, str]:
    if sys.platform == "darwin":
        return ("SF Pro Display", 18, "bold")
    if sys.platform == "win32":
        return ("Segoe UI", 18, "bold")
    return ("DejaVu Sans", 18, "bold")


def _font_body() -> tuple[str, int]:
    if sys.platform == "darwin":
        return ("SF Pro Text", 11)
    if sys.platform == "win32":
        return ("Segoe UI", 11)
    return ("DejaVu Sans", 11)


def _font_mono(size: int = 10) -> tuple[str, int]:
    if sys.platform == "darwin":
        return ("SF Mono", size)
    if sys.platform == "win32":
        return ("Consolas", size)
    return ("DejaVu Sans Mono", size)


# Tokyo Night–inspired palette (immutable mapping)
COLORS = MappingProxyType(
    {
        "bg": "#1a1b26",
        "bg_elevated": "#24283b",
        "fg": "#c0caf5",
        "fg_dim": "#565f89",
        "accent": "#7aa2f7",
        "accent_hover": "#89b4fa",
        "success": "#9ece6a",
        "warn": "#e0af68",
        "error": "#f7768e",
        "border": "#3b4261",
        "btn_bg": "#1f2335",
        "btn_fg": "#7f8aab",
        "btn_fg_emphasis": "#9aa4bd",
        "btn_active_bg": "#2b3048",
        "btn_active_fg": "#b4bdd1",
        "btn_border": "#2a2f42",
    }
)


def _assets_dir() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / "assets"
    return Path(__file__).resolve().parent.parent / "assets"


def _apply_dark_theme(root: tk.Tk) -> None:
    root.configure(bg=COLORS["bg"])
    style = ttk.Style()
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    style.configure(
        "Dark.TCombobox",
        fieldbackground=COLORS["bg_elevated"],
        background=COLORS["bg_elevated"],
        foreground=COLORS["fg"],
        arrowcolor=COLORS["fg_dim"],
    )
    style.map(
        "Dark.TCombobox",
        fieldbackground=[("readonly", COLORS["bg_elevated"])],
        selectbackground=[("readonly", COLORS["bg_elevated"])],
        selectforeground=[("readonly", COLORS["fg"])],
    )


def _dark_button(
    parent: tk.Misc,
    text: str,
    command: Callable[..., None],
    *,
    emphasis: bool = False,
    **kw,
) -> tk.Button:
    """Flat muted button; ``emphasis=True`` for the primary action (slightly lighter label)."""
    fg = COLORS["btn_fg_emphasis"] if emphasis else COLORS["btn_fg"]
    return tk.Button(
        parent,
        text=text,
        command=command,
        bg=COLORS["btn_bg"],
        fg=fg,
        activebackground=COLORS["btn_active_bg"],
        activeforeground=COLORS["btn_active_fg"],
        relief=tk.FLAT,
        padx=14,
        pady=8,
        cursor="hand2",
        highlightthickness=1,
        highlightbackground=COLORS["btn_border"],
        **kw,
    )


def _run_worker(
    root_path: Path,
    batch_size: int,
    model: str,
    delay_ms: int,
    log_queue: queue.Queue,
    result_slot: _WorkerResultSlot,
    cancel_event: threading.Event,
    *,
    review_markdown: bool = False,
) -> None:
    def progress(event: str, data: dict) -> None:
        log_queue.put(("progress", event, data))

    try:
        api_key = get_api_key(root_path)
        if not api_key:
            log_queue.put(("need_key", str(root_path.resolve()), None))
            return

        res = run_review_batch(
            root_path,
            api_key,
            batch_size=batch_size,
            model=model,
            delay_ms=delay_ms,
            dry_run=False,
            reset_progress=False,
            progress=progress,
            cancel_event=cancel_event,
            review_markdown=review_markdown,
        )
        result_slot.value = res
        log_queue.put(("done", None, None))
    except Exception as e:
        log_queue.put(("error", str(e), None))


def main() -> None:
    root = tk.Tk()
    root.title(f"Autoreview {__version__}")
    root.minsize(720, 520)
    _apply_dark_theme(root)

    selected_root = tk.StringVar(value="")
    batch_var = tk.StringVar(value="10")
    delay_var = tk.StringVar(value="0")
    review_md_var = tk.BooleanVar(value=False)

    _default_model = os.environ.get("VENICE_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    _groups: list[tuple[str, tuple[str, ...]]] = list(VENICE_MODEL_GROUPS)
    if venice_category_for_model(_default_model) is None:
        _groups.append(("Custom (env)", (_default_model,)))

    def _models_for_cat(cat: str) -> tuple[str, ...]:
        for c, models in _groups:
            if c == cat:
                return models
        return ()

    _initial_cat = venice_category_for_model(_default_model)
    if _initial_cat is None:
        _initial_cat = "Custom (env)"
    category_var = tk.StringVar(value=_initial_cat)
    _initial_models = _models_for_cat(_initial_cat)
    model_var = tk.StringVar(
        value=_default_model if _default_model in _initial_models else (_initial_models[0] if _initial_models else DEFAULT_MODEL)
    )

    header = tk.Label(
        root,
        text="Venice code review",
        font=_font_title(),
        bg=COLORS["bg"],
        fg=COLORS["accent"],
    )
    header.pack(pady=(16, 4))

    sub = tk.Label(
        root,
        text="Choose a project folder. Batch “All” reviews every remaining file in one run.",
        font=_font_body(),
        bg=COLORS["bg"],
        fg=COLORS["fg_dim"],
    )
    sub.pack(pady=(0, 8))

    usage_label = tk.Label(
        root,
        text="This project: — (choose a folder)",
        font=_font_body(),
        bg=COLORS["bg"],
        fg=COLORS["fg_dim"],
        wraplength=680,
        justify=tk.LEFT,
        anchor="w",
    )
    usage_label.pack(fill=tk.X, padx=16, pady=(0, 8))

    path_frame = tk.Frame(root, bg=COLORS["bg"])
    path_frame.pack(fill=tk.X, padx=16, pady=4)

    path_entry = tk.Entry(
        path_frame,
        textvariable=selected_root,
        width=60,
        bg=COLORS["bg_elevated"],
        fg=COLORS["fg"],
        insertbackground=COLORS["accent"],
        relief=tk.FLAT,
        highlightthickness=1,
        highlightbackground=COLORS["border"],
        font=_font_mono(11),
    )
    path_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=6)

    def refresh_usage_label() -> None:
        p = selected_root.get().strip()
        if not p:
            usage_label.config(text="This project: — (choose a folder)")
            return
        rp = normalize_root(p)
        if not rp.is_dir():
            usage_label.config(text="This project: — (invalid folder)")
            return
        try:
            key = get_api_key(rp)
        except Exception:
            key = None
        usage_label.config(text=project_usage_display_text(rp, key))

    def browse() -> None:
        d = filedialog.askdirectory(title="Select project root")
        if d:
            selected_root.set(d)
            root.after(0, refresh_usage_label)

    _dark_button(path_frame, "Browse…", browse).pack(side=tk.LEFT, padx=(8, 0))

    opts = tk.Frame(root, bg=COLORS["bg"])
    opts.pack(fill=tk.X, padx=16, pady=8)

    r = 0
    pad = (0, 10)
    tk.Label(opts, text="Batch", bg=COLORS["bg"], fg=COLORS["fg"]).grid(row=r, column=0, sticky="w")
    batch_combo = ttk.Combobox(
        opts,
        textvariable=batch_var,
        values=("All", "3", "5", "10", "15", "25", "50"),
        width=5,
        state="readonly",
        style="Dark.TCombobox",
    )
    batch_combo.grid(row=r, column=1, padx=(4, pad[1]), sticky="w")

    tk.Label(opts, text="Category", bg=COLORS["bg"], fg=COLORS["fg"]).grid(row=r, column=2, sticky="w")
    category_combo = ttk.Combobox(
        opts,
        textvariable=category_var,
        values=tuple(c for c, _ in _groups),
        width=18,
        state="readonly",
        style="Dark.TCombobox",
    )
    category_combo.grid(row=r, column=3, padx=(4, pad[1]), sticky="w")

    tk.Label(opts, text="Model", bg=COLORS["bg"], fg=COLORS["fg"]).grid(row=r, column=4, sticky="w")
    model_combo = ttk.Combobox(
        opts,
        textvariable=model_var,
        values=_initial_models,
        width=22,
        state="readonly",
        style="Dark.TCombobox",
    )
    model_combo.grid(row=r, column=5, padx=(4, pad[1]), sticky="ew")

    tk.Label(opts, text="Delay ms", bg=COLORS["bg"], fg=COLORS["fg"]).grid(row=r, column=6, sticky="w")
    tk.Entry(
        opts,
        textvariable=delay_var,
        width=7,
        bg=COLORS["bg_elevated"],
        fg=COLORS["fg"],
        insertbackground=COLORS["accent"],
        relief=tk.FLAT,
        highlightthickness=1,
        highlightbackground=COLORS["border"],
    ).grid(row=r, column=7, padx=(4, 0), sticky="w")

    def _on_category_change(_event: object | None = None) -> None:
        cat = category_var.get()
        models = _models_for_cat(cat)
        model_combo["values"] = models
        cur = model_var.get()
        if models and cur not in models:
            model_var.set(models[0])

    category_combo.bind("<<ComboboxSelected>>", _on_category_change)

    opts.columnconfigure(5, weight=1)

    tk.Checkbutton(
        opts,
        text="Include markdown & docs folders (.md, docs/, …)",
        variable=review_md_var,
        bg=COLORS["bg"],
        fg=COLORS["fg"],
        selectcolor=COLORS["bg_elevated"],
        activebackground=COLORS["bg"],
        activeforeground=COLORS["fg"],
        font=_font_body(),
    ).grid(row=r + 1, column=0, columnspan=8, sticky="w", pady=(8, 0))

    log_frame = tk.Frame(root, bg=COLORS["bg"])
    log_frame.pack(fill=tk.BOTH, expand=True, padx=16, pady=8)

    log = tk.Text(
        log_frame,
        height=14,
        bg=COLORS["bg_elevated"],
        fg=COLORS["fg"],
        insertbackground=COLORS["accent"],
        relief=tk.FLAT,
        highlightthickness=1,
        highlightbackground=COLORS["border"],
        font=_font_mono(10),
        wrap=tk.WORD,
    )
    log.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    sb = tk.Scrollbar(log_frame, command=log.yview)
    sb.pack(side=tk.RIGHT, fill=tk.Y)
    log.config(yscrollcommand=sb.set)

    for tag, fg in (
        ("success", COLORS["success"]),
        ("warn", COLORS["warn"]),
        ("error", COLORS["error"]),
        ("accent", COLORS["accent"]),
        ("dim", COLORS["fg_dim"]),
    ):
        log.tag_configure(tag, foreground=fg)

    def append_log(msg: str, tag: str | None = None) -> None:
        if tag:
            log.insert(tk.END, msg + "\n", (tag,))
        else:
            log.insert(tk.END, msg + "\n")
        log.see(tk.END)

    worker_thread: threading.Thread | None = None
    result_queue: queue.Queue = queue.Queue()
    result_slot = _WorkerResultSlot()
    cancel_ref: list[threading.Event] = []

    btn_row = tk.Frame(root, bg=COLORS["bg"])
    btn_row.pack(pady=12)

    def stop_clicked() -> None:
        if cancel_ref:
            cancel_ref[0].set()
            append_log("Stop requested…", "warn")

    run_btn = _dark_button(btn_row, "Run review", command=lambda: None, emphasis=True)
    _bf = _font_body()
    run_btn.configure(font=(_bf[0], 13))
    stop_btn = _dark_button(btn_row, "Stop", stop_clicked)
    stop_btn.configure(font=(_bf[0], 13))
    run_btn.pack(side=tk.LEFT, padx=(0, 8))
    stop_btn.pack(side=tk.LEFT)
    stop_btn.config(state=tk.DISABLED)

    def set_running_ui(running: bool) -> None:
        run_btn.config(state=tk.DISABLED if running else tk.NORMAL)
        stop_btn.config(state=tk.NORMAL if running else tk.DISABLED)

    def poll_queue() -> None:
        try:
            while True:
                kind, a, b = result_queue.get_nowait()
                if kind == "need_key":
                    set_running_ui(False)
                    # `a` is the project root path captured when the worker ran (not the path field if edited later).
                    key_root = Path(a).resolve() if a else normalize_root(selected_root.get())
                    append_log(
                        "No API key. Enter it in the dialog (saved in Keychain once; reused for all folders).",
                        "warn",
                    )
                    key_win = tk.Toplevel(root)
                    key_win.title("API key")
                    key_win.configure(bg=COLORS["bg"])
                    tk.Label(
                        key_win,
                        text="Enter your Venice API key (stored in Keychain for Autoreview; all projects):",
                        bg=COLORS["bg"],
                        fg=COLORS["fg"],
                        wraplength=400,
                    ).pack(padx=16, pady=8)
                    ent = tk.Entry(key_win, width=48, show="•", bg=COLORS["bg_elevated"], fg=COLORS["fg"])
                    ent.pack(padx=16, pady=4)

                    def save_key(
                        ent_w: tk.Entry = ent,
                        key_root_w: Path = key_root,
                        key_win_w: tk.Toplevel = key_win,
                    ) -> None:
                        k = ent_w.get().strip()
                        if not k:
                            messagebox.showerror("Error", "Key cannot be empty.")
                            return
                        try:
                            set_api_key(key_root_w, k)
                        except Exception as e:
                            messagebox.showerror("Error", str(e))
                            return
                        key_win_w.destroy()
                        append_log("API key saved. Click Run again.", "success")
                        root.after(0, refresh_usage_label)

                    _dark_button(key_win, "Save", save_key, emphasis=True).pack(pady=12)
                elif kind == "progress":
                    ev, data = a, b or {}
                    if ev == "file_start":
                        append_log(f"→ Reviewing `{data.get('path', '')}` …", "accent")
                    elif ev == "file_done":
                        append_log(f"  Done `{data.get('path', '')}`", "success")
                    elif ev == "skip":
                        append_log(f"  Skip `{data.get('path')}`: {data.get('reason')}", "warn")
                    elif ev == "error":
                        append_log(f"  Error: {data.get('error')}", "error")
                    elif ev == "warning":
                        append_log(data.get("message", ""), "warn")
                    elif ev == "cancelled":
                        append_log("Stopping before the next file (current step finished).", "warn")
                    elif ev == "usage":
                        usage_label.config(text=data.get("message", ""))
                elif kind == "error":
                    append_log(f"Fatal: {a}", "error")
                    set_running_ui(False)
                elif kind == "done":
                    if result_slot.value is not None:
                        r: RunResult = result_slot.value
                        for line in r.log_lines:
                            append_log(line)
                        if r.output_path:
                            append_log(f"Report: {r.output_path}", "success")
                        if r.cancelled:
                            append_log("Run stopped; progress was saved.", "warn")
                        elif r.complete:
                            append_log("All files reviewed for this project.", "success")
                        result_slot.value = None
                    set_running_ui(False)
                    refresh_usage_label()
                    append_log("— Ready —", "dim")
        except queue.Empty:
            pass
        root.after(120, poll_queue)

    def run_clicked() -> None:
        nonlocal worker_thread
        if worker_thread and worker_thread.is_alive():
            messagebox.showinfo("Busy", "A run is already in progress.")
            return
        path = selected_root.get().strip()
        if not path:
            messagebox.showerror("Error", "Choose a project folder first.")
            return
        rp = normalize_root(path)
        if not rp.is_dir():
            messagebox.showerror("Error", f"Not a directory: {rp}")
            return
        raw_batch = batch_var.get().strip()
        if raw_batch.lower() == "all":
            bs = BATCH_SIZE_ALL
        else:
            try:
                bs = int(raw_batch)
            except ValueError:
                messagebox.showerror("Error", "Batch size must be a number or All.")
                return
        if bs < 0:
            messagebox.showerror("Error", "Batch size cannot be negative.")
            return
        try:
            dm = int(delay_var.get())
        except ValueError:
            messagebox.showerror("Error", "Delay must be an integer.")
            return
        dm = max(0, min(dm, 600_000))  # cap 10 minutes between files (sanity)
        model = model_var.get().strip() or DEFAULT_MODEL

        result_slot.value = None
        cancel_ref.clear()
        batch_desc = "all pending" if bs == BATCH_SIZE_ALL else str(bs)
        append_log(f"Starting (batch={batch_desc}, model={model})…")
        set_running_ui(True)

        def work() -> None:
            ce = threading.Event()
            cancel_ref.append(ce)
            _run_worker(
                rp,
                bs,
                model,
                dm,
                result_queue,
                result_slot,
                ce,
                review_markdown=review_md_var.get(),
            )

        worker_thread = threading.Thread(target=work, daemon=True)
        worker_thread.start()

    run_btn.config(command=run_clicked)

    selected_root.trace_add("write", lambda *_: root.after(0, refresh_usage_label))
    path_entry.bind("<FocusOut>", lambda _e: refresh_usage_label())

    # Window icon (PNG in assets)
    icon_png = _assets_dir() / "app_icon_256.png"
    if icon_png.is_file():
        try:
            img = tk.PhotoImage(file=str(icon_png))
            root.iconphoto(True, img)
            root._icon_ref = img  # prevent GC
        except tk.TclError:
            pass

    append_log("Select a folder, then click Run review.")
    root.after(50, refresh_usage_label)
    root.after(100, poll_queue)
    root.mainloop()


if __name__ == "__main__":
    main()
