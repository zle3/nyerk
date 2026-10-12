"""Everything Nyerk knows about one member, for the admin Members page.

Nyerk has no member list (it runs without the privileged Server Members
intent), so "known members" are the people who show up in its own tables.
Live roles for one member are fetched from Discord when their page opens.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import party, quiet
from .db import get_settings
from .models import (
    Category, LogEntry, MemberProfile, PfInterest, PfParty, PfPartyMember, QuietHours, RoleHolder, UserTimer,
)


@dataclass
class Summary:
    user_id: int
    name: str
    classes: list[str] = field(default_factory=list)
    pings: int = 0
    quiet: str = ""          # "", "on", "quiet now"
    timers: int = 0
    registered: bool = False
    in_party: str = ""
    gear: str = ""


def known(s: Session, query: str = "") -> list[Summary]:
    people: dict[int, Summary] = {}

    def touch(uid: int, name: str) -> Summary:
        row = people.get(uid)
        if row is None:
            row = people[uid] = Summary(uid, name or f"User {uid}")
        elif name and row.name.startswith("User "):
            row.name = name
        return row

    class_names = {c.role_id: c.name for c in party.classes(s) if c.role_id}
    for h in s.scalars(select(RoleHolder)):
        m = touch(h.user_id, h.user_name)
        if h.kind == "class" and h.role_id in class_names:
            m.classes.append(class_names[h.role_id])
        elif h.kind == "ping":
            m.pings += 1
    for p in s.scalars(select(MemberProfile).where(MemberProfile.bracket_id.is_not(None))):
        touch(p.user_id, p.user_name).gear = p.bracket.label if p.bracket else ""
    for q in s.scalars(select(QuietHours).where(QuietHours.enabled.is_(True))):
        touch(q.user_id, q.user_name).quiet = "quiet now" if q.state == "quiet" else "on"
    for t in s.scalars(select(UserTimer).where(UserTimer.status == "active")):
        touch(t.user_id, t.user_name).timers += 1
    for r in s.scalars(select(PfInterest).where(PfInterest.status == "active")):
        touch(r.user_id, r.user_name).registered = True
    for m, p in s.execute(select(PfPartyMember, PfParty).join(PfParty).where(PfParty.status.in_(party.LIVE))):
        touch(m.user_id, m.user_name).in_party = party.party_title(p)
    # Names seen anywhere else (finished timers and parties) fill gaps.
    for t in s.scalars(select(UserTimer).order_by(UserTimer.id.desc()).limit(500)):
        if t.user_id in people and people[t.user_id].name.startswith("User "):
            people[t.user_id].name = t.user_name

    rows = sorted(people.values(), key=lambda m: m.name.lower())
    q = query.strip().lower()
    if q:
        rows = [m for m in rows if q in m.name.lower() or q == str(m.user_id)]
    return rows


@dataclass
class RoleOption:
    role_id: int
    label: str
    group: str               # class | event | dungeon
    type_label: str = ""


def role_options(s: Session) -> list[RoleOption]:
    opts = [RoleOption(c.role_id, f"{c.emoji} {c.name}".strip(), "class", party.TYPE_LABEL[c.class_type])
            for c in party.classes(s) if c.role_id]
    opts += [RoleOption(c.role_id, c.name, "event")
             for c in s.scalars(select(Category).where(Category.role_id != 0).order_by(Category.sort_order))]
    opts += [RoleOption(c.role_id, party.party_title_for(c), "dungeon")
             for c in party.contents(s, False) if c.role_id]
    return opts


def managed_role_ids(s: Session) -> set[int]:
    st = get_settings(s)
    types = {r for r in (st.tank_role_id, st.support_role_id, st.dps_role_id) if r}
    return {o.role_id for o in role_options(s)} | types


def desired_roles(s: Session, chosen: set[int]) -> set[int]:
    """Chosen class and ping roles plus the type roles the classes need."""
    opts = {o.role_id: o for o in role_options(s)}
    st = get_settings(s)
    by_type = {"tank": st.tank_role_id, "support": st.support_role_id, "dps": st.dps_role_id}
    want = {r for r in chosen if r in opts}
    for c in party.classes(s):
        if c.role_id in want and by_type.get(c.class_type):
            want.add(by_type[c.class_type])
    return want


def plan_roles(s: Session, user_id: int, live: set[int], chosen: set[int]) -> tuple[set[int], set[int], set[int]]:
    """-> (add in Discord, remove in Discord, dungeon roles to hold as paused).
    During quiet hours, chosen dungeon roles are held instead of granted."""
    want = desired_roles(s, chosen)
    managed = managed_role_ids(s)
    hold: set[int] = set()
    if quiet.is_quiet(s, user_id):
        hold = want & quiet.content_role_ids(s)
        want -= hold
    return want - live, (managed & live) - want, hold


def paused_roles(s: Session, user_id: int) -> set[int]:
    return {r.role_id for r in s.scalars(select(RoleHolder).where(
        RoleHolder.user_id == user_id, RoleHolder.suspended.is_(True)))}


def record(s: Session, user_id: int, name: str, chosen: set[int], hold: set[int]) -> None:
    """Bring role_holders in line with what the admin just set."""
    opts = {o.role_id: o for o in role_options(s)}
    rows = {r.role_id: r for r in s.scalars(select(RoleHolder).where(RoleHolder.user_id == user_id))}
    now = int(time.time())
    for rid, row in rows.items():
        if rid in opts and rid not in chosen:
            s.delete(row)
    for rid in chosen:
        o = opts.get(rid)
        if o is None:
            continue
        kind = "class" if o.group == "class" else "ping"
        row = rows.get(rid)
        if row is None:
            s.add(RoleHolder(user_id=user_id, user_name=name, role_id=rid, kind=kind, since=now,
                             suspended=rid in hold))
        else:
            row.suspended = rid in hold
            if name:
                row.user_name = name


def detail(s: Session, user_id: int) -> dict:
    name = next((m.name for m in known(s) if m.user_id == user_id), "")
    timers = list(s.scalars(select(UserTimer).where(UserTimer.user_id == user_id, UserTimer.status == "active")
                            .order_by(UserTimer.fire_at)))
    for t in timers:
        _ = t.event
    reg = s.scalar(select(PfInterest).where(PfInterest.user_id == user_id, PfInterest.status == "active"))
    reg_text = ""
    if reg:
        names = [party.party_title_for(c) for c in party.contents(s, False) if c.id in (reg.content_ids or [])]
        reg_text = f"{', '.join(names)} as {reg.pf_class.name if reg.pf_class else '?'}"
    p = party.live_party_for(s, user_id)
    q = quiet.get(s, user_id)
    tracked = {r.role_id for r in s.scalars(select(RoleHolder).where(RoleHolder.user_id == user_id))}
    return dict(name=name, timers=timers, registration=reg, registration_text=reg_text,
                party=p, party_title=party.party_title(p) if p else "", quiet=q,
                quiet_text=quiet.window_text(q) if q else "", tracked=tracked, paused=paused_roles(s, user_id))


FORMER = "Former member"


@dataclass
class Forgotten:
    name: str
    left_party: str = ""
    removed: dict[str, int] = field(default_factory=dict)


def forget(s: Session, user_id: int, now: int) -> Forgotten:
    """Erase what Nyerk keeps about one member (a privacy request). Their
    roles, gear score, quiet hours, registrations and past party seats are
    deleted; boss timers they reported and parties they led stay for
    everyone else, without their name; log lines lose their name. Discord
    roles are removed separately by the caller, before this runs."""
    name = next((m.name for m in known(s) if m.user_id == user_id), "")
    out = Forgotten(name)
    p = party.live_party_for(s, user_id)
    if p is not None:
        out.left_party = party.party_title(p)
        party.remove_member(s, p, user_id, now)
    for label, model in (("roles", RoleHolder), ("gear score", MemberProfile), ("quiet hours", QuietHours),
                         ("registrations", PfInterest), ("party seats", PfPartyMember)):
        rows = list(s.scalars(select(model).where(model.user_id == user_id)))
        for r in rows:
            s.delete(r)
        if rows:
            out.removed[label] = len(rows)
    timers = list(s.scalars(select(UserTimer).where(UserTimer.user_id == user_id)))
    for t in timers:
        t.user_id, t.user_name = 0, FORMER
    if timers:
        out.removed["boss timers (kept, name removed)"] = len(timers)
    led = list(s.scalars(select(PfParty).where(PfParty.leader_id == user_id)))
    for p in led:
        p.leader_id, p.leader_name = 0, FORMER
    if led:
        out.removed["parties led (kept, name removed)"] = len(led)
    if name:
        pattern = re.compile(rf"(?<!\w){re.escape(name)}(?!\w)")
        scrubbed = 0
        for row in s.scalars(select(LogEntry).where(LogEntry.text.contains(name))):
            new = pattern.sub(FORMER.lower(), row.text)
            if new != row.text:
                row.text, scrubbed = new, scrubbed + 1
        if scrubbed:
            out.removed["log lines (name removed)"] = scrubbed
    return out
