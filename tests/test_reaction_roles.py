import asyncio
import itertools

from sqlalchemy import select

from nyerk import bot as botmod
from nyerk import reaction_roles
from nyerk.db import get_settings, get_state
from nyerk.models import Category, PfContent, ReactionGroup

from .test_bot import FakeChannel, FakeMessage


def give_roles(s, n_dungeons=3):
    for i, c in enumerate(s.scalars(select(Category).order_by(Category.sort_order))):
        c.role_id = 100_000_000_000_000_000 + i
    for i in range(n_dungeons):
        s.add(PfContent(name=f"Dungeon {i}", role_id=200_000_000_000_000_000 + i, sort_order=i))
    s.flush()


def test_event_and_dungeon_pings_are_separate_messages(s):
    give_roles(s)
    msgs = reaction_roles.messages(s)
    assert [t.splitlines()[0] for t, _ in msgs] == ["## Event pings", "## Dungeon pings"]
    assert [e.emoji for e in msgs[0][1]] == ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣"]
    assert [e.emoji for e in msgs[1][1]] == ["1️⃣", "2️⃣", "3️⃣"]  # numbering restarts per message
    assert "1️⃣ Dungeon 0" in msgs[1][0]


def test_custom_emoji_and_letters_after_ten(s):
    give_roles(s, n_dungeons=13)
    s.scalar(select(PfContent).where(PfContent.name == "Dungeon 1")).react_emoji = "🔥"
    msgs = reaction_roles.messages(s)
    dungeon = [e.emoji for e in msgs[1][1]]
    assert dungeon[:3] == ["1️⃣", "🔥", "2️⃣"] and dungeon[-1] == "🇧"  # 12 automatic: 1-10, A, B
    assert len(set(dungeon)) == len(dungeon)


def test_custom_role_message_and_split_past_twenty(s):
    give_roles(s, n_dungeons=22)
    siege = ReactionGroup(name="Siege pings", description="", sort_order=5)
    s.add(siege)
    s.flush()
    s.scalar(select(Category).where(Category.name == "Shugo")).group_id = siege.id
    msgs = reaction_roles.messages(s)
    assert [t.splitlines()[0] for t, _ in msgs] == [
        "## Event pings", "## Dungeon pings", "## Dungeon pings (continued)", "## Siege pings"]
    assert [len(c) for _, c in msgs] == [5, 20, 2, 1]
    assert msgs[2][1][0].emoji == "1️⃣"


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


def test_post_roles_channel_order_and_mapping(s, monkeypatch):
    give_roles(s, n_dungeons=3)
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
    firsts = [m.content.splitlines()[0] for m, _ in ch.sent]
    assert firsts == ["# 🎭 Roles", "## AION 2 Classes", "## Event pings", "## Dungeon pings", "## Remove all pings"]
    header, classes, events, dungeons, clear = (m for m, _ in ch.sent)
    assert events.reactions == ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣"] and dungeons.reactions == ["1️⃣", "2️⃣", "3️⃣"]
    assert [c.custom_id for c in clear.view.children] == ["nyerk:roles:clear"]
    state = get_state(s)
    s.refresh(state)
    assert state.reaction_messages[str(dungeons.id)]["1️⃣"] == 200_000_000_000_000_000
    assert state.roles_class_message_id == classes.id and state.roles_clear_message_id == clear.id
    assert state.roles_header_message_id == header.id


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


class FakeResponse:
    def __init__(self):
        self.sent = []

    async def send_message(self, text, ephemeral=False, **_):
        self.sent.append((text, ephemeral))


class FakeMember:
    def __init__(self, role_ids):
        self.id, self.display_name = 77, "Rae"
        self.roles = [type("Role", (), {"id": r})() for r in role_ids]
        self.removed = []

    async def remove_roles(self, *objs, reason=""):
        self.removed.extend(o.id for o in objs)


def test_remove_all_pings_keeps_class_role(s, monkeypatch):
    give_roles(s, n_dungeons=2)
    get_settings(s).roles_channel_id = 1
    get_state(s).reaction_messages = {"555": {"1️⃣": 100_000_000_000_000_000}}
    s.commit()
    member = FakeMember({100_000_000_000_000_000, 200_000_000_000_000_001, 1001})  # 1001 = a class role
    interaction = type("I", (), {"user": member, "response": FakeResponse()})()
    ch = FakeChannel()
    removed_reactions = []

    class Partial:
        def __init__(self, mid):
            self.mid = mid

        async def remove_reaction(self, emoji, who):
            removed_reactions.append((self.mid, emoji, who.id))

    ch.get_partial_message = Partial
    b = botmod.NyerkBot(0)

    async def fake_channel(_cid):
        return ch

    monkeypatch.setattr(b, "_channel", fake_channel)
    asyncio.run(b.clear_pings(interaction))
    assert sorted(member.removed) == [100_000_000_000_000_000, 200_000_000_000_000_001]
    assert interaction.response.sent == [("Removed 2 ping roles.", True)]
    assert removed_reactions == [(555, "1️⃣", 77)]
