"""The setup guide walks a new server's owner through everything."""
import re

from sqlalchemy import select

from nyerk import db, games, onboarding
from nyerk.db import get_settings
from nyerk.models import Category

from .test_servers import A, B, HOST, OWNER_A, WAITING, app, csrf, sign_in, two_servers  # noqa: F401


def test_steps_follow_the_setup(s):
    a, _ = two_servers(s)
    st = get_settings(s, A)
    rows = {x.key: x for x in onboarding.steps(s, A, a, problems=None)}
    assert rows["game"].done and not rows["channels"].done and "Roles channel" in rows["channels"].todo
    assert not rows["access"].done and "isn't connected" in rows["access"].detail
    assert not rows["ping_roles"].done and not rows["posted"].done
    assert onboarding.progress(list(rows.values())) == (1, 6)
    st.roles_channel_id = st.log_channel_id = 1
    for c in s.scalars(select(Category).where(Category.game_id == a.id)):
        c.role_id = 5000 + c.id
    rows = {x.key: x for x in onboarding.steps(s, A, a, problems=[])}
    assert rows["channels"].done and rows["access"].done and rows["ping_roles"].done
    assert rows["share"].optional and not rows["share"].done
    rows = {x.key: x for x in onboarding.steps(s, A, a, problems=["#timers: Nyerk needs Send Messages."])}
    assert not rows["access"].done and rows["access"].todo == ["#timers: Nyerk needs Send Messages."]


def test_party_roles_step_counts_types_and_dungeons(s):
    a, _ = two_servers(s)
    step = next(x for x in onboarding.steps(s, A, a, []) if x.key == "party_roles")
    assert not step.done and "Tank (type)" in step.todo and "Add the dungeons" in step.detail


def test_new_server_owner_is_guided(app, monkeypatch):  # noqa: F811
    with db.session() as s:
        for g in games.all_games(s, A):  # a brand-new server: no game yet
            s.delete(g)
    owner = sign_in(app, OWNER_A, monkeypatch)
    assert "Which game is this for?" in owner.get("/setup").text
    r = owner.post("/setup", data={"csrf": csrf(owner, "/setup"), "kind": "aion2", "name": "AION 2"})
    assert "Setup guide" in r.text and "Create missing channels" in r.text and "Post everything" in r.text
    assert "Continue setup" in owner.get("/").text


def test_create_missing_roles_from_the_guide(app, monkeypatch):  # noqa: F811
    calls = []
    bot = app.state.bot

    async def record(name, *args, **kw):
        calls.append(name)
        return [name]
    bot.create_category_roles = lambda g, ids: record("ping", g, ids)
    bot.create_class_roles = lambda g, ids, game_id=None: record("class", g, ids)
    bot.create_type_roles = lambda g, gid: record("types", g, gid)
    bot.create_content_roles = lambda g, ids, game_id=None: record("content", g, ids)
    bot.setup_problems = lambda g, gid: []
    owner = sign_in(app, OWNER_A, monkeypatch)
    owner.post("/roles/create-missing", data={"csrf": csrf(owner, "/setup"), "what": "ping"})
    owner.post("/roles/create-missing", data={"csrf": csrf(owner, "/setup"), "what": "party"})
    assert calls == ["ping", "class", "types", "content"]


def test_host_sees_the_invite_and_waiting_servers(app, monkeypatch):  # noqa: F811
    host = sign_in(app, HOST, monkeypatch)
    page = host.get("/servers").text
    assert "Add Nyerk to a server" in page and 'data-copy="#invite-link"' in page
    host.post("/server/use", data={"csrf": csrf(host, "/"), "guild_id": str(A)})
    assert re.search(r"waiting for your approval", host.get("/").text)


class _Role:
    def __init__(self, rid, name, pos):
        self.id, self.name, self.position = rid, name, pos

    def __ge__(self, other):
        return self.position >= other.position


class _Perms:
    def __init__(self, **kw):
        self.__dict__.update(dict(view_channel=True, send_messages=True, read_message_history=True,
                                  add_reactions=True, manage_messages=True, manage_roles=True) | kw)


def test_setup_problems_in_plain_words(s, monkeypatch):
    from nyerk import bot as botmod
    a, _ = two_servers(s)
    a.tank_role_id = 777
    get_settings(s, A).roles_channel_id = 555
    s.commit()
    top = _Role(1, "Nyerk", 5)
    channels = {a.timer_channel_id: type("C", (), {"name": "timers", "permissions_for": lambda self, m: _Perms(
        send_messages=False)})()}
    guild = type("G", (), {
        "me": type("M", (), {"guild_permissions": _Perms(manage_roles=False), "top_role": top})(),
        "get_channel": lambda self, cid: channels.get(cid),
        "get_role": lambda self, rid: _Role(rid, "Tank", 9) if rid == 777 else None})()
    b = botmod.NyerkBot(0)
    monkeypatch.setattr(b, "_guild", lambda gid: guild)
    out = b.setup_problems(A, a.id)
    assert "Nyerk's role needs Manage Roles" in out[0]
    assert any(x.startswith("#timers (timer channel): Nyerk needs Send Messages") for x in out)
    assert any(x.startswith("Party finder channel: not found") for x in out)
    assert any(x.startswith("Roles channel: not found") for x in out)
    assert out[-1] == "Drag Nyerk's role above these roles so it can hand them out: Tank."
