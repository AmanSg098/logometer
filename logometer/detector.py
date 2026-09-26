"""
Turns a stream of Windows into a stream of (Window, AnomalyVerdict) pairs.

A window is flagged anomalous if either:
  (a) its error count deviates from the rolling baseline by more than
      `sensitivity` standard deviations, or
  (b) it contains an error "shape" that has never been seen before in
      this run (and we're past the warm-up period).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .baseline import RollingBaseline
from .windower import Window

SENSITIVITY_MULTIPLIERS = {
    "low": 3.0,      # only flag big, obvious spikes
    "medium": 2.0,   # default
    "high": 1.2,     # flag smaller deviations too — noisier
}


@dataclass
class AnomalyVerdict:
    is_anomaly: bool
    error_score: float          # std-devs above baseline (0 if not ready/not anomalous)
    new_shapes: list[str] = field(default_factory=list)
    baseline_mean: float = 0.0


class AnomalyDetector:
    def __init__(self, sensitivity: str = "medium", history_size: int = 20):
        """Configure spike threshold (low/medium/high) and rolling error-count baseline.
        Also initializes the set of error shapes seen so far in this run."""
        if sensitivity not in SENSITIVITY_MULTIPLIERS:
            raise ValueError(f"unknown sensitivity {sensitivity!r}, expected one of {list(SENSITIVITY_MULTIPLIERS)}")
        self.multiplier = SENSITIVITY_MULTIPLIERS[sensitivity]
        self._error_baseline = RollingBaseline(history_size=history_size)
        self._seen_shapes: set[str] = set()
        self._warmed_up = False  # true once we've built at least one baseline observation

    def evaluate(self, window: Window) -> AnomalyVerdict:
        """Score one window for error spikes and unseen error shapes; update baseline state.
        Volume spikes are omitted from baseline updates; new shapes still learn normal volume."""
        error_count = window.error_count
        score = self._error_baseline.deviation_score(error_count)
        score_anomaly = self._error_baseline.ready and score >= self.multiplier

        new_shapes = []
        if self._warmed_up:
            for shape in window.shapes:
                if shape not in self._seen_shapes:
                    new_shapes.append(shape)

        for shape in window.shapes:
            self._seen_shapes.add(shape)

        verdict = AnomalyVerdict(
            is_anomaly=bool(score_anomaly or new_shapes),
            error_score=score if score != float("inf") else 999.0,
            new_shapes=new_shapes,
            baseline_mean=self._error_baseline.mean,
        )

        if not score_anomaly:
            self._error_baseline.update(error_count)
        self._warmed_up = True

        return verdict
