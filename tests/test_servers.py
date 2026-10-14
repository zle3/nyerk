"""One Nyerk, several Discord servers: each server's data stays its own,
levels are per server, and servers wait for the host's approval."""
import asyncio
import re
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from nyerk import bot as botmod
from nyerk import db, engine, games, members, party, servers, timers, tracking, transfer
from nyerk.config import Config
from nyerk.db import get_settings, get_state
from nyerk.logbus import bus, for_guild
from nyerk.models import (
    Category, Event, Game, LogEntry, PfContent, PfParty, QuietHours, ReactionGroup, RoleHolder, UserTimer,
)
from nyerk.party import PartyError, Who
from nyerk.seed import seed
from nyerk.web import auth
from nyerk.web.app import create_app, hash_password

from .conftest import ny
from .test_bot import FakeChannel

A, B, WAITING = 910000000000000001, 920000000000000002, 930000000000000003
HOST, OWNER_A, ADMIN_A, MEMBER_AB, OUTSIDER = 1, 2, 3, 4, 5
ADMIN_ROLE_A = 940000000000000004
CH = 950000000000000000  # channel IDs: CH + 101 is A's timer channel, CH + 201 B's


def two_servers(s):
    """Servers A and B, both approved with AION 2; a third one waiting."""
    seed(s, A)
    seed(s, B)
    servers.seen(s, WAITING, "Strangers")
    for gid, base in ((A, 101), (B, 201)):
        g = games.first(s, gid)
        g.timer_channel_id, g.party_channel_id = CH + base, CH + base + 1
        for i, c in enumerate(party.classes(s, g.id)):
            c.role_id = gid + 10 + i
    get_settings(s, A).guild_name, get_settings(s, B).guild_name = "Alpha", "Beta"
    s.flush()
    return games.first(s, A), games.first(s, B)


# -- servers --------------------------------------------------------------------------


def test_waiting_servers_get_nothing_until_approved(s):
    two_servers(s)
    assert {A, B} <= servers.approved_ids(s) and WAITING not in servers.approved_ids(s)
    games.create(s, WAITING, "Theirs", "aion2")
    s.flush()
    assert WAITING not in {g.guild_id for g in games.all_games(s, None)}   # the scheduler's view
    due = engine.due_slots(s, ny(2026, 10, 9, 13, 26), ny(2026, 10, 9, 13, 24))
    assert due and all(s.get(Game, d.occ.event.game_id).guild_id != WAITING for d in due)
    servers.approve(s, WAITING)
    assert WAITING in servers.approved_ids(s)
    assert s.scalar(select(ReactionGroup).where(ReactionGroup.guild_id == WAITING)) is not None


def test_remove_deletes_only_that_server(s):
    a, b = two_servers(s)
    tracking.add_ping(A, 7, "Rae", 555)
    tracking.add_ping(B, 7, "Rae", 666)
    s.commit()
    servers.remove(s, A)
    s.commit()
    assert servers.get(s, A) is None and not games.all_games(s, A)
    assert not s.scalars(select(Event).where(Event.game_id == a.id)).all()
    assert games.first(s, B) and s.scalars(select(Event).where(Event.game_id == b.id)).all()
    assert [r.role_id for r in s.scalars(select(RoleHolder))] == [666]


def test_everyone_role_of_every_server_is_refused(s):
    from nyerk import safety
    two_servers(s)
    for gid in (A, B, WAITING):
        with pytest.raises(ValueError, match="@everyone"):
            safety.check_role(gid)


# -- data stays in its server ---------------------------------------------------------


def test_members_quiet_hours_and_roles_are_per_server(s):
    a, b = two_servers(s)
    tracking.add_ping(A, 7, "Rae", 555)
    tracking.add_ping(B, 7, "Rae in Beta", 666)
    from nyerk import quiet
    quiet.set_hours(s, A, 7, "Rae", 23, 7, "UTC", set())
    s.commit()
    assert [m.name for m in members.known(s, A)] == ["Rae"]
    assert [m.name for m in members.known(s, B)] == ["Rae in Beta"]
    assert quiet.get(s, B, 7) is None and quiet.get(s, A, 7) is not None
    # Forgetting someone in one server leaves the other alone.
    members.forget(s, A, 7, 0)
    s.flush()
    assert not members.known(s, A) and [m.name for m in members.known(s, B)] == ["Rae in Beta"]


def test_parties_and_bosses_stay_in_their_server(s):
    a, b = two_servers(s)
    cleric_a = next(c for c in party.classes(s, a.id) if c.name == "Cleric")
    fire_b = PfContent(game_id=b.id, name="Fire Temple")
    s.add(fire_b)
    s.flush()
    with pytest.raises(PartyError, match="class"):   # A's class role means nothing in B
        party.create(s, Who(7, "Rae", {cleric_a.role_id}), fire_b.id, 5, "none", "", "", 0)
    assert timers.find_boss(s, "Eternal Gartua", A).game_id == a.id
    assert timers.find_boss(s, "Eternal Gartua", B).game_id == b.id
    b_boss = timers.find_boss(s, "Eternal Gartua", B)
    assert timers.find_boss(s, str(b_boss.id), A) is None            # B's boss by ID, asked from A


def test_export_and_import_touch_one_server(s):
    two_servers(s)
    get_settings(s, A).roles_channel_id = 111111111111111111
    get_settings(s, B).roles_channel_id = 222222222222222222
    s.flush()
    data = transfer.export(s, A)
    assert data["settings"]["roles_channel_id"] == "111111111111111111" and len(data["games"]) == 1
    assert "guild_id" not in data["settings"] and "status" not in data["settings"]
    data["settings"]["roles_channel_id"] = "333333333333333333"
    data["games"][0]["settings"]["timer_channel_id"] = "444444444444444444"
    transfer.apply_import(s, data, A)
    assert get_settings(s, A).roles_channel_id == 333333333333333333
    assert get_settings(s, B).roles_channel_id == 222222222222222222
    assert games.first(s, B).timer_channel_id == CH + 201 and games.first(s, A).timer_channel_id == 444444444444444444


def test_log_lines_belong_to_their_server(s):
    two_servers(s)
    s.commit()
    with for_guild(A):
        bus.info("test", "only in Alpha")
    bus.info("test", "host only")
    with db.session() as x:
        rows = {(r.guild_id, r.text) for r in x.scalars(select(LogEntry).where(LogEntry.kind == "test"))}
    assert rows == {(A, "only in Alpha"), (0, "host only")}


# -- Discord --------------------------------------------------------------------------


def test_each_servers_dashboard_and_alerts_go_to_its_channels(s, monkeypatch):
    two_servers(s)
    get_state(s).last_tick_at = ny(2026, 10, 9, 13, 25, 1) - 30
    s.commit()
    channels = {CH + 101: FakeChannel(), CH + 201: FakeChannel()}

    async def run():
        b = botmod.NyerkBot(0)

        async def fake_channel(cid):
            return channels.get(cid)
        monkeypatch.setattr(b, "_channel", fake_channel)
        monkeypatch.setattr(botmod, "now", lambda: ny(2026, 10, 9, 13, 25, 1))
        await b.tick()
    asyncio.run(run())
    for ch in channels.values():
        texts = [m.content for m, _ in ch.sent]
        assert any(t.startswith("Dimensional Invasion starts in 5 minutes") for t in texts)
        assert texts[-1].startswith("━")
    with db.session() as x:
        lines = list(x.scalars(select(LogEntry).where(LogEntry.kind == "alert")))
    assert {r.guild_id for r in lines} == {A, B}             # each alert logged in its own server


def test_slash_commands_refuse_waiting_servers(s):
    two_servers(s)
    s.commit()
    sent = []

    class Resp:
        async def send_message(self, text, **kw):
            sent.append(text)
    b = botmod.NyerkBot(0)
    ok = asyncio.run(b._approved_only(type("I", (), {"guild_id": WAITING, "response": Resp()})()))
    assert ok is False and "host needs to approve" in sent[0]
    assert asyncio.run(b._approved_only(type("I", (), {"guild_id": A, "response": Resp()})())) is True


def test_party_buttons_refuse_another_servers_party(s):
    from nyerk import party_bot
    a, b = two_servers(s)
    fire_b = PfContent(game_id=b.id, name="Fire Temple")
    s.add(fire_b)
    s.flush()
    cleric_b = next(c for c in party.classes(s, b.id) if c.name == "Cleric")
    p = party.create(s, Who(8, "Bo", {cleric_b.role_id}), fire_b.id, 5, "none", "", "", 0)
    s.commit()
    with pytest.raises(PartyError, match="isn't part of this server"):
        party_bot.check_here(s, type("I", (), {"guild_id": A})(), p.game_id)
    party_bot.check_here(s, type("I", (), {"guild_id": B})(), p.game_id)


# -- web page -------------------------------------------------------------------------


class Bot:
    """In A: OWNER_A owns it, ADMIN_A has the admin role, MEMBER_AB is a
    member; in B: MEMBER_AB has Manage Server."""
    roles = {A: {OWNER_A: set(), ADMIN_A: {ADMIN_ROLE_A}, MEMBER_AB: set()}, B: {MEMBER_AB: set()}}

    def is_ready(self):
        return True

    def status(self, guild_id=None):
        return {"connected": True, "user": "Nyerk", "guild": "", "guilds": 2, "latency_ms": 1, "last_tick": 0}

    def server_ids(self):
        return {A, B, WAITING}

    def request_refresh(self, repost=False, game_id=None):
        pass

    async def sync_commands(self, guild_id):
        pass

    async def member_access(self, guild_id, uid):
        if uid not in self.roles.get(guild_id, {}):
            return None
        return f"user{uid}", set(self.roles[guild_id][uid]), (guild_id, uid) == (A, OWNER_A), \
            (guild_id, uid) == (B, MEMBER_AB)

    async def fetch_member(self, guild_id, uid):
        found = await self.member_access(guild_id, uid)
        return (found[0], found[1]) if found else None


@pytest.fixture()
def app(tmp_path, monkeypatch):
    for k, v in {"NYERK_DATA_DIR": str(tmp_path), "WEB_TLS": "off", "PUBLIC_URL": "https://nyerk.example.com",
                 "DISCORD_CLIENT_ID": "1", "DISCORD_CLIENT_SECRET": "x", "OWNER_IDS": str(HOST),
                 "ADMIN_ROLE_IDS": "", "GUILD_ID": ""}.items():
        monkeypatch.setenv(k, v)
    db.init_engine(":memory:")
    with db.session() as s:
        two_servers(s)
        get_settings(s, A).admin_role_ids = str(ADMIN_ROLE_A)
        get_state(s).admin_password_hash = hash_password("pw")
    return create_app(Config(), Bot())


def sign_in(app, uid, monkeypatch):
    async def identify(cfg, code):
        return uid, f"user{uid}"
    monkeypatch.setattr(auth, "discord_identify", identify)
    c = TestClient(app, base_url="https://nyerk.example.com")
    r = c.get("/auth/discord", follow_redirects=False)
    state = re.search(r"state=([^&]+)", r.headers["location"])[1]
    c.get(f"/auth/callback?code=abc&state={state}")
    return c


def csrf(c, path="/me"):
    return re.search(r'name="csrf" value="([^"]+)"', c.get(path).text)[1]


def ids_in(server):
    with db.session() as s:
        g = games.first(s, server)
        return dict(event=s.scalar(select(Event.id).where(Event.game_id == g.id).limit(1)),
                    category=s.scalar(select(Category.id).where(Category.game_id == g.id).limit(1)),
                    group=s.scalar(select(ReactionGroup.id).where(ReactionGroup.guild_id == server).limit(1)),
                    game=g.id)


def test_levels_are_per_server(app, monkeypatch):
    owner = sign_in(app, OWNER_A, monkeypatch)
    assert owner.get("/settings").status_code == 200                # owner of A
    admin = sign_in(app, ADMIN_A, monkeypatch)
    assert admin.get("/events").status_code == 200 and admin.get("/settings").status_code == 403
    both = sign_in(app, MEMBER_AB, monkeypatch)
    # A member in A, an admin (Manage Server) in B: the picker switches between them.
    tok = csrf(both)
    both.post("/server/use", data={"csrf": tok, "guild_id": str(A)})
    assert both.get("/events", follow_redirects=False).status_code == 403
    both.post("/server/use", data={"csrf": tok, "guild_id": str(B)})
    assert both.get("/events").status_code == 200
    # Nobody but the host can switch to a server they aren't in.
    assert admin.post("/server/use", data={"csrf": csrf(admin, "/"), "guild_id": str(B)}).status_code == 404
    assert sign_in(app, OUTSIDER, monkeypatch).get("/me", follow_redirects=False).status_code == 303


def test_an_admin_of_one_server_cant_touch_anothers_rows(app, monkeypatch):
    admin = sign_in(app, OWNER_A, monkeypatch)  # can do anything in A: a 404 here is the server wall
    tok = csrf(admin, "/")
    b = ids_in(B)
    assert admin.get(f"/events/{b['event']}").status_code == 404
    assert admin.post(f"/events/{b['event']}/toggle", data={"csrf": tok}).status_code == 404
    assert admin.post(f"/events/{b['event']}/delete", data={"csrf": tok}).status_code == 404
    assert admin.post(f"/categories/{b['category']}/move", data={"csrf": tok, "dir": "up"}).status_code == 404
    assert admin.post(f"/roles/groups/{b['group']}/delete", data={"csrf": tok}).status_code == 404
    assert admin.post("/game/use", data={"csrf": tok, "game_id": str(b["game"])}).status_code == 404
    assert "Shugo Festival" in admin.get(f"/events?game={b['game']}").text   # ?game= of another server is ignored
    with db.session() as s:
        assert s.get(Event, b["event"]).enabled and s.get(Event, b["event"]) is not None


def test_pages_show_only_this_server(app, monkeypatch):
    with db.session() as s:
        tracking.add_ping(A, 77, "Alpha person", 555)
        tracking.add_ping(B, 88, "Beta person", 666)
        with for_guild(B):
            bus.info("test", "Beta's secret log line")
    admin = sign_in(app, ADMIN_A, monkeypatch)
    page = admin.get("/members").text
    assert "Alpha person" in page and "Beta person" not in page
    assert "Beta&#39;s secret" not in admin.get("/logs").text and "secret log line" not in admin.get("/logs").text
    assert not [x for x in admin.get("/search/suggest?q=beta person").json() if x["page"] == "Member"]
    exported = admin.get("/settings/export")
    assert exported.status_code == 403                                 # owner-only by default anyway
    owner = sign_in(app, OWNER_A, monkeypatch)
    data = owner.get("/settings/export").json()
    assert data["settings"]["roles_channel_id"] == "" and len(data["games"]) == 1


def test_host_sees_every_server_and_approves_new_ones(app, monkeypatch):
    host = sign_in(app, HOST, monkeypatch)
    page = host.get("/servers").text
    assert "Alpha" in page and "Beta" in page and "Strangers" in page and "waiting" in page
    host.post(f"/servers/{WAITING}/approve", data={"csrf": csrf(host, "/servers")})
    with db.session() as s:
        assert servers.is_approved(s, WAITING)
    owner = sign_in(app, OWNER_A, monkeypatch)
    assert owner.get("/servers").status_code == 403                   # a server owner isn't the host
    tok = csrf(host, "/servers")
    host.post(f"/servers/{B}/remove", data={"csrf": tok, "confirm": "wrong"})
    with db.session() as s:
        assert servers.get(s, B) is not None
    host.post(f"/servers/{B}/remove", data={"csrf": tok, "confirm": "beta"})
    with db.session() as s:
        assert servers.get(s, B) is None and games.first(s, A) is not None


def test_access_switches_are_per_server(app, monkeypatch):
    owner = sign_in(app, OWNER_A, monkeypatch)
    owner.post("/access/lock", data={"csrf": csrf(owner, "/access"), "lock": "1"})
    admin_a = sign_in(app, ADMIN_A, monkeypatch)
    assert admin_a.get("/events", follow_redirects=False).status_code == 403   # locked in A
    both = sign_in(app, MEMBER_AB, monkeypatch)
    both.post("/server/use", data={"csrf": csrf(both), "guild_id": str(B)})
    assert both.get("/events").status_code == 200                              # B isn't locked


# -- upgrade --------------------------------------------------------------------------


def test_pre_servers_install_is_claimed_by_its_server(tmp_path):
    path = tmp_path / "nyerk.db"
    shutil.copy(Path(__file__).parent / "fixtures" / "pre_games.db", path)
    db.init_engine(path)
    assert (tmp_path / "nyerk-before-v2.db").exists()
    with db.session() as s:
        assert servers.get(s, 0) is not None and servers.claim_legacy(s, A, "Alpha")
        assert not servers.claim_legacy(s, B)                       # only once
        assert get_state(s).admin_password_hash                     # the password moved to the host
        g = games.first(s, A)
        assert g.timer_channel_id == 920000000000000001
        assert s.scalar(select(func.count()).select_from(RoleHolder).where(RoleHolder.guild_id == A)) == 60
        assert s.scalar(select(func.count()).select_from(QuietHours).where(QuietHours.guild_id == 0)) == 0
        assert servers.is_approved(s, A) and get_settings(s, A).guild_name == "Alpha"


def test_form_fields_cant_reach_another_server(app, monkeypatch):
    owner = sign_in(app, OWNER_A, monkeypatch)
    tok = csrf(owner, "/")
    with db.session() as s:
        b_game = games.first(s, B)
        b_bracket = party.brackets(s, b_game.id)[0].id
        a_game = games.first(s, A)
    r = owner.post(f"/members/{MEMBER_AB}/gear", data={"csrf": tok, "game_id": str(b_game.id),
                                                       "bracket": str(b_bracket)})
    assert r.status_code == 404
    r = owner.post(f"/members/{MEMBER_AB}/gear", data={"csrf": tok, "game_id": str(a_game.id),
                                                       "bracket": str(b_bracket)})
    assert r.status_code == 404                                    # B's bracket under A's game
    assert owner.post(f"/members/{MEMBER_AB}/registration/cancel",
                      data={"csrf": tok, "game_id": str(b_game.id)}).status_code == 404
    with db.session() as s:
        assert not s.scalars(select(party.MemberGear)).all()


def test_roles_assignments_only_change_this_servers_roles(app, monkeypatch):
    owner = sign_in(app, OWNER_A, monkeypatch)
    tok = csrf(owner, "/categories")
    b = ids_in(B)
    with db.session() as s:
        a_group = s.scalar(select(ReactionGroup.id).where(ReactionGroup.guild_id == A).limit(1))
    owner.post("/roles/assign", data={"csrf": tok, f"group_category_{b['category']}": str(a_group),
                                      f"emoji_category_{b['category']}": "💀"})
    with db.session() as s:
        cat = s.get(Category, b["category"])
        assert cat.emoji != "💀" and cat.group_id != a_group


def test_history_and_quiet_hours_lists_are_per_server(app, monkeypatch):
    from nyerk import quiet
    from nyerk.models import ConfigChange
    with db.session() as s:
        s.add(ConfigChange(guild_id=B, at=0, user_name="someone in Beta", summary="Beta's change", before={}))
        quiet.set_hours(s, B, 88, "Beta sleeper", 23, 7, "UTC", set())
    owner = sign_in(app, OWNER_A, monkeypatch)
    assert "Beta" not in owner.get("/history").text
    assert "Beta sleeper" not in owner.get("/party").text
