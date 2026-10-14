"""Linked party finders: two servers sharing one party finder, each with
its own roles, channel, pings and members."""
import asyncio

import pytest
from sqlalchemy import select

from nyerk import db, games, linking, members, party, party_bot
from nyerk.models import Game, LinkedRole, PartyPost, PfClass, PfContent, PfInterest, PfParty
from nyerk.party import PartyError, Who

from .test_bot import FakeChannel
from .test_party import T
from .test_servers import A, B, CH, HOST, WAITING, app, csrf, sign_in, two_servers  # noqa: F401

FIRE_A, FIRE_B = 7100, 7200          # each server's Fire Temple ping role
B_CLASS = 7300                       # B's class roles: 7300 + class index
B_TANK, B_SUPPORT, B_DPS = 7401, 7402, 7403


def linked(s):
    """A's AION 2 party finder, shared with B's game; B maps every class and
    Fire Temple to its own roles."""
    a, b = two_servers(s)
    fire = PfContent(game_id=a.id, name="Fire Temple", role_id=FIRE_A)
    s.add(fire)
    s.flush()
    linking.join(s, b, linking.new_code(s, a, now=T), now=T)
    b.tank_role_id, b.support_role_id, b.dps_role_id = B_TANK, B_SUPPORT, B_DPS
    for i, c in enumerate(party.classes(s, a.id)):
        linking.set_role(s, b, "class", c.id, B_CLASS + i)
    linking.set_role(s, b, "content", fire.id, FIRE_B)
    s.flush()
    return a, b, fire


def cls(s, game, name):
    """(class, this server's role for it)."""
    c = next(x for x in party.classes_for(s, game) if x.name == name)
    return c, c.role_id


def who(s, game, uid, name):
    return Who(uid, f"user{uid}", {cls(s, game, name)[1]}, game.id)


def sign_up(s, game, uid, name, content_ids, now=T):
    """Gear score first (it's the home's brackets), then register."""
    pf = party.pf_id(game)
    party.set_gear(s, uid, f"user{uid}", party.brackets(s, pf)[-1].id, now, pf, guild_id=game.guild_id)
    return party.register(s, who(s, game, uid, name), party.windows(s, pf)[0].id, content_ids, now)


class Channels:
    """A bot whose channels are fakes, one per channel ID."""

    def __init__(self):
        self.by_id: dict[int, FakeChannel] = {}

    async def _channel(self, cid):
        return self.by_id.setdefault(cid, FakeChannel()) if cid else None


def test_link_codes(s):
    a, b = two_servers(s)
    code = linking.new_code(s, a, now=T)
    with pytest.raises(linking.LinkError, match="expired"):
        linking.join(s, b, code, now=T + linking.CODE_TTL + 1)
    with pytest.raises(linking.LinkError, match="wrong"):
        linking.join(s, b, "NYERK-nope", now=T)
    other = games.create(s, A, "Second", "custom")
    s.flush()
    with pytest.raises(linking.LinkError, match="in this server"):
        linking.join(s, other, code, now=T)
    assert linking.join(s, b, code, now=T).id == a.id and b.link_home_id == a.id
    with pytest.raises(linking.LinkError, match="another server's party finder"):
        linking.new_code(s, b, now=T)  # no chains
    c = games.create(s, WAITING, "Theirs", "aion2")
    s.flush()
    with pytest.raises(linking.LinkError, match="approved"):
        linking.join(s, a, linking.new_code(s, c, now=T), now=T)


def test_joining_closes_own_parties_and_sign_ups(s):
    a, b = two_servers(s)
    fire_b = PfContent(game_id=b.id, name="Fire Temple")
    s.add(fire_b)
    s.flush()
    cleric = next(c for c in party.classes(s, b.id) if c.name == "Cleric")
    p = party.create(s, Who(8, "Bo", {cleric.role_id}), fire_b.id, 5, "none", "", "", T)
    party.set_gear(s, 9, "Cy", party.brackets(s, b.id)[0].id, T)
    reg = party.register(s, Who(9, "Cy", {cleric.role_id}), party.windows(s, b.id)[0].id, [fire_b.id], T)
    linking.join(s, b, linking.new_code(s, a, now=T), now=T)
    assert p.status == "closed" and reg.status == "cancelled"


def test_classes_use_this_servers_roles(s):
    a, b, _ = linked(s)
    assert [c.name for c in party.classes_for(s, b)] == [c.name for c in party.classes(s, a.id)]
    cleric, role = cls(s, b, "Cleric")
    add, remove, text, held = party.class_change(s, Who(5, "Bo", set(), b.id), cleric.id, b.id)
    assert add == {role, B_SUPPORT} and remove == set() and "Added Cleric" in text
    assert [c.role_id for c in held] == [role]
    assert party.member_classes(s, b.id, {role}) == [cleric]
    # A's own role for Cleric means nothing in B.
    assert party.member_classes(s, b.id, {s.get(PfClass, cleric.id).role_id}) == []
    # A class of a third, unlinked game is refused.
    own_b = s.scalar(select(PfClass).where(PfClass.game_id == b.id))
    with pytest.raises(PartyError, match="isn't part of this game"):
        party.class_change(s, Who(5, "Bo", set(), b.id), own_b.id, b.id)


def test_clicks_from_a_linked_server_are_allowed(s):
    a, b, fire = linked(s)
    p = party.create(s, who(s, a, 1, "Templar"), fire.id, 5, "none", "", "", T)
    s.commit()
    assert party_bot.check_here(s, type("I", (), {"guild_id": B})(), p.game_id).id == b.id
    assert party_bot.check_here(s, type("I", (), {"guild_id": A})(), p.game_id).id == a.id
    with pytest.raises(PartyError):
        party_bot.check_here(s, type("I", (), {"guild_id": WAITING})(), p.game_id)


def test_pings_and_mentions_stay_in_each_server(s, monkeypatch):
    a, b, fire = linked(s)
    for g, uid in ((a, 11), (b, 21)):
        r = sign_up(s, g, uid, "Cleric", [fire.id])
        assert r.game_id == a.id and r.origin_game_id == g.id
    p = party.create(s, who(s, a, 1, "Templar"), fire.id, 5, "none", "", "", T)
    pid = p.id
    s.commit()
    monkeypatch.setattr(party_bot, "now", lambda: T + 5)
    bot = Channels()
    pf = party_bot.PartyFinder(bot)
    asyncio.run(pf.sync())
    ch_a, ch_b = bot.by_id[a.party_channel_id], bot.by_id[b.party_channel_id]
    ping_a = next(x for x in ch_a.sent if "A party opened" in x[0].content)
    ping_b = next(x for x in ch_b.sent if "A party opened" in x[0].content)
    assert ping_a[0].content.startswith(f"<@&{FIRE_A}> <@11>") and [u.id for u in ping_a[1].users] == [11]
    assert ping_b[0].content.startswith(f"<@&{FIRE_B}> <@21>") and [u.id for u in ping_b[1].users] == [21]
    assert [r.id for r in ping_b[1].roles] == [FIRE_B]
    assert "[Alpha]" in ch_b.sent[0][0].content  # listings show each member's server
    s.expire_all()
    assert not s.get(PfParty, pid).ping_pending

    # Joining from B, then filling: each server's ready ping mentions its own members.
    for uid, g, name in ((21, b, "Cleric"), (12, a, "Ranger"), (22, b, "Ranger"), (23, b, "Sorcerer")):
        party.join(s, s.get(PfParty, pid), who(s, g, uid, name), T + 10)
    s.commit()
    asyncio.run(pf.sync())
    ready_a = ch_a.sent[-1]
    ready_b = ch_b.sent[-1]
    assert "full and ready" in ready_a[0].content and sorted(u.id for u in ready_a[1].users) == [1, 12]
    assert "full and ready" in ready_b[0].content and sorted(u.id for u in ready_b[1].users) == [21, 22, 23]
    # The party left both channels' listings.
    assert ch_a.sent[0][0].id in ch_a.deleted and ch_b.sent[0][0].id in ch_b.deleted


def test_warning_goes_to_the_leaders_server(s, monkeypatch):
    a, b, fire = linked(s)
    p = party.create(s, who(s, b, 21, "Templar"), fire.id, 5, "none", "", "", T)
    party.tick(s, T + 50 * 60)
    s.commit()
    bot = Channels()
    asyncio.run(party_bot.PartyFinder(bot).sync())
    warn = [x for x in bot.by_id[b.party_channel_id].sent if x[0].content.startswith("<@21> your")]
    assert len(warn) == 1 and f"nyerk:pf:extend:{p.id}" in {c.custom_id for c in warn[0][0].view.children}
    assert not any(x[0].content.startswith("<@21> your") for x in bot.by_id[a.party_channel_id].sent)


def test_new_linked_server_shows_parties_without_pinging(s, monkeypatch):
    a, b = two_servers(s)
    fire = PfContent(game_id=a.id, name="Fire Temple", role_id=FIRE_A)
    s.add(fire)
    s.flush()
    party.create(s, Who(1, "Al", {next(c for c in party.classes(s, a.id) if c.name == "Templar").role_id}),
                 fire.id, 5, "none", "", "", T)
    s.commit()
    bot = Channels()
    pf = party_bot.PartyFinder(bot)
    asyncio.run(pf.sync())
    linking.join(s, b, linking.new_code(s, a, now=T), now=T)
    linking.set_role(s, b, "content", fire.id, FIRE_B)
    s.commit()
    asyncio.run(pf.sync())
    sent_b = [m.content for m, _ in bot.by_id[b.party_channel_id].sent]
    assert sent_b and sent_b[0].startswith("**[Party] Fire Temple**")
    assert not any("A party opened" in t for t in sent_b)


def test_unlink_clears_the_channel_and_sign_ups(s, monkeypatch):
    a, b, fire = linked(s)
    party.create(s, who(s, a, 1, "Templar"), fire.id, 5, "none", "", "", T)
    r = sign_up(s, b, 21, "Cleric", [fire.id])
    s.commit()
    bot = Channels()
    pf = party_bot.PartyFinder(bot)
    asyncio.run(pf.sync())
    listing_b = bot.by_id[b.party_channel_id].sent[0][0].id
    linking.unlink(s, b)
    s.commit()
    asyncio.run(pf.sync())
    assert listing_b in bot.by_id[b.party_channel_id].deleted
    assert s.get(PfInterest, r.id).status == "cancelled" and b.link_home_id is None
    assert not s.scalars(select(PartyPost).where(PartyPost.game_id == b.id)).all()


def test_members_pages_show_only_this_servers_people(s):
    a, b, fire = linked(s)
    p = party.create(s, who(s, a, 1, "Templar"), fire.id, 5, "none", "", "", T)
    party.join(s, p, who(s, b, 21, "Cleric"), T)
    sign_up(s, b, 22, "Ranger", [fire.id])
    s.commit()
    in_b = {m.user_id: m for m in members.known(s, B)}
    in_a = {m.user_id: m for m in members.known(s, A)}
    assert set(in_b) == {21, 22} and in_b[21].in_party and in_b[22].registered
    assert set(in_a) == {1}
    assert [x.id for x in party.live_parties_for(s, 21, B)] == [p.id] and party.live_parties_for(s, 21, A) == []
    assert {FIRE_B} <= {o.role_id for o in members.role_options(s, B)}


def test_deleting_the_home_game_unlinks(s):
    a, b, fire = linked(s)
    s.commit()
    s.execute(Game.__table__.delete().where(Game.id == a.id))
    s.commit()
    s.expire_all()
    b = s.get(Game, b.id)
    assert b.link_home_id is None and party.pf_id(b) == b.id


# -- web --------------------------------------------------------------------------------


def test_link_from_the_games_page(app, monkeypatch):  # noqa: F811
    host = sign_in(app, HOST, monkeypatch)
    host.post("/server/use", data={"csrf": csrf(host, "/"), "guild_id": str(A)})
    with db.session() as s:
        a, b = games.first(s, A).id, games.first(s, B).id
    host.post(f"/games/{a}/link/code", data={"csrf": csrf(host, "/games")})
    with db.session() as s:
        code = s.get(Game, a).link_code
    assert code and code in host.get("/games").text
    host.post("/server/use", data={"csrf": csrf(host, "/"), "guild_id": str(B)})
    r = host.post(f"/games/{b}/link/join", data={"csrf": csrf(host, "/games"), "code": code})
    assert "Shared party finder" in r.text
    with db.session() as s:
        assert s.get(Game, b).link_home_id == a
        cleric = s.scalar(select(PfClass).where(PfClass.game_id == a, PfClass.name == "Cleric"))
    host.post("/party/linked", data={"csrf": csrf(host, "/party"), f"role_class_{cleric.id}": "960000000000000001"})
    with db.session() as s:
        row = s.scalar(select(LinkedRole).where(LinkedRole.game_id == b, LinkedRole.ref_id == cleric.id))
        assert row.role_id == 960000000000000001
    host.post(f"/games/{b}/link/unlink", data={"csrf": csrf(host, "/games")})
    with db.session() as s:
        assert s.get(Game, b).link_home_id is None


def test_an_admin_cant_join_with_a_code(app, monkeypatch):  # noqa: F811
    from .test_servers import MEMBER_AB
    with db.session() as s:
        a, b = games.first(s, A), games.first(s, B)
        code, bid = linking.new_code(s, a), b.id
    admin_b = sign_in(app, MEMBER_AB, monkeypatch)
    admin_b.post("/server/use", data={"csrf": csrf(admin_b, "/"), "guild_id": str(B)})
    r = admin_b.post(f"/games/{bid}/link/join", data={"csrf": csrf(admin_b, "/"), "code": code})
    assert r.status_code in (403, 404) or "owner" in r.text.lower()
    with db.session() as s:
        assert s.get(Game, bid).link_home_id is None


def test_upgrade_moves_party_messages_to_posts(tmp_path):
    import shutil
    import sqlite3
    from pathlib import Path
    path = tmp_path / "nyerk.db"
    shutil.copy(Path(__file__).parent / "fixtures" / "pre_games.db", path)
    db.init_engine(path)
    db.engine().dispose()
    # Back to how version 2 kept a party's messages: on the party row.
    raw = sqlite3.connect(path)
    with raw:
        for col in ("listing_message_id", "ping_message_id", "warning_message_id", "ready_message_id"):
            raw.execute(f"ALTER TABLE pf_parties ADD COLUMN {col} BIGINT NOT NULL DEFAULT 0")
        raw.execute("ALTER TABLE pf_parties ADD COLUMN listing_hash VARCHAR(64) NOT NULL DEFAULT ''")
        raw.execute("ALTER TABLE pf_parties ADD COLUMN ready_pinged BOOLEAN NOT NULL DEFAULT 0")
        raw.execute("UPDATE pf_parties SET status = 'open', listing_message_id = 555, origin_game_id = NULL "
                    "WHERE id = 1")
        raw.execute("DELETE FROM party_posts")
        seats = raw.execute("SELECT COUNT(*) FROM pf_party_members").fetchone()[0]
        raw.execute("PRAGMA user_version = 2")
    raw.close()
    (tmp_path / "nyerk-before-v3.db").unlink()
    db.init_engine(path)
    assert (tmp_path / "nyerk-before-v3.db").exists()
    with db.engine().connect() as conn:
        assert conn.exec_driver_sql("PRAGMA user_version").scalar() == 3
        cols = [r[1] for r in conn.exec_driver_sql("PRAGMA table_info(pf_parties)")]
        assert "listing_message_id" not in cols and "origin_game_id" in cols
    with db.session() as s:
        post = s.scalar(select(PartyPost).where(PartyPost.party_id == 1))
        assert (post.listing_message_id, post.pinged) == (555, True)  # an old party never pings again
        assert s.get(PfParty, 1).origin_game_id == post.game_id
        from nyerk.models import PfPartyMember
        assert seats and len(s.scalars(select(PfPartyMember)).all()) == seats  # rebuilding kept every seat
        g = s.scalar(select(Game))
        content = PfContent(game_id=g.id, name="Linked test")
        s.add(content)
        s.flush()
        cleric = s.scalar(select(PfClass).where(PfClass.game_id == g.id, PfClass.name == "Cleric"))
        cleric.role_id = 1234
        p = party.create(s, Who(1, "Al", {1234}), content.id, 5, "none", "", "", T)  # the rebuilt table takes rows
        assert p.origin_game_id == g.id
