import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from nyerk import db
from nyerk.config import Config
from nyerk.db import get_settings
from nyerk.models import Category, Event, PfClass, PfContent, ReactionGroup, UserTimer
from nyerk.seed import seed, seed_party_finder, seed_reaction_groups
from nyerk.web.app import create_app, hash_password


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("NYERK_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("WEB_TLS", "off")
    db.init_engine(":memory:")
    with db.session() as s:
        seed(s)
        seed_party_finder(s)
        seed_reaction_groups(s)
        get_settings(s).admin_password_hash = hash_password("correct horse")
    c = TestClient(create_app(Config()))
    page = c.get("/login")
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)[1]
    r = c.post("/login", data={"csrf": csrf, "password": "correct horse"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    c.csrf = csrf if "csrf" not in c.cookies else _csrf(c)
    return c


def _csrf(c):
    return re.search(r'name="csrf" value="([^"]+)"', c.get("/").text)[1]


def test_requires_login(tmp_path, monkeypatch):
    monkeypatch.setenv("NYERK_DATA_DIR", str(tmp_path))
    db.init_engine(":memory:")
    c = TestClient(create_app(Config()))
    r = c.get("/events", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_wrong_password(tmp_path, monkeypatch):
    monkeypatch.setenv("NYERK_DATA_DIR", str(tmp_path))
    db.init_engine(":memory:")
    with db.session() as s:
        seed(s)
        get_settings(s).admin_password_hash = hash_password("right")
    c = TestClient(create_app(Config()))
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text)[1]
    c.post("/login", data={"csrf": csrf, "password": "wrong"})
    assert c.get("/", follow_redirects=False).status_code == 303


@pytest.mark.parametrize("path", ["/", "/events", "/events/1", "/events/5", "/events/new", "/categories",
                                  "/timers", "/maintenance", "/settings", "/defaults", "/logs", "/party"])
def test_pages_render(client, path):
    r = client.get(path)
    assert r.status_code == 200, r.text[:500]
    assert "<main>" in r.text


def test_csrf_required(client):
    r = client.post("/settings", data={"user_timer_limit": "5"})
    assert r.status_code == 400


def test_edit_event_marks_hand_edited(client):
    with db.session() as s:
        rift = s.scalar(select(Event).where(Event.name == "Spacetime Rift"))
        rid, cat = rift.id, rift.category_id
    csrf = _csrf(client)
    r = client.post(f"/events/{rid}", data={
        "csrf": csrf, "name": "Spacetime Rift", "category_id": cat, "schedule_type": "daily",
        "p_daily_times": "02:00, 14:00", "p_daily_tz": "America/New_York", "duration_minutes": "60",
        "enabled": "1", "dashboard": "main", "slot_15_enabled": "1", "slot_15_ping": "1",
        "slot_0_enabled": "1", "slot_0_template": "{role}\n{name} now!", "new_slot_lead": "30",
    }, follow_redirects=False)
    assert r.status_code == 303
    with db.session() as s:
        rift = s.get(Event, rid)
        assert rift.schedule_params["times"] == ["02:00", "14:00"]
        assert rift.edited_fields == ["schedule"]
        slots = {sl.lead_minutes: sl for sl in rift.slots}
        assert slots[15].ping and not slots[5].enabled and slots[0].template == "{role}\n{name} now!"
        assert 30 in slots


def test_bad_schedule_shows_error(client):
    r = client.post("/events/new", data={"csrf": _csrf(client), "name": "X", "schedule_type": "weekly",
                                         "p_weekly_time": "25:00", "p_weekly_tz": "UTC"})
    assert r.status_code == 200 and "Pick at least one day" in r.text


def test_category_role_and_order(client):
    csrf = _csrf(client)
    with db.session() as s:
        shugo = s.scalar(select(Category).where(Category.name == "Shugo"))
        sid = shugo.id
    client.post(f"/categories/{sid}", data={"csrf": csrf, "name": "Shugo", "role_id": "<@&123456789012345678>"})
    client.post(f"/categories/{sid}/move", data={"csrf": csrf, "dir": "up"})
    with db.session() as s:
        c = s.get(Category, sid)
        assert c.role_id == 123456789012345678 and c.sort_order == 0


def test_maintenance_override_and_cancel_timer(client):
    csrf = _csrf(client)
    page = client.get("/maintenance").text
    replaces = re.search(r'name="replaces" value="(\d{9,})"', page)[1]
    r = client.post("/maintenance/override", data={"csrf": csrf, "replaces": replaces, "action": "cancel"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert "cancelled" in client.get("/maintenance").text

    with db.session() as s:
        boss = s.scalar(select(Event).where(Event.schedule_type == "kill_report"))
        s.add(UserTimer(event_id=boss.id, user_id=1, user_name="R", mode="kill", fire_at=2_000_000_000,
                        created_at=1, armed_at=1))
    client.post("/timers/1/cancel", data={"csrf": csrf})
    with db.session() as s:
        assert s.get(UserTimer, 1).status == "cancelled"


def test_settings_validation(client):
    csrf = _csrf(client)
    base = {"csrf": csrf, "timer_channel_id": "111111111111111111", "roles_channel_id": "", "log_channel_id": "",
            "user_timer_limit": "7", "report_offset_min": "5", "keep_occurrences": "2",
            "dashboard_max_oneoffs": "20", "default_tz": "America/New_York", "time_style_near": "t",
            "time_style_far": "f", "oneoff_label": "One-offs", "shugo_url": "https://shugo.gg/timers",
            "shugo_region": "NA East", "party_lifetime_min": "60", "party_warn_min": "10",
            "party_extend_min": "60", "party_max_extensions": "2", "party_status_hold_min": "120",
            "party_ping_gap_min": "15"}
    client.post("/settings", data=base)
    with db.session() as s:
        assert get_settings(s).user_timer_limit == 7
    r = client.post("/settings", data={**base, "default_tz": "Nowhere/Land"})
    assert "Unknown time zone" in r.text
    with db.session() as s:
        assert get_settings(s).default_tz == "America/New_York"


def test_repost_roles_route_not_shadowed(client):
    r = client.post("/categories/repost-roles", data={"csrf": _csrf(client)})
    assert r.status_code == 200 and "Bot is not connected" in r.text


def test_party_page_and_crud(client):
    assert client.get("/party").status_code == 200
    csrf = _csrf(client)
    r = client.post("/party/content/new", data={"csrf": csrf, "name": "Fire Temple", "emoji": "🔥",
                                               "min_gear_score": "1600", "sort_order": "0", "active": "1"})
    assert r.status_code == 200 and "Fire Temple" in r.text
    client.post("/party/types", data={"csrf": csrf, "tank_role_id": "555555555555555555", "support_role_id": "",
                                      "dps_role_id": "", "party_default_tank": "1", "party_default_support": "1",
                                      "party_default_dps": "3"})
    with db.session() as s:
        assert get_settings(s).tank_role_id == 555555555555555555
    page = client.get("/party").text
    form = {k: v for k, v in re.findall(r'name="((?:name|emoji|role_id|sort_order)_\d+)" value="([^"]*)"', page)}
    form.update({f"class_type_{i}": t for i, t in re.findall(r'name="class_type_(\d+)".*?<option value="(\w+)" selected', page)})
    ids = sorted({k.rsplit("_", 1)[1] for k in form})
    assert len(ids) == 8
    r = client.post("/party/classes/bulk", data={"csrf": csrf, **form, f"role_id_{ids[0]}": "777777777777777777",
                                                 f"role_id_{ids[1]}": "888888888888888888"})
    assert "Saved 2 classes" in r.text
    with db.session() as s:
        assert {c.role_id for c in s.scalars(select(PfClass))} >= {777777777777777777, 888888888888888888}
    r = client.post("/party/classes/bulk", data={"csrf": csrf, **form, f"role_id_{ids[2]}": "bad"})
    assert "Nothing saved" in r.text and "not a Discord ID" in r.text
    r = client.post("/party/classes/bulk", data={"csrf": csrf, **form, f"role_id_{ids[2]}": "777777777777777777",
                                                 f"role_id_{ids[0]}": "777777777777777777"})
    assert "same role ID" in r.text


def test_settings_export_import_flow(client):
    import json
    exported = client.get("/settings/export")
    assert exported.status_code == 200 and "attachment" in exported.headers["content-disposition"]
    data = exported.json()
    data["settings"]["party_channel_id"] = "666666666666666666"
    csrf = _csrf(client)
    preview = client.post("/settings/import", data={"csrf": csrf},
                          files={"file": ("n.json", json.dumps(data).encode(), "application/json")})
    assert preview.status_code == 200 and "party_channel_id" in preview.text
    with db.session() as s:
        assert get_settings(s).party_channel_id == 0  # preview changed nothing
    raw = re.search(r'<textarea name="raw" hidden>(.*?)</textarea>', preview.text, re.S)[1]
    import html
    client.post("/settings/import/apply", data={"csrf": csrf, "raw": html.unescape(raw)})
    with db.session() as s:
        assert get_settings(s).party_channel_id == 666666666666666666


def test_content_bulk_save_and_roles_preview(client):
    csrf = _csrf(client)
    client.post("/party/content/new", data={"csrf": csrf, "name": "Fire Temple", "emoji": "", "role_id": "",
                                           "react_emoji": "", "min_gear_score": "1600", "sort_order": "0",
                                           "active": "1"})
    with db.session() as s:
        cid = s.scalar(select(PfContent)).id
    r = client.post("/party/content/bulk", data={
        "csrf": csrf, f"name_{cid}": "Fire Temple", f"emoji_{cid}": "🔥", f"role_id_{cid}": "123456789012345679",
        f"react_emoji_{cid}": "", f"min_gear_score_{cid}": "1700", f"sort_order_{cid}": "0", f"active_{cid}": "1"})
    assert "Saved 1 dungeon" in r.text
    page = client.get("/categories").text
    assert "## Dungeon pings" not in page  # rendered as a heading
    assert "Dungeon pings" in page and "1️⃣ Fire Temple" in page
    r = client.post("/party/content/create-roles", data={"csrf": csrf})
    assert "Bot is not connected" in r.text


def test_role_messages_and_assignment(client):
    csrf = _csrf(client)
    client.post("/roles/groups/new", data={"csrf": csrf, "name": "Siege pings", "description": "Abyss"})
    with db.session() as s:
        siege = s.scalar(select(ReactionGroup).where(ReactionGroup.name == "Siege pings"))
        shugo = s.scalar(select(Category).where(Category.name == "Shugo"))
        shugo.role_id = 123456789012345671
        sid, gid = shugo.id, siege.id
    r = client.post("/roles/assign", data={"csrf": csrf, f"group_category_{sid}": str(gid),
                                           f"emoji_category_{sid}": "🎪"})
    assert "Saved 1 change" in r.text
    page = client.get("/categories").text
    assert "Siege pings" in page and "🎪 Shugo" in page
    r = client.post(f"/roles/groups/{gid}/delete", data={"csrf": csrf})
    with db.session() as s:
        assert s.get(Category, sid).group_id is None  # falls back to the default message


def test_roles_header(client):
    csrf = _csrf(client)
    r = client.post("/roles/header", data={"csrf": csrf, "roles_header": "# Pick roles\nhi"})
    assert "Header saved" in r.text and 'class="h1">Pick roles' in r.text
    with db.session() as s:
        assert get_settings(s).roles_header == "# Pick roles\nhi"


@pytest.mark.parametrize("rng", ["7", "30", "all", "bogus"])
def test_stats_page(client, rng):
    r = client.get(f"/stats?range={rng}")
    assert r.status_code == 200 and "Class distribution" in r.text and "Parties led" in r.text
