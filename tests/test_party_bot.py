"""Party finder reconciler against a fake Discord channel."""
import asyncio

from sqlalchemy import select

from nyerk import party, party_bot
from nyerk.db import get_settings, get_state
from nyerk.models import PfClass, PfContent, PfParty

from .test_bot import FakeChannel
from .test_party import T, reg, who


class StubBot:
    def __init__(self, channel):
        self.channel = channel

    async def _channel(self, _cid):
        return self.channel


def setup(s):
    for i, c in enumerate(party.classes(s)):
        c.role_id = 1001 + i
    get_settings(s).party_channel_id = 1
    s.add(PfContent(name="Fire Temple", min_gear_score=1600))
    s.flush()
    return s.scalar(select(PfContent)).id


def contents(ch):
    return [m.content for m, _ in ch.sent]


def test_listing_ping_fill_ready_reopen(s, monkeypatch):
    cid = setup(s)
    monkeypatch.setattr(party_bot, "now", lambda: T + 5)
    b = {x.label: x.id for x in party.brackets(s)}
    reg(s, who(s, 11, "Cleric"), b["1600-1899"], party.windows(s)[0].id, [cid], T)
    p = party.create(s, who(s, 1, "Templar"), cid, 5, "default", "", "", T)
    pid = p.id
    s.commit()
    ch = FakeChannel()
    pf = party_bot.PartyFinder(StubBot(ch))

    asyncio.run(pf.sync())
    listing, ping = ch.sent[0], ch.sent[1]
    assert listing[0].content.startswith("**[Party] Fire Temple**")
    assert listing[1].users is False and listing[1].roles is False  # listing mentions nobody
    assert ping[0].content.startswith("<@11>\nA party opened for **Fire Temple** (min gear 1600)")
    assert [u.id for u in ping[1].users] == [11]
    assert {c.custom_id for c in ping[0].view.children} == {f"nyerk:pf:join:{pid}", f"nyerk:pf:pass:{pid}"}

    asyncio.run(pf.sync())
    assert len(ch.sent) == 2  # nothing changed, nothing posted

    p = s.get(PfParty, pid)
    for uid, cls in ((11, "Cleric"), (4, "Ranger"), (5, "Ranger")):
        party.join(s, p, who(s, uid, cls), T + 10)
    s.commit()
    asyncio.run(pf.sync())
    assert ch.edited and "Kai" not in ch.edited[-1][1]  # roster edit in place
    party.join(s, s.get(PfParty, pid), who(s, 6, "Sorcerer"), T + 20)
    s.commit()
    asyncio.run(pf.sync())
    assert listing[0].id in ch.deleted and ping[0].id in ch.deleted
    ready = ch.sent[-1]
    assert "is full and ready" in ready[0].content and sorted(u.id for u in ready[1].users) == [1, 4, 5, 6, 11]

    party.remove_member(s, s.get(PfParty, pid), 6, T + 30)
    s.commit()
    asyncio.run(pf.sync())
    assert ready[0].id in ch.deleted
    relisted = ch.sent[-1]
    assert relisted[0].content.startswith("**[Party]") and relisted[0].id != listing[0].id
    assert not any(c.startswith("<@11>\nA party") for c in contents(ch)[3:])  # no re-ping


def test_warning_mentions_only_leader(s, monkeypatch):
    cid = setup(s)
    p = party.create(s, who(s, 1, "Templar"), cid, 5, "none", "", "", T)
    party.tick(s, T + 50 * 60)
    s.commit()
    ch = FakeChannel()
    asyncio.run(party_bot.PartyFinder(StubBot(ch)).sync())
    warn = next(x for x in ch.sent if "closes" in x[0].content and x[0].content.startswith("<@1>"))
    assert [u.id for u in warn[1].users] == [1]
    assert {c.custom_id for c in warn[0].view.children} == {f"nyerk:pf:extend:{p.id}", f"nyerk:pf:close:{p.id}"}


def test_post_panels_in_order(s, monkeypatch):
    setup(s)
    s.commit()
    ch = FakeChannel()
    asyncio.run(party_bot.PartyFinder(StubBot(ch)).post_panels("all"))
    texts = contents(ch)
    assert [t.splitlines()[0] for t in texts] == [
        "## AION 2 Classes", "## Gear score", "## Looking for a group?", "## Start a party", "## Party status"]
    class_ids = [c.custom_id for c in ch.sent[0][0].view.children]
    assert len(class_ids) == 10 and class_ids[-2:] == ["nyerk:pf:class:mine", "nyerk:pf:class:clear"]
    st = get_state(s)
    s.refresh(st)
    assert st.class_panel_message_id == ch.sent[0][0].id and st.party_status_message_id == ch.sent[4][0].id


def test_new_party_pings_dungeon_role(s, monkeypatch):
    cid = setup(s)
    s.get(PfContent, cid).role_id = 4242
    party.create(s, who(s, 1, "Templar"), cid, 5, "none", "", "", T)
    s.commit()
    ch = FakeChannel()
    monkeypatch.setattr(party_bot, "now", lambda: T + 5)
    asyncio.run(party_bot.PartyFinder(StubBot(ch)).sync())
    ping = ch.sent[1]
    assert ping[0].content.startswith("<@&4242>\nA party opened for **Fire Temple**")
    assert [r.id for r in ping[1].roles] == [4242] and ping[1].users == []


def test_create_form_defaults_to_open(s):
    async def build():
        return party_bot.CreateModal([("Fire Temple", "1")]).to_dict()
    req = asyncio.run(build())["components"][2]["component"]
    assert [o["value"] for o in req["options"] if o["default"]] == ["none"]
    assert req["options"][0]["label"].startswith("Open")


class ChatMessage:
    def __init__(self, ch, author_id, age, pinned=False):
        from datetime import datetime, timezone
        self.ch, self.id = ch, author_id * 1000 + age
        self.author = type("A", (), {"id": author_id})()
        self.created_at = datetime.fromtimestamp(T - age, timezone.utc)
        self.pinned = pinned

    async def delete(self):
        self.ch.single.append(self.id)


class ChatChannel:
    def __init__(self, messages):
        self.messages, self.bulk, self.single, self.before = messages, [], [], None

    def history(self, limit, before):
        self.before = before

        async def gen():
            for m in self.messages:
                if m.created_at < before:
                    yield m
        return gen()

    async def delete_messages(self, msgs):
        self.bulk += [m.id for m in msgs]


def test_chat_cleanup_deletes_old_member_messages_only(s, monkeypatch):
    setup(s)
    s.commit()
    ch = ChatChannel([])
    ch.messages = [
        ChatMessage(ch, 5, 20 * 60),             # member, old: bulk delete
        ChatMessage(ch, 5, 5 * 60),              # member, recent: stays
        ChatMessage(ch, 99, 60 * 60),            # Nyerk: stays
        ChatMessage(ch, 6, 30 * 60, pinned=True),  # pinned: stays
        ChatMessage(ch, 7, 20 * 86400),          # too old for bulk: single delete
    ]
    bot = StubBot(ch)
    bot.user = type("U", (), {"id": 99})()
    monkeypatch.setattr(party_bot, "now", lambda: T)
    asyncio.run(party_bot.PartyFinder(bot).clean_chat())
    assert ch.bulk == [5000 + 20 * 60] and ch.single == [7000 + 20 * 86400]
    assert ch.before.timestamp() == T - 15 * 60


def test_chat_cleanup_off_and_status_note(s, monkeypatch):
    setup(s)
    assert "deleted after 15 minutes" in party.status_text([], get_settings(s).party_chat_delete_min)
    get_settings(s).party_chat_delete_min = 0
    s.commit()
    assert "Chat below" not in party.status_text([], 0)
    ch = ChatChannel([])
    bot = StubBot(ch)
    bot.user = type("U", (), {"id": 99})()
    asyncio.run(party_bot.PartyFinder(bot).clean_chat())
    assert ch.before is None


def test_downtime_pauses_party_and_registration_clocks(s, monkeypatch):
    cid = setup(s)
    p = party.create(s, who(s, 1, "Templar"), cid, 5, "none", "", "", T)
    p.warned_at = T + 3000
    s.commit()
    before = p.expires_at
    moved = party.pause_clocks(s, 600, T + 3100)
    assert moved == 1 and p.expires_at == before + 600
    assert p.warned_at is None                 # warning comes back when due again


def test_leader_notes(s):
    cid = setup(s)
    p = party.create(s, who(s, 1, "Templar"), cid, 5, "none", "", "", T)
    party.join(s, p, who(s, 2, "Cleric"), T)
    s.flush()
    assert party.leader_note(p, 2, "user2", "joined").startswith("🟢 user2 (Cleric")
    assert "2/5" in party.leader_note(p, 2, "user2", "joined")
    assert party.leader_note(p, 2, "user2", "left", became_leader=True).endswith("You're the leader now.")
    assert "was removed from" in party.leader_note(p, 2, "user2", "removed")


class Click:
    def __init__(self, uid, channel_id, fail=False):
        self.user = type("U", (), {"id": uid})()
        self.channel_id, self.fail, self.sent = channel_id, fail, []
        click = self

        class Followup:
            async def send(self, text, ephemeral, allowed_mentions):
                if click.fail:
                    import discord
                    raise discord.NotFound(type("R", (), {"status": 404, "reason": "x"})(), "expired")
                click.sent.append((text, ephemeral))
        self.followup = Followup()


def test_leader_gets_hidden_note_only_after_recent_click(s):
    setup(s)
    s.commit()
    bot = StubBot(FakeChannel())
    bot.get_user = bot.fetch_user = lambda uid: (_ for _ in ()).throw(AssertionError("no DMs"))
    finder = party_bot.PartyFinder(bot)
    finder.seen(Click(1, 1))
    asyncio.run(finder.tell_leader(1, "hi"))
    assert finder._last_click[1][0].sent == [("hi", True)]
    other = Click(1, 999)                       # last click was in another channel: nothing
    finder.seen(other)
    asyncio.run(finder.tell_leader(1, "two"))
    assert other.sent == []
    finder.seen(Click(1, 1, fail=True))         # click token expired: nothing, no error
    asyncio.run(finder.tell_leader(1, "three"))
    asyncio.run(finder.tell_leader(2, "four"))  # never clicked: nothing


class OwnMessage:
    def __init__(self, ch, mid, author_id):
        self.ch, self.id, self.pinned = ch, mid, False
        self.author = type("A", (), {"id": author_id})()

    async def delete(self):
        self.ch.deleted.append(self.id)


def test_restart_sweeps_unrecorded_party_messages(s):
    setup(s)
    get_state(s).party_status_message_id = 50
    s.commit()
    ch = FakeChannel()
    msgs = [OwnMessage(ch, 50, 99), OwnMessage(ch, 51, 99), OwnMessage(ch, 52, 7)]

    def history(limit):
        async def gen():
            for m in msgs:
                yield m
        return gen()
    ch.history = history
    bot = StubBot(ch)
    bot.user = type("U", (), {"id": 99})()
    asyncio.run(party_bot.PartyFinder(bot)._sweep_orphans(ch))
    assert ch.deleted == [51]                    # ours and unknown; the panel and the member's message stay


def test_drain_waits_for_sync(s):
    finder = party_bot.PartyFinder(StubBot(FakeChannel()))

    async def run():
        order = []

        async def busy():
            async with finder._sync_lock:
                await asyncio.sleep(0.05)
                order.append("sync done")
        task = asyncio.create_task(busy())
        await asyncio.sleep(0)
        await finder.drain(timeout=2)
        order.append("drained")
        await task
        return order
    assert asyncio.run(run()) == ["sync done", "drained"]
