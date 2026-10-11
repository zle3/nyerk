from sqlalchemy import select

from nyerk import party, stats, tracking
from nyerk.models import PfClass, PfContent

from .test_party import T, reg, who


def setup(s):
    for i, c in enumerate(party.classes(s)):
        c.role_id = 1001 + i
    s.add_all([PfContent(name="Fire Temple", min_gear_score=1600), PfContent(name="Raid")])
    s.flush()
    return {c.name: c.id for c in s.scalars(select(PfContent))}


def test_activity_demand_and_top_members(s):
    ids = setup(s)
    b = {x.label: x.id for x in party.brackets(s)}
    w = party.windows(s)
    reg(s, who(s, 11, "Cleric"), b["1600-1899"], w[2].id, [ids["Fire Temple"], ids["Raid"]], T)
    reg(s, who(s, 12, "Ranger"), b["1900-2100"], w[0].id, [ids["Fire Temple"]], T)
    p = party.create(s, who(s, 1, "Templar"), ids["Fire Temple"], 5, "none", "", "", T)
    for uid, cls in ((11, "Cleric"), (12, "Ranger"), (13, "Ranger"), (14, "Sorcerer")):
        party.join(s, p, who(s, uid, cls), T + 600)
    q = party.create(s, who(s, 2, "Templar"), ids["Raid"], 20, "none", "", "", T + 3600)
    party.tick(s, q.expires_at + 1)
    s.flush()
    st = stats.compute(s, "all", T + 7200, "America/New_York")
    assert (st.parties, st.filled, st.expired, st.fill_rate, st.avg_fill_min) == (2, 1, 1, 50, 10)
    assert [(x.label, x.value) for x in st.content_interest] == [("Fire Temple", 2), ("Raid", 1)]
    assert {x.label: x.value for x in st.brackets}["1600-1899"] == 1
    assert {x.label: x.value for x in st.windows} == {"15 min": 1, "30 min": 0, "1 hr": 1, "2 hr": 0, "4 hr": 0}
    assert st.top_joiners[0][1] == 1 and len(st.top_joiners) == 4
    assert st.top_registrants[0] == ("user11", 1) or st.top_registrants[0][1] == 1
    assert sum(map(sum, st.heat)) == 2 and st.busiest
    assert [b.pct for b in st.content_interest] == [100, 50]


def test_range_filters_activity(s):
    ids = setup(s)
    party.create(s, who(s, 1, "Templar"), ids["Raid"], 5, "none", "", "", T)
    s.flush()
    assert stats.compute(s, "7", T + 8 * 86400, "UTC").parties == 0
    assert stats.compute(s, "30", T + 8 * 86400, "UTC").parties == 1


def test_class_and_ping_tracking(s):
    setup(s)
    s.commit()
    cleric = s.scalar(select(PfClass).where(PfClass.name == "Cleric"))
    templar = s.scalar(select(PfClass).where(PfClass.name == "Templar"))
    tracking.set_class(1, "a", cleric.role_id)
    tracking.set_class(2, "b", cleric.role_id)
    tracking.set_class(2, "b", templar.role_id)  # switching class replaces it
    tracking.set_class(3, "c", cleric.role_id)
    tracking.set_class(3, "c", None)             # cleared
    tracking.add_ping(1, "a", 555)
    tracking.add_ping(1, "a", 555)               # no double count
    tracking.add_ping(2, "b", 555)
    tracking.remove_pings(2, {555})
    st = stats.compute(s, "all", T, "UTC")
    counts = {x.label.split(" ", 1)[-1]: (x.value, x.note) for x in st.classes}
    assert counts["Cleric"] == (1, "50%") and counts["Templar"] == (1, "50%")
    assert {x.label: x.value for x in st.types} == {"Tank": 1, "Support": 1, "DPS": 0}
    assert [(x.label, x.value) for x in st.ping_roles] == [("Role 555", 1)]
    assert st.tracked_members == 2
