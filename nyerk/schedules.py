"""Schedule types.

Every schedule type answers one question: "what is the next occurrence
strictly after time T?" (Unix seconds in, Unix seconds or None out). The
scheduler, dashboard and admin preview only ever call that.

To add a type, subclass ScheduleType, fill in `fields`, and decorate it with
@register. The admin page builds its form from `fields`, so no template work
is needed.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, ClassVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = timezone.utc
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

# How far ahead the dashboard looks, by repeat class.
LOOKAHEAD = {
    "hourly": 24 * 3600,
    "daily": 24 * 3600,
    "weekly": 7 * 24 * 3600,
    "monthly": 31 * 24 * 3600,
}


class ScheduleError(ValueError):
    pass


@dataclass(frozen=True)
class Field:
    name: str
    label: str
    kind: str  # int | time | times | days | datetime | tz
    help: str = ""


def zone(params: dict) -> ZoneInfo:
    name = params.get("tz") or "UTC"
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ScheduleError(f"Unknown time zone {name!r}") from exc


def parse_hhmm(value: str) -> tuple[int, int]:
    try:
        h, m = str(value).strip().split(":")
        h, m = int(h), int(m)
    except ValueError as exc:
        raise ScheduleError(f"Time must be HH:MM, got {value!r}") from exc
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ScheduleError(f"Time out of range: {value!r}")
    return h, m


def _local_wall(day: datetime, h: int, m: int, tz: ZoneInfo) -> int:
    """Unix time of wall-clock h:m on `day` (a date in tz).

    A wall time skipped by a spring-forward gap resolves to the instant just
    after the gap; an ambiguous fall-back time resolves to its first instance.
    """
    # fold=0 gives exactly that: a gap time is read with the pre-transition
    # offset (02:30 EST -> 03:30 EDT), an ambiguous one as its first instance.
    return int(datetime(day.year, day.month, day.day, h, m, tzinfo=tz, fold=0).timestamp())


class ScheduleType:
    key: ClassVar[str]
    label: ClassVar[str]
    repeat: ClassVar[str]  # hourly | daily | weekly | monthly | oneoff | none
    fields: ClassVar[list[Field]] = []

    def validate(self, params: dict) -> dict:
        """Return cleaned params or raise ScheduleError."""
        zone(params)
        return params

    def next_after(self, params: dict, t: int) -> int | None:
        raise NotImplementedError

    def describe(self, params: dict) -> str:
        return self.label

    def _day_scan(self, params: dict, t: int, wall_times: Callable[[datetime], list[tuple[int, int]]], max_days: int = 400) -> int | None:
        """Walk local days from the day of t, returning the first wall time
        produced by `wall_times(day)` that lands strictly after t."""
        tz = zone(params)
        day = datetime.fromtimestamp(t, tz).replace(hour=0, minute=0, second=0, microsecond=0)
        for _ in range(max_days):
            for h, m in sorted(wall_times(day)):
                ts = _local_wall(day, h, m, tz)
                if ts > t:
                    return ts
            day = (day + timedelta(days=1, hours=2)).replace(hour=0)
        return None


REGISTRY: dict[str, ScheduleType] = {}


def register(cls: type[ScheduleType]) -> type[ScheduleType]:
    REGISTRY[cls.key] = cls()
    return cls


def get(key: str) -> ScheduleType:
    try:
        return REGISTRY[key]
    except KeyError as exc:
        raise ScheduleError(f"Unknown schedule type {key!r}") from exc


TZ_FIELD = Field("tz", "Time zone", "tz", "IANA name, e.g. America/New_York or UTC")


@register
class HourlyAtMinute(ScheduleType):
    key = "hourly"
    label = "Hourly at minute"
    repeat = "hourly"
    fields = [Field("minute", "Minute past the hour", "int"), TZ_FIELD]

    def validate(self, params):
        zone(params)
        minute = int(params.get("minute", 0))
        if not 0 <= minute < 60:
            raise ScheduleError("Minute must be 0-59")
        return {"minute": minute, "tz": params.get("tz") or "UTC"}

    def next_after(self, params, t):
        tz = zone(params)
        minute = int(params["minute"])
        # Step real minutes so both instances of a repeated fall-back hour fire.
        start = (t // 60 + 1) * 60
        for k in range(0, 3 * 60):
            ts = start + k * 60
            if datetime.fromtimestamp(ts, tz).minute == minute:
                return ts
        return None

    def describe(self, params):
        return f"Every hour at :{int(params['minute']):02d} ({params.get('tz')})"


@register
class DailyAtTimes(ScheduleType):
    key = "daily"
    label = "Daily at times"
    repeat = "daily"
    fields = [Field("times", "Times", "times", "Comma separated HH:MM"), TZ_FIELD]

    def validate(self, params):
        zone(params)
        times = params.get("times") or []
        if isinstance(times, str):
            times = [x for x in (p.strip() for p in times.split(",")) if x]
        if not times:
            raise ScheduleError("At least one time is required")
        times = sorted({"%02d:%02d" % parse_hhmm(x) for x in times})
        return {"times": times, "tz": params.get("tz") or "UTC"}

    def next_after(self, params, t):
        wall = [parse_hhmm(x) for x in params["times"]]
        return self._day_scan(params, t, lambda _day: wall, max_days=3)

    def describe(self, params):
        return f"Daily {', '.join(params['times'])} ({params.get('tz')})"


@register
class EveryNHours(ScheduleType):
    key = "every_n_hours"
    label = "Every N hours from anchor"
    repeat = "daily"
    fields = [
        Field("hours", "Every N hours", "int"),
        Field("anchor", "Anchor time", "time", "First occurrence each day, HH:MM"),
        TZ_FIELD,
    ]

    def validate(self, params):
        zone(params)
        hours = int(params.get("hours") or 0)
        if not 1 <= hours <= 24:
            raise ScheduleError("Hours must be 1-24")
        anchor = "%02d:%02d" % parse_hhmm(params.get("anchor") or "00:00")
        return {"hours": hours, "anchor": anchor, "tz": params.get("tz") or "UTC"}

    def _walls(self, params):
        # The cycle restarts from the anchor each local day, matching how the
        # game lists these (e.g. Watcher Kaira 01:00, 04:00 ... 22:00).
        h0, m0 = parse_hhmm(params["anchor"])
        n = int(params["hours"])
        return [((h0 + k * n) % 24, m0) for k in range(0, 24 // n + 1) if h0 + k * n < 24]

    def next_after(self, params, t):
        walls = self._walls(params)
        return self._day_scan(params, t, lambda _day: walls, max_days=3)

    def describe(self, params):
        return f"Every {params['hours']} h from {params['anchor']} ({params.get('tz')})"


@register
class WeeklyOnDays(ScheduleType):
    key = "weekly"
    label = "Weekly on days at time"
    repeat = "weekly"
    fields = [
        Field("days", "Days", "days"),
        Field("time", "Time", "time", "HH:MM"),
        TZ_FIELD,
    ]

    def validate(self, params):
        zone(params)
        days = params.get("days") or []
        if isinstance(days, str):
            days = [d.strip() for d in days.split(",")]
        days = [d.lower()[:3] for d in days if d]
        bad = [d for d in days if d not in DAYS]
        if bad or not days:
            raise ScheduleError("Pick at least one day")
        days = sorted(set(days), key=DAYS.index)
        time = "%02d:%02d" % parse_hhmm(params.get("time") or "")
        return {"days": days, "time": time, "tz": params.get("tz") or "UTC"}

    def next_after(self, params, t):
        h, m = parse_hhmm(params["time"])
        wanted = {DAYS.index(d) for d in params["days"]}
        return self._day_scan(params, t, lambda day: [(h, m)] if day.weekday() in wanted else [], max_days=9)

    def describe(self, params):
        days = ", ".join(d.capitalize() for d in params["days"])
        return f"{days} at {params['time']} ({params.get('tz')})"


@register
class MonthlyOnDay(ScheduleType):
    key = "monthly"
    label = "Monthly on a day at time"
    repeat = "monthly"
    fields = [
        Field("day", "Day of month", "int", "1-31; short months use their last day"),
        Field("time", "Time", "time", "HH:MM"),
        TZ_FIELD,
    ]

    def validate(self, params):
        zone(params)
        day = int(params.get("day") or 0)
        if not 1 <= day <= 31:
            raise ScheduleError("Day must be 1-31")
        time = "%02d:%02d" % parse_hhmm(params.get("time") or "")
        return {"day": day, "time": time, "tz": params.get("tz") or "UTC"}

    def next_after(self, params, t):
        h, m = parse_hhmm(params["time"])
        want = int(params["day"])

        def walls(day: datetime):
            last = calendar.monthrange(day.year, day.month)[1]
            return [(h, m)] if day.day == min(want, last) else []

        return self._day_scan(params, t, walls, max_days=64)

    def describe(self, params):
        return f"Monthly on day {params['day']} at {params['time']} ({params.get('tz')})"


@register
class OneOff(ScheduleType):
    key = "one_off"
    label = "One-off at timestamp"
    repeat = "oneoff"
    fields = [Field("at", "Date and time", "datetime", "YYYY-MM-DD HH:MM"), TZ_FIELD]

    def validate(self, params):
        tz = zone(params)
        raw = str(params.get("at") or "").strip().replace("T", " ")
        try:
            dt = datetime.strptime(raw[:16], "%Y-%m-%d %H:%M")
        except ValueError as exc:
            raise ScheduleError("Date must be YYYY-MM-DD HH:MM") from exc
        return {"at": dt.strftime("%Y-%m-%d %H:%M"), "tz": params.get("tz") or "UTC", "_ts": _local_wall(dt, dt.hour, dt.minute, tz)}

    def timestamp(self, params) -> int:
        if "_ts" in params:
            return int(params["_ts"])
        return self.validate(params)["_ts"]

    def next_after(self, params, t):
        ts = self.timestamp(params)
        return ts if ts > t else None

    def describe(self, params):
        return f"Once at {params['at']} ({params.get('tz')})"


@register
class KillReport(ScheduleType):
    """World bosses: no occurrences of their own. Each kill report creates a
    user timer, and the timer's fire time is a one-off occurrence."""

    key = "kill_report"
    label = "Kill-reported (world boss)"
    repeat = "none"
    fields = []

    def validate(self, params):
        return {}

    def next_after(self, params, t):
        return None

    def describe(self, params):
        return "Kill-reported respawn timer"


def occurrences_between(schedule_type: str, params: dict, start: int, end: int, limit: int = 2000) -> list[int]:
    """All occurrences in (start, end]."""
    st = get(schedule_type)
    out: list[int] = []
    t = start
    while len(out) < limit:
        nxt = st.next_after(params, t)
        if nxt is None or nxt > end:
            break
        out.append(nxt)
        t = nxt
    return out


def previous_at_or_before(schedule_type: str, params: dict, t: int, horizon: int) -> int | None:
    """Latest occurrence in (t - horizon, t]."""
    occ = occurrences_between(schedule_type, params, t - horizon, t)
    return occ[-1] if occ else None


def to_utc_iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d %H:%M UTC")
