"""
Classifies a single log line into a severity level, and produces a
"shape" fingerprint for error/warn lines so that structurally similar
messages (same error, different id/timestamp/number) collapse into the
same bucket.

This is intentionally simple regex/heuristic matching, not a trained
model — the goal is to be transparent and predictable, not clever.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Checked in this order — first match wins. ERROR-ish tokens must be
# checked before WARN/INFO so a line like "ERROR: retrying after warning"
# is classified as ERROR, not WARN.
_LEVEL_PATTERNS = [
    ("ERROR", re.compile(r"\b(error|err|fatal|critical|exception|traceback|panic)\b", re.IGNORECASE)),
    ("WARN", re.compile(r"\b(warn|warning)\b", re.IGNORECASE)),
    ("INFO", re.compile(r"\b(info|notice)\b", re.IGNORECASE)),
    ("DEBUG", re.compile(r"\b(debug|trace)\b", re.IGNORECASE)),
]

# Patterns stripped out (in order) when building a fingerprint, so that
# two error lines differing only in a request id / timestamp / number
# are recognized as "the same shape".
_FINGERPRINT_STRIPS = [
    re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"),  # UUID
    re.compile(r"\b0x[0-9a-fA-F]+\b"),  # hex addresses
    re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b"),  # ISO timestamp
    re.compile(r'"[^"]*"'),  # quoted strings
    re.compile(r"'[^']*'"),  # single-quoted strings
    re.compile(r"\d+"),  # numbers, including ones glued to units/words (e.g. "313ms", "Errno104")
]


@dataclass(frozen=True)
class ClassifiedLine:
    raw: str
    level: str  # ERROR / WARN / INFO / DEBUG / UNKNOWN
    shape: str | None  # only set for ERROR / WARN lines; None otherwise


def classify_level(line: str) -> str:
    """Tag a log line with ERROR/WARN/INFO/DEBUG via regex; UNKNOWN if no match.
    Patterns are checked in priority order so ERROR wins over WARN on the same line."""
    for level, pattern in _LEVEL_PATTERNS:
        if pattern.search(line):
            return level
    return "UNKNOWN"


def fingerprint(line: str) -> str:
    """Normalize a line to an error/warn shape by stripping ids, numbers, and quotes.
    Similar messages with different volatile tokens collapse to the same string."""
    text = line.strip()
    for pattern in _FINGERPRINT_STRIPS:
        text = pattern.sub("<x>", text)
    # collapse repeated whitespace and cap length so pathologically long
    # lines don't blow up memory in the "seen shapes" set
    text = re.sub(r"\s+", " ", text).strip()
    return text[:200]


def classify_line(line: str) -> ClassifiedLine:
    """Classify severity and attach a shape fingerprint for ERROR/WARN lines.
    INFO/DEBUG/UNKNOWN lines get shape=None."""
    level = classify_level(line)
    shape = fingerprint(line) if level in ("ERROR", "WARN") else None
    return ClassifiedLine(raw=line, level=level, shape=shape)
