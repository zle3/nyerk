"""Linked party finders: servers sharing one party finder.

A game can join another server's game's party finder (its "home"): it then
uses the home's classes, dungeons, gear scores and time windows, and shares
its parties and sign-ups; every linked server's party channel shows them.
Roles stay per server: each joined game maps the shared classes and dungeons
to its own server's roles (LinkedRole), and keeps its own Tank, Support and
DPS roles. Pings and mentions stay in each member's own server.

party.classes_for and party.contents_for give a game's classes and
dungeons with its own server's roles; party.pf_id the game whose party
finder it shows.
"""
from __future__ import annotations

import secrets
import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Game, LinkedRole, PfInterest, PfParty

CODE_TTL = 24 * 3600


class LinkError(Exception):
    """Shown to the owner as is."""


def home_of(s: Session, game: Game) -> Game:
    if game.link_home_id:
        home = s.get(Game, game.link_home_id)
        if home is not None:
            return home
    return game


def members(s: Session, home: Game) -> list[Game]:
    """Every game showing this party finder: the home and the games linked
    to it (in approved servers, switched on)."""
    from . import servers

    approved = servers.approved_ids(s)
    linked = [g for g in s.scalars(select(Game).where(Game.link_home_id == home.id).order_by(Game.id))
              if g.enabled and g.guild_id in approved]
    return [home] + linked


def view_in_guild(s: Session, home_id: int, guild_id: int) -> Game | None:
    """The game in this server that shows the party finder of `home_id`."""
    home = s.get(Game, home_id)
    if home is None:
        return None
    for g in members(s, home):
        if g.guild_id == guild_id:
            return g
    return None


# -- linking ---------------------------------------------------------------------------


def new_code(s: Session, game: Game, now: int | None = None) -> str:
    """A code that lets another server's game join this game's party finder."""
    if game.link_home_id:
        raise LinkError("This game uses another server's party finder; it can't share one too.")
    now = int(time.time()) if now is None else now
    game.link_code = "NYERK-" + secrets.token_urlsafe(9).replace("-", "x").replace("_", "y")
    game.link_code_expires = now + CODE_TTL
    return game.link_code


def join(s: Session, game: Game, code: str, now: int | None = None) -> Game:
    """Make `game` use the party finder behind `code`. Returns the home."""
    from . import servers

    now = int(time.time()) if now is None else now
    code = (code or "").strip()
    home = s.scalar(select(Game).where(Game.link_code == code)) if code else None
    if home is None or home.link_code_expires < now:
        raise LinkError("That link code is wrong or has expired. Ask the other server's owner for a new one.")
    if home.guild_id == game.guild_id:
        raise LinkError("That code is for a game in this server.")
    if not servers.is_approved(s, home.guild_id):
        raise LinkError("That server isn't approved on this Nyerk.")
    if s.scalar(select(Game.id).where(Game.link_home_id == game.id).limit(1)):
        raise LinkError("Other servers use this game's party finder: unlink them first.")
    if game.link_home_id:
        raise LinkError("This game already uses another server's party finder.")
    if view_in_guild(s, home.id, game.guild_id) is not None:
        raise LinkError("Another game in this server already uses that party finder.")
    game.link_home_id = home.id
    _drop_registrations(s, game, now)
    for p in s.scalars(select(PfParty).where(PfParty.game_id == game.id, PfParty.status.in_(("open", "full")))):
        p.status, p.ended_at, p.ping_pending = "closed", now, False  # its own parties end; the channel clears
    return home


def unlink(s: Session, game: Game, now: int | None = None) -> None:
    """Stop using the other server's party finder. This server's members'
    open registrations there end; parties carry on."""
    if not game.link_home_id:
        return
    _drop_registrations(s, game, int(time.time()) if now is None else now, home_id=game.link_home_id)
    game.link_home_id = None


def _drop_registrations(s: Session, game: Game, now: int, home_id: int | None = None) -> None:
    rows = select(PfInterest).where(PfInterest.status == "active",
                                    (PfInterest.origin_game_id == game.id) | (PfInterest.game_id == game.id))
    if home_id is not None:
        rows = rows.where(PfInterest.game_id == home_id)
    for r in s.scalars(rows):
        r.status = "cancelled"


def set_role(s: Session, game: Game, kind: str, ref_id: int, role_id: int) -> None:
    """This server's role for a shared class or dungeon."""
    row = s.scalar(select(LinkedRole).where(LinkedRole.game_id == game.id, LinkedRole.kind == kind,
                                            LinkedRole.ref_id == ref_id))
    if row is None:
        row = LinkedRole(game_id=game.id, kind=kind, ref_id=ref_id)
        s.add(row)
    row.role_id = role_id


def tags(s: Session, home_id: int) -> dict[int, str]:
    """Server names by game, to show where each member came from: only in
    a party finder shared by several servers."""
    from .models import Settings

    home = s.get(Game, home_id)
    shown = members(s, home) if home is not None else []
    if len(shown) < 2:
        return {}
    names = {st.guild_id: st.guild_name for st in s.scalars(select(Settings))}
    return {g.id: names.get(g.guild_id) or str(g.guild_id) for g in shown}
