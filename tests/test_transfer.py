import json

import pytest
from sqlalchemy import select

from nyerk import transfer
from nyerk.db import get_settings, get_state
from nyerk.models import Category, Event, PfClass
from .conftest import GUILD, G  # noqa: F401


def test_export_has_no_secrets_and_ids_are_strings(s):
    get_state(s).admin_password_hash = "secret-hash"
    G(s).timer_channel_id = 1234567890123456789
    data = transfer.export(s, GUILD)
    raw = json.dumps(data)
    assert "secret-hash" not in raw and "admin_password_hash" not in raw
    game = data["games"][0]
    assert data["nyerk_export"] == 2 and game["name"] == "AION 2" and game["kind"] == "aion2"
    assert game["settings"]["timer_channel_id"] == "1234567890123456789"
    assert "timer_channel_id" not in data["settings"] and "dashboard_message_id" not in game["settings"]
    assert {e["name"] for e in game["events"]} >= {"Silent Dartan", "Spacetime Rift"}
    assert len(game["party_finder"]["classes"]) == 8


def test_round_trip_is_a_no_op(s):
    data = json.loads(json.dumps(transfer.export(s, GUILD)))
    assert transfer.apply_import(s, data, GUILD) == []


def test_partial_id_file(s):
    changes = transfer.apply_import(s, {
        "nyerk_export": 1,
        "settings": {"party_channel_id": "222222222222222222"},
        "party_finder": {"classes": [{"name": "Cleric", "role_id": "<@&333333333333333333>"}]},
    }, GUILD, game_id=1)  # a file from before games goes into the game you're looking at
    assert G(s).party_channel_id == 222222222222222222
    assert s.scalar(select(PfClass).where(PfClass.name == "Cleric")).role_id == 333333333333333333
    assert len(changes) == 2


def test_import_merges_events_and_categories(s):
    transfer.apply_import(s, {
        "nyerk_export": 1,
        "categories": [{"name": "Siege", "role_id": "444444444444444444"}],
        "events": [
            {"name": "Artifact Siege", "category": "Siege", "schedule_type": "weekly",
             "schedule_params": {"days": ["mon"], "time": "21:00", "tz": "America/New_York"},
             "slots": [{"lead_minutes": 15, "enabled": True, "ping": True}]},
            {"name": "Shugo Festival", "enabled": False},
        ],
    }, GUILD, game_id=1)
    siege = s.scalar(select(Event).where(Event.name == "Artifact Siege"))
    assert siege.category.name == "Siege" and siege.schedule_params["days"] == ["mon"]
    assert {sl.lead_minutes: sl.ping for sl in siege.slots}[15] is True
    assert not s.scalar(select(Event).where(Event.name == "Shugo Festival")).enabled
    assert s.scalar(select(Category).where(Category.name == "World Boss")) is not None  # untouched


@pytest.mark.parametrize("bad,msg", [
    ({}, "Not a Nyerk settings file"),
    ({"nyerk_export": 1, "settings": {"timer_channel_id": "12ab"}}, "not a Discord ID"),
    ({"nyerk_export": 1, "settings": {"user_timer_limit": "lots"}}, "whole number"),
    ({"nyerk_export": 1, "events": [{"name": "X"}]}, "need a schedule_type"),
    ({"nyerk_export": 1, "party_finder": {"classes": [{"name": "X", "class_type": "healer"}]}}, "tank, support or dps"),
])
def test_bad_files(s, bad, msg):
    with pytest.raises(transfer.ImportError_, match=msg):
        transfer.apply_import(s, bad, GUILD, game_id=1)


def test_events_and_roles_round_trip_between_installs(s):
    """Change Events and Roles tab settings, export, import into a fresh install."""
    from nyerk import db
    from nyerk.seed import seed, seed_party_finder

    cat = s.scalar(select(Category).where(Category.name == "Rift"))
    cat.role_id, cat.sort_order = 999999999999999999, 0
    rift = s.scalar(select(Event).where(Event.name == "Spacetime Rift"))
    rift.dashboard_label = "Rift opens"
    slot = next(sl for sl in rift.slots if sl.lead_minutes == 5)
    slot.ping, slot.template = False, "{name} soon"
    s.commit()
    data = json.loads(json.dumps(transfer.export(s, GUILD)))

    db.init_engine(":memory:")
    with db.session() as fresh:
        seed(fresh)
        seed_party_finder(fresh)
        transfer.apply_import(fresh, data, GUILD)
        cat2 = fresh.scalar(select(Category).where(Category.name == "Rift"))
        rift2 = fresh.scalar(select(Event).where(Event.name == "Spacetime Rift"))
        slot2 = next(sl for sl in rift2.slots if sl.lead_minutes == 5)
        assert cat2.role_id == 999999999999999999 and cat2.sort_order == 0
        assert rift2.dashboard_label == "Rift opens" and rift2.category.name == "Rift"
        assert slot2.ping is False and slot2.template == "{name} soon"


def test_role_messages_round_trip(s):
    from nyerk.models import ReactionGroup
    g = ReactionGroup(guild_id=GUILD, name="Siege pings", description="Abyss", sort_order=3)
    s.add(g)
    s.flush()
    s.scalar(select(Category).where(Category.name == "Shugo")).group_id = g.id
    data = json.loads(json.dumps(transfer.export(s, GUILD)))
    assert {"name": "Siege pings", "description": "Abyss", "sort_order": 3} in data["role_messages"]
    assert next(c for c in data["games"][0]["categories"] if c["name"] == "Shugo")["role_message"] == "Siege pings"
    assert transfer.apply_import(s, data, GUILD) == []
    with pytest.raises(transfer.ImportError_, match="unknown role message"):
        transfer.apply_import(s, {"nyerk_export": 1, "categories": [{"name": "Shugo", "role_message": "Nope"}]},
                              GUILD, game_id=1)


def test_old_format_needs_a_game_to_go_into(s):
    with pytest.raises(transfer.ImportError_, match="Pick the game"):
        transfer.apply_import(s, {"nyerk_export": 1, "settings": {"party_channel_id": "222222222222222222"}}, GUILD)


def test_two_games_round_trip(s):
    """Each game's rows stay in that game; same names in two games are fine."""
    from nyerk import db, games
    from nyerk.seed import seed

    other = games.create(s, GUILD, "Throne and Liberty", "custom")
    s.add_all([Category(game_id=other.id, name="World Boss", role_id=777777777777777777),
               PfClass(game_id=other.id, name="Cleric", class_type="support", role_id=888888888888888888)])
    other.party_channel_id = 555555555555555555
    s.commit()
    data = json.loads(json.dumps(transfer.export(s, GUILD)))
    assert [g["name"] for g in data["games"]] == ["AION 2", "Throne and Liberty"]

    db.init_engine(":memory:")
    with db.session() as fresh:
        seed(fresh)                       # a fresh install with just AION 2
        changes = transfer.apply_import(fresh, data, GUILD)
        assert "Game Throne and Liberty: added" in changes
        tl = fresh.scalar(select(games.Game).where(games.Game.name == "Throne and Liberty"))
        assert tl.party_channel_id == 555555555555555555 and tl.kind == "custom"
        bosses = {c.game_id: c.role_id for c in fresh.scalars(select(Category).where(Category.name == "World Boss"))}
        assert bosses[tl.id] == 777777777777777777 and bosses[1] == 0
        clerics = {c.game_id: c.class_type for c in fresh.scalars(select(PfClass).where(PfClass.name == "Cleric"))}
        assert clerics == {1: "support", tl.id: "support"} and len(clerics) == 2
