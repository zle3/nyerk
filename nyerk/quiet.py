"""Quiet hours: a member's daily window without dungeon ping roles.

During the window Nyerk removes the dungeon ping roles the member holds and
marks them suspended in role_holders; after it, they are given back. The
scheduler tick asks `transitions()` what to change and reports back with
`confirm()` once Discord accepted it, so a failed call is retried on the
next tick and a restart picks up where it left off.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import party
from .models import QuietHours, RoleHolder

ZONES = [
    ("America/New_York", "Eastern (New York)"), ("America/Chicago", "Central (Chicago)"),
    ("America/Denver", "Mountain (Denver)"), ("America/Phoenix", "Arizona"),
    ("America/Los_Angeles", "Pacific (Los Angeles)"), ("America/Anchorage", "Alaska"),
    ("Pacific/Honolulu", "Hawaii"), ("America/Sao_Paulo", "Brazil (São Paulo)"),
    ("Europe/London", "UK (London)"), ("Europe/Berlin", "Central Europe (Berlin)"),
    ("Asia/Seoul", "Korea (Seoul)"), ("Australia/Sydney", "Australia (Sydney)"), ("UTC", "UTC"),
]


def hour_label(h: int) -> str:
    ampm = "AM" if h < 12 else "PM"
    return f"{(h % 12) or 12} {ampm} ({h:02d}:00)"


def window_text(q: QuietHours) -> str:
    zone = dict(ZONES).get(q.tz, q.tz)
    return f"{hour_label(q.start_min // 60)} to {hour_label(q.end_min // 60)}, {zone}"


def in_window(q: QuietHours, now: int) -> bool:
    local = datetime.fromtimestamp(now, ZoneInfo(q.tz))
    m = local.hour * 60 + local.minute
    if q.start_min == q.end_min:
        return False
    if q.start_min < q.end_min:
        return q.start_min <= m < q.end_min
    return m >= q.start_min or m < q.end_min  # crosses midnight


def content_role_ids(s: Session, guild_id: int) -> set[int]:
    """Every game's dungeon ping roles in a server: quiet hours pause them all."""
    return {c.role_id for c in party.server_contents(s, guild_id, False) if c.role_id}


def get(s: Session, guild_id: int, user_id: int) -> QuietHours | None:
    return s.scalar(select(QuietHours).where(QuietHours.guild_id == guild_id, QuietHours.user_id == user_id))


def is_quiet(s: Session, guild_id: int, user_id: int) -> bool:
    q = get(s, guild_id, user_id)
    return bool(q and q.enabled and q.state == "quiet")


def set_hours(s: Session, guild_id: int, user_id: int, user_name: str, start_h: int, end_h: int, tz: str,
              held_role_ids: set[int]) -> QuietHours:
    """Save the window and make sure the member's current dungeon roles are
    tracked, including ones they got before Nyerk started tracking."""
    if start_h == end_h:
        raise ValueError("Start and end can't be the same hour.")
    ZoneInfo(tz)
    q = get(s, guild_id, user_id) or QuietHours(guild_id=guild_id, user_id=user_id, state="active")
    q.user_name, q.start_min, q.end_min, q.tz, q.enabled = user_name, start_h * 60, end_h * 60, tz, True
    q.updated_at = int(time.time())
    s.add(q)
    now = int(time.time())
    for role_id in held_role_ids & content_role_ids(s, guild_id):
        row = s.scalar(select(RoleHolder).where(RoleHolder.user_id == user_id, RoleHolder.role_id == role_id))
        if row is None:
            s.add(RoleHolder(guild_id=guild_id, user_id=user_id, user_name=user_name, role_id=role_id, kind="ping",
                             since=now))
    s.flush()
    return q


def turn_off(s: Session, guild_id: int, user_id: int) -> QuietHours | None:
    q = get(s, guild_id, user_id)
    if q:
        q.enabled = False
        q.updated_at = int(time.time())
    return q


@dataclass
class Change:
    guild_id: int
    user_id: int
    user_name: str
    to_state: str            # quiet | active
    role_ids: set[int]       # roles to remove (quiet) or give back (active)


def transitions(s: Session, now: int, guild_ids: set[int]) -> list[Change]:
    """What to change, in the servers listed (the approved ones)."""
    content_roles = {g: content_role_ids(s, g) for g in guild_ids}
    out = []
    for q in s.scalars(select(QuietHours).where(QuietHours.guild_id.in_(guild_ids))):
        want = "quiet" if q.enabled and in_window(q, now) else "active"
        rows = list(s.scalars(select(RoleHolder).where(RoleHolder.guild_id == q.guild_id,
                                                       RoleHolder.user_id == q.user_id, RoleHolder.kind == "ping")))
        if want == "quiet":
            todo = {r.role_id for r in rows if not r.suspended and r.role_id in content_roles[q.guild_id]}
        else:
            todo = {r.role_id for r in rows if r.suspended}
        if want != q.state or todo:
            out.append(Change(q.guild_id, q.user_id, q.user_name, want, todo))
    return out


def confirm(s: Session, guild_id: int, user_id: int, to_state: str, done: set[int]) -> None:
    q = get(s, guild_id, user_id)
    if q:
        q.state = to_state
    for r in s.scalars(select(RoleHolder).where(RoleHolder.guild_id == guild_id, RoleHolder.user_id == user_id,
                                                RoleHolder.role_id.in_(done))):
        r.suspended = to_state == "quiet"
