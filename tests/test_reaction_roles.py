import asyncio
import itertools

from sqlalchemy import select

from nyerk import bot as botmod
from nyerk import reaction_roles
from nyerk.db import get_settings, get_state
from nyerk.models import Category, PfContent

from .test_bot import FakeChannel, FakeMessage


def give_roles(s, n_dungeons=3):
    for i, c in enumerate(s.scalars(select(Category).order_by(Category.sort_order))):
        c.role_id = 100_000_000_000_000_000 + i
    for i in range(n_dungeons):
        s.add(PfContent(name=f"Dungeon {i}", role_id=200_000_000_000_000_000 + i, sort_order=i))
    s.flush()


def test_numbers_in_order_events_then_dungeons(s):
    give_roles(s)
    rows = reaction_roles.entries(s)
    assert [e.emoji for e in rows] == ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣"]
    assert [e.kind for e in rows] == ["category"] * 6 + ["content"] * 3
    text = reaction_roles.message_text(rows, True)
    assert "### Event pings\n1️⃣ World Boss" in text and "### Dungeon pings\n7️⃣ Dungeon 0" in text


def test_custom_emoji_and_letters_after_ten(s):
    give_roles(s, n_dungeons=8)
    s.scalar(select(Category).where(Category.name == "Shugo")).emoji = "🎪"
    rows = reaction_roles.entries(s)
    emojis = [e.emoji for e in rows]
    assert emojis[:3] == ["1️⃣", "🎪", "2️⃣"]
    assert "🔟" in emojis and emojis[-1] == "🇨"  # 13 automatic: 1-10 then A, B, C
    assert len(set(emojis)) == len(emojis)


def test_more_than_twenty_split_across_messages(s):
    give_roles(s, n_dungeons=20)
    chunks = reaction_roles.chunks(reaction_roles.entries(s))
    assert [len(c) for c in chunks] == [20, 6]
    assert reaction_roles.message_text(chunks[1], False).startswith("### Dungeon pings")


class FakeGuild:
    def __init__(self, ids):
        self.ids, self._new = set(ids), itertools.count(300_000_000_000_000_000)
        self.created = []

    def get_role(self, rid):
        return object() if rid in self.ids else None

    async def create_role(self, name, mentionable, reason):
        rid = next(self._new)
        self.created.append((name, mentionable))
        return type("R", (), {"id": rid})()


class ReactingChannel(FakeChannel):
    async def send(self, content, view=None, allowed_mentions=None):
        msg = await super().send(content, view, allowed_mentions)
        msg.reactions = []

        async def add_reaction(e, m=msg):
            m.reactions.append(e)
        msg.add_reaction = add_reaction
        return msg


def test_post_roles_maps_each_message_and_reacts_in_order(s, monkeypatch):
    give_roles(s, n_dungeons=16)
    get_settings(s).roles_channel_id = 1
    s.commit()
    roles = {e.role_id for e in reaction_roles.entries(s)}
    ch = ReactingChannel()
    b = botmod.NyerkBot(0)

    async def fake_channel(_cid):
        return ch

    monkeypatch.setattr(b, "_channel", fake_channel)
    monkeypatch.setattr(b, "_guild", lambda: FakeGuild(roles))
    monkeypatch.setattr(botmod, "REACTION_PAUSE", 0)
    asyncio.run(b.post_roles_message())
    first, second = ch.sent[0][0], ch.sent[1][0]
    assert first.reactions[:3] == ["1️⃣", "2️⃣", "3️⃣"] and len(first.reactions) == 20
    assert second.reactions == ["🇰", "🇱"]  # entries 21 and 22: 1-10, then A-L
    state = get_state(s)
    s.refresh(state)
    assert set(state.reaction_messages) == {str(first.id), str(second.id)}
    assert state.reaction_messages[str(second.id)]["🇱"] == 200_000_000_000_000_015


def test_create_missing_dungeon_roles(s, monkeypatch):
    s.add_all([PfContent(name="Fire Temple"), PfContent(name="Raid", role_id=5)])
    s.commit()
    guild = FakeGuild(set())
    b = botmod.NyerkBot(0)
    monkeypatch.setattr(b, "_guild", lambda: guild)
    ids = [c.id for c in s.scalars(select(PfContent))]
    made = asyncio.run(b.create_content_roles(ids))
    assert made == ["Fire Temple"] and guild.created == [("Fire Temple", True)]
    c = s.scalar(select(PfContent).where(PfContent.name == "Fire Temple"))
    s.refresh(c)
    assert c.role_id == 300_000_000_000_000_000
