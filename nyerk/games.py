"""Games: each one has its own timers, dashboard, classes, gear scores and
party finder, posted in its own channels. A server can run several games,
switch between them, or add a game Nyerk has no template for.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Game

KINDS = {"aion2": "AION 2", "custom": "Another game"}

# Per-game settings the AION 2 template sets differently from a blank game.
AION2_SETTINGS = dict(
    dashboard_title="⏱️ AION 2 Timers",
    oneoff_label="World Bosses",
    dashboard_boss_note="Route: Gartua (Eternal Isle) > Lagta (Laborer's Rest Stop) > "
                        "Kashapa (Ancient Zumion Temple Site) > Dartan (Beast Hunter Campsite)",
    dashboard_footer="Times are in your own time zone. Killed a boss? Tap its button below.",
)


class GameError(Exception):
    """Shown to the admin as is."""


def all_games(s: Session, enabled_only: bool = False) -> list[Game]:
    q = select(Game).order_by(Game.sort_order, Game.id)
    if enabled_only:
        q = q.where(Game.enabled.is_(True))
    return list(s.scalars(q))


def get(s: Session, game_id: int | None) -> Game | None:
    return s.get(Game, game_id) if game_id else None


def first(s: Session) -> Game | None:
    games = all_games(s)
    return next((g for g in games if g.enabled), games[0] if games else None)


def by_party_channel(s: Session, channel_id: int | None) -> Game | None:
    if not channel_id:
        return None
    return s.scalar(select(Game).where(Game.party_channel_id == channel_id).order_by(Game.id).limit(1))


def by_timer_channel(s: Session, channel_id: int | None) -> Game | None:
    if not channel_id:
        return None
    return s.scalar(select(Game).where(Game.timer_channel_id == channel_id).order_by(Game.id).limit(1))


def create(s: Session, name: str, kind: str) -> Game:
    """A new game from a template: "aion2" brings the AION 2 events, boss
    timers, ping categories, classes and gear scores; "custom" starts blank
    apart from the party finder's availability windows."""
    from . import seed

    name = " ".join((name or "").split())[:64]
    if kind not in KINDS:
        raise GameError("Pick AION 2 or another game.")
    if not name:
        name = KINDS[kind] if kind == "aion2" else ""
    if not name:
        raise GameError("Give the game a name.")
    if any(g.name.lower() == name.lower() for g in all_games(s)):
        raise GameError(f"There is already a game called {name}.")
    order = max((g.sort_order for g in all_games(s)), default=-1) + 1
    game = Game(name=name, kind=kind, sort_order=order)
    if kind == "aion2":
        for k, v in AION2_SETTINGS.items():
            setattr(game, k, v)
    else:
        game.dashboard_title = f"⏱️ {name} Timers"
    s.add(game)
    s.flush()
    seed.seed_game(s, game)
    return game


def check_channels(s: Session, game: Game) -> None:
    """Two games can't share a timer or party channel: each keeps its own
    dashboard or panels at the bottom of its channel, and they'd take turns
    reposting forever."""
    for other in all_games(s):
        if other.id == game.id:
            continue
        if game.timer_channel_id and game.timer_channel_id in (other.timer_channel_id, other.party_channel_id):
            raise GameError(f"{other.name} already uses that timer channel. Each game needs its own.")
        if game.party_channel_id and game.party_channel_id in (other.timer_channel_id, other.party_channel_id):
            raise GameError(f"{other.name} already uses that party channel. Each game needs its own.")
    if game.timer_channel_id and game.timer_channel_id == game.party_channel_id:
        raise GameError("The timer and party channels must be different.")
