"""Record role changes Nyerk makes, for the Stats page."""
from __future__ import annotations

import time

from sqlalchemy import delete, select

from . import db
from .models import RoleHolder


def _now() -> int:
    return int(time.time())


def set_classes(user_id: int, user_name: str, role_ids: set[int]) -> None:
    """Replace the member's class roles with `role_ids` (empty clears)."""
    with db.session() as s:
        rows = {r.role_id: r for r in s.scalars(select(RoleHolder).where(
            RoleHolder.user_id == user_id, RoleHolder.kind == "class"))}
        for rid, row in rows.items():
            if rid not in role_ids:
                s.delete(row)
        for rid in role_ids - set(rows):
            s.add(RoleHolder(user_id=user_id, user_name=user_name, role_id=rid, kind="class", since=_now()))


def set_class(user_id: int, user_name: str, role_id: int | None) -> None:
    set_classes(user_id, user_name, {role_id} if role_id else set())


def add_ping(user_id: int, user_name: str, role_id: int, suspended: bool = False) -> None:
    with db.session() as s:
        row = s.scalar(select(RoleHolder).where(RoleHolder.user_id == user_id, RoleHolder.role_id == role_id))
        if row is None:
            s.add(RoleHolder(user_id=user_id, user_name=user_name, role_id=role_id, kind="ping", since=_now(),
                             suspended=suspended))
        else:
            row.suspended = suspended
            if user_name:
                row.user_name = user_name


def remove_pings(user_id: int, role_ids: set[int]) -> None:
    if not role_ids:
        return
    with db.session() as s:
        s.execute(delete(RoleHolder).where(RoleHolder.user_id == user_id, RoleHolder.role_id.in_(role_ids),
                                           RoleHolder.kind == "ping"))
