"""Settings → Create channel: Nyerk makes its channels with members' permissions set."""
import asyncio
import itertools

from nyerk import bot as botmod
from nyerk.db import get_settings

from .conftest import GUILD, G

_ids = itertools.count(970_000_000_000_000_000)


class FakeRole:
    def __init__(self, rid):
        self.id = rid

    def __hash__(self):
        return hash(self.id)


class FakeGuild:
    def __init__(self):
        self.default_role, self.me, self.categories, self.channels = FakeRole(GUILD), FakeRole(1), [], []

    def get_role(self, rid):
        return FakeRole(rid)

    async def create_category(self, name, reason=None):
        c = type("Cat", (), {"id": next(_ids), "name": name})()
        self.categories.append(c)
        return c

    async def create_text_channel(self, name, category=None, overwrites=None, reason=None):
        ch = type("Ch", (), {"id": next(_ids), "name": name, "category": category, "overwrites": overwrites})()
        self.channels.append(ch)
        return ch


def test_create_missing_channels(s, monkeypatch):
    st, g = get_settings(s, GUILD), G(s)
    st.roles_channel_id, g.timer_channel_id, g.party_channel_id, st.log_channel_id = 0, 0, 0, 0
    st.admin_role_ids = "555"
    s.commit()
    guild = FakeGuild()
    b = botmod.NyerkBot(0)
    monkeypatch.setattr(b, "_guild", lambda gid: guild)
    made = asyncio.run(b.create_channels(GUILD, g.id, ["timer", "party", "roles", "logs"]))
    assert made == ["#timers", "#party-finder", "#roles", "#nyerk-logs"]
    assert [c.name for c in guild.categories] == ["Nyerk"]
    by = {c.name: c for c in guild.channels}
    everyone = {n: c.overwrites[guild.default_role] for n, c in by.items()}
    assert everyone["timers"].send_messages is False and everyone["party-finder"].send_messages is True
    assert everyone["roles"].add_reactions is False                    # only Nyerk's own reactions
    assert everyone["nyerk-logs"].view_channel is False
    assert any(r.id == 555 and o.view_channel for r, o in by["nyerk-logs"].overwrites.items())  # admin role
    s.expire_all()
    st, g = get_settings(s, GUILD), G(s)
    assert (g.timer_channel_id, g.party_channel_id, st.roles_channel_id, st.log_channel_id) == \
        tuple(by[n].id for n in ("timers", "party-finder", "roles", "nyerk-logs"))
    assert asyncio.run(b.create_channels(GUILD, g.id, ["timer"])) == []  # already set: nothing new
