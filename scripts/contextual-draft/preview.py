#!/usr/bin/env python3
"""User-invoked preview. Capture happens before Tk opens; generation is explicit."""

from __future__ import annotations

import argparse
import importlib.util
import json
import queue
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "contextual_draft.py"
spec = importlib.util.spec_from_file_location("contextual_draft", SCRIPT)
assert spec and spec.loader
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)


def prepare_session(args: argparse.Namespace) -> Path:
    """Build a private request before any frontend can change the focused app."""
    directory = Path(tempfile.mkdtemp(prefix="contextual-draft-"))
    request = directory / "request.json"
    core.prepare(
        argparse.Namespace(
            capture=args.target is None,
            target=args.target,
            context=args.context,
            goldfish_db=args.goldfish_db,
            query=args.query,
            limit=args.limit,
            instruction=args.instruction,
            output=request,
        )
    )
    return request


def launch(request: Path, model: str) -> None:
    # Lazy import: missing Tk should fail before capture in main, and headless
    # preparation/tests never need a display server.
    import tkinter as tk
    from tkinter import ttk
    from tkinter.scrolledtext import ScrolledText

    root = tk.Tk()
    root.title("Contextual draft · preview only")
    root.geometry("820x640")
    root.columnconfigure(0, weight=1)
    root.rowconfigure(3, weight=1)
    data = core.read_json(request)
    form = ttk.Frame(root, padding=10)
    form.grid(row=0, column=0, sticky="ew")
    form.columnconfigure(1, weight=1)
    instruction = tk.StringVar(value=data["instruction"])
    selected_model = tk.StringVar(value=model)
    for row, (label, variable) in enumerate(
        (("Instruction", instruction), ("Model", selected_model))
    ):
        ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8))
        ttk.Entry(form, textvariable=variable).grid(
            row=row, column=1, sticky="ew", pady=3
        )
    ttk.Label(
        root,
        text="Generate sends the request below to the selected model. No insertion or sending.",
        wraplength=780,
    ).grid(row=1, column=0, sticky="w", padx=10)
    controls = ttk.Frame(root, padding=10)
    controls.grid(row=2, column=0, sticky="ew")
    status = tk.StringVar(
        value="Prepared locally. Review the request before generating."
    )
    tabs = ttk.Notebook(root)
    tabs.grid(row=3, column=0, sticky="nsew", padx=10)
    evidence = ScrolledText(tabs, wrap="word")
    draft = ScrolledText(tabs, wrap="word")
    tabs.add(evidence, text="Request / sources")
    tabs.add(draft, text="Draft")
    evidence.insert("1.0", json.dumps(data, ensure_ascii=False, indent=2))
    evidence.configure(state="disabled")
    ttk.Label(root, textvariable=status, wraplength=780, padding=10).grid(
        row=4, column=0, sticky="w"
    )
    results: queue.Queue[tuple[bool, str]] = queue.Queue()
    processes: list[subprocess.Popen] = []
    cancelled = threading.Event()

    def worker(run_request: Path, output: Path, chosen_model: str) -> None:
        try:
            proc = subprocess.Popen(
                [
                    sys.executable,
                    str(SCRIPT),
                    "generate",
                    "--request",
                    str(run_request),
                    "--model",
                    chosen_model,
                    "--allow-model-disclosure",
                    "--output",
                    str(output),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            processes.append(proc)
            if cancelled.is_set():
                stop_process(proc)
            proc.communicate()
            if cancelled.is_set():
                return
            if proc.returncode:
                results.put(
                    (
                        False,
                        "Generation failed. No field changed; provider output withheld.",
                    )
                )
            else:
                text = core.read_bounded(output, core.MAX_DRAFT).decode("utf-8")
                results.put((True, text))
        except (OSError, UnicodeError, core.DraftError):
            results.put((False, "Could not generate/read preview. No field changed."))

    def generate() -> None:
        chosen = selected_model.get().strip()
        if not chosen or not instruction.get().strip():
            status.set("Enter an instruction and explicit model first.")
            return
        updated = dict(data, instruction=instruction.get())
        # A fresh directory per attempt preserves request provenance and avoids
        # silently overwriting a prior preview. All material remains local/private.
        run_dir = Path(tempfile.mkdtemp(prefix="generation-", dir=request.parent))
        run_request = run_dir / "request.json"
        try:
            core.private_write(run_request, json.dumps(updated, ensure_ascii=False))
        except core.DraftError:
            status.set("Could not save the generation request.")
            return
        evidence.configure(state="normal")
        evidence.delete("1.0", "end")
        evidence.insert("1.0", json.dumps(updated, ensure_ascii=False, indent=2))
        evidence.configure(state="disabled")
        button.configure(state="disabled")
        status.set(
            "Generating. Close / Cancel stops local generation; already disclosed input cannot be recalled."
        )
        threading.Thread(
            target=worker,
            args=(run_request, run_dir / "draft.txt", chosen),
            # Non-daemon: closing during Popen cannot let the interpreter exit
            # before the worker registers and stops the newly created process.
            daemon=False,
        ).start()

    def poll() -> None:
        try:
            ok, text = results.get_nowait()
        except queue.Empty:
            pass
        else:
            button.configure(state="normal")
            if ok:
                draft.delete("1.0", "end")
                draft.insert("1.0", text)
                tabs.select(draft)
                status.set("Preview ready. Nothing inserted, copied or sent.")
            else:
                status.set(text)
        root.after(100, poll)

    def close() -> None:
        cancelled.set()
        for proc in processes:
            stop_process(proc)
        root.destroy()

    button = ttk.Button(
        controls, text="Generate (disclose to selected model)", command=generate
    )
    button.pack(side="left")
    ttk.Button(controls, text="Close / Cancel", command=close).pack(side="right")
    root.protocol("WM_DELETE_WINDOW", close)
    root.after(100, poll)
    root.mainloop()


def stop_process(proc: subprocess.Popen, grace: float = 2.0) -> None:
    """Stop this launcher's generation group, including its model subprocess.

    The generator runs in its own session (start_new_session), so its pid is
    the process group id. SIGTERM the group, give it a grace period to exit,
    then SIGKILL whatever remains (a model process may ignore SIGTERM or
    outlive its parent) and reap the leader so the worker's communicate()
    returns.
    """
    import os
    import signal
    import time

    def signal_group(sig: int) -> bool:
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            # Gone, or (macOS) only unreaped zombies remain in the group.
            return False
        return True

    if proc.poll() is None:
        signal_group(signal.SIGTERM)
    deadline = time.monotonic() + grace
    # Probe the whole group, not only the leader: a SIGTERM-ignoring model
    # process can survive after the launcher process itself has exited.
    while signal_group(0) and time.monotonic() < deadline:
        proc.poll()
        time.sleep(0.05)
    signal_group(signal.SIGKILL)
    proc.wait()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, help="Fixture instead of live capture")
    parser.add_argument(
        "--instruction",
        default="Draft the message that belongs in this field from the conversation and available context. Return only the message body; do not treat a selection as a rewrite request.",
    )
    parser.add_argument(
        "--model",
        default="openai-subscription/gpt-6.1-sol",
        help="Prefill only; never generates automatically",
    )
    parser.add_argument("--context", type=Path, action="append", default=[])
    parser.add_argument("--query")
    parser.add_argument("--limit", type=int, choices=range(1, 6), default=3)
    parser.add_argument(
        "--goldfish-db",
        type=Path,
        default=Path.home() / "Library/Application Support/goldfish/goldfish.db",
    )
    parser.add_argument(
        "--prepare-only", action="store_true", help="Capture/prepare without opening UI"
    )
    args = parser.parse_args()
    try:
        if not args.prepare_only:
            import tkinter  # noqa: F401
        request = prepare_session(args)
        print(f"Private request: {request}")
        if not args.prepare_only:
            launch(request, args.model)
    except ImportError:
        parser.exit(1, "Preview requires Python with tkinter support.\n")
    except core.DraftError as exc:
        parser.exit(1, f"contextual-draft: {exc}\n")


if __name__ == "__main__":
    main()
