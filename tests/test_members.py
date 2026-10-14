import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from nyerk.games import first as G
from nyerk import db, members, party, quiet, tracking
from nyerk.config import Config
from nyerk.db import get_settings
from nyerk.models import Category, PfContent, QuietHours, RoleHolder, UserTimer
from nyerk.seed import seed, seed_party_finder, seed_reaction_groups
from nyerk.web.app import create_app, hash_password

CLERIC, RANGER = 1007, 1004
SUPPORT, DPS = 2002, 2003
SHUGO, FIRE = 800, 900


def setup_roles(s):
    for i, c in enumerate(party.classes(s, 1)):
        c.role_id = 1001 + i  # Gladiator 1001 … Ranger 1004 … Cleric 1007
    st = G(s)
    st.tank_role_id, st.support_role_id, st.dps_role_id = 2001, SUPPORT, DPS
    s.scalar(select(Category).where(Category.name == "Shugo")).role_id = SHUGO
    s.add(PfContent(game_id=1, name="Fire Temple", role_id=FIRE))
    s.flush()


def test_plan_adds_type_roles_and_removes_unticked(s):
    setup_roles(s)
    live = {RANGER, DPS, SHUGO, 555}  # 555 isn't managed by Nyerk and must stay
    add, remove, hold = members.plan_roles(s, 1, live, {CLERIC, FIRE})
    assert add == {CLERIC, SUPPORT, FIRE} and remove == {RANGER, DPS, SHUGO} and hold == set()


def test_plan_holds_dungeon_roles_during_quiet_hours(s):
    setup_roles(s)
    quiet.set_hours(s, 1, "Rae", 23, 7, "UTC", set())
    quiet.confirm(s, 1, "quiet", set())
    add, remove, hold = members.plan_roles(s, 1, set(), {FIRE, SHUGO})
    assert add == {SHUGO} and hold == {FIRE}
    members.record(s, 1, "Rae", {FIRE, SHUGO}, hold)
    rows = {r.role_id: r.suspended for r in s.scalars(select(RoleHolder).where(RoleHolder.user_id == 1))}
    assert rows == {FIRE: True, SHUGO: False}


def test_known_members_merges_sources(s):
    setup_roles(s)
    s.commit()
    tracking.set_classes(5, "Kai", {CLERIC})
    tracking.add_ping(5, "Kai", SHUGO)
    quiet.set_hours(s, 6, "Mira", 22, 6, "UTC", set())
    s.commit()
    rows = {m.name: m for m in members.known(s)}
    assert rows["Kai"].classes == ["Cleric"] and rows["Kai"].pings == 1
    assert rows["Mira"].quiet == "on"
    assert [m.name for m in members.known(s, "mir")] == ["Mira"]
    assert [m.name for m in members.known(s, "5")] == ["Kai"]


class FakeBot:
    def __init__(self, roles):
        self.roles, self.applied, self.quiet_runs = roles, [], []

    def is_ready(self):
        return True

    def status(self):
        return {"connected": True, "user": "Nyerk", "guild": "g", "latency_ms": 1, "last_tick": 0}

    def request_refresh(self, repost=False, game_id=None):
        pass

    async def fetch_member(self, uid):
        return ("Kai", set(self.roles)) if uid == 5 else None

    async def admin_apply_roles(self, uid, add, remove):
        self.applied.append((uid, add, remove))
        self.roles = (self.roles | add) - remove
        return []

    async def apply_quiet_hours(self, only=None):
        self.quiet_runs.append(only)


@pytest.fixture()
def admin(tmp_path, monkeypatch):
    monkeypatch.setenv("NYERK_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("WEB_TLS", "off")
    db.init_engine(":memory:")
    with db.session() as s:
        seed(s), seed_party_finder(s), seed_reaction_groups(s)
        setup_roles(s)
        get_settings(s).admin_password_hash = hash_password("pw")
    bot = FakeBot({RANGER, DPS})
    c = TestClient(create_app(Config(), bot))
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text)[1]
    c.post("/login", data={"csrf": csrf, "password": "pw"})
    c.bot = bot
    return c


def _csrf(c, path="/members"):
    return re.search(r'name="csrf" value="([^"]+)"', c.get(path).text)[1] if "csrf" in c.get(path).text else \
        re.search(r'name="csrf" value="([^"]+)"', c.get("/").text)[1]


def test_member_page_and_role_save(admin):
    page = admin.get("/members/5").text
    assert "Kai" in page and f'value="{RANGER}" checked' in page
    r = admin.post("/members/5/roles", data={"csrf": _csrf(admin, "/members/5"), "role": [str(CLERIC), str(FIRE)]})
    assert "Saved roles for Kai" in r.text
    uid, add, remove = admin.bot.applied[-1]
    assert add == {CLERIC, SUPPORT, FIRE} and remove == {RANGER, DPS}
    with db.session() as s:
        assert {r.role_id for r in s.scalars(select(RoleHolder).where(RoleHolder.user_id == 5))} == {CLERIC, FIRE}
    assert "Kai" in admin.get("/members").text


def test_unknown_member_lookup(admin):
    r = admin.get("/members?q=123456789012345678")
    assert "Open them anyway" in r.text
    r = admin.get("/members/123456789012345678")
    assert "No member with ID" in r.text


def test_member_quiet_hours_and_activity(admin):
    csrf = _csrf(admin, "/members/5")
    r = admin.post("/members/5/quiet", data={"csrf": csrf, "enabled": "1", "start": "22", "end": "6",
                                             "tz": "Europe/London"})
    assert "10 PM (22:00) to 6 AM (06:00), UK (London)" in r.text and admin.bot.quiet_runs == [5]
    admin.post("/members/5/quiet", data={"csrf": csrf})
    with db.session() as s:
        assert not s.scalar(select(QuietHours).where(QuietHours.user_id == 5)).enabled
        from nyerk.models import Event
        boss = s.scalar(select(Event).where(Event.schedule_type == "kill_report"))
        s.add(UserTimer(event_id=boss.id, user_id=5, user_name="Kai", mode="kill", fire_at=2_000_000_000,
                        created_at=1, armed_at=1))
    page = admin.get("/members/5").text
    tid = re.search(r"/members/5/timers/(\d+)/cancel", page)[1]
    admin.post(f"/members/5/timers/{tid}/cancel", data={"csrf": csrf})
    with db.session() as s:
        assert s.get(UserTimer, int(tid)).status == "cancelled"


def test_forget_member_erases_their_data(s):
    from nyerk.models import LogEntry, MemberProfile, PfParty, PfPartyMember
    from .test_party import T, who
    m = members
    s.add(PfContent(game_id=1, name="Fire Temple", min_gear_score=0))
    s.flush()
    cid = s.scalar(select(PfContent)).id
    for i, c in enumerate(party.classes(s, 1)):
        c.role_id = 1001 + i
    s.flush()
    tracking.add_ping(5, "Raelin", 777)
    quiet.set_hours(s, 5, "Raelin", 23, 7, "America/New_York", set())
    party.set_gear(s, 5, "Raelin", party.brackets(s, 1)[0].id, T)
    old = party.create(s, who(s, 5, "Templar"), cid, 5, "none", "", "", T - 9000)
    party.end(old, "done", T - 5000)
    live = party.create(s, who(s, 6, "Cleric"), cid, 5, "none", "", "", T)
    party.join(s, live, who(s, 5, "Templar"), T)
    s.add(LogEntry(at=T, level="INFO", kind="party", text="Raelin joined; Raelinx is someone else."))
    s.flush()
    res = m.forget(s, 5, T + 10)
    s.flush()
    assert res.name == "Raelin" and res.left_party
    for model in (RoleHolder, MemberProfile, QuietHours, PfPartyMember):
        assert not s.scalars(select(model).where(model.user_id == 5)).all()
    assert s.get(PfParty, old.id).leader_name == m.FORMER and s.get(PfParty, old.id).leader_id == 0
    assert [x.user_id for x in s.get(PfParty, live.id).members] == [6]
    text = s.scalar(select(LogEntry.text).where(LogEntry.kind == "party"))
    assert text == "former member joined; Raelinx is someone else."
    assert all(x.user_id != 5 for x in m.known(s))


def test_forgotten_list_survives_a_restore_and_expires(s, tmp_path, monkeypatch):
    from nyerk import forgotten
    from nyerk.db import get_state
    from .test_party import T
    monkeypatch.setenv("NYERK_DATA_DIR", str(tmp_path))
    cfg = Config()
    tracking.add_ping(5, "Raelin", 777)
    s.commit()
    members.forget(s, 5, T)
    forgotten.record(cfg, s, 5, T)
    s.commit()
    entry = forgotten.load(cfg)[0]
    assert entry["user_id"] == 5 and set(entry) == {"id", "user_id", "at"}
    # Live database: already applied, so a member who comes back keeps new data.
    tracking.add_ping(5, "Raelin", 888)
    assert forgotten.reapply(cfg, T + 60) == 0
    assert s.scalars(select(RoleHolder).where(RoleHolder.user_id == 5)).all()
    # A restored older copy: it doesn't know the forget, so it runs again.
    get_state(s).forgets_applied = []
    s.commit()
    assert forgotten.reapply(cfg, T + 120) == 1
    s.expire_all()
    assert not s.scalars(select(RoleHolder).where(RoleHolder.user_id == 5)).all()
    # After the backups have aged out, the entry is dropped.
    assert forgotten.reapply(cfg, T + (cfg.backup_keep + 2) * 86400) == 0
    assert forgotten.load(cfg) == []
