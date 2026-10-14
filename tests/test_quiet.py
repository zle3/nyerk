import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select

from nyerk.db import get_settings
from nyerk import bot as botmod
from nyerk import quiet, tracking
from nyerk.models import Category, PfContent, QuietHours, RoleHolder
from .conftest import GUILD, G  # noqa: F401

NY = ZoneInfo("America/New_York")


def at(h, m=0, day=12):
    return int(datetime(2026, 10, day, h, m, tzinfo=NY).timestamp())


def q(start, end, tz="America/New_York"):
    return QuietHours(user_id=1, start_min=start * 60, end_min=end * 60, tz=tz, enabled=True, state="active")


def test_window_same_day_and_overnight():
    assert quiet.in_window(q(9, 17), at(9)) and not quiet.in_window(q(9, 17), at(17))
    night = q(23, 7)
    assert quiet.in_window(night, at(23, 30)) and quiet.in_window(night, at(3)) and not quiet.in_window(night, at(7))
    assert quiet.in_window(q(23, 7, "Europe/London"), at(19))  # 00:00 in London


def setup(s):
    s.add_all([PfContent(game_id=1, name="Fire Temple", role_id=900), PfContent(game_id=1, name="Raid", role_id=901)])
    s.scalar(select(Category).where(Category.name == "Shugo")).role_id = 800
    s.commit()


def test_quiet_removes_only_dungeon_roles_then_restores(s):
    setup(s)
    with_held = {900, 901, 800}  # two dungeon roles and one event role
    quiet.set_hours(s, GUILD, 1, "Rae", 23, 7, "America/New_York", with_held)
    tracking_rows = {r.role_id for r in s.scalars(select(RoleHolder).where(RoleHolder.user_id == 1))}
    assert tracking_rows == {900, 901}  # current dungeon roles picked up on setup
    [c] = quiet.transitions(s, at(23, 30), {GUILD})
    assert (c.to_state, c.role_ids) == ("quiet", {900, 901})
    quiet.confirm(s, GUILD, 1, "quiet", c.role_ids)
    assert quiet.transitions(s, at(2), {GUILD}) == []          # nothing more to do overnight
    [c] = quiet.transitions(s, at(7, 1), {GUILD})
    assert (c.to_state, c.role_ids) == ("active", {900, 901})
    quiet.confirm(s, GUILD, 1, "active", c.role_ids)
    assert not any(r.suspended for r in s.scalars(select(RoleHolder)))


def test_turn_off_restores_during_window(s):
    setup(s)
    quiet.set_hours(s, GUILD, 1, "Rae", 23, 7, "America/New_York", {900})
    quiet.confirm(s, GUILD, 1, "quiet", {900})
    quiet.turn_off(s, GUILD, 1)
    [c] = quiet.transitions(s, at(1), {GUILD})
    assert (c.to_state, c.role_ids) == ("active", {900})


def test_same_start_and_end_rejected(s):
    import pytest
    with pytest.raises(ValueError):
        quiet.set_hours(s, GUILD, 1, "Rae", 5, 5, "UTC", set())


class FakeHttp:
    def __init__(self):
        self.calls = []

    async def add_role(self, g, u, r, reason=""):
        self.calls.append(("add", u, r))

    async def remove_role(self, g, u, r, reason=""):
        self.calls.append(("remove", u, r))


def test_bot_applies_and_confirms(s, monkeypatch):
    setup(s)
    quiet.set_hours(s, GUILD, 1, "Rae", 23, 7, "America/New_York", {900, 800})
    s.commit()
    b = botmod.NyerkBot(0)
    http = FakeHttp()
    monkeypatch.setattr(b, "http", http)
    monkeypatch.setattr(b, "_guild", lambda gid: type("G", (), {"id": gid})())
    monkeypatch.setattr(botmod, "now", lambda: at(23, 30))
    asyncio.run(b.apply_quiet_hours())
    assert http.calls == [("remove", 1, 900)]
    s.expire_all()
    assert quiet.get(s, GUILD, 1).state == "quiet"
    monkeypatch.setattr(botmod, "now", lambda: at(8))
    asyncio.run(b.apply_quiet_hours())
    assert http.calls[-1] == ("add", 1, 900)


def test_reaction_during_quiet_is_held_not_granted(s, monkeypatch):
    setup(s)
    quiet.set_hours(s, GUILD, 1, "Rae", 23, 7, "America/New_York", set())
    quiet.confirm(s, GUILD, 1, "quiet", set())
    get_settings(s, GUILD).reaction_messages = {"42": {"1️⃣": 900}}
    s.commit()
    b = botmod.NyerkBot(0)
    http = FakeHttp()
    monkeypatch.setattr(b, "http", http)
    monkeypatch.setattr(b, "get_guild", lambda _gid: None)
    payload = type("P", (), {"user_id": 1, "guild_id": GUILD, "message_id": 42, "emoji": "1️⃣",
                             "member": type("M", (), {"display_name": "Rae"})()})()
    asyncio.run(b._reaction(payload, True))
    assert http.calls == []
    s.expire_all()
    row = s.scalar(select(RoleHolder).where(RoleHolder.user_id == 1, RoleHolder.role_id == 900))
    assert row.suspended
