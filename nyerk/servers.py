"""The Discord servers this Nyerk serves.

Each server has its own settings row (Settings), games, roles channel,
members, quiet hours, logs and access switches; nothing is shared between
servers. A server that invites the bot is "waiting" until the host (the
people in OWNER_IDS, or the password sign-in) approves it on the Servers
page; Nyerk does nothing in a waiting server. Servers listed in GUILD_ID
are approved on their own.
"""
from __future__ import annotations

import time

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from . import safety
from .models import Settings

APPROVED, WAITING = "approved", "waiting"
LEGACY = 0  # the server of an install from before servers, until claimed
# Every table with a guild_id column; claim_legacy and remove walk them.
TABLES = ("settings", "games", "reaction_groups", "log_entries", "role_holders", "quiet_hours", "member_profiles",
          "config_changes")


class ServerError(Exception):
    """Shown to the host as is."""


def get(s: Session, guild_id: int | None) -> Settings | None:
    if guild_id is None:
        return None
    return s.scalar(select(Settings).where(Settings.guild_id == int(guild_id)))


def all_servers(s: Session) -> list[Settings]:
    return list(s.scalars(select(Settings).order_by(Settings.status, Settings.guild_name, Settings.guild_id)))


def approved(s: Session) -> list[Settings]:
    return [x for x in all_servers(s) if x.status == APPROVED and x.guild_id != LEGACY]


def approved_ids(s: Session) -> set[int]:
    return {x.guild_id for x in approved(s)}


def is_approved(s: Session, guild_id: int | None) -> bool:
    row = get(s, guild_id)
    return row is not None and row.status == APPROVED


def seen(s: Session, guild_id: int, name: str, auto_approve: bool = False, now: int | None = None) -> Settings:
    """The bot is in this server (it joined, or was there at start). Adds
    it as waiting unless it is known; keeps its name current."""
    row = get(s, guild_id)
    if row is None:
        row = Settings(guild_id=int(guild_id), guild_name=(name or "")[:100], joined_at=now or int(time.time()),
                       status=APPROVED if auto_approve else WAITING)
        s.add(row)
        s.flush()
    else:
        if name and row.guild_name != name[:100]:
            row.guild_name = name[:100]
        if auto_approve and row.status != APPROVED:
            row.status = APPROVED
    safety.protect_guild(row.guild_id)
    if row.status == APPROVED:
        _setup(s, row.guild_id)
    return row


def _setup(s: Session, guild_id: int) -> None:
    from .seed import seed_reaction_groups
    seed_reaction_groups(s, guild_id)


def approve(s: Session, guild_id: int) -> Settings:
    row = get(s, guild_id)
    if row is None:
        raise ServerError("Nyerk isn't in that server.")
    row.status = APPROVED
    _setup(s, guild_id)
    return row


def remove(s: Session, guild_id: int) -> str:
    """Forget a server and everything Nyerk keeps for it. Games cascade to
    their events, roles setup, parties and the rest."""
    row = get(s, guild_id)
    if row is None:
        raise ServerError("Nyerk doesn't know that server.")
    name = row.guild_name or str(guild_id)
    for table in TABLES:
        s.execute(text(f"DELETE FROM {table} WHERE guild_id = :g"), {"g": int(guild_id)})
    return name


def claim_legacy(s: Session, guild_id: int, name: str = "") -> bool:
    """An install from before servers keeps all its data under server 0;
    hand it to its real server. Only once, and never onto a server that
    already has data."""
    if not guild_id or get(s, LEGACY) is None or get(s, guild_id) is not None:
        return False
    for table in TABLES:
        s.execute(text(f"UPDATE {table} SET guild_id = :g WHERE guild_id = 0"), {"g": int(guild_id)})
    s.expire_all()
    row = get(s, guild_id)
    row.status = APPROVED
    if name:
        row.guild_name = name[:100]
    safety.protect_guild(row.guild_id)
    return True


def protect_all(s: Session) -> None:
    """A server's ID is also its @everyone role's ID: never accept or ping it."""
    for row in all_servers(s):
        if row.guild_id:
            safety.protect_guild(row.guild_id)


def row_guild(s: Session, row) -> int | None:
    """The Discord server any row belongs to: its own guild_id, or its
    game's, or (boss timers, alert slots) its event's game's."""
    from .models import Event, Game

    if row is None:
        return None
    if hasattr(row, "guild_id"):
        return row.guild_id
    game_id = getattr(row, "game_id", None)
    if game_id is None and getattr(row, "event_id", None) is not None:
        event = s.get(Event, row.event_id)
        game_id = event.game_id if event else None
    game = s.get(Game, game_id) if game_id is not None else None
    return game.guild_id if game else None
