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
