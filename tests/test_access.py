"""Public access: Discord sign-in, owner/admin/member levels, owner-only
fields, admin lock, sign-everyone-out, password login via Cloudflare."""
import re
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from nyerk import db
from nyerk.config import Config
from nyerk.db import get_settings, get_state
from nyerk.models import AlertSlot, Event, UserTimer
from nyerk.seed import seed, seed_party_finder, seed_reaction_groups
from nyerk.web import app as webapp
from nyerk.web import auth
from nyerk.web.app import create_app, hash_password

OWNER, ADMIN, MEMBER, OUTSIDER = 11, 22, 33, 44
ADMIN_ROLE = 555


class Bot:
    def __init__(self):
        self.roles = {OWNER: set(), ADMIN: {ADMIN_ROLE}, MEMBER: set()}

    def is_ready(self):
        return True

    def status(self):
        return {"connected": True, "user": "Nyerk", "guild": "g", "latency_ms": 1, "last_tick": 0}

    def request_refresh(self, repost=False):
        pass

    async def fetch_member(self, uid):
        return (f"user{uid}", set(self.roles[uid])) if uid in self.roles else None

    async def admin_apply_roles(self, uid, add, remove):
        return []

    async def apply_quiet_hours(self, only=None):
        pass


@pytest.fixture()
def app(tmp_path, monkeypatch):
    for k, v in {"NYERK_DATA_DIR": str(tmp_path), "WEB_TLS": "off", "PUBLIC_URL": "https://nyerk.example.com",
                 "DISCORD_CLIENT_ID": "1", "DISCORD_CLIENT_SECRET": "x", "OWNER_IDS": str(OWNER),
                 "ADMIN_ROLE_IDS": str(ADMIN_ROLE), "GUILD_ID": ""}.items():
        monkeypatch.setenv(k, v)
    db.init_engine(":memory:")
    with db.session() as s:
        seed(s), seed_party_finder(s), seed_reaction_groups(s)
        get_settings(s).admin_password_hash = hash_password("pw")
    bot = Bot()
    a = create_app(Config(), bot)
    a.state.test_bot = bot
    return a


def sign_in(app, uid, monkeypatch, peer=("203.0.113.5", 1)):
    async def identify(cfg, code):
        return uid, f"user{uid}"
    monkeypatch.setattr(auth, "discord_identify", identify)
    c = TestClient(app, base_url="https://nyerk.example.com", client=peer)
    r = c.get("/auth/discord", follow_redirects=False)
    state = re.search(r"state=([^&]+)", r.headers["location"])[1]
    c.get(f"/auth/callback?code=abc&state={state}")
    return c


def csrf(c, path="/me"):
    return re.search(r'name="csrf" value="([^"]+)"', c.get(path).text)[1]


def test_member_sees_only_their_page(app, monkeypatch):
    c = sign_in(app, MEMBER, monkeypatch)
    assert c.get("/", follow_redirects=False).headers["location"] == "/me"
    assert c.get("/me").status_code == 200
    for path in ("/events", "/settings", "/members", "/logs", "/party"):
        assert c.get(path).status_code == 403, path
    assert c.post("/settings", data={"csrf": csrf(c)}).status_code == 403
    nav = c.get("/me").text
    assert 'href="/events"' not in nav and 'href="/settings"' not in nav


def test_admin_runs_things_but_cannot_change_wording_or_settings(app, monkeypatch):
    c = sign_in(app, ADMIN, monkeypatch)
    assert c.get("/events").status_code == 200 and c.get("/members").status_code == 200
    assert c.get("/settings").status_code == 403
    assert 'href="/settings"' not in c.get("/").text
    with db.session() as s:
        ev = s.scalar(select(Event).where(Event.name == "Shugo Festival"))
        eid, cat = ev.id, ev.category_id
    tok = csrf(c, f"/events/{eid}")
    c.post(f"/events/{eid}", data={"csrf": tok, "name": "Shugo Festival", "category_id": cat, "schedule_type": "hourly",
                                   "p_hourly_minute": "0", "p_hourly_tz": "America/New_York", "enabled": "1",
                                   "dashboard": "main", "slot_5_enabled": "1",
                                   "slot_5_template": "JOIN discord.gg/spam", "dashboard_label": "AD"})
    with db.session() as s:
        ev = s.get(Event, eid)
        assert ev.dashboard_label == "" and all(sl.template == "" for sl in ev.slots)  # wording kept
        assert {sl.lead_minutes: sl.enabled for sl in ev.slots}[5]                     # toggles applied
    assert c.post("/categories", data={"csrf": tok, "name": "X"}).status_code == 403
    assert c.post("/roles/header", data={"csrf": tok, "roles_header": "ad"}).status_code == 403
    assert c.post("/party/types", data={"csrf": tok}).status_code == 403
    assert c.post("/categories/repost-roles", data={"csrf": tok}).status_code == 200  # posting panels is allowed


def test_owner_gets_settings(app, monkeypatch):
    c = sign_in(app, OWNER, monkeypatch)
    assert c.get("/settings").status_code == 200 and 'href="/settings"' in c.get("/").text


def test_outsider_is_turned_away(app, monkeypatch):
    async def identify(cfg, code):
        return OUTSIDER, "stranger"
    monkeypatch.setattr(auth, "discord_identify", identify)
    c = TestClient(app, base_url="https://nyerk.example.com")
    state = re.search(r"state=([^&]+)", c.get("/auth/discord", follow_redirects=False).headers["location"])[1]
    assert "member of the server" in c.get(f"/auth/callback?code=abc&state={state}").text
    assert c.get("/me", follow_redirects=False).status_code == 303


def test_bad_oauth_state_rejected(app, monkeypatch):
    c = TestClient(app, base_url="https://nyerk.example.com")
    c.get("/auth/discord", follow_redirects=False)
    c.get("/auth/callback?code=abc&state=forged")
    assert c.get("/me", follow_redirects=False).status_code == 303


def test_admin_lock_and_sign_everyone_out(app, monkeypatch):
    c = sign_in(app, ADMIN, monkeypatch)
    with db.session() as s:
        get_settings(s).admin_locked = True
    assert c.get("/events").status_code == 403  # demoted to member right away
    with db.session() as s:
        get_settings(s).admin_locked = False
        get_state(s).session_epoch += 1
    assert c.get("/me", follow_redirects=False).status_code == 303  # signed out


def test_admin_loses_role_on_recheck(app, monkeypatch):
    c = sign_in(app, ADMIN, monkeypatch)
    app.state.test_bot.roles[ADMIN] = set()
    monkeypatch.setattr(auth, "RECHECK_SECONDS", -1)
    assert c.get("/events").status_code == 403


def test_password_login_blocked_through_cloudflare(app):
    tunnel = TestClient(app, base_url="https://nyerk.example.com", client=("172.18.0.3", 1),
                        headers={"cf-connecting-ip": "203.0.113.9"})
    page = tunnel.get("/login").text
    assert "Sign in with Discord" in page and 'name="password"' not in page
    assert tunnel.post("/login", data={"password": "pw"}).status_code == 404
    lan = TestClient(app, base_url="https://nyerk.example.com", client=("192.168.1.20", 1))
    assert 'name="password"' in lan.get("/login").text


def test_owner_changes_need_a_recent_sign_in(app, monkeypatch):
    c = sign_in(app, OWNER, monkeypatch)
    tok = csrf(c, "/settings")
    monkeypatch.setattr(auth, "FRESH_SECONDS", -1)
    r = c.post("/settings/password", data={"csrf": tok, "current": "x", "new": "y", "confirm": "y"},
               follow_redirects=False)
    assert r.headers["location"] == "/login"


def test_member_manages_only_own_timers(app, monkeypatch):
    with db.session() as s:
        boss = s.scalar(select(Event).where(Event.schedule_type == "kill_report"))
        mine = UserTimer(event_id=boss.id, user_id=MEMBER, user_name="m", mode="kill", fire_at=int(time.time()) + 9999,
                         created_at=1, armed_at=1)
        theirs = UserTimer(event_id=boss.id, user_id=ADMIN, user_name="a", mode="kill",
                           fire_at=int(time.time()) + 9999, created_at=1, armed_at=1, status="active")
        s.add_all([mine, theirs])
        s.flush()
        mine_id, theirs_id = mine.id, theirs.id
    c = sign_in(app, MEMBER, monkeypatch)
    tok = csrf(c)
    assert c.post(f"/me/timers/{theirs_id}/cancel", data={"csrf": tok}).status_code == 404
    c.post(f"/me/timers/{mine_id}/edit", data={"csrf": tok, "respawns_in": "2h"})
    c.post(f"/me/gear", data={"csrf": tok, "bracket": "1"})
    with db.session() as s:
        assert s.get(UserTimer, mine_id).fire_at > time.time() + 7000
        assert s.get(UserTimer, theirs_id).status == "active"
    c.post(f"/me/timers/{mine_id}/cancel", data={"csrf": tok})
    with db.session() as s:
        assert s.get(UserTimer, mine_id).status == "cancelled"


def test_policy_table():
    assert auth.needed_level("/members", "GET") == "admin"      # not mistaken for /me
    assert auth.needed_level("/members/5/roles", "POST") == "admin"
    assert auth.needed_level("/me", "GET") == "member"
    assert auth.needed_level("/settingsx", "GET") == "admin"    # prefixes stop at a "/"
    assert auth.needed_level("/me/roles", "POST") == "member"
    assert auth.needed_level("/settings/export", "GET") == "owner"
    assert auth.needed_level("/categories", "GET") == "admin"
    assert auth.needed_level("/categories/5", "POST") == "owner"
    assert auth.needed_level("/categories/repost-roles", "POST") == "admin"
    assert auth.needed_level("/party/content/bulk", "POST") == "admin"
    assert auth.needed_level("/events/3", "POST") == "admin"


def test_every_route_requires_sign_in(app):
    from fastapi.routing import APIRoute
    c = TestClient(app, base_url="https://nyerk.example.com")
    public = {"/login", "/auth/discord", "/auth/callback"}
    checked = 0
    for r in app.routes:
        if not isinstance(r, APIRoute) or r.path in public:
            continue
        path = re.sub(r"\{[^}]+\}", "1", r.path)
        for method in r.methods:
            resp = c.request(method, path, follow_redirects=False)
            assert resp.status_code == 303 and resp.headers["location"] == "/login", (method, r.path, resp.status_code)
            checked += 1
    assert checked > 60
