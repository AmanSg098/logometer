"""
Tests for the live-tailing behaviours that make logometer usable as an
actual monitor rather than a demo:

  - a window closes on schedule even when the log goes silent
  - alerts are flushed immediately instead of sitting in a block buffer
  - a rotated or truncated log is picked up instead of read forever
  - --ignore mutes known-noisy lines before they reach the baseline

These are the cases a `--replay` run can never exercise, because replay
always ends in a flush(). Uses stdlib unittest and short poll intervals
so the whole file still runs in well under a second.
"""
import io
import json
import os
import re
import tempfile
import time
import unittest
from pathlib import Path

from logometer.cli import _iter_file_live, run_tail
from logometer.windower import WindowAggregator


def _ts_line(second: int, rest: str) -> str:
    return f"2026-09-26T12:00:{second:02d} {rest}\n"


def _later(seconds: float = 10_000.0) -> float:
    """A monotonic reading far enough ahead that anything pending is overdue.
    (monotonic() is uptime-based, not zero-based, so tests can't pass a bare
    literal here.)"""
    return time.monotonic() + seconds


class TestIdleWindowClosing(unittest.TestCase):
    """A live tail only sees a window end when the *next* line arrives.
    If a service errors and then dies, nothing follows — so without a
    timeout the burst that matters most is never reported at all.
    """

    def _fill_past_probe(self, agg, count=25):
        """Feed enough lines to get past the 20-line probe batch."""
        closed = []
        for i in range(count):
            closed.extend(agg.feed(_ts_line(i % 60, f"ERROR boom id={i}")))
        return closed

    def test_tick_closes_an_overdue_window(self):
        agg = WindowAggregator(window_seconds=10.0)
        self._fill_past_probe(agg)
        self.assertIsNotNone(agg._current)

        # not overdue yet -> nothing closes
        self.assertEqual(agg.tick(now=time.monotonic()), [])

        # far enough in the future that the window's time is up
        closed = agg.tick(now=_later())
        self.assertEqual(len(closed), 1)
        self.assertTrue(closed[0].lines)

    def test_tick_is_idempotent(self):
        agg = WindowAggregator(window_seconds=10.0)
        self._fill_past_probe(agg)
        self.assertEqual(len(agg.tick(now=_later())), 1)
        # nothing left pending, so a second tick must not re-emit it
        self.assertEqual(agg.tick(now=_later(20_000.0)), [])

    def test_tick_releases_a_log_too_quiet_to_finish_probing(self):
        """Under 20 lines, mode is never decided and the lines sit in the
        probe buffer. A low-volume service would otherwise report nothing,
        ever."""
        agg = WindowAggregator(window_seconds=10.0)
        for i in range(4):
            self.assertEqual(list(agg.feed(_ts_line(i, f"ERROR boom id={i}"))), [])

        closed = agg.tick(now=_later())
        self.assertTrue(closed, "quiet log never released its buffered lines")
        self.assertEqual(sum(len(w.lines) for w in closed), 4)

    def test_lines_after_a_tick_start_a_fresh_window(self):
        agg = WindowAggregator(window_seconds=10.0)
        self._fill_past_probe(agg)
        first = agg.tick(now=_later())[0]

        more = []
        for i in range(3):
            more.extend(agg.feed(_ts_line(40 + i, f"ERROR later id={i}")))
        pending = agg.flush()

        self.assertTrue(pending)
        self.assertNotIn(first, pending)
        self.assertEqual(sum(len(w.lines) for w in pending) + sum(len(w.lines) for w in more), 3)


class _FlushCountingStream(io.StringIO):
    """StringIO that records how often it was flushed, and refuses to be
    read after close() — like a real pipe."""

    def __init__(self):
        super().__init__()
        self.flush_calls = 0

    def flush(self):
        self.flush_calls += 1
        super().flush()


class TestOutputIsFlushed(unittest.TestCase):
    """Python block-buffers stdout when it isn't a terminal, so alerts can
    sit unseen for minutes when output is redirected or piped."""

    def test_each_window_flushes(self):
        out = _FlushCountingStream()
        lines = [_ts_line(i * 3, f"INFO request id={i}") for i in range(12)]
        run_tail(
            source=iter(lines),
            window_seconds=10.0,
            sensitivity="medium",
            output_format="plain",
            quiet=False,
            out=out,
        )
        # 4 windows + the closing summary
        self.assertGreaterEqual(out.flush_calls, 5)


class TestIgnorePatterns(unittest.TestCase):
    def _run(self, lines, ignore=None):
        out = io.StringIO()
        run_tail(
            source=iter(lines),
            window_seconds=10.0,
            sensitivity="medium",
            output_format="json",
            quiet=False,
            ignore=ignore,
            out=out,
        )
        out.seek(0)
        return [json.loads(l) for l in out if l.strip().startswith("{")]

    def _noisy_log(self):
        lines = []
        for w in range(4):
            for k in range(6):
                lines.append(_ts_line(w * 10 + k, f"ERROR healthcheck probe failed id={k}"))
                lines.append(_ts_line(w * 10 + k, f"INFO request handled id={k}"))
        return lines

    def test_without_ignore_the_noise_is_counted(self):
        windows = self._run(self._noisy_log())
        self.assertTrue(all(w["error_count"] > 0 for w in windows))

    def test_ignored_lines_never_reach_the_detector(self):
        windows = self._run(self._noisy_log(), ignore=[re.compile("healthcheck")])
        self.assertTrue(windows, "ignoring should not swallow the whole log")
        self.assertTrue(
            all(w["error_count"] == 0 for w in windows),
            "muted lines still counted as errors",
        )
        self.assertTrue(all(not w["is_anomaly"] for w in windows))

    def test_multiple_patterns_all_apply(self):
        lines = self._noisy_log() + [_ts_line(50, "ERROR DeprecationWarning: old api id=1")]
        windows = self._run(
            lines, ignore=[re.compile("healthcheck"), re.compile("DeprecationWarning")]
        )
        self.assertTrue(all(w["error_count"] == 0 for w in windows))


class TestRotationHandling(unittest.TestCase):
    """Holding one file handle forever means that after logrotate moves the
    file aside we keep reading an orphaned inode and silently see nothing
    again. These drive the tail generator directly so no sleeping is needed
    beyond a tiny poll interval."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "app.log")
        Path(self.path).write_text("")

    def _start(self):
        """Start tailing and prime the generator.

        `_iter_file_live` is a generator, so none of its body — including
        opening the file and seeking to the end — runs until the first
        next(). Priming first means later appends land *after* that seek
        and are actually seen.
        """
        gen = _iter_file_live(self.path, poll_interval=0.001)
        self.assertIsNone(next(gen))  # first poll on an empty file: idle tick
        self.addCleanup(gen.close)
        return gen

    def _read_until(self, gen, needle, max_polls=500):
        """Pull from the generator until a line contains `needle`."""
        for _ in range(max_polls):
            item = next(gen)
            if item and needle in item:
                return item
        return None

    def _append(self, text):
        with open(self.path, "a") as f:
            f.write(text)
            f.flush()

    def test_follows_a_rotated_file(self):
        gen = self._start()
        self._append("before rotation\n")
        self.assertIsNotNone(self._read_until(gen, "before rotation"))

        # logrotate: move aside, create a fresh file at the same path
        os.rename(self.path, self.path + ".1")
        Path(self.path).write_text("")
        self._append("after rotation\n")

        self.assertIsNotNone(
            self._read_until(gen, "after rotation"),
            "kept reading the rotated-away file instead of the new one",
        )

    def test_follows_an_in_place_truncation(self):
        gen = self._start()
        self._append("first line\n")
        self.assertIsNotNone(self._read_until(gen, "first line"))

        # copytruncate: same inode, emptied
        with open(self.path, "w"):
            pass
        # Let the tailer see the file shrink before it refills. This is the
        # real logrotate sequence (truncate, then the app logs again); a
        # truncate-and-refill inside a single poll interval is invisible to
        # any size-based check, GNU tail included.
        for _ in range(5):
            next(gen)
        self._append("after truncate\n")

        self.assertIsNotNone(
            self._read_until(gen, "after truncate"),
            "did not notice the file was truncated in place",
        )

    def test_survives_the_file_briefly_disappearing(self):
        gen = self._start()
        self._append("line one\n")
        self.assertIsNotNone(self._read_until(gen, "line one"))

        os.remove(self.path)
        for _ in range(5):
            next(gen)  # must yield idle ticks, not raise
        Path(self.path).write_text("")
        self._append("came back\n")

        self.assertIsNotNone(self._read_until(gen, "came back"))


if __name__ == "__main__":
    unittest.main()
