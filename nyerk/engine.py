"""Pure scheduling logic: occurrences, due alerts, message text, dashboard.

Nothing here talks to Discord, so all of it is unit-testable with a clock.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from . import schedules
from .db import get_state
from .models import AlertSlot, Event, SentMessage, Settings, UserTimer

GRACE_SECONDS = 120          # a slot later than this is "missed" and skipped
MAX_CATCHUP_SECONDS = 2 * 86400
DISCORD_LIMIT = 2000

DEFAULT_TEMPLATES = {
    "boss_warn": "{role}\n{name} spawns in {minutes} minutes\n{time_full} ({time_relative})\n{location} | reported by {reporter}",
    "boss_now": "{role}\n{name} is spawning now\n{time_full} ({time_relative})\n{location} | reported by {reporter}",
    "warn": "{role}\n{name} starts in {minutes} minutes\n{time_full} ({time_relative})",
    "now": "{role}\n{name} is starting now\n{time_full} ({time_relative})",
}


def default_template(event: Event, lead_minutes: int) -> str:
    boss = event.schedule_type == "kill_report"
    return DEFAULT_TEMPLATES[("boss_" if boss else "") + ("warn" if lead_minutes else "now")]


@dataclass
class Occurrence:
    event: Event
    at: int
    duration_min: int = 0
    timer: UserTimer | None = None

    @property
    def timer_id(self) -> int:
        return self.timer.id if self.timer else 0

    @property
    def end(self) -> int:
        return self.at + self.duration_min * 60


@dataclass
class DueSlot:
    occ: Occurrence
    slot: AlertSlot
    send_at: int
    action: str  # send | skip


# --------------------------------------------------------------------------
# Occurrences


def load_events(s: Session) -> list[Event]:
    return list(
        s.scalars(
            select(Event)
            .options(selectinload(Event.slots), selectinload(Event.category))
            .order_by(Event.sort_order, Event.name)
        )
    )


def _overrides(events: list[Event]) -> dict[int, list[Event]]:
    out: dict[int, list[Event]] = {}
    for e in events:
        if e.overrides_event_id and e.enabled:
            out.setdefault(e.overrides_event_id, []).append(e)
    return out


def event_occurrences(
    event: Event, start: int, end: int, overrides: list[Event] | None = None, timers: list[UserTimer] | None = None
) -> list[Occurrence]:
    """Occurrences of `event` with start time in (start, end]."""
    if event.schedule_type == "kill_report":
        return [
            Occurrence(event, t.fire_at, event.duration_minutes, t)
            for t in (timers or [])
            if t.event_id == event.id and start < t.fire_at <= end and t.status in ("active", "done")
        ]
    overrides = overrides or []
    replaced = {int(o.schedule_params.get("replaces", 0)) for o in overrides}
    # Look back a little so a moved override that starts in range is found
    # even when the regular slot it replaces is outside the range.
    occ = [
        Occurrence(event, t, event.duration_minutes)
        for t in schedules.occurrences_between(event.schedule_type, event.schedule_params, start, end)
        if t not in replaced
    ]
    for o in overrides:
        if o.schedule_params.get("cancelled"):
            continue
        at = schedules.get("one_off").timestamp(o.schedule_params)
        if start < at <= end:
            occ.append(Occurrence(event, at, o.duration_minutes))
    occ.sort(key=lambda x: x.at)
    return occ


def next_occurrence(event: Event, now: int, overrides: list[Event] | None = None, horizon: int = 40 * 86400) -> Occurrence | None:
    if schedules.get(event.schedule_type).repeat == "hourly":
        horizon = 3 * 3600
    elif schedules.get(event.schedule_type).repeat == "daily":
        horizon = 2 * 86400
    occ = event_occurrences(event, now, now + horizon, overrides)
    return occ[0] if occ else None


def running_occurrence(event: Event, now: int, overrides: list[Event] | None = None) -> Occurrence | None:
    """An occurrence that has started and not yet ended (needs a duration)."""
    longest = max([event.duration_minutes] + [o.duration_minutes for o in overrides or []])
    if longest <= 0:
        return None
    for occ in reversed(event_occurrences(event, now - longest * 60, now, overrides)):
        if occ.at <= now < occ.end:
            return occ
    return None


# --------------------------------------------------------------------------
# Alerts


def due_slots(s: Session, now: int, last_tick: int) -> list[DueSlot]:
    """Slots whose send time falls in (last_tick, now], minus ones already in
    the sent ledger. Slots older than GRACE_SECONDS are marked skip."""
    last_tick = max(last_tick, now - MAX_CATCHUP_SECONDS)
    events = load_events(s)
    overrides = _overrides(events)
    timers = list(s.scalars(select(UserTimer).where(UserTimer.status == "active")))
    out: list[DueSlot] = []
    for event in events:
        if event.overrides_event_id:
            continue
        is_boss = event.schedule_type == "kill_report"
        if not event.enabled and not is_boss:
            continue
        slots = [sl for sl in event.slots if sl.enabled]
        if not slots:
            continue
        max_lead = max(sl.lead_minutes for sl in slots) * 60
        for occ in event_occurrences(event, last_tick, now + max_lead, overrides.get(event.id), timers):
            for sl in slots:
                send_at = occ.at - sl.lead_minutes * 60
                if not (last_tick < send_at <= now):
                    continue
                if occ.timer and send_at < occ.timer.armed_at - 1:
                    continue  # warning was already in the past when reported
                out.append(DueSlot(occ, sl, send_at, "skip" if now - send_at > GRACE_SECONDS else "send"))
    if not out:
        return out
    seen = {
        (r.event_id, r.timer_id, r.occurrence_at, r.slot)
        for r in s.scalars(select(SentMessage).where(SentMessage.occurrence_at >= last_tick - 86400))
    }
    out = [d for d in out if (d.occ.event.id, d.occ.timer_id, d.occ.at, d.slot.lead_minutes) not in seen]
    out.sort(key=lambda d: (d.send_at, d.occ.event.sort_order))
    return out


class _Blank(dict):
    def __missing__(self, key):
        return "{" + key + "}"


_MD = re.compile(r"([\\*_~`|>#\-\[\]()])")
_PLACEHOLDER = re.compile(r"\{(\w+)\}")


def escape_md(text: str) -> str:
    return _MD.sub(r"\\\1", text)


def ts(at: int, style: str) -> str:
    return f"<t:{int(at)}:{style}>"


def render_template(template: str, values: dict[str, str]) -> str:
    """Fill {fields}. A line whose placeholders all came out empty and has no
    other words left (e.g. a bare '{role}' with ping off) is dropped, and
    dangling ' | ' separators are trimmed."""
    lines = []
    for line in template.splitlines():
        names = _PLACEHOLDER.findall(line)
        rendered = line.format_map(_Blank(values)) if names else line
        if names and all(not values.get(n) for n in names):
            leftover = _PLACEHOLDER.sub("", line).strip(" |·-:,")
            if not leftover:
                continue
        rendered = re.sub(r"^(\s*\|\s*)+|(\s*\|\s*)+$", "", rendered)
        rendered = re.sub(r"(\s*\|\s*){2,}", " | ", rendered)
        if rendered.strip() or not names:
            lines.append(rendered.rstrip())
    while lines and not lines[0].strip():
        lines.pop(0)
    return "\n".join(lines).strip()[:DISCORD_LIMIT]


def alert_values(occ: Occurrence, slot: AlertSlot, ping: bool) -> dict[str, str]:
    event = occ.event
    role_id = event.category.role_id if event.category else 0
    return {
        "name": event.name,
        "role": f"<@&{role_id}>" if ping and role_id else "",
        "category": event.category.name if event.category else "",
        "minutes": str(slot.lead_minutes),
        "time_full": ts(occ.at, "F"),
        "time_relative": ts(occ.at, "R"),
        "time_short": ts(occ.at, "t"),
        "end_full": ts(occ.end, "F") if occ.duration_min else "",
        "end_relative": ts(occ.end, "R") if occ.duration_min else "",
        "location": event.location or "",
        "reporter": escape_md(occ.timer.user_name) if occ.timer and occ.timer.user_name else "",
        "duration": str(occ.duration_min) if occ.duration_min else "",
    }


def render_alert(occ: Occurrence, slot: AlertSlot) -> tuple[str, int]:
    """Returns (content, role_id to allow-mention or 0)."""
    role_id = occ.event.category.role_id if occ.event.category else 0
    ping = bool(slot.ping and role_id)
    template = slot.template.strip() or default_template(occ.event, slot.lead_minutes)
    return render_template(template, alert_values(occ, slot, ping)), (role_id if ping else 0)


# --------------------------------------------------------------------------
# Dashboard


@dataclass
class Dashboard:
    text: str
    bosses: list[tuple[int, str]] = field(default_factory=list)  # (event_id, name)

    @property
    def digest(self) -> str:
        raw = self.text + "|" + ",".join(f"{i}:{n}" for i, n in self.bosses)
        return hashlib.sha256(raw.encode()).hexdigest()


def _label(event: Event, active: bool) -> str:
    if active:
        tmpl = event.dashboard_label_active or "{name} ends"
    else:
        tmpl = event.dashboard_label or ("{name} respawning" if event.schedule_type == "kill_report" else "{name} starts")
    return tmpl.replace("{name}", event.name)


def _line(label: str, at: int, now: int, settings: Settings) -> str:
    style = settings.time_style_near if at - now <= 86400 else settings.time_style_far
    return f"{label} {ts(at, 'R')} at {ts(at, style)}"


COMMANDS = [
    ("worldboss", "report a boss kill, with a time or the time left"),
    ("mytimers", "see your boss timers"),
    ("edit", "fix the time on your timer"),
    ("cancel", "remove your timer"),
]


def commands_lines(command_id: int) -> list[str]:
    """Clickable command mentions once the command ID is known."""
    def mention(sub: str) -> str:
        return f"</nyerk {sub}:{command_id}>" if command_id else f"`/nyerk {sub}`"
    return [f"{mention(sub)} {what}" for sub, what in COMMANDS]


def build_dashboard(s: Session, settings: Settings, now: int) -> Dashboard:
    events = load_events(s)
    overrides = _overrides(events)
    top: list[str] = []
    main: list[tuple[int, str]] = []
    oneoffs: list[tuple[int, str]] = []

    for e in events:
        if not e.enabled or e.overrides_event_id or e.dashboard == "hidden":
            continue
        st = schedules.get(e.schedule_type)
        if e.schedule_type == "kill_report":
            continue
        if e.dashboard == "top":
            running = running_occurrence(e, now, overrides.get(e.id))
            if running:
                top.append(_line(_label(e, True), running.end, now, settings))
                continue
            nxt = next_occurrence(e, now, overrides.get(e.id))
            if nxt:
                top.append(_line(_label(e, False), nxt.at, now, settings))
            continue
        nxt = next_occurrence(e, now, overrides.get(e.id))
        if not nxt:
            continue
        if st.repeat == "oneoff":
            oneoffs.append((nxt.at, _line(_label(e, False), nxt.at, now, settings)))
        elif nxt.at - now <= schedules.LOOKAHEAD.get(st.repeat, 86400):
            main.append((nxt.at, _line(_label(e, False), nxt.at, now, settings)))

    timers = s.scalars(
        select(UserTimer).where(UserTimer.status == "active", UserTimer.fire_at > now).order_by(UserTimer.fire_at)
    )
    for t in timers:
        oneoffs.append((t.fire_at, _line(_label(t.event, False), t.fire_at, now, settings)))

    # (heading, lines). Headings are Discord markdown, still plain text.
    sections: list[tuple[str, list[str]]] = []
    if top:
        sections.append((settings.dashboard_top_label, top))
    if main:
        sections.append((settings.dashboard_main_label, [line for _, line in sorted(main)]))
    if oneoffs:
        oneoffs.sort()
        cap = max(1, settings.dashboard_max_oneoffs)
        lines = [line for _, line in oneoffs[:cap]]
        if len(oneoffs) > cap:
            lines.append(f"+{len(oneoffs) - cap} more")
        sections.append((settings.oneoff_label, lines))

    parts = [settings.dashboard_divider.strip()] if settings.dashboard_divider.strip() else []
    if settings.dashboard_title.strip():
        parts.append(f"## {settings.dashboard_title.strip()}")
    for heading, lines in sections:
        if heading.strip():
            parts.append(f"### {heading.strip()}")
        elif parts:
            parts.append("")  # no heading: a blank line still separates sections
        parts.extend(lines)
    if not sections:
        parts.append("No upcoming events.")
    label = (settings.dashboard_commands_label or "").strip()
    if label:
        parts.append(f"### {label}")
        parts.extend(commands_lines(get_state(s).nyerk_command_id))
    if settings.dashboard_footer.strip():
        parts.append(f"-# {settings.dashboard_footer.strip()}")
    text = "\n".join(parts)
    while len(text) > DISCORD_LIMIT:  # last resort; normal configs stay far below
        text = text[: text.rfind("\n")]

    bosses = [(e.id, e.name) for e in events if e.schedule_type == "kill_report" and e.enabled][:24]
    return Dashboard(text, bosses)


# --------------------------------------------------------------------------
# Cleanup


CLEANUP_MIN_AGE = 3600  # never clean up an alert younger than this


def messages_to_delete(s: Session, keep: int, now: int | None = None) -> list[SentMessage]:
    """Alert messages beyond the `keep` most recent occurrences per event.
    A world boss kill report counts as one occurrence (keyed by timer).
    Alerts sent within the last hour are never returned, as a safety net."""
    rows = list(
        s.scalars(
            select(SentMessage).where(SentMessage.message_id.is_not(None), SentMessage.deleted.is_(False))
        )
    )
    by_event: dict[int, dict[tuple[int, int], list[SentMessage]]] = {}
    for r in rows:
        by_event.setdefault(r.event_id, {}).setdefault((r.timer_id, r.occurrence_at), []).append(r)
    doomed: list[SentMessage] = []
    for groups in by_event.values():
        keys = sorted(groups, key=lambda k: k[1], reverse=True)
        for k in keys[max(1, keep):]:
            doomed.extend(groups[k])
    if now is not None:
        doomed = [r for r in doomed if r.sent_at <= now - CLEANUP_MIN_AGE]
    return doomed
