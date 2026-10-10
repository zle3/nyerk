"""Parsing of the free-text `killed_at` and `respawns_in` fields."""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


class TimeParseError(ValueError):
    pass


_UNIT = {
    "d": 86400, "day": 86400, "days": 86400,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
}
_PART = re.compile(r"(\d+(?:\.\d+)?)\s*([a-z]+)")


def parse_duration(text: str) -> int:
    """'1h 20m', '1h20', '80m', '1:20', '90' (minutes) -> seconds."""
    raw = text.strip().lower()
    if not raw:
        raise TimeParseError("Empty duration")
    m = re.fullmatch(r"(\d{1,3}):(\d{2})(?::(\d{2}))?", raw)
    if m:
        h, mi, s = int(m[1]), int(m[2]), int(m[3] or 0)
        return h * 3600 + mi * 60 + s
    if re.fullmatch(r"\d+", raw):
        return int(raw) * 60
    # "1h20" -> trailing bare number after hours means minutes.
    raw = re.sub(r"(\d+\s*h(?:ours?|rs?)?)\s*(\d+)$", r"\1 \2m", raw)
    parts = _PART.findall(raw)
    leftover = re.sub(r"\band\b|[\s,]", "", _PART.sub("", raw))
    if not parts or leftover:
        raise TimeParseError(f"Could not read duration {text!r}. Try 1h 20m.")
    total = 0.0
    for num, unit in parts:
        if unit not in _UNIT:
            raise TimeParseError(f"Unknown unit {unit!r}")
        total += float(num) * _UNIT[unit]
    if total <= 0:
        raise TimeParseError("Duration must be positive")
    return int(total)


def parse_moment(text: str, now: int, tz_name: str) -> int:
    """A past moment for `killed_at`.

    Accepts '10 min ago', '1h ago', 'now', a Discord timestamp '<t:123:F>',
    a raw Unix time, a clock time '14:30' / '2:30pm' (most recent past one in
    the server time zone), or 'YYYY-MM-DD HH:MM'.
    """
    raw = text.strip().lower()
    if not raw:
        raise TimeParseError("Empty time")
    if raw == "now":
        return now
    m = re.fullmatch(r"<t:(\d+)(?::[a-z])?>", raw)
    if m:
        return int(m[1])
    if re.fullmatch(r"\d{9,11}", raw):
        return int(raw)
    if raw.endswith("ago"):
        return now - parse_duration(raw[:-3])

    tz = ZoneInfo(tz_name)
    local_now = datetime.fromtimestamp(now, tz)
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})[ t](\d{1,2}):(\d{2})", raw)
    if m:
        dt = datetime(int(m[1]), int(m[2]), int(m[3]), int(m[4]), int(m[5]), tzinfo=tz)
        return int(dt.timestamp())
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", raw)
    if m and (m[2] or m[3]):
        h, mi = int(m[1]), int(m[2] or 0)
        if m[3]:
            if not 1 <= h <= 12:
                raise TimeParseError("Hour must be 1-12 with am/pm")
            h = h % 12 + (12 if m[3] == "pm" else 0)
        if not (0 <= h < 24 and 0 <= mi < 60):
            raise TimeParseError(f"Bad time {text!r}")
        cand = local_now.replace(hour=h, minute=mi, second=0, microsecond=0)
        if cand.timestamp() > now:
            cand = (cand - timedelta(days=1)).replace(hour=h, minute=mi)
        return int(cand.timestamp())
    raise TimeParseError(f"Could not read time {text!r}. Try '10 min ago' or '14:30'.")


def humanize(seconds: int) -> str:
    """Compact 'in 2h 10m' style text for autocomplete labels."""
    seconds = max(0, int(seconds))
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    parts = [f"{d}d"] * bool(d) + [f"{h}h"] * bool(h) + [f"{m}m"] * bool(m or not (d or h))
    return " ".join(parts)
