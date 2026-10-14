import pytest
from sqlalchemy import select

from nyerk.games import first as G
from nyerk import party
from nyerk.db import get_settings
from nyerk.models import PfClass, PfContent, PfInterest
from nyerk.party import PartyError, Who

T = 1_800_000_000


@pytest.fixture()
def pf(s):
    """Role IDs 1001.. for the 8 classes, two pieces of content."""
    for i, c in enumerate(party.classes(s, 1)):
        c.role_id = 1001 + i
    st = G(s)
    st.tank_role_id, st.support_role_id, st.dps_role_id = 2001, 2002, 2003
    s.add_all([PfContent(game_id=1, name="Fire Temple", min_gear_score=1600), PfContent(game_id=1, name="Raid", min_gear_score=1900)])
    s.flush()
    return s


def reg(s, w, bracket_id, window_id, content_ids, now, class_id=None):
    """Gear score is set first (roles channel), then the member registers."""
    party.set_gear(s, w.user_id, w.user_name, bracket_id, now)
    return party.register(s, w, window_id, content_ids, now, class_id)


def who(s, uid, cls):
    c = s.scalar(select(PfClass).where(PfClass.name == cls))
    return Who(uid, f"user{uid}", {c.role_id})


def content(s, name="Fire Temple"):
    return s.scalar(select(PfContent).where(PfContent.name == name))


def test_classes_toggle_and_type_roles_follow(pf):
    s = pf
    by = {c.name: c for c in party.classes(s, 1)}
    me = Who(1, "Rae", {by["Gladiator"].role_id, 2003, 555})  # Gladiator + DPS + unrelated
    add, remove, text, held = party.class_change(s, me, by["Cleric"].id)
    assert add == {by["Cleric"].role_id, 2002} and remove == set()  # keeps Gladiator and DPS
    assert text == "Added Cleric. Your classes: Gladiator (DPS), Cleric (Support)."
    me.role_ids |= add
    add, remove, text, held = party.class_change(s, me, by["Gladiator"].id)  # click again removes
    assert add == set() and remove == {by["Gladiator"].role_id, 2003}     # DPS role goes with it
    assert [c.name for c in held] == ["Cleric"]
    me.role_ids -= remove
    party.class_change(s, me, by["Ranger"].id)
    add, remove, _, held = party.class_change(s, Who(1, "Rae", {by["Cleric"].role_id, by["Ranger"].role_id,
                                                                2002, 2003}), None, 1)
    assert add == set() and remove == {by["Cleric"].role_id, by["Ranger"].role_id, 2002, 2003} and held == []


def test_type_role_stays_while_another_class_has_it(pf):
    s = pf
    by = {c.name: c for c in party.classes(s, 1)}
    me = Who(1, "Rae", {by["Ranger"].role_id, by["Sorcerer"].role_id, 2003})
    add, remove, _, _ = party.class_change(s, me, by["Ranger"].id)
    assert remove == {by["Ranger"].role_id}  # Sorcerer still needs DPS


def test_multi_class_member_fills_the_needed_slot(pf):
    s = pf
    by = {c.name: c for c in party.classes(s, 1)}
    p = party.create(s, who(s, 1, "Templar"), content(s).id, 5, "default", "", "", T)
    for uid in (2, 3, 4):
        party.join(s, p, who(s, uid, "Ranger"), T)
    flex = Who(9, "Flex", {by["Ranger"].role_id, by["Cleric"].role_id})  # DPS is full, support is open
    party.join(s, p, flex, T)
    m = next(m for m in p.members if m.user_id == 9)
    assert (m.class_name, m.slot_type) == ("Cleric", "support")


def test_register_as_chosen_class(pf):
    s = pf
    by = {c.name: c for c in party.classes(s, 1)}
    flex = Who(9, "Flex", {by["Ranger"].role_id, by["Cleric"].role_id})
    b, w = party.brackets(s, 1)[0].id, party.windows(s, 1)[0].id
    row = reg(s, flex, b, w, [content(s).id], T, class_id=by["Cleric"].id)
    assert (row.class_id, row.class_type) == (by["Cleric"].id, "support")


def test_makeup_rules(pf):
    st = G(pf)
    assert party.parse_makeup(st, 5, "default", "") == {"tank": 1, "support": 1, "dps": 3, "any": 0}
    assert party.parse_makeup(st, 20, "custom", "2/4/10") == {"tank": 2, "support": 4, "dps": 10, "any": 4}
    assert party.parse_makeup(st, 20, "none", "")["any"] == 20
    with pytest.raises(PartyError, match="Standard makeup is for 5"):
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
    b = {x.label: x.id for x in party.brackets(s, 1)}
    w = party.windows(s, 1)[0].id
    low = reg(s, who(s, 10, "Ranger"), b["1400-1599"], w, [content(s).id], T)
    ok = reg(s, who(s, 11, "Cleric"), b["1600-1899"], w, [content(s).id], T)
    other = reg(s, who(s, 12, "Ranger"), b["1900-2100"], w, [content(s, "Raid").id], T)
    again = reg(s, who(s, 11, "Cleric"), b["1600-1899"], w, [content(s).id], T + 1)
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
    with pytest.raises(PartyError, match="Pick at least one class"):
        party.register(pf, Who(9, "x", set()), 1, [1], T)


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


def test_gear_score_is_single_choice_and_feeds_pings(pf):
    s = pf
    b = {x.label: x.id for x in party.brackets(s, 1)}
    w = party.windows(s, 1)[0].id
    member = who(s, 11, "Cleric")
    assert party.set_gear(s, 11, "user11", b["1400-1599"], T).label == "1400-1599"
    party.register(s, member, w, [content(s).id], T)
    p = party.create(s, who(s, 1, "Templar"), content(s).id, 5, "none", "", "", T)  # min gear 1600
    assert party.ping_targets(s, p, T + 1) == []
    party.set_gear(s, 11, "user11", b["2100+"], T + 2)  # picking another replaces it
    assert [t.user_id for t in party.ping_targets(s, p, T + 3)] == [11]
    assert party.gear_of(s, 11, 1).label == "2100+"
    party.set_gear(s, 11, "user11", None, T + 4, 1)
    assert party.gear_of(s, 11, 1) is None


def test_register_needs_gear_score(pf):
    with pytest.raises(PartyError, match="gear score"):
        party.register(pf, who(pf, 11, "Cleric"), party.windows(pf, 1)[0].id, [content(pf).id], T)


def test_listing_shows_saved_gear(pf):
    s = pf
    party.set_gear(s, 1, "user1", party.brackets(s, 1)[3].id, T)
    p = party.create(s, who(s, 1, "Templar"), content(s).id, 5, "default", "", "", T)
    assert "Tank 1/1: user1 (Templar, 2100+)" in party.listing_text(p)
