"""The reaction roles message(s) in the roles channel.

Event ping categories and dungeon (party finder content) roles share one
list. Each entry uses its own emoji if set, otherwise the next free emoji
from 1️⃣ 2️⃣ 3️⃣ … 🔟 then 🇦 🇧 🇨 …. Discord allows 20 different reactions
per message, so longer lists are split across several messages.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Category, PfContent

NUMBERS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
LETTERS = [chr(0x1F1E6 + i) for i in range(26)]  # 🇦 … 🇿
AUTO_EMOJI = NUMBERS + LETTERS
PER_MESSAGE = 20  # Discord's limit of distinct reactions on one message

SECTIONS = {"category": "Event pings", "content": "Dungeon pings"}


@dataclass
class Entry:
    kind: str       # category | content
    ref_id: int
    label: str
    role_id: int
    emoji: str = ""
    auto: bool = False


def entries(s: Session) -> list[Entry]:
    rows: list[Entry] = []
    for c in s.scalars(select(Category).where(Category.role_id != 0).order_by(Category.sort_order, Category.id)):
        rows.append(Entry("category", c.id, c.name, c.role_id, (c.emoji or "").strip()))
    for c in s.scalars(select(PfContent).where(PfContent.role_id != 0, PfContent.active.is_(True))
                       .order_by(PfContent.sort_order, PfContent.name)):
        rows.append(Entry("content", c.id, c.name, c.role_id, (c.react_emoji or "").strip()))
    used = {e.emoji for e in rows if e.emoji}
    pool = (x for x in AUTO_EMOJI if x not in used)
    for e in rows:
        if not e.emoji:
            e.emoji, e.auto = next(pool, ""), True
    # Two entries with the same custom emoji would clash; keep the first.
    seen: set[str] = set()
    out = []
    for e in rows:
        if e.emoji and e.emoji not in seen:
            seen.add(e.emoji)
            out.append(e)
    return out


def chunks(rows: list[Entry]) -> list[list[Entry]]:
    return [rows[i:i + PER_MESSAGE] for i in range(0, len(rows), PER_MESSAGE)] or []


def message_text(rows: list[Entry], first: bool) -> str:
    lines = ["## Ping roles", "React to get pinged. Remove your reaction to stop."] if first else []
    section = None
    for e in rows:
        if e.kind != section:
            section = e.kind
            lines.append(f"### {SECTIONS[section]}")
        lines.append(f"{e.emoji} {e.label}")
    return "\n".join(lines)
