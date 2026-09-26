"""
logometer CLI — Day 1 scope: `logometer tail <file>`.

Pure stdlib (argparse, no typer/rich dependency) so the tool runs with
nothing but Python installed. --explain (LLM-powered) and prettier
rich-based output are Day 2 additions.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sys
import threading
import time
from queue import Empty, Queue
from typing import Iterator, Pattern, TextIO

from . import __version__
from .detector import AnomalyDetector, AnomalyVerdict
from .explain import ExplainError, explain_window
from .pretty import HAS_RICH
from .windower import Window, WindowAggregator

_RED = "\033[91m"
_DIM = "\033[2m"
_YELLOW = "\033[93m"
_CYAN = "\033[96m"
_RESET = "\033[0m"

_POLL_INTERVAL_SECONDS = 0.5


def _supports_color(stream: TextIO) -> bool:
    return hasattr(stream, "isatty") and stream.isatty()


def _flush(stream: TextIO) -> None:
    """Push buffered output out now. Never raises — a closed or exotic
    stream must not take down a tailing run."""
    try:
        stream.flush()
    except (AttributeError, ValueError, OSError):
        pass


def _iter_stdin() -> Iterator[str | None]:
    """Yield lines from stdin, plus a None "idle tick" whenever nothing
    arrives for a poll interval, so a window can still close when the
    upstream producer goes quiet.

    A reader thread is used rather than select(): Python buffers stdin
    internally, so select() on the file descriptor can report "no data"
    while whole lines already sit in that buffer, waiting.
    """
    queue: Queue[str | None] = Queue()

    def reader() -> None:
        try:
            for line in sys.stdin:
                queue.put(line)
        finally:
            queue.put(None)  # sentinel: end of input

    threading.Thread(target=reader, daemon=True).start()

    while True:
        try:
            item = queue.get(timeout=_POLL_INTERVAL_SECONDS)
        except Empty:
            yield None  # nothing upstream right now — let windows close
            continue
        if item is None:
            return
        yield item


def _iter_file_replay(path: str) -> Iterator[str]:
    with open(path, "r", errors="replace") as f:
        for line in f:
            yield line


def _file_identity(path: str) -> tuple[int, int] | None:
    """(device, inode) for a path, or None if it isn't there right now."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


def _iter_file_live(path: str, poll_interval: float = _POLL_INTERVAL_SECONDS) -> Iterator[str | None]:
    """Classic tail -f polling loop: seek to EOF, then poll for appended
    lines, yielding None while idle so windows can close on time.

    Also survives log rotation. Holding one file handle forever means
    that after logrotate moves the file aside, we keep reading a
    now-orphaned inode and silently see nothing ever again — so each
    idle poll checks whether the path points somewhere new (rotated) or
    has shrunk (truncated in place) and reopens accordingly.

    Opened in binary mode on purpose: it gives an exact byte position, which
    is what makes the truncation check reliable, and it lets a half-written
    line (one whose newline hasn't been flushed yet) be held back instead of
    handed over as though it were complete.
    """
    f = open(path, "rb")
    try:
        f.seek(0, 2)  # seek to end
        identity = _file_identity(path)

        while True:
            chunk = f.readline()
            if chunk.endswith(b"\n"):
                yield chunk.decode("utf-8", errors="replace")
                continue
            if chunk:
                # partial line — rewind and wait for the writer to finish it
                f.seek(-len(chunk), os.SEEK_CUR)

            time.sleep(poll_interval)

            current = _file_identity(path)
            if current is None:
                yield None  # mid-rotation; the path will be back shortly
                continue
            if current != identity:
                f.close()
                f = open(path, "rb")  # new file: read it from the top
                identity = current
            elif os.stat(path).st_size < f.tell():
                # truncated in place (copytruncate). Caveat: if the file is
                # emptied and then refilled past our read offset inside one
                # poll interval, the shrink is never observable by size and
                # we resume mid-file — the same blind spot GNU tail has.
                f.seek(0)

            yield None
    finally:
        f.close()


def _shape_levels(window: Window) -> dict[str, str]:
    """Map each shape in a window back to the level of the line that produced it,
    so a WARN-triggered anomaly isn't reported as an 'error signature'."""
    return {l.shape: l.level for l in window.lines if l.shape}


def _signature_label(level: str) -> str:
    return "warning" if level == "WARN" else "error"


def _format_plain(window: Window, verdict: AnomalyVerdict, color: bool, explanation: str | None = None) -> str:
    label = f"[{window.start_label} \u2013 {window.end_label}]"
    if verdict.is_anomaly:
        marker = f"{_RED}\u26a0 ANOMALY{_RESET}" if color else "! ANOMALY"
        # A new WARN shape can flag a window that holds zero errors \u2014
        # show the warn count too, or the header reads "errors: 0" with
        # no hint of what actually tripped the detector.
        counts = f"errors: {window.error_count}"
        if window.warn_count:
            counts += f"  warns: {window.warn_count}"
        header = (
            f"  {label}  {marker}  {counts}  "
            f"baseline: ~{verdict.baseline_mean:.1f}"
            + (f"   score: {verdict.error_score:.1f}x" if verdict.error_score else "")
        )
        lines = [header]
        levels = _shape_levels(window)
        for shape in verdict.new_shapes:
            kind = _signature_label(levels.get(shape, "ERROR"))
            note = f"    New {kind} signature detected: {shape!r}"
            lines.append(f"{_YELLOW}{note}{_RESET}" if color else note)
        if not verdict.new_shapes and not explanation:
            lines.append("    (error rate spike \u2014 no brand-new error signature)")
        if explanation:
            note = f"    Explanation: {explanation}"
            lines.append(f"{_CYAN}{note}{_RESET}" if color else note)
        return "\n".join(lines)
    else:
        text = f"  {label}  ok        errors: {window.error_count}   baseline: ~{verdict.baseline_mean:.1f}"
        return f"{_DIM}{text}{_RESET}" if color else text


def _format_json(window: Window, verdict: AnomalyVerdict, explanation: str | None = None) -> str:
    payload = {
        "window_index": window.index,
        "start": window.start_label,
        "end": window.end_label,
        "error_count": window.error_count,
        "warn_count": window.warn_count,
        "baseline_mean": round(verdict.baseline_mean, 3),
        "error_score": round(verdict.error_score, 3),
        "is_anomaly": verdict.is_anomaly,
        "new_shapes": verdict.new_shapes,
        "explanation": explanation,
    }
    return json.dumps(payload)


def run_tail(
    source: Iterator[str | None],
    window_seconds: float,
    sensitivity: str,
    output_format: str,
    quiet: bool,
    explain: bool = False,
    explain_provider: str = "anthropic",
    explain_model: str | None = None,
    use_rich: bool = False,
    ignore: list[Pattern[str]] | None = None,
    out: TextIO = sys.stdout,
) -> int:
    """Core loop shared by file/stdin, replay/live. Returns an exit code."""
    aggregator = WindowAggregator(window_seconds=window_seconds)
    detector = AnomalyDetector(sensitivity=sensitivity)
    color = output_format == "plain" and _supports_color(out)
    anomaly_count = 0
    window_count = 0
    explain_warned = False  # only print an --explain setup problem once, not per-anomaly

    console = None
    if use_rich and output_format == "plain" and HAS_RICH:
        from rich.console import Console
        console = Console(file=out)

    def get_explanation(window: Window) -> str | None:
        nonlocal explain_warned
        if not explain:
            return None
        try:
            raw_lines = [l.raw for l in window.lines if l.level in ("ERROR", "WARN")]
            return explain_window(raw_lines or [l.raw for l in window.lines], provider=explain_provider, model=explain_model)
        except ExplainError as e:
            if not explain_warned:
                print(f"[logometer] --explain unavailable: {e}", file=sys.stderr)
                explain_warned = True
            return None

    def handle(window: Window) -> None:
        nonlocal anomaly_count, window_count
        if not window.lines:
            return
        window_count += 1
        verdict = detector.evaluate(window)
        if verdict.is_anomaly:
            anomaly_count += 1
        if quiet and not verdict.is_anomaly:
            return

        explanation = get_explanation(window) if verdict.is_anomaly else None

        if output_format == "json":
            print(_format_json(window, verdict, explanation), file=out)
        elif console is not None:
            from .pretty import render_window
            render_window(window, verdict, explanation, console)
        else:
            print(_format_plain(window, verdict, color, explanation), file=out)
            print(file=out)

        # Python block-buffers stdout when it isn't a terminal, so without
        # this an alert can sit in memory for minutes when the output is
        # redirected to a file or piped into something else — the exact
        # setups a monitoring tool actually runs in.
        _flush(out)

    try:
        for raw_line in source:
            if raw_line is None:
                # idle tick from a live source: close anything overdue
                for window in aggregator.tick():
                    handle(window)
                continue
            if ignore and any(p.search(raw_line) for p in ignore):
                continue
            for window in aggregator.feed(raw_line):
                handle(window)
    except KeyboardInterrupt:
        pass
    finally:
        for window in aggregator.flush():
            handle(window)

    if output_format != "json":
        summary = f"-- {window_count} window(s) processed, {anomaly_count} anomaly(ies) flagged --"
        if console is not None:
            console.print(summary, style="dim")
        else:
            print(summary, file=out)
        _flush(out)

    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="logometer",
        description="Tail your logs. Catch anomalies. Skip the 2am grep.",
    )
    parser.add_argument("--version", action="version", version=f"logometer {__version__}")

    subparsers = parser.add_subparsers(dest="command", required=True)

    tail_parser = subparsers.add_parser("tail", help="Tail a log file (or stdin) and flag anomalous windows")
    tail_parser.add_argument(
        "file",
        help="Path to a log file, or '-' to read from stdin",
    )
    tail_parser.add_argument(
        "--window",
        type=float,
        default=10.0,
        metavar="SECONDS",
        help="Window size in seconds when timestamps are parseable (default: 10)",
    )
    tail_parser.add_argument(
        "--sensitivity",
        choices=["low", "medium", "high"],
        default="medium",
        help="How aggressively to flag deviations (default: medium)",
    )
    tail_parser.add_argument(
        "--format",
        choices=["plain", "json"],
        default="plain",
        dest="output_format",
        help="Output format (default: plain)",
    )
    tail_parser.add_argument(
        "--replay",
        action="store_true",
        help="Read a file from start to end and exit, instead of live-tailing it",
    )
    tail_parser.add_argument(
        "--quiet",
        action="store_true",
        help="Only print anomalous windows, suppress 'ok' windows",
    )
    tail_parser.add_argument(
        "--ignore",
        action="append",
        default=None,
        metavar="REGEX",
        dest="ignore",
        help="Drop lines matching this regex before any analysis — use it to mute "
             "known-noisy messages that would otherwise dominate the baseline. "
             "Repeatable: --ignore 'healthcheck' --ignore 'DeprecationWarning'",
    )
    tail_parser.add_argument(
        "--explain",
        action="store_true",
        help="Ask an LLM for a one-sentence explanation of each anomaly "
             "(requires ANTHROPIC_API_KEY or OPENAI_API_KEY; fails soft if unset)",
    )
    tail_parser.add_argument(
        "--explain-provider",
        choices=["anthropic", "openai"],
        default="anthropic",
        help="Which API to use for --explain (default: anthropic)",
    )
    tail_parser.add_argument(
        "--explain-model",
        default=None,
        metavar="MODEL",
        help="Override the default model used for --explain",
    )
    tail_parser.add_argument(
        "--no-pretty",
        action="store_true",
        help="Disable rich-styled output even if the 'rich' package is installed",
    )

    return parser


def _raise_keyboard_interrupt(signum, frame):
    raise KeyboardInterrupt()


def main(argv: list[str] | None = None) -> int:
    # Treat SIGTERM the same as Ctrl-C (SIGINT) so a live-tailing process
    # shuts down cleanly (flushing its in-progress window) whether it's
    # interrupted at the terminal or killed by a process manager/`timeout`.
    signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)

    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "tail":
        ignore = []
        for raw_pattern in args.ignore or []:
            try:
                ignore.append(re.compile(raw_pattern))
            except re.error as e:
                parser.error(f"invalid --ignore regex {raw_pattern!r}: {e}")

        if args.file == "-":
            source = _iter_stdin()
        else:
            # Check readability up front so a typo'd path produces one clear
            # line instead of a traceback out of the middle of a generator.
            if not os.path.exists(args.file):
                print(f"[logometer] no such file: {args.file}", file=sys.stderr)
                return 2
            if os.path.isdir(args.file):
                print(f"[logometer] {args.file} is a directory, not a log file", file=sys.stderr)
                return 2
            try:
                open(args.file, "rb").close()
            except OSError as e:
                print(f"[logometer] cannot read {args.file}: {e.strerror}", file=sys.stderr)
                return 2
            source = _iter_file_replay(args.file) if args.replay else _iter_file_live(args.file)

        try:
            return run_tail(
                source=source,
                window_seconds=args.window,
                sensitivity=args.sensitivity,
                output_format=args.output_format,
                quiet=args.quiet,
                explain=args.explain,
                explain_provider=args.explain_provider,
                explain_model=args.explain_model,
                use_rich=not args.no_pretty,
                ignore=ignore,
            )
        except OSError as e:
            # e.g. the file is deleted for good while we're live-tailing it
            print(f"[logometer] stopped reading {args.file}: {e}", file=sys.stderr)
            return 1

    parser.error(f"unknown command {args.command!r}")
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        # e.g. piped into `head` — exit quietly instead of a traceback
        sys.stderr.close()
        sys.exit(0)
