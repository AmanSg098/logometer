"""
Best-effort extraction of a timestamp from the start of a log line.

We only need this to decide how to bucket lines into windows. If a
line's timestamp can't be parsed, the caller falls back to a
line-count-based window instead of a time-based one — see windower.py.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

# ISO 8601, e.g. "2026-09-17T12:03:10.123Z" or "2026-09-17 12:03:10"
_ISO_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?)(Z|[+-]\d{2}:?\d{2})?"
)

# Syslog-style, e.g. "Sep 17 12:03:10"
_SYSLOG_RE = re.compile(
    r"\b([A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})\b"
)

_SYSLOG_MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}


def parse_timestamp(line: str, assumed_year: Optional[int] = None) -> Optional[datetime]:
    """Best-effort ISO or syslog timestamp extraction from a log line.
    Returns None when no recognizable time is found (caller may use line-based windows)."""
    m = _ISO_RE.search(line)
    if m:
        raw = m.group(1)
        try:
            # normalize a couple of common variants Python's fromisoformat
            # is picky about
            raw = raw.replace(" ", "T") if "T" not in raw else raw
            return datetime.fromisoformat(raw)
        except ValueError:
            pass

    m = _SYSLOG_RE.search(line)
    if m:
        try:
            month_str, day_str, time_str = m.group(1).split(None, 2)
            # handle "Sep  1 12:03:10" (double space) already collapsed by split
            month = _SYSLOG_MONTHS.get(month_str)
            if month is None:
                return None
            year = assumed_year or datetime.now().year
            hh, mm, ss = (int(x) for x in time_str.split(":"))
            return datetime(year, month, int(day_str), hh, mm, ss)
        except (ValueError, IndexError):
            return None

    return None
