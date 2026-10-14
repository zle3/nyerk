"""Record role changes Nyerk makes in a server, for the Stats and Members pages."""
from __future__ import annotations

import time

from sqlalchemy import delete, select

from . import db
from .models import RoleHolder


def _now() -> int:
    return int(time.time())


def set_classes(guild_id: int, user_id: int, user_name: str, role_ids: set[int],
                only: set[int] | None = None) -> None:
    """Replace the member's class roles with `role_ids` (empty clears).
    `only`: the class roles in play (one game's); others are left alone."""
    with db.session() as s:
        rows = {r.role_id: r for r in s.scalars(select(RoleHolder).where(
            RoleHolder.guild_id == guild_id, RoleHolder.user_id == user_id, RoleHolder.kind == "class"))}
        for rid, row in rows.items():
            if rid not in role_ids and (only is None or rid in only):
                s.delete(row)
        for rid in role_ids - set(rows):
            s.add(RoleHolder(guild_id=guild_id, user_id=user_id, user_name=user_name, role_id=rid, kind="class",
                             since=_now()))


def set_class(guild_id: int, user_id: int, user_name: str, role_id: int | None) -> None:
    set_classes(guild_id, user_id, user_name, {role_id} if role_id else set())


def add_ping(guild_id: int, user_id: int, user_name: str, role_id: int, suspended: bool = False) -> None:
    with db.session() as s:
        row = s.scalar(select(RoleHolder).where(RoleHolder.user_id == user_id, RoleHolder.role_id == role_id))
        if row is None:
            s.add(RoleHolder(guild_id=guild_id, user_id=user_id, user_name=user_name, role_id=role_id, kind="ping",
                             since=_now(), suspended=suspended))
        else:
            row.suspended = suspended
            if user_name:
                row.user_name = user_name


def remove_pings(guild_id: int, user_id: int, role_ids: set[int]) -> None:
    if not role_ids:
        return
    with db.session() as s:
        s.execute(delete(RoleHolder).where(RoleHolder.guild_id == guild_id, RoleHolder.user_id == user_id,
                                           RoleHolder.role_id.in_(role_ids),
                                           RoleHolder.kind == "ping"))
