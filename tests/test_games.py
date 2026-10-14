"""Several games in one server: setup, the Games page, and each game's
timers, roles and party finder staying apart."""
import asyncio
import re
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from nyerk import bot as botmod
from nyerk import db, engine, games, members, party, reaction_roles, timers
from nyerk.config import Config
from nyerk.db import get_settings, get_state
from nyerk.models import (
    Category, Event, Game, MemberGear, PfBracket, PfClass, PfContent, PfInterest, PfParty, PfWindow, UserTimer,
)
from nyerk.party import PartyError, Who
from nyerk.seed import seed_reaction_groups
from nyerk.web.app import create_app, hash_password

from .conftest import ny, GUILD, G  # noqa: F401
from .test_bot import FakeChannel

FIXTURES = Path(__file__).parent / "fixtures"


def two_games(s) -> tuple[Game, Game]:
    """AION 2 (from the conftest seed) plus a custom game with a class, a
    bracket, a dungeon and a boss of its own; same names as AION 2's on purpose."""
    aion = games.first(s, GUILD)
    tl = games.create(s, GUILD, "Throne and Liberty", "custom")
    cat = Category(game_id=tl.id, name="World Boss", role_id=700000000000000001)
    s.add_all([cat, PfClass(game_id=tl.id, name="Cleric", class_type="support", role_id=700000000000000002),
               PfBracket(game_id=tl.id, label="2100+", min_score=2100, max_score=9999),
               PfContent(game_id=tl.id, name="Fire Temple", role_id=700000000000000003)])
    s.flush()
    s.add(Event(game_id=tl.id, category_id=cat.id, name="Morokai", schedule_type="kill_report",
                schedule_params={}, respawn_minutes=60, slots=[]))
    aion.timer_channel_id, aion.party_channel_id = 101, 102
    tl.timer_channel_id, tl.party_channel_id = 201, 202
    for i, c in enumerate(party.classes(s, aion.id)):
        c.role_id = 600000000000000000 + i
    s.flush()
    return aion, tl


# -- the games themselves ------------------------------------------------------------


def test_templates(s):
    aion = games.first(s, GUILD)
    assert aion.kind == "aion2" and aion.dashboard_title == "⏱️ AION 2 Timers"
    tl = games.create(s, GUILD, "Throne and Liberty", "custom")
    assert tl.dashboard_title == "⏱️ Throne and Liberty Timers" and tl.dashboard_boss_note == ""
    assert not engine.load_events(s, tl.id) and not party.classes(s, tl.id) and not party.brackets(s, tl.id)
    assert len(party.windows(s, tl.id)) == 5                       # availability windows come with every game
    assert len(engine.load_events(s, aion.id)) == 10
    with pytest.raises(games.GameError, match="already a game"):
        games.create(s, GUILD, "", "aion2")                               # blank name with the template: AION 2 again
    with pytest.raises(games.GameError, match="already a game"):
        games.create(s, GUILD, "throne and liberty", "custom")
    with pytest.raises(games.GameError, match="name"):
        games.create(s, GUILD, "  ", "custom")


def test_each_game_needs_its_own_channels(s):
    aion, tl = two_games(s)
    tl.timer_channel_id = aion.timer_channel_id
    with pytest.raises(games.GameError, match="AION 2 already uses that timer channel"):
        games.check_channels(s, tl)
    tl.timer_channel_id, tl.party_channel_id = 201, 201
    with pytest.raises(games.GameError, match="must be different"):
        games.check_channels(s, tl)


# -- timers and dashboards ---------------------------------------------------------------


def test_dashboards_and_alerts_stay_in_their_game(s, monkeypatch):
    aion, tl = two_games(s)
    now = ny(2026, 10, 9, 12, 0)
    morokai = s.scalar(select(Event).where(Event.name == "Morokai"))
    assert timers.report(s, morokai, 1, "Rae", now).ok
    tl_dash = engine.build_dashboard(s, tl, now)
    aion_dash = engine.build_dashboard(s, aion, now)
    assert "Morokai" in tl_dash.text and "Shugo" not in tl_dash.text and tl_dash.text.count("##") >= 1
    assert "Morokai" not in aion_dash.text and "Shugo Festival" in aion_dash.text
    assert [label for _, label in tl_dash.bosses] == ["Morokai died", "Custom time"]
    # A switched-off game sends nothing.
    tl.enabled = False
    s.flush()
    due = engine.due_slots(s, now + 60 * 60, now)
    assert due and all(d.occ.event.game_id == aion.id for d in due)
    assert timers.boss_events(s) and all(b.game_id == aion.id for b in timers.boss_events(s))


def test_bot_sends_each_games_alerts_and_dashboard_to_its_channel(s, monkeypatch):
    aion, tl = two_games(s)
    clock = [ny(2026, 10, 9, 13, 25, 1)]   # Dimensional Invasion (AION 2) 5 min warning
    get_state(s).last_tick_at = clock[0] - 30
    s.commit()
    channels = {101: FakeChannel(), 201: FakeChannel()}

    async def run():
        b = botmod.NyerkBot(0)

        async def fake_channel(cid):
            return channels.get(cid)
        monkeypatch.setattr(b, "_channel", fake_channel)
        monkeypatch.setattr(botmod, "now", lambda: clock[0])
        await b.tick()
    asyncio.run(run())
    aion_texts = [m.content for m, _ in channels[101].sent]
    tl_texts = [m.content for m, _ in channels[201].sent]
    assert any(t.startswith("Dimensional Invasion starts in 5 minutes") for t in aion_texts)
    assert aion_texts[-1].startswith("━") and "AION 2 Timers" in aion_texts[-1]
    assert len(tl_texts) == 1 and "Throne and Liberty Timers" in tl_texts[0]  # just its own dashboard
    s.expire_all()
    assert games.first(s, GUILD).dashboard_message_id and s.get(Game, tl.id).dashboard_message_id
    assert s.get(Game, tl.id).dashboard_message_id != games.first(s, GUILD).dashboard_message_id


def test_boss_choices_name_the_game_when_there_are_several(s):
    aion, tl = two_games(s)
    labels = [label for label, _ in botmod.boss_choices(s, GUILD)]
    assert "Throne and Liberty: Morokai" in labels and "AION 2: Eternal Gartua" in labels


# -- party finder -------------------------------------------------------------------------


def test_one_party_and_registration_per_game(s):
    aion, tl = two_games(s)
    aion_cleric = next(c for c in party.classes(s, aion.id) if c.name == "Cleric")
    tl_cleric = next(c for c in party.classes(s, tl.id) if c.name == "Cleric")
    me = Who(5, "Rae", {aion_cleric.role_id, tl_cleric.role_id})
    t = ny(2026, 10, 9, 12, 0)
    aion_fire = PfContent(game_id=aion.id, name="Fire Temple")   # same name as TL's: fine in another game
    s.add(aion_fire)
    s.flush()
    tl_fire = next(c for c in party.contents(s, tl.id))
    p1 = party.create(s, me, aion_fire.id, 5, "none", "", "", t)
    p2 = party.create(s, me, tl_fire.id, 5, "none", "", "", t)   # another game: allowed
    assert (p1.game_id, p2.game_id) == (aion.id, tl.id)
    with pytest.raises(PartyError, match="already in a party"):
        party.create(s, me, tl_fire.id, 5, "none", "", "", t)
    assert party.live_party_for(s, 5, aion.id) is p1 and party.live_party_for(s, 5, tl.id) is p2
    assert len(party.live_parties_for(s, 5, GUILD)) == 2
    # Gear is per game; a game's dungeons, windows and classes only count in that game.
    party.set_gear(s, 6, "Kai", party.brackets(s, tl.id)[0].id, t)
    assert party.gear_of(s, 6, tl.id).label == "2100+" and party.gear_of(s, 6, aion.id) is None
    kai = Who(6, "Kai", {tl_cleric.role_id})
    reg = party.register(s, kai, party.windows(s, tl.id)[0].id, [tl_fire.id, aion_fire.id], t)
    assert reg.game_id == tl.id and reg.content_ids == [tl_fire.id]   # the other game's dungeon is ignored
    with pytest.raises(PartyError, match="class"):
        party.register(s, kai, party.windows(s, aion.id)[0].id, [aion_fire.id], t)   # no AION 2 class


def test_a_game_without_gear_scores_still_registers(s):
    tl = games.create(s, GUILD, "Lost Ark", "custom")
    s.add_all([PfClass(game_id=tl.id, name="Bard", class_type="support", role_id=700000000000000010),
               PfContent(game_id=tl.id, name="Valtan")])
    s.flush()
    bard = party.classes(s, tl.id)[0]
    reg = party.register(s, Who(7, "Mo", {bard.role_id}), party.windows(s, tl.id)[0].id,
                         [party.contents(s, tl.id)[0].id], ny(2026, 10, 9, 12, 0))
    assert reg.bracket_id is None


def test_party_finder_panels_post_into_their_games_channel(s):
    from nyerk import party_bot

    aion, tl = two_games(s)
    s.commit()
    channels = {102: FakeChannel(), 202: FakeChannel()}

    class StubBot:
        user = None

        async def _channel(self, cid):
            return channels.get(cid)
    asyncio.run(party_bot.PartyFinder(StubBot()).post_panels(tl.id, "all"))
    firsts = [m.content.splitlines()[0] for m, _ in channels[202].sent]
    assert firsts[0] == "## Throne and Liberty Classes" and "## Party status" in firsts
    assert channels[102].sent == []
    clear = [c.custom_id for c in channels[202].sent[0][0].view.children][-1]
    assert clear == f"nyerk:pf:class:clear-{tl.id}"


def test_roles_channel_and_member_roles_cover_every_game(s):
    aion, tl = two_games(s)
    aion_cat = s.scalar(select(Category).where(Category.game_id == aion.id, Category.name == "World Boss"))
    aion_cat.role_id = 600000000000000099
    s.flush()
    labels = [e.label for e in reaction_roles.entries(s, GUILD)]
    assert "AION 2: World Boss" in labels and "Throne and Liberty: World Boss" in labels
    opts = {o.role_id: o.label for o in members.role_options(s, GUILD)}
    tl_cleric = next(c for c in party.classes(s, tl.id))
    assert opts[tl_cleric.role_id] == "Throne and Liberty: Cleric"
    tl.support_role_id = 700000000000000020
    assert 700000000000000020 in members.desired_roles(s, GUILD, {tl_cleric.role_id})
    # Switching a game off drops its roles from the roles channel.
    tl.enabled = False
    s.flush()
    assert all(not e.label.startswith("Throne") for e in reaction_roles.entries(s, GUILD))


# -- web page -------------------------------------------------------------------------------


def _owner(tmp_path, monkeypatch, with_game=True):
    monkeypatch.setenv("NYERK_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("WEB_TLS", "off")
    db.init_engine(":memory:")
    with db.session() as s:
        if with_game:
            from nyerk.seed import seed
            seed(s)
        else:
            from nyerk import servers
            servers.seen(s, GUILD, "Your Server", auto_approve=True)  # the bot is in an approved server, no game yet
        get_state(s)
        get_state(s).admin_password_hash = hash_password("correct horse")
    c = TestClient(create_app(Config()))
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text)[1]
    c.post("/login", data={"csrf": csrf, "password": "correct horse"})
    c.csrf = csrf
    return c


def _csrf(c, path):
    return re.search(r'name="csrf" value="([^"]+)"', c.get(path).text)[1]


def test_first_start_asks_which_game(tmp_path, monkeypatch):
    c = _owner(tmp_path, monkeypatch, with_game=False)
    r = c.get("/events", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/setup"
    page = c.get("/setup").text
    assert "Which game is this for?" in page and "Set up for AION 2" in page
    tok = _csrf(c, "/setup")
    r = c.post("/setup", data={"csrf": tok, "kind": "custom", "name": "Throne and Liberty"})
    assert r.status_code == 200 and "Throne and Liberty" in r.text
    with db.session() as s:
        assert [g.name for g in games.all_games(s, GUILD)] == ["Throne and Liberty"]
    assert c.get("/setup", follow_redirects=False).headers["location"] == "/"   # only once


def test_games_page_add_switch_rename_and_delete(tmp_path, monkeypatch):
    c = _owner(tmp_path, monkeypatch)
    tok = _csrf(c, "/games")
    c.post("/games/new", data={"csrf": tok, "kind": "custom", "name": "Throne and Liberty"})
    with db.session() as s:
        tl = s.scalar(select(Game).where(Game.name == "Throne and Liberty"))
        tl_id = tl.id
        s.add(Event(game_id=tl_id, name="Morokai", schedule_type="kill_report", schedule_params={}, slots=[]))
    # Adding a game switches the page to it; the events page shows only its events.
    events = c.get("/events").text
    assert "Morokai" in events and "Shugo Festival" not in events
    assert "Throne and Liberty" in c.get("/").text                    # in the game picker
    c.post("/game/use", data={"csrf": tok, "game_id": "1"}, headers={"referer": "http://testserver/events"})
    events = c.get("/events").text
    assert "Shugo Festival" in events and "Morokai" not in events
    assert "Morokai" in c.get(f"/events?game={tl_id}").text           # a link can switch too
    # Rename and switch off.
    c.post(f"/games/{tl_id}", data={"csrf": tok, "name": "TL", "sort_order": "1"})
    with db.session() as s:
        g = s.get(Game, tl_id)
        assert g.name == "TL" and not g.enabled
    # Delete needs the name typed; then everything of that game goes.
    c.post(f"/games/{tl_id}/delete", data={"csrf": tok, "confirm": "nope"})
    with db.session() as s:
        assert s.get(Game, tl_id) is not None
    c.post(f"/games/{tl_id}/delete", data={"csrf": tok, "confirm": "tl"})
    with db.session() as s:
        assert s.get(Game, tl_id) is None
        assert not s.scalars(select(Event).where(Event.name == "Morokai")).all()
        assert s.scalars(select(Event)).all()                          # AION 2 untouched


def test_settings_page_saves_server_and_game_settings_apart(tmp_path, monkeypatch):
    c = _owner(tmp_path, monkeypatch)
    page = c.get("/settings").text
    assert 'id="game"' in page and "AION 2" in page and "Defaults source" in page
    tok = _csrf(c, "/settings")
    form = {k: v for k, v in re.findall(r'name="(\w+)"[^>]*value="([^"]*)"', page)}
    form.update(csrf=tok, timer_channel_id="111111111111111111", roles_channel_id="222222222222222222",
                time_style_near="t", time_style_far="f")
    c.post("/settings", data=form)
    with db.session() as s:
        assert games.first(s, GUILD).timer_channel_id == 111111111111111111
        assert get_settings(s, GUILD).roles_channel_id == 222222222222222222


def test_search_names_the_game(tmp_path, monkeypatch):
    c = _owner(tmp_path, monkeypatch)
    tok = _csrf(c, "/games")
    c.post("/games/new", data={"csrf": tok, "kind": "custom", "name": "Throne and Liberty"})
    found = c.get("/search/suggest?q=throne").json()
    assert any(x["page"] == "Game" and x["url"].startswith("/settings?game=") for x in found)
    found = c.get("/search/suggest?q=shugo festival").json()
    assert found[0]["page"] == "AION 2 event"


# -- upgrading an install from before games ------------------------------------------------


def test_upgrade_from_before_games(tmp_path):
    path = tmp_path / "nyerk.db"
    shutil.copy(FIXTURES / "pre_games.db", path)
    db.init_engine(path)
    assert (tmp_path / "nyerk-before-v1.db").exists()                 # a copy first
    with db.session() as s:
        from nyerk import servers
        assert servers.claim_legacy(s, GUILD)                          # the next start, with GUILD_ID set
        g = s.get(Game, 1)
        assert (g.name, g.kind) == ("AION 2", "aion2")
        assert g.timer_channel_id == 920000000000000001 and g.party_channel_id == 920000000000000004
        assert get_settings(s, GUILD).roles_channel_id == 920000000000000002
        assert g.dashboard_boss_note.startswith("Route: Gartua")
        for model in (Category, Event, PfClass, PfBracket, PfWindow, PfContent, PfInterest, PfParty):
            total = s.scalar(select(func.count()).select_from(model))
            assert total and total == s.scalar(select(func.count()).select_from(model).where(model.game_id == 1))
        assert s.scalar(select(func.count()).select_from(MemberGear)) == 16
        assert s.scalar(select(func.count()).select_from(UserTimer)) == 2
        assert s.scalar(select(func.count()).select_from(PfParty)) == 37
    # Running again changes nothing.
    db.init_engine(path)
    with db.session() as s:
        assert len(games.all_games(s, GUILD)) == 1
