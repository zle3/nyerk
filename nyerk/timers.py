"""World boss kill reports. Shared by the slash command, dashboard buttons,
the Custom time form and the web admin."""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .db import get_settings
from .engine import ts
from .models import Event, UserTimer
from .timeparse import TimeParseError, parse_duration, parse_moment


@dataclass
class Result:
    ok: bool
    reply: str              # private reply for the user
    log: str = ""           # line for #logs ("" = nothing to log)
    timer: UserTimer | None = None
    log_level: str = "info"
    conflict: UserTimer | None = None  # someone else's active timer for this boss


def boss_events(s: Session, enabled_only: bool = True) -> list[Event]:
    q = select(Event).where(Event.schedule_type == "kill_report").order_by(Event.sort_order, Event.name)
    if enabled_only:
        q = q.where(Event.enabled.is_(True))
    return list(s.scalars(q))


def find_boss(s: Session, name_or_id: str) -> Event | None:
    bosses = boss_events(s)
    key = str(name_or_id).strip()
    for b in bosses:
        if str(b.id) == key or b.name.lower() == key.lower():
            return b
    matches = [b for b in bosses if key.lower() in b.name.lower()]
    return matches[0] if len(matches) == 1 else None


def active_count(s: Session, user_id: int) -> int:
    return s.scalar(
        select(func.count()).select_from(UserTimer).where(UserTimer.user_id == user_id, UserTimer.status == "active")
    ) or 0


def _compute(s: Session, boss: Event, now: int, killed_at: str, respawns_in: str) -> tuple[str, int | None, int]:
    """-> (mode, killed_at_ts, fire_at). Raises TimeParseError."""
    settings = get_settings(s)
    killed_at, respawns_in = (killed_at or "").strip(), (respawns_in or "").strip()
    if killed_at and respawns_in:
        raise TimeParseError("Fill in killed_at or respawns_in, not both.")
    if respawns_in:
        return "countdown", None, now + parse_duration(respawns_in)
    if killed_at:
        kill = parse_moment(killed_at, now, settings.default_tz)
        if kill > now + 60:
            raise TimeParseError("killed_at is in the future.")
    else:
        kill = now - settings.report_offset_min * 60
    if boss.respawn_minutes <= 0:
        raise TimeParseError(f"{boss.name} has no respawn time set.")
    return "kill", kill, kill + boss.respawn_minutes * 60


def active_for(s: Session, boss: Event) -> UserTimer | None:
    return s.scalar(select(UserTimer).where(UserTimer.event_id == boss.id, UserTimer.status == "active").limit(1))


def override_warning(boss: Event, existing: UserTimer) -> str:
    return (
        f"⚠️ **{boss.name}** already has a timer from **{existing.user_name}**, reported "
        f"{ts(existing.created_at, 'R')}, firing {ts(existing.fire_at, 'F')} ({ts(existing.fire_at, 'R')}).\n"
        "Only override it if you're sure it's wrong, for example you saw the kill yourself. "
        "Overrides replace their timer, are logged with your name, and misuse can cost you access."
    )


def report(
    s: Session, boss: Event, user_id: int, user_name: str, now: int, killed_at: str = "", respawns_in: str = "",
    override_id: int | None = None,
) -> Result:
    """Report a kill. Re-reporting your own active timer replaces it. Someone
    else's active timer comes back as a conflict, unless `override_id`
    names that exact timer (the reporter confirmed the override)."""
    settings = get_settings(s)
    existing = active_for(s, boss)
    if existing and existing.user_id == user_id:
        return _replace_own(s, boss, existing, now, killed_at, respawns_in, user_name)
    if existing and existing.id != override_id:
        return Result(False, override_warning(boss, existing), conflict=existing)
    count = active_count(s, user_id)
    if count >= settings.user_timer_limit:
        return Result(
            False,
            f"You already have {count}/{settings.user_timer_limit} active timers. Cancel one with /nyerk cancel first.",
            f"Denied report for {boss.name} by {user_name}: timer limit reached.",
        )
    try:
        mode, kill, fire = _compute(s, boss, now, killed_at, respawns_in)
    except TimeParseError as exc:
        return Result(False, str(exc))
    if fire <= now:
        return Result(False, f"That puts the respawn at {ts(fire, 'F')}, which has already passed.")
    replaced = ""
    if existing:  # confirmed override of someone else's timer
        existing.status = "overridden"
        replaced = (f" Overrode timer #{existing.id} by {existing.user_name}, which fired "
                    f"{ts(existing.fire_at, 'f')}.")
    timer = UserTimer(
        event_id=boss.id, user_id=user_id, user_name=user_name, mode=mode, killed_at=kill,
        fire_at=fire, status="active", created_at=now, armed_at=now,
    )
    s.add(timer)
    s.flush()
    how = f"respawns in {respawns_in}" if mode == "countdown" else f"killed {ts(kill, 'f')}" + (" (assumed)" if not killed_at else "")
    return Result(
        True,
        f"Timer set: {boss.name}, alert at {ts(fire, 'F')} ({ts(fire, 'R')}). "
        f"Active timers: {count + 1}/{settings.user_timer_limit}.",
        f"Timer #{timer.id} created by {user_name}: {boss.name}, {how}, fires {ts(fire, 'f')}.{replaced}",
        timer,
        log_level="warn" if existing else "info",
    )


def _replace_own(s: Session, boss: Event, timer: UserTimer, now: int, killed_at: str, respawns_in: str,
                 user_name: str) -> Result:
    try:
        mode, kill, fire = _compute(s, boss, now, killed_at, respawns_in)
    except TimeParseError as exc:
        return Result(False, str(exc))
    if fire <= now:
        return Result(False, f"That puts the respawn at {ts(fire, 'F')}, which has already passed.")
    old = timer.fire_at
    timer.mode, timer.killed_at, timer.fire_at, timer.armed_at = mode, kill, fire, now
    return Result(
        True,
        f"Updated your {boss.name} timer: alert at {ts(fire, 'F')} ({ts(fire, 'R')}).",
        f"Timer #{timer.id} ({boss.name}) re-reported by {user_name}: was {ts(old, 'f')}, now fires {ts(fire, 'f')}.",
        timer,
    )


def edit(s: Session, timer: UserTimer, now: int, killed_at: str = "", respawns_in: str = "", actor: str = "") -> Result:
    if timer.status != "active":
        return Result(False, "That timer is no longer active.")
    if not (killed_at or "").strip() and not (respawns_in or "").strip():
        return Result(False, "Give a new killed_at or respawns_in.")
    try:
        mode, kill, fire = _compute(s, timer.event, now, killed_at, respawns_in)
    except TimeParseError as exc:
        return Result(False, str(exc))
    if fire <= now:
        return Result(False, f"That puts the respawn at {ts(fire, 'F')}, which has already passed.")
    timer.mode, timer.killed_at, timer.fire_at, timer.armed_at = mode, kill, fire, now
    return Result(
        True,
        f"Timer updated: {timer.event.name}, alert at {ts(fire, 'F')} ({ts(fire, 'R')}).",
        f"Timer #{timer.id} ({timer.event.name}) edited by {actor or timer.user_name}: now fires {ts(fire, 'f')}.",
        timer,
    )


def cancel(timer: UserTimer, actor: str) -> Result:
    if timer.status != "active":
        return Result(False, "That timer is no longer active.")
    timer.status = "cancelled"
    return Result(
        True,
        f"Cancelled your {timer.event.name} timer.",
        f"Timer #{timer.id} ({timer.event.name}, reported by {timer.user_name}) cancelled by {actor}.",
        timer,
    )


def user_timers(s: Session, user_id: int) -> list[UserTimer]:
    return list(
        s.scalars(
            select(UserTimer)
            .where(UserTimer.user_id == user_id, UserTimer.status == "active")
            .order_by(UserTimer.fire_at)
        )
    )
