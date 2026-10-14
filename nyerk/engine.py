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
from .models import AlertSlot, Event, Game, SentMessage, UserTimer

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
    action: str  # send | skip (missed while Nyerk was down) | maintenance (the game is down then)


# --------------------------------------------------------------------------
# Occurrences


def load_events(s: Session, game_id: int | None = None) -> list[Event]:
    """One game's events; None = every game's."""
    q = select(Event).options(selectinload(Event.slots), selectinload(Event.category))
    if game_id is not None:
        q = q.where(Event.game_id == game_id)
    return list(s.scalars(q.order_by(Event.sort_order, Event.name)))


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


def is_maintenance(event: Event) -> bool:
    """The game's maintenance: an event in its "Maintenance" category."""
    return bool(event.category and event.category.name.strip().lower() == "maintenance")


def maintenance_windows(events: list[Event], start: int, end: int) -> dict[int, list[tuple[int, int]]]:
    """{game_id: [(start, end)]} of maintenance running at some point in
    (start, end]: the regular window, moved weeks and emergency windows.
    A switched-off maintenance event still counts: switching it off stops
    its own alerts, the game still goes down then."""
    overrides = _overrides(events)
    out: dict[int, list[tuple[int, int]]] = {}
    for e in events:
        if e.overrides_event_id or not is_maintenance(e):
            continue
        longest = max([e.duration_minutes] + [o.duration_minutes for o in overrides.get(e.id, [])]) * 60
        for occ in event_occurrences(e, start - longest, end, overrides.get(e.id)):
            if occ.end > occ.at and occ.end > start:
                out.setdefault(e.game_id, []).append((occ.at, occ.end))
    return out


def during(windows: list[tuple[int, int]], at: int) -> bool:
    return any(a <= at < b for a, b in windows)


def next_occurrence(event: Event, now: int, overrides: list[Event] | None = None, horizon: int = 40 * 86400,
                    skip: list[tuple[int, int]] | None = None) -> Occurrence | None:
    """The next occurrence, passing over any that fall in `skip` (maintenance)."""
    if schedules.get(event.schedule_type).repeat == "hourly":
        horizon = 3 * 3600
    elif schedules.get(event.schedule_type).repeat == "daily":
        horizon = 2 * 86400
    if skip:  # look past the end of maintenance
        horizon = max(horizon, max(b for _, b in skip) - now + horizon)
    occ = [o for o in event_occurrences(event, now, now + horizon, overrides) if not during(skip or [], o.at)]
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
    from . import servers
    live = set(s.scalars(select(Game.id).where(Game.enabled.is_(True),
                                               Game.guild_id.in_(servers.approved_ids(s)))))
    events = [e for e in load_events(s) if e.game_id in live]  # switched-off games and waiting servers send nothing
    overrides = _overrides(events)
    down = maintenance_windows(events, last_tick, now + 7 * 86400)
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
        held = [] if is_maintenance(event) else down.get(event.game_id, [])
        for occ in event_occurrences(event, last_tick, now + max_lead, overrides.get(event.id), timers):
            for sl in slots:
                send_at = occ.at - sl.lead_minutes * 60
                if not (last_tick < send_at <= now):
                    continue
                if occ.timer and send_at < occ.timer.armed_at - 1:
                    continue  # warning was already in the past when reported
                action = "skip" if now - send_at > GRACE_SECONDS else "send"
                if during(held, occ.at):
                    action = "maintenance"  # it would happen while the game is down
                out.append(DueSlot(occ, sl, send_at, action))
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
        # Plain {name} substitution only: no attribute or index lookups.
        rendered = _PLACEHOLDER.sub(lambda m: values.get(m[1], m[0]), line) if names else line
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


def _line(label: str, at: int, now: int, settings: Game) -> str:
    style = settings.time_style_near if at - now <= 86400 else settings.time_style_far
    return f"{label} {ts(at, 'R')} at {ts(at, style)}"


def build_dashboard(s: Session, settings: Game, now: int) -> Dashboard:
    """The dashboard for one game (`settings` is the game: its wording and events)."""
    events = load_events(s, settings.id)
    overrides = _overrides(events)
    down = maintenance_windows(events, now, now + 7 * 86400).get(settings.id, [])
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
        nxt = next_occurrence(e, now, overrides.get(e.id), skip=down)  # nothing happens during maintenance
        if not nxt:
            continue
        if st.repeat == "oneoff":
            oneoffs.append((nxt.at, _line(_label(e, False), nxt.at, now, settings)))
        elif nxt.at - now <= schedules.LOOKAHEAD.get(st.repeat, 86400):
            main.append((nxt.at, _line(_label(e, False), nxt.at, now, settings)))

    timers = s.scalars(
        select(UserTimer).join(Event).where(UserTimer.status == "active", UserTimer.fire_at > now,
                                            Event.game_id == settings.id).order_by(UserTimer.fire_at)
    )
    for t in timers:
        oneoffs.append((t.fire_at, _line(_label(t.event, False), t.fire_at, now, settings)))

    # (heading, lines). Headings are Discord markdown, still plain text.
    sections: list[tuple[str, list[str]]] = []
    if top:
        sections.append((settings.dashboard_top_label, top))
    if main:
        sections.append((settings.dashboard_main_label, [line for _, line in sorted(main)]))
    note = (settings.dashboard_boss_note or "").strip()
    has_bosses = any(e.schedule_type == "kill_report" and e.enabled for e in events)
    if oneoffs or (note and has_bosses):
        oneoffs.sort()
        cap = max(1, settings.dashboard_max_oneoffs)
        lines = [f"-# {note}"] if note and has_bosses else []
        lines += [line for _, line in oneoffs[:cap]]
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
    hint = (settings.dashboard_commands_label or "").strip()
    if hint:
        parts.append("")
        parts.append(hint)
    if settings.dashboard_footer.strip():
        parts.append(f"-# {settings.dashboard_footer.strip()}")
    text = "\n".join(parts)
    while len(text) > DISCORD_LIMIT:  # last resort; normal configs stay far below
        text = text[: text.rfind("\n")]

    template = settings.boss_button_label or "{name} died"
    bosses = [(e.id, template.replace("{name}", e.name)[:80])
              for e in sorted((e for e in events if e.schedule_type == "kill_report" and e.enabled),
                              key=lambda e: (e.sort_order, e.name))][:24]
    if bosses:
        bosses.append((0, (settings.custom_button_label or "Custom time")[:80]))  # 0 = Custom time
    return Dashboard(text, bosses)


# --------------------------------------------------------------------------
# Cleanup


CLEANUP_MIN_AGE = 3600  # never clean up an alert younger than this


def messages_to_delete(s: Session, retention_min: int, now: int, game_id: int | None = None) -> list[SentMessage]:
    """Alert messages whose event is more than `retention_min` minutes in
    the past (scheduled events: the start time; boss timers: the spawn time),
    for one game's events (None = all). Alerts sent within the last hour are
    never returned, as a safety net."""
    cutoff = now - max(1, retention_min) * 60
    q = select(SentMessage).where(
        SentMessage.message_id.is_not(None), SentMessage.deleted.is_(False),
        SentMessage.occurrence_at <= cutoff, SentMessage.sent_at <= now - CLEANUP_MIN_AGE,
    )
    if game_id is not None:
        q = q.where(SentMessage.event_id.in_(select(Event.id).where(Event.game_id == game_id)))
    return list(s.scalars(q))
