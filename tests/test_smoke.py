"""
Smoke tests for the Day 1 core: classification, fingerprinting, and
end-to-end anomaly detection on a synthetic log with a known injected
spike. Uses stdlib unittest so running tests needs nothing beyond Python.

Run with: python3 -m unittest discover -s tests
"""
import io
import json
import unittest
from pathlib import Path

from logometer.classifier import classify_level, fingerprint
from logometer.cli import run_tail
from logometer.windower import WindowAggregator

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"


def _ts_line(second: int, rest: str) -> str:
    return f"2026-09-17T12:00:{second:02d} {rest}\n"


def _run_json(lines, **kwargs):
    """Run the pipeline over `lines` and return one dict per emitted window."""
    out = io.StringIO()
    options = dict(
        window_seconds=10.0,
        sensitivity="medium",
        output_format="json",
        quiet=False,
    )
    options.update(kwargs)
    run_tail(source=iter(lines), out=out, **options)
    out.seek(0)
    return [json.loads(l) for l in out if l.strip().startswith("{")]


class TestClassifier(unittest.TestCase):
    def test_level_detection(self):
        self.assertEqual(classify_level("2026-01-01 ERROR something broke"), "ERROR")
        self.assertEqual(classify_level("2026-01-01 WARN slow request"), "WARN")
        self.assertEqual(classify_level("2026-01-01 INFO all good"), "INFO")
        self.assertEqual(classify_level("just some text"), "UNKNOWN")

    def test_error_beats_warn_when_both_present(self):
        self.assertEqual(classify_level("ERROR: retrying after warning"), "ERROR")

    def test_fingerprint_collapses_volatile_numbers(self):
        a = fingerprint("slow query took 313ms id=42")
        b = fingerprint("slow query took 907ms id=99")
        self.assertEqual(a, b)

    def test_fingerprint_strips_python_logging_timestamp(self):
        shape = fingerprint("2026-09-26 23:44:18,259 ERROR search parse failed")
        self.assertEqual(shape, "<x> ERROR search parse failed")

    def test_fingerprint_distinguishes_different_messages(self):
        a = fingerprint("timeout contacting upstream service")
        b = fingerprint("ConnectionResetError: connection reset by peer")
        self.assertNotEqual(a, b)


class TestEndToEnd(unittest.TestCase):
    def test_sample_log_flags_injected_spike(self):
        sample = EXAMPLES_DIR / "sample.log"
        with open(sample) as f:
            lines = f.readlines()

        out = io.StringIO()
        run_tail(
            source=iter(lines),
            window_seconds=10.0,
            sensitivity="medium",
            output_format="json",
            quiet=True,
            out=out,
        )
        out.seek(0)
        anomalies = [json.loads(line) for line in out if line.strip().startswith("{")]

        # the injected spike should produce at least one high-confidence anomaly
        self.assertTrue(any(a["error_count"] >= 5 and a["is_anomaly"] for a in anomalies))

        # and it should not be buried among dozens of false positives —
        # a handful of "first time seeing this shape" flags is expected,
        # a flood of them would mean the detector is too noisy
        self.assertLess(len(anomalies), 8)


class TestShortInput(unittest.TestCase):
    """Regression: logs shorter than the windower's 20-line probe batch.

    These used to collapse to a single window — every earlier window was
    built during flush() and then silently discarded, taking its lines
    with it. Worse, the dropped windows were exactly the baseline history
    the detector needs, so a short log could never raise an alarm at all.
    """

    def test_every_window_survives_flush(self):
        # 12 lines at 3s apart = 0..33s = four 10s windows, all below the probe batch
        lines = [_ts_line(i * 3, f"INFO request handled id={i}") for i in range(12)]
        windows = _run_json(lines)

        self.assertEqual(len(windows), 4)
        self.assertEqual([w["window_index"] for w in windows], [0, 1, 2, 3])

    def test_no_lines_are_lost(self):
        agg = WindowAggregator(window_seconds=10.0)
        closed = []
        for i in range(12):
            closed.extend(agg.feed(_ts_line(i * 3, f"INFO request handled id={i}")))

        self.assertEqual(closed, [])  # still probing — nothing closed yet
        pending = agg.flush()
        self.assertEqual(sum(len(w.lines) for w in pending), 12)

    def test_short_log_can_still_flag_a_spike(self):
        # five quiet windows then a burst — 18 lines total, under the probe batch.
        # The baseline only becomes 'ready' if those quiet windows survive.
        lines = [
            _ts_line(w * 10 + k * 2, f"INFO request handled id={w}{k}")
            for w in range(5)
            for k in range(3)
        ]
        lines += [
            _ts_line(50 + k * 2, f"ERROR database connection refused id={k}")
            for k in range(3)
        ]

        windows = _run_json(lines)
        self.assertEqual(len(windows), 6)

        spike = windows[-1]
        self.assertEqual(spike["error_count"], 3)
        self.assertTrue(spike["is_anomaly"])
        # scored against a real baseline, not just flagged as a new shape
        self.assertGreaterEqual(spike["error_score"], 2.0)


class TestAnomalyLabelling(unittest.TestCase):
    def test_warn_triggered_anomaly_is_not_called_an_error(self):
        # a clean first window, then a first-seen WARN shape and no errors
        lines = [_ts_line(k * 2, f"INFO request handled id={k}") for k in range(3)]
        lines.append(_ts_line(12, "WARN slow query took 313ms id=7"))

        out = io.StringIO()
        run_tail(
            source=iter(lines),
            window_seconds=10.0,
            sensitivity="medium",
            output_format="plain",
            quiet=True,
            out=out,
        )
        text = out.getvalue()

        self.assertIn("New warning signature detected", text)
        self.assertNotIn("New error signature detected", text)
        # the header must not read "errors: 0" with nothing to explain it
        self.assertIn("warns: 1", text)


if __name__ == "__main__":
    unittest.main()
