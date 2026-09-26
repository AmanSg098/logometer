"""
A small rolling baseline: keeps the last N observed values for a metric
(e.g. error count per window) and reports the mean/std of that history.

Deliberately simple — a moving window of raw values, not an
exponentially-weighted or Bayesian estimator. Easy to explain, easy to
reason about when it gets something wrong.
"""
from __future__ import annotations

import statistics
from collections import deque


class RollingBaseline:
    def __init__(self, history_size: int = 20):
        """Keep a fixed-size deque of recent metric samples (e.g. errors per window).
        Older values drop off automatically when history_size is exceeded."""
        self._values: deque[float] = deque(maxlen=history_size)

    def update(self, value: float) -> None:
        """Append one observation; drop the oldest when history exceeds history_size.
        Called after each non-spike window in AnomalyDetector.evaluate."""
        self._values.append(value)

    @property
    def ready(self) -> bool:
        """True once there are enough samples to compare against (>= 3).
        Until ready, deviation_score returns 0 and spikes are not scored."""
        return len(self._values) >= 3

    @property
    def mean(self) -> float:
        """Arithmetic mean of stored samples, or 0.0 if history is empty.
        Shown in CLI output as the approximate errors-per-window baseline."""
        if not self._values:
            return 0.0
        return statistics.fmean(self._values)

    @property
    def stdev(self) -> float:
        """Population standard deviation of stored samples, or 0.0 if fewer than two.
        Used with mean inside deviation_score for spike detection."""
        if len(self._values) < 2:
            return 0.0
        return statistics.pstdev(self._values)

    def deviation_score(self, value: float, min_stdev: float = 1.0) -> float:
        """Return how many std devs above the mean `value` is (0 if not ready).
        Uses max(stdev, min_stdev) so flat histories do not over-flag single events."""
        if not self.ready:
            return 0.0
        std = max(self.stdev, min_stdev)
        return (value - self.mean) / std
