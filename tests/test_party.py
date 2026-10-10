import pytest
from sqlalchemy import select

from nyerk import party
from nyerk.db import get_settings
from nyerk.models import PfClass, PfContent, PfInterest
from nyerk.party import PartyError, Who

T = 1_800_000_000


@pytest.fixture()
def pf(s):
    """Role IDs 1001.. for the 8 classes, two pieces of content."""
    for i, c in enumerate(party.classes(s)):
        c.role_id = 1001 + i
    st = get_settings(s)
    st.tank_role_id, st.support_role_id, st.dps_role_id = 2001, 2002, 2003
    s.add_all([PfContent(name="Fire Temple", min_gear_score=1600), PfContent(name="Raid", min_gear_score=1900)])
    s.flush()
    return s


def who(s, uid, cls):
    c = s.scalar(select(PfClass).where(PfClass.name == cls))
    return Who(uid, f"user{uid}", {c.role_id})


def content(s, name="Fire Temple"):
    return s.scalar(select(PfContent).where(PfContent.name == name))


def test_class_change_swaps_class_and_type_roles(pf):
    s = pf
    me = Who(1, "Rae", {1001, 2003, 555})  # Gladiator + DPS + unrelated
    cleric = s.scalar(select(PfClass).where(PfClass.name == "Cleric"))
    add, remove, text = party.class_change(s, me, cleric.id)
    assert add == {cleric.role_id, 2002} and remove == {1001, 2003}
    assert text == "You are now Cleric (Support)."
    add, remove, _ = party.class_change(s, Who(1, "Rae", {cleric.role_id, 2002}), None)
    assert add == set() and remove == {cleric.role_id, 2002}


def test_makeup_rules(pf):
    st = get_settings(pf)
    assert party.parse_makeup(st, 5, "default", "") == {"tank": 1, "support": 1, "dps": 3, "any": 0}
    assert party.parse_makeup(st, 20, "custom", "2/4/10") == {"tank": 2, "support": 4, "dps": 10, "any": 4}
    assert party.parse_makeup(st, 20, "none", "")["any"] == 20
    with pytest.raises(PartyError, match="Default makeup is for 5"):
        party.parse_makeup(st, 20, "default", "")
    with pytest.raises(PartyError, match="needs 21"):
        party.parse_makeup(st, 20, "custom", "5/5/11")


def test_create_join_fill_leave_reopen(pf):
    s = pf
    p = party.create(s, who(s, 1, "Templar"), content(s).id, 5, "default", "", "fast", T)
    assert p.members[0].slot_type == "tank" and p.ping_pending
    with pytest.raises(PartyError, match="No open slot for Tank"):
        party.join(s, p, who(s, 2, "Templar"), T)
    assert not party.join(s, p, who(s, 3, "Cleric"), T)
    for uid in (4, 5):
        party.join(s, p, who(s, uid, "Ranger"), T)
    assert party.join(s, p, who(s, 6, "Sorcerer"), T + 600)  # fills it
    assert p.status == "full" and p.remaining_s == 3000
    with pytest.raises(PartyError, match="no longer open"):
        party.join(s, p, who(s, 7, "Gladiator"), T)
    res = party.remove_member(s, p, 4, T + 1200)
    assert res.reopened and p.status == "open" and p.expires_at == T + 1200 + 3000
    assert p.listing_message_id == 0 and not p.ping_pending  # new listing, no re-ping


def test_one_party_at_a_time_and_leader_promotion(pf):
    s = pf
    p = party.create(s, who(s, 1, "Templar"), content(s).id, 5, "none", "", "", T)
    party.join(s, p, who(s, 2, "Cleric"), T + 5)
    party.join(s, p, who(s, 3, "Ranger"), T + 10)
    with pytest.raises(PartyError, match="already in a party"):
        party.create(s, who(s, 2, "Cleric"), content(s).id, 5, "none", "", "", T)
    res = party.remove_member(s, p, 1, T + 20)
    assert res.new_leader == "user2" and p.leader_id == 2
    party.remove_member(s, p, 2, T + 30)
    assert party.remove_member(s, p, 3, T + 40).closed and p.status == "closed"


def test_registration_ping_targets_and_cooldown(pf):
    s = pf
    b = {x.label: x.id for x in party.brackets(s)}
    w = party.windows(s)[0].id
    low = party.register(s, who(s, 10, "Ranger"), b["1400-1599"], w, [content(s).id], T)
    ok = party.register(s, who(s, 11, "Cleric"), b["1600-1899"], w, [content(s).id], T)
    other = party.register(s, who(s, 12, "Ranger"), b["1900-2100"], w, [content(s, "Raid").id], T)
    again = party.register(s, who(s, 11, "Cleric"), b["1600-1899"], w, [content(s).id], T + 1)
    assert ok.status == "cancelled" and again.status == "active"  # replaced, not stacked
    p = party.create(s, who(s, 1, "Templar"), content(s).id, 5, "default", "", "", T + 2)
    targets = party.ping_targets(s, p, T + 3)
    assert [t.user_id for t in targets] == [11]  # 10 below min gear, 12 other content
    again.last_pinged_at = T + 3
    assert party.ping_targets(s, p, T + 60) == []  # 15 minute cooldown
    party.join(s, p, who(s, 11, "Cleric"), T + 70)
    assert again.status == "joined"
    assert low.status == "active" and other.status == "active"


def test_register_needs_class(pf):
    with pytest.raises(PartyError, match="Pick your class"):
        party.register(pf, Who(9, "x", set()), 1, 1, [1], T)


def test_tick_warns_expires_and_holds(pf):
    s = pf
    p = party.create(s, who(s, 1, "Templar"), content(s).id, 5, "none", "", "", T)
    party.tick(s, T + 49 * 60)
    assert p.warned_at is None
    party.tick(s, T + 50 * 60)
    assert p.warned_at == T + 50 * 60
    party.extend(s, p, T + 51 * 60)
    assert p.expires_at == T + 120 * 60 and p.warned_at is None
    party.extend(s, p, T + 52 * 60)
    with pytest.raises(PartyError, match="already been extended 2"):
        party.extend(s, p, T + 53 * 60)
    res = party.tick(s, T + 181 * 60)
    assert p.status == "expired" and res.expired


def test_listing_text(pf):
    s = pf
    p = party.create(s, who(s, 1, "Templar"), content(s).id, 5, "default", "", "know the mechanics", T)
    text = party.listing_text(p)
    lines = text.splitlines()
    assert lines[0] == "**[Party] Fire Temple** | 5 players | min gear 1600 | leader user1"
    assert lines[1] == "know the mechanics"
    assert lines[2] == "Tank 1/1: user1 (Templar)"
    assert lines[3] == "Support 0/1: open" and lines[4] == "DPS 0/3: open"
    assert lines[5].startswith("Closes <t:")
