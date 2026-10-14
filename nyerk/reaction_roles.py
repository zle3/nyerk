"""Reaction role messages in the roles channel.

Ping roles (event categories and dungeon roles) are grouped into role
messages that the admin defines; each group posts as its own message. In
each message, entries use their own emoji if set, otherwise the next free
one from 1️⃣ 2️⃣ 3️⃣ … 🔟 then 🇦 🇧 🇨 …. Discord allows 20 different
reactions per message, so a bigger group continues in a second message.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import party
from .models import Category, Game, LinkedRole, PfContent, ReactionGroup

NUMBERS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
LETTERS = [chr(0x1F1E6 + i) for i in range(26)]  # 🇦 … 🇿
AUTO_EMOJI = NUMBERS + LETTERS
PER_MESSAGE = 20  # Discord's limit of distinct reactions on one message

CLEAR_TEXT = ("## Ping settings\n"
              "**My roles** shows your classes, gear score and pings.\n"
              "**Quiet hours** pause your dungeon pings every day during hours you choose, then turn them back on.\n"
              "**Remove all my ping roles** removes every event and dungeon ping role you have. Your class role stays.")


@dataclass
class Entry:
    kind: str       # category | content | linked (a LinkedRole)
    ref_id: int
    label: str
    role_id: int
    emoji: str = ""
    auto: bool = False


@dataclass
class Group:
    id: int
    name: str
    description: str
    entries: list[Entry] = field(default_factory=list)
    chunks: list[list[Entry]] = field(default_factory=list)  # one per Discord message


def groups(s: Session, guild_id: int) -> list[ReactionGroup]:
    return list(s.scalars(select(ReactionGroup).where(ReactionGroup.guild_id == guild_id)
                          .order_by(ReactionGroup.sort_order, ReactionGroup.id)))


def build(s: Session, guild_id: int, skip: set[int] | None = None) -> list[Group]:
    """Every group with its entries and final emojis, in posting order.
    Groups with no roles are left out; roles in `skip` are left out, and so
    are switched-off games' roles. With several games, labels name the game."""
    skip = skip or set()
    rows = groups(s, guild_id)
    live = {g.id: g.name for g in s.scalars(select(Game).where(Game.enabled.is_(True), Game.guild_id == guild_id))}
    many = len(live) > 1
    # Games using another server's party finder: their own dungeons sit unused.
    linked = set(s.scalars(select(Game.id).where(Game.guild_id == guild_id, Game.link_home_id.is_not(None))))

    def label(game_id, name):
        return f"{live[game_id]}: {name}" if many else name

    by_id = {g.id: Group(g.id, g.name, g.description) for g in rows}
    default = {g.default_for: g.id for g in rows if g.default_for}
    fallback = rows[0].id if rows else None

    def place(group_id, kind):
        return group_id if group_id in by_id else default.get(kind, fallback)

    for c in s.scalars(select(Category).where(Category.role_id != 0).order_by(Category.sort_order, Category.id)):
        gid = place(c.group_id, "category")
        if gid is not None and c.role_id not in skip and c.game_id in live:
            by_id[gid].entries.append(Entry("category", c.id, label(c.game_id, c.name), c.role_id,
                                            (c.emoji or "").strip()))
    for c in s.scalars(select(PfContent).where(PfContent.role_id != 0, PfContent.active.is_(True))
                       .order_by(PfContent.sort_order, PfContent.name)):
        gid = place(c.group_id, "content")
        if gid is not None and c.role_id not in skip and c.game_id in live and c.game_id not in linked:
            by_id[gid].entries.append(Entry("content", c.id, label(c.game_id, c.name), c.role_id,
                                            (c.react_emoji or "").strip()))
    # A linked game's dungeons are another server's: this server's roles for them.
    for g in s.scalars(select(Game).where(Game.id.in_(live), Game.link_home_id.is_not(None))):
        mapped = {r.ref_id: r for r in s.scalars(select(LinkedRole).where(LinkedRole.game_id == g.id,
                                                                          LinkedRole.kind == "content"))}
        for c in party.contents(s, g.link_home_id):
            r = mapped.get(c.id)
            if r is None or not r.role_id or r.role_id in skip:
                continue
            gid = place(r.group_id, "content")
            if gid is not None:
                by_id[gid].entries.append(Entry("linked", r.id, label(g.id, c.name), r.role_id,
                                                (r.react_emoji or "").strip()))

    out = []
    for g in (by_id[r.id] for r in rows):
        if not g.entries:
            continue
        for chunk in _chunks(g.entries):
            used = {e.emoji for e in chunk if e.emoji}
            pool = (x for x in AUTO_EMOJI if x not in used)
            kept, seen = [], set()
            for e in chunk:
                if not e.emoji:
                    e.emoji, e.auto = next(pool, ""), True
                if e.emoji and e.emoji not in seen:  # emojis must be unique within a message
                    seen.add(e.emoji)
                    kept.append(e)
            g.chunks.append(kept)
        g.entries = [e for c in g.chunks for e in c]
        out.append(g)
    return out


def _chunks(rows: list[Entry]) -> list[list[Entry]]:
    return [rows[i:i + PER_MESSAGE] for i in range(0, len(rows), PER_MESSAGE)]


def messages(s: Session, guild_id: int, skip: set[int] | None = None) -> list[tuple[str, list[Entry]]]:
    """(text, entries) for every reaction message, in posting order."""
    out = []
    for g in build(s, guild_id, skip):
        for i, chunk in enumerate(g.chunks):
            head = [f"## {g.name}"] + ([g.description] if g.description else []) if i == 0 \
                else [f"## {g.name} (continued)"]
            out.append(("\n".join(head + [f"{e.emoji} {e.label}" for e in chunk]), chunk))
    return out


def entries(s: Session, guild_id: int) -> list[Entry]:
    return [e for g in build(s, guild_id) for e in g.entries]


def ping_role_ids(s: Session, guild_id: int) -> set[int]:
    games = select(Game.id).where(Game.guild_id == guild_id)
    ids = {c.role_id for c in s.scalars(select(Category).where(Category.role_id != 0, Category.game_id.in_(games)))}
    ids |= {c.role_id for c in party.server_contents(s, guild_id, False) if c.role_id}
    return ids
