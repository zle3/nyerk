"""Numbers for the Stats page. Read-only queries over the database.

Class and ping-role counts come from role_holders, which Nyerk fills from
its own class buttons and reactions, so they cover members who used Nyerk
since tracking started. Activity counts come from registrations and parties
created in the selected time range.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import party
from .models import (
    Category, PfContent, PfInterest, PfParty, PfPartyMember, RoleHolder,
)

RANGES = {"7": ("Last 7 days", 7 * 86400), "30": ("Last 30 days", 30 * 86400), "all": ("All time", None)}
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
TOP = 10


@dataclass
class Bar:
    label: str
    value: float
    pct: int = 0          # bar length, 0-100, relative to the largest value in its chart
    note: str = ""        # shown after the value, e.g. "40%"


def bars(items: list[tuple[str, float]], note_share: bool = False) -> list[Bar]:
    top = max((v for _, v in items), default=0)
    total = sum(v for _, v in items) or 0
    out = []
    for label, v in items:
        pct = round(v * 100 / top) if top else 0
        note = f"{round(v * 100 / total)}%" if note_share and total else ""
        out.append(Bar(label, v, max(pct, 1 if v else 0), note))
    return out


@dataclass
class Stats:
    range_key: str
    range_label: str
    tz: str
    tracked_members: int = 0
    classes: list[Bar] = field(default_factory=list)
    types: list[Bar] = field(default_factory=list)
    ping_roles: list[Bar] = field(default_factory=list)
    registrations: int = 0
    registrants: int = 0
    content_interest: list[Bar] = field(default_factory=list)
    content_parties: list[Bar] = field(default_factory=list)
    brackets: list[Bar] = field(default_factory=list)
    windows: list[Bar] = field(default_factory=list)
    parties: int = 0
    filled: int = 0
    expired: int = 0
    closed: int = 0
    fill_rate: int | None = None
    avg_fill_min: int | None = None
    sizes: list[Bar] = field(default_factory=list)
    heat: list[list[int]] = field(default_factory=list)   # [weekday][hour]
    heat_max: int = 0
    busiest: list[str] = field(default_factory=list)
    top_leaders: list[tuple[str, int]] = field(default_factory=list)
    top_joiners: list[tuple[str, int]] = field(default_factory=list)
    top_registrants: list[tuple[str, int]] = field(default_factory=list)


def compute(s: Session, range_key: str, now: int, tz: str) -> Stats:
    if range_key not in RANGES:
        range_key = "30"
    label, span = RANGES[range_key]
    since = now - span if span else 0
    st = Stats(range_key, label, tz)
    zone = ZoneInfo(tz)

    # -- classes and roles (current holders) -----------------------------------
    holders = list(s.scalars(select(RoleHolder)))
    st.tracked_members = len({h.user_id for h in holders})
    class_by_role = {c.role_id: c for c in party.classes(s) if c.role_id}
    class_counts = Counter(h.role_id for h in holders if h.kind == "class")
    st.classes = bars([(f"{c.emoji} {c.name}".strip(), class_counts.get(c.role_id, 0))
                       for c in sorted(class_by_role.values(), key=lambda c: -class_counts.get(c.role_id, 0))],
                      note_share=True)
    type_counts = Counter(class_by_role[r].class_type for r in class_counts.elements() if r in class_by_role)
    st.types = bars([(party.TYPE_LABEL[t], type_counts.get(t, 0)) for t in party.TYPES], note_share=True)
    ping_counts = Counter(h.role_id for h in holders if h.kind == "ping")
    labels = {c.role_id: c.name for c in s.scalars(select(Category).where(Category.role_id != 0))}
    labels |= {c.role_id: f"{c.name} (dungeon)" for c in s.scalars(select(PfContent).where(PfContent.role_id != 0))}
    st.ping_roles = bars(sorted(((labels.get(r, f"Role {r}"), n) for r, n in ping_counts.items()),
                                key=lambda x: -x[1]))

    # -- content demand -----------------------------------------------------------
    regs = list(s.scalars(select(PfInterest).where(PfInterest.created_at >= since)))
    st.registrations = len(regs)
    st.registrants = len({r.user_id for r in regs})
    contents = party.contents(s, False)
    names = {c.id: party.party_title_for(c) for c in contents}
    interest = Counter(cid for r in regs for cid in (r.content_ids or []))
    parties = list(s.scalars(select(PfParty).where(PfParty.created_at >= since)))
    by_content = Counter(p.content_id for p in parties)
    st.content_interest = bars(sorted(((names.get(c, "Removed"), n) for c, n in interest.items()),
                                      key=lambda x: -x[1]))
    st.content_parties = bars(sorted(((names.get(c, "Removed"), n) for c, n in by_content.items()),
                                     key=lambda x: -x[1]))
    bracket_counts = Counter(r.bracket_id for r in regs)
    st.brackets = bars([(b.label, bracket_counts.get(b.id, 0)) for b in party.brackets(s)], note_share=True)
    window_labels = {w.minutes: w.label or f"{w.minutes} min" for w in party.windows(s)}
    window_counts = Counter(round((r.expires_at - r.created_at) / 60) for r in regs)
    st.windows = bars([(window_labels.get(m, f"{m} min"), window_counts.get(m, 0))
                       for m in sorted(set(window_labels) | set(window_counts))], note_share=True)

    # -- party activity -------------------------------------------------------------
    st.parties = len(parties)
    ever_full = [p for p in parties if p.filled_at or p.status in ("full", "done")]
    st.filled = len(ever_full)
    st.expired = sum(p.status == "expired" for p in parties)
    st.closed = sum(p.status == "closed" for p in parties)
    finished = [p for p in parties if p.status != "open"]
    if finished:
        st.fill_rate = round(100 * sum(p in ever_full for p in finished) / len(finished))
    waits = [p.filled_at - p.created_at for p in ever_full if p.filled_at and p.filled_at >= p.created_at]
    if waits:
        st.avg_fill_min = round(sum(waits) / len(waits) / 60)
    size_counts = Counter(p.size for p in parties)
    st.sizes = bars([(f"{n} players", size_counts.get(n, 0)) for n in (5, 20)], note_share=True)
    st.heat = [[0] * 24 for _ in range(7)]
    for p in parties:
        t = datetime.fromtimestamp(p.created_at, zone)
        st.heat[t.weekday()][t.hour] += 1
    st.heat_max = max((max(row) for row in st.heat), default=0)
    cells = sorted(((n, d, h) for d, row in enumerate(st.heat) for h, n in enumerate(row) if n), reverse=True)
    st.busiest = [f"{DAYS[d]} {h:02d}:00 ({n})" for n, d, h in cells[:3]]

    # -- top members ----------------------------------------------------------------
    leaders = Counter((p.leader_id, p.leader_name) for p in parties)
    st.top_leaders = [(name, n) for (_, name), n in leaders.most_common(TOP)]
    pids = [p.id for p in parties]
    leader_of = {p.id: p.leader_id for p in parties}
    joins = Counter()
    if pids:
        for m in s.scalars(select(PfPartyMember).where(PfPartyMember.party_id.in_(pids))):
            if m.user_id != leader_of.get(m.party_id):
                joins[(m.user_id, m.user_name)] += 1
    st.top_joiners = [(name, n) for (_, name), n in joins.most_common(TOP)]
    regs_by = Counter((r.user_id, r.user_name) for r in regs)
    st.top_registrants = [(name, n) for (_, name), n in regs_by.most_common(TOP)]
    return st


def heat_level(n: int, top: int) -> int:
    """0 for empty, then 1-5 for a light-to-dark single-hue ramp."""
    if not n or not top:
        return 0
    return max(1, min(5, round(n * 5 / top)))
