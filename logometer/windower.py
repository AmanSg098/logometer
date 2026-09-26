"""
Groups a stream of classified log lines into fixed-size windows.

Two modes, chosen automatically:
  - "timed": if timestamps can be parsed from the log lines, windows are
    bucketed by wall-clock/log-clock time (--window seconds).
  - "count": if no timestamps are found (first N lines all fail to
    parse), windows are bucketed by a fixed number of lines instead.
    This keeps the tool useful on logs with no recognizable timestamp
    format, at the cost of windows not corresponding to a fixed time span.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterator, Optional

from .classifier import ClassifiedLine, classify_line
from .timeparse import parse_timestamp

# how many leading lines we sample to decide timed vs. count mode
_PROBE_LINES = 20
# fraction of probe lines that must have a parseable timestamp to use timed mode
_PROBE_THRESHOLD = 0.5
# fallback window size (in lines) when no timestamps are parseable
_DEFAULT_LINES_PER_WINDOW = 50


@dataclass
class Window:
    index: int
    start_label: str  # human-readable label for the window (time range or line range)
    end_label: str
    lines: list[ClassifiedLine] = field(default_factory=list)

    @property
    def error_count(self) -> int:
        """Number of lines in this window classified as ERROR.
        Fed to RollingBaseline as the primary spike metric."""
        return sum(1 for l in self.lines if l.level == "ERROR")

    @property
    def warn_count(self) -> int:
        """Number of lines in this window classified as WARN.
        Included in JSON output; not used for baseline spike scoring."""
        return sum(1 for l in self.lines if l.level == "WARN")

    @property
    def shapes(self) -> set[str]:
        """Distinct error/warn fingerprints present in this window.
        Compared against AnomalyDetector._seen_shapes for new-signature anomalies."""
        return {l.shape for l in self.lines if l.shape}


class WindowAggregator:
    """Feed classified lines in; get completed Window objects out.

    Usage:
        agg = WindowAggregator(window_seconds=10)
        for line in source:
            for window in agg.feed(line):
                handle(window)
        for window in agg.flush():
            handle(window)
    """

    def __init__(self, window_seconds: float = 10.0, lines_per_window: int = _DEFAULT_LINES_PER_WINDOW):
        """Create an aggregator; timed vs count mode is chosen after probing first lines.
        window_seconds applies in timed mode; lines_per_window in count mode."""
        self.window_seconds = window_seconds
        self.lines_per_window = lines_per_window

        self._mode: Optional[str] = None  # "timed" or "count", decided after probing
        self._probe_buffer: list[str] = []
        self._probe_timestamps: list[Optional[datetime]] = []
        self._probe_started_at: Optional[float] = None

        self._current: Optional[Window] = None
        self._current_bucket_key = None
        self._window_index = 0
        self._first_timestamp: Optional[datetime] = None
        # real-time clock (not log time) for the in-progress window, so
        # tick() can close it on schedule when the log goes silent
        self._current_opened_at: Optional[float] = None

    # -- mode detection -----------------------------------------------------

    def _decide_mode(self) -> None:
        """Pick timed windows if enough probe lines had timestamps, else count-based.
        Sets _first_timestamp when entering timed mode."""
        parseable = sum(1 for t in self._probe_timestamps if t is not None)
        if self._probe_timestamps and parseable / len(self._probe_timestamps) >= _PROBE_THRESHOLD:
            self._mode = "timed"
            for t in self._probe_timestamps:
                if t is not None:
                    self._first_timestamp = t
                    break
        else:
            self._mode = "count"

    # -- public API -----------------------------------------------------

    def feed(self, raw_line: str) -> Iterator[Window]:
        """Ingest one raw log line; yield any windows that closed (often zero or one).
        Buffers an initial probe batch before choosing timed vs count mode."""
        raw_line = raw_line.rstrip("\n")
        if not raw_line:
            return

        if self._mode is None:
            if self._probe_started_at is None:
                self._probe_started_at = time.monotonic()
            self._probe_buffer.append(raw_line)
            self._probe_timestamps.append(parse_timestamp(raw_line))
            if len(self._probe_buffer) < _PROBE_LINES:
                return  # still probing, hold lines until mode is decided
            self._decide_mode()
            # replay the buffered probe lines now that we know the mode
            buffered = self._probe_buffer
            self._probe_buffer = []
            for buffered_line in buffered:
                yield from self._feed_decided(buffered_line)
            return

        yield from self._feed_decided(raw_line)

    def _feed_decided(self, raw_line: str) -> Iterator[Window]:
        """Classify a line, assign it to the current bucket, yield completed windows.
        Requires _mode to already be timed or count."""
        classified = classify_line(raw_line)

        if self._mode == "timed":
            ts = parse_timestamp(raw_line) or self._last_seen_timestamp()
            bucket_key = math.floor((ts - self._first_timestamp).total_seconds() / self.window_seconds)
            self._last_ts = ts
        else:
            bucket_key = None  # computed below from line count

        if self._current is None:
            self._current = self._new_window(bucket_key)
            self._current_bucket_key = bucket_key

        if self._mode == "count":
            if len(self._current.lines) >= self.lines_per_window:
                yield self._current
                self._window_index += 1
                self._current = self._new_window(None)
        else:
            if bucket_key != self._current_bucket_key:
                yield self._current
                self._window_index += 1
                self._current = self._new_window(bucket_key)
                self._current_bucket_key = bucket_key

        self._current.lines.append(classified)

    def _last_seen_timestamp(self) -> datetime:
        """Fallback clock when a line in timed mode has no parseable timestamp.
        Reuses last seen time, else log start, else wall clock."""
        return getattr(self, "_last_ts", self._first_timestamp or datetime.now())

    def _new_window(self, bucket_key) -> Window:
        """Allocate the next window with human-readable start/end labels.
        Timed labels are HH:MM:SS ranges; count mode uses line number ranges."""
        if self._mode == "timed" and bucket_key is not None:
            start = self._first_timestamp.timestamp() + bucket_key * self.window_seconds
            end = start + self.window_seconds
            start_label = datetime.fromtimestamp(start).strftime("%H:%M:%S")
            end_label = datetime.fromtimestamp(end).strftime("%H:%M:%S")
        else:
            start_n = self._window_index * self.lines_per_window + 1
            end_n = start_n + self.lines_per_window - 1
            start_label = f"line {start_n}"
            end_label = f"line {end_n}"
        self._current_opened_at = time.monotonic()
        return Window(index=self._window_index, start_label=start_label, end_label=end_label)

    def tick(self, now: Optional[float] = None) -> list[Window]:
        """Close anything whose time is up, without needing a new line.

        Feeding alone only closes a window when the *next* line arrives
        in a later bucket. On a live tail that means a service which
        errors and then dies never gets reported at all: the burst that
        matters most stays buffered forever, because nothing follows it.
        Callers poll this while idle so windows close on schedule.

        Two things can be overdue:
          - the in-progress window, once `window_seconds` of real time
            have passed since it opened
          - the probe batch itself, on a log too quiet to produce 20
            lines — otherwise a low-volume service reports nothing ever

        Real (monotonic) time is used deliberately, not log time: a log
        whose clock is skewed or in another timezone would otherwise
        close every window the moment it went idle.
        """
        now = time.monotonic() if now is None else now
        closed: list[Window] = []

        if self._mode is None:
            waited = self._probe_started_at is not None and now - self._probe_started_at >= self.window_seconds
            if not (self._probe_buffer and waited):
                return []
            self._decide_mode()
            buffered = self._probe_buffer
            self._probe_buffer = []
            for buffered_line in buffered:
                closed.extend(self._feed_decided(buffered_line))

        if (
            self._current is not None
            and self._current.lines
            and self._current_opened_at is not None
            and now - self._current_opened_at >= self.window_seconds
        ):
            closed.append(self._current)
            self._current = None
            self._current_opened_at = None
            self._window_index += 1

        return closed

    def flush(self) -> list[Window]:
        """Return every window still pending at EOF, oldest first.

        Usually that's just the one in-progress window. But on a file
        shorter than the probe batch, mode is decided here and the
        buffered lines are replayed now — which can close several
        windows at once. All of them must come back: an earlier version
        kept only the last one, so a short log silently lost every
        window but its final one (and with them the baseline history
        the detector needs to flag anything at all).
        """
        pending: list[Window] = []
        if self._mode is None and self._probe_buffer:
            # never reached probe threshold (short file) — decide now
            self._decide_mode()
            buffered = self._probe_buffer
            self._probe_buffer = []
            for buffered_line in buffered:
                pending.extend(self._feed_decided(buffered_line))
        if self._current and self._current.lines:
            pending.append(self._current)
            self._current = None
        return pending
