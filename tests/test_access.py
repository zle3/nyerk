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
from nyerk.games import first as G

OWNER, ADMIN, MEMBER, OUTSIDER = 11, 22, 33, 44
ADMIN_ROLE = 555


class Bot:
    def __init__(self):
        self.roles = {OWNER: set(), ADMIN: {ADMIN_ROLE}, MEMBER: set()}

    def is_ready(self):
        return True

    def status(self):
        return {"connected": True, "user": "Nyerk", "guild": "g", "latency_ms": 1, "last_tick": 0}

    def request_refresh(self, repost=False, game_id=None):
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
    public = {"/login", "/auth/discord", "/auth/callback", "/terms", "/privacy"}
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


@pytest.mark.parametrize("text", ["JOIN https://x.co", "discord.gg/abc", "visit spam.com now", "hey @everyone",
                                  "www.site", "dsc.gg/x", "free nitro at nitro.gift.xyz"])
def test_filter_blocks_ads(text):
    from nyerk import safety
    assert safety.blocked_text(text)


@pytest.mark.parametrize("text", ["Fire Temple", "Raid: Abyss", "{name} spawns in {minutes} minutes", "14:30",
                                  "10 min ago", "1h 20m", "America/New_York", "💀 {name}", "e.g. fast clear"])
def test_filter_allows_normal_text(text):
    from nyerk import safety
    assert not safety.blocked_text(text)


def _event_form(c, eid, **extra):
    with db.session() as s:
        ev = s.get(Event, eid)
        cat = ev.category_id
    return {"csrf": csrf(c, f"/events/{eid}"), "name": "Shugo Festival", "category_id": cat,
            "schedule_type": "hourly", "p_hourly_minute": "0", "p_hourly_tz": "America/New_York",
            "enabled": "1", "dashboard": "main", **extra}


def test_owner_cannot_save_links_either(app, monkeypatch):
    c = sign_in(app, OWNER, monkeypatch)
    with db.session() as s:
        eid = s.scalar(select(Event).where(Event.name == "Shugo Festival")).id
    r = c.post(f"/events/{eid}", data=_event_form(c, eid, slot_5_template="{name} — join discord.gg/x"))
    assert "aren&#39;t allowed" in r.text or "aren't allowed" in r.text
    with db.session() as s:
        assert all("discord.gg" not in sl.template for sl in s.get(Event, eid).slots)


def test_changes_are_recorded_and_restorable(app, monkeypatch):
    from nyerk.models import ConfigChange
    c = sign_in(app, ADMIN, monkeypatch)
    with db.session() as s:
        eid = s.scalar(select(Event).where(Event.name == "Shugo Festival")).id
    c.post(f"/events/{eid}", data=_event_form(c, eid, slot_15_enabled="1", slot_5_enabled="1"))
    with db.session() as s:
        row = s.scalar(select(ConfigChange).order_by(ConfigChange.id.desc()))
        assert row.user_id == ADMIN and row.level == "admin"
        assert "events[Shugo Festival].slots[15].enabled: False → True" in row.summary
        cid = row.id
    owner = sign_in(app, OWNER, monkeypatch)
    page = owner.get("/history").text
    assert "user22" in page and "slots[15].enabled" in page
    owner.post(f"/history/{cid}/restore", data={"csrf": csrf(owner, "/history")})
    with db.session() as s:
        assert not {sl.lead_minutes: sl.enabled for sl in s.get(Event, eid).slots}[15]


def test_admin_change_limit(app, monkeypatch):
    from nyerk.web import app as webapp_module
    monkeypatch.setattr(webapp_module, "ADMIN_CHANGES_PER_HOUR", 2)
    c = sign_in(app, ADMIN, monkeypatch)
    with db.session() as s:
        eid = s.scalar(select(Event).where(Event.name == "Shugo Festival")).id
    codes = [c.post(f"/events/{eid}", data=_event_form(c, eid)).status_code for _ in range(3)]
    assert codes[-1] == 429


def test_owner_switches(app, monkeypatch):
    admin = sign_in(app, ADMIN, monkeypatch)
    assert admin.get("/access").status_code == 403 and admin.get("/history").status_code == 403
    owner = sign_in(app, OWNER, monkeypatch)
    assert "Lock admin access" in owner.get("/access").text
    owner.post("/access/lock", data={"csrf": csrf(owner, "/access"), "lock": "1"})
    assert admin.get("/events").status_code == 403
    owner.post("/access/lock", data={"csrf": csrf(owner, "/access")})
    assert admin.get("/events").status_code == 200
    owner.post("/access/signout-all", data={"csrf": csrf(owner, "/access")})
    assert admin.get("/me", follow_redirects=False).status_code == 303


def test_admin_sets_party_chat_delete_time(app, monkeypatch):
    member = sign_in(app, MEMBER, monkeypatch)
    assert member.post("/party-chat", data={"csrf": csrf(member), "party_chat_delete_min": "5"}).status_code == 403
    admin = sign_in(app, ADMIN, monkeypatch)
    assert 'name="party_chat_delete_min"' in admin.get("/party").text
    admin.post("/party-chat", data={"csrf": csrf(admin, "/party"), "party_chat_delete_min": "30"})
    with db.session() as s:
        assert G(s).party_chat_delete_min == 30
    admin.post("/party-chat", data={"csrf": csrf(admin, "/party"), "party_chat_delete_min": "5000"})
    with db.session() as s:
        assert G(s).party_chat_delete_min == 30  # out of range: refused


def test_owner_opens_areas_to_admins(app, monkeypatch):
    owner = sign_in(app, OWNER, monkeypatch)
    admin = sign_in(app, ADMIN, monkeypatch)
    assert admin.get("/settings").status_code == 403
    owner.post("/access/grants", data={"csrf": csrf(owner, "/access"), "area_settings": "1", "area_wording": "1"})
    with db.session() as s:
        assert get_settings(s).admin_grants == "settings,wording"
    page = admin.get("/settings").text
    assert 'href="/settings"' in admin.get("/").text
    assert "Admin password" not in page and 'href="/access"' not in page and 'href="/history"' not in page
    assert 'action="/settings/import"' not in page                       # import also needs roles
    # Wording opened: an admin's template change is kept.
    with db.session() as s:
        ev = s.scalar(select(Event).where(Event.name == "Shugo Festival"))
        eid, cat = ev.id, ev.category_id
    tok = csrf(admin, f"/events/{eid}")
    admin.post(f"/events/{eid}", data={"csrf": tok, "name": "Shugo Festival", "category_id": cat,
                                       "schedule_type": "hourly", "p_hourly_minute": "0",
                                       "p_hourly_tz": "America/New_York", "enabled": "1", "dashboard": "main",
                                       "slot_5_enabled": "1", "dashboard_label": "Shugo opens"})
    with db.session() as s:
        assert s.get(Event, eid).dashboard_label == "Shugo opens"
    # Still closed: roles, history, the access page and the owner password.
    assert admin.post("/party/types", data={"csrf": tok}).status_code == 403
    assert admin.get("/history").status_code == 403
    assert admin.get("/access").status_code == 403
    assert admin.post("/access/grants", data={"csrf": tok, "area_roles": "1"}).status_code == 403
    assert admin.post("/settings/password", data={"csrf": tok, "current": "pw", "new": "x" * 12,
                                                  "confirm": "x" * 12}).status_code == 403
    assert admin.post("/settings/import", data={"csrf": tok, "text": "{}"}).status_code == 403
    # The admin lock still wins over opened areas.
    owner.post("/access/lock", data={"csrf": csrf(owner, "/access"), "lock": "1"})
    assert admin.get("/settings").status_code == 403


def test_history_restore_needs_every_area(app, monkeypatch):
    assert auth.areas_for("/history", "GET") == {"history"}
    assert auth.areas_for("/history/3/restore", "POST") == {"history", "settings", "wording", "roles"}
    assert auth.areas_for("/settings/import/apply", "POST") == {"settings", "wording", "roles"}
    assert auth.areas_for("/access/grants", "POST") is None
    assert auth.areas_for("/settings/password", "POST") is None
    assert not auth.may_use("member", "owner", "/settings", "GET", frozenset(auth.AREAS))


def test_import_cannot_change_access_controls(app, monkeypatch):
    from nyerk import transfer
    with db.session() as s:
        data = transfer.export(s)
        assert "admin_grants" not in data["settings"] and "admin_locked" not in data["settings"]
        data["settings"].update(admin_grants="settings,wording,roles,history", admin_locked=True)
        transfer.apply_import(s, data)
        assert get_settings(s).admin_grants == "" and not get_settings(s).admin_locked


def test_search_finds_settings_the_viewer_may_open(app, monkeypatch):
    admin = sign_in(app, ADMIN, monkeypatch)
    page = admin.get("/search?q=listing+lifetime").text
    assert "Listing lifetime" not in page                  # Settings is owner-only by default
    assert 'href="/party#party_chat_delete_min"' in admin.get("/search?q=chat").text
    assert "Shugo Festival" in admin.get("/search?q=shugo").text   # events by name
    owner = sign_in(app, OWNER, monkeypatch)
    assert 'href="/settings#party_lifetime_min"' in owner.get("/search?q=LIFETIME").text
    assert "Admin password" in owner.get("/search?q=password").text
    owner.post("/access/grants", data={"csrf": csrf(owner, "/access"), "area_settings": "1"})
    page = admin.get("/search?q=lifetime").text
    assert "Listing lifetime" in page
    assert "Admin password" not in admin.get("/search?q=password").text  # never opened to admins
    member = sign_in(app, MEMBER, monkeypatch)
    assert member.get("/search?q=x").status_code == 403


def test_search_caps_the_query(app, monkeypatch):
    from nyerk.web import search
    assert search.clean_query("a " * 500) == ["a"] * search.MAX_WORDS
    assert len("".join(search.clean_query("x" * 10_000))) == search.MAX_QUERY
    admin = sign_in(app, ADMIN, monkeypatch)
    assert admin.get("/search?q=" + "z" * 5000).status_code == 200


def test_one_account_cannot_flood(app, monkeypatch):
    admin = sign_in(app, ADMIN, monkeypatch)
    codes = [admin.get("/search?q=role").status_code for _ in range(35)]
    assert codes[:30] == [200] * 30 and codes[-1] == 429
    # The limit is per account: another IP doesn't help.
    other = sign_in(app, ADMIN, monkeypatch, peer=("198.51.100.9", 1))
    assert other.get("/search?q=role").status_code == 429
    owner = sign_in(app, OWNER, monkeypatch)
    assert all(owner.get("/search?q=role").status_code == 200 for _ in range(35))  # owners exempt


def test_discord_heavy_actions_have_a_cooldown(app, monkeypatch):
    admin = sign_in(app, ADMIN, monkeypatch)
    tok = csrf(admin, "/categories")
    assert admin.post("/categories/repost-roles", data={"csrf": tok}).status_code == 200
    assert admin.post("/categories/repost-roles", data={"csrf": tok}).status_code == 429
    owner = sign_in(app, OWNER, monkeypatch)
    assert owner.post("/categories/repost-roles", data={"csrf": csrf(owner, "/categories")}).status_code == 200


def test_limit_warnings_do_not_flood_logs(app, monkeypatch):
    from nyerk.models import LogEntry
    admin = sign_in(app, ADMIN, monkeypatch)
    for _ in range(40):
        admin.get("/search?q=role")
    with db.session() as s:
        hits = s.scalars(select(LogEntry).where(LogEntry.text.like("%hit the request limit%"))).all()
    assert len(hits) == 1


def test_live_suggestions(app, monkeypatch):
    from nyerk import tracking
    tracking.add_ping(910000000000000005, "Raelin", 123)
    admin = sign_in(app, ADMIN, monkeypatch)
    r = admin.get("/search/suggest?q=chat")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
    assert {"label", "page", "url", "detail"} <= set(r.json()[0])
    assert any(x["url"] == "/party#party_chat_delete_min" for x in r.json())
    assert admin.get("/search/suggest?q=c").json() == []                          # too short
    assert "/party" in [x["url"] for x in admin.get("/search/suggest?q=lfg").json()]  # synonym
    assert any(x["label"] == "Quiet hours" for x in admin.get("/search/suggest?q=quite+hours").json())  # typo
    assert admin.get("/search/suggest?q=stats").json()[0]["url"] == "/stats"     # pages
    assert any(x["url"] == "/members/910000000000000005" for x in admin.get("/search/suggest?q=raelin").json())
    assert len(admin.get("/search/suggest?q=e").json()) == 0
    assert len(admin.get("/search/suggest?q=role").json()) <= 8
    assert all(not x["url"].startswith("/settings") for x in admin.get("/search/suggest?q=channel").json())
    member = sign_in(app, MEMBER, monkeypatch)
    assert member.get("/search/suggest?q=chat").status_code == 403
    assert 'id="site-search"' in admin.get("/").text and 'id="site-search"' not in member.get("/me").text


def test_live_suggestions_have_their_own_limit(app, monkeypatch):
    admin = sign_in(app, ADMIN, monkeypatch)
    for _ in range(150):                         # more than the 120/min page budget
        assert admin.get("/search/suggest?q=ro").status_code == 200
    assert admin.get("/events").status_code == 200   # pages still work
    codes = [admin.get("/search/suggest?q=ro").status_code for _ in range(40)]
    assert codes[-1] == 429                       # but suggestions are capped too


def test_terms_privacy_and_footer_are_public(app, monkeypatch):
    c = TestClient(app, base_url="https://nyerk.example.com")
    for path, title in (("/terms", "Terms of use"), ("/privacy", "Privacy policy")):
        r = c.get(path)
        assert r.status_code == 200 and f"<h1>{title}</h1>" in r.text
        assert 'href="/login"' in r.text and 'action="/logout"' not in r.text   # signed out: Sign in link
        assert 'id="site-search"' not in r.text and 'href="/settings"' not in r.text
    page = c.get("/login").text
    for link in ('href="/terms"', 'href="/privacy"', 'href="https://kotone.dev"', 'href="https://github.com/zle3/nyerk"'):
        assert link in page
    admin = sign_in(app, ADMIN, monkeypatch)
    assert 'href="https://kotone.dev"' in admin.get("/").text and "Nyerk dev" in admin.get("/").text


def test_admin_forgets_a_member(app, monkeypatch):
    from nyerk import tracking
    from nyerk.models import LogEntry, RoleHolder
    tracking.add_ping(910000000000000009, "Iska", 555)
    admin = sign_in(app, ADMIN, monkeypatch)
    page = admin.get("/members/910000000000000009").text
    assert 'action="/members/910000000000000009/forget"' in page
    r = admin.post("/members/910000000000000009/forget", data={"csrf": csrf(admin, "/members/910000000000000009")})
    assert "Forgot Iska" in r.text
    with db.session() as s:
        assert not s.scalars(select(RoleHolder).where(RoleHolder.user_id == 910000000000000009)).all()
        line = s.scalar(select(LogEntry.text).where(LogEntry.text.like("%Forget member%")))
        assert line and "Iska" not in line
    assert "Iska" not in admin.get("/members").text
    member = sign_in(app, MEMBER, monkeypatch)
    assert member.post("/members/1/forget", data={"csrf": csrf(member)}).status_code == 403
