"""Everything Nyerk knows about the members of one Discord server, for the
admin Members page.

Nyerk has no member list (it runs without the privileged Server Members
intent), so "known members" are the people who show up in its own tables.
Live roles for one member are fetched from Discord when their page opens.
Every function here is for one server (guild_id): the same person in two
servers is two separate members.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import party, quiet
from .models import (
    Category, Event, Game, LogEntry, MemberGear, MemberProfile, PfInterest, PfParty, PfPartyMember, QuietHours,
    RoleHolder, UserTimer,
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


def _game_ids(s: Session, guild_id: int):
    return select(Game.id).where(Game.guild_id == guild_id)


def known(s: Session, guild_id: int, query: str = "") -> list[Summary]:
    people: dict[int, Summary] = {}

    def touch(uid: int, name: str) -> Summary:
        row = people.get(uid)
        if row is None:
            row = people[uid] = Summary(uid, name or f"User {uid}")
        elif name and row.name.startswith("User "):
            row.name = name
        return row

    games = {g.id: g.name for g in s.scalars(select(Game).where(Game.guild_id == guild_id))}
    many = len(games) > 1
    in_server = _game_ids(s, guild_id)
    class_names = {c.role_id: c.name for c in party.server_classes(s, guild_id) if c.role_id}
    for h in s.scalars(select(RoleHolder).where(RoleHolder.guild_id == guild_id)):
        m = touch(h.user_id, h.user_name)
        if h.kind == "class" and h.role_id in class_names:
            m.classes.append(class_names[h.role_id])
        elif h.kind == "ping":
            m.pings += 1
    names = {p.user_id: p.user_name for p in s.scalars(select(MemberProfile).where(MemberProfile.guild_id == guild_id))}
    for g in s.scalars(select(MemberGear).where(MemberGear.bracket_id.is_not(None), MemberGear.game_id.in_(in_server))
                       .order_by(MemberGear.game_id)):
        if g.bracket:
            m = touch(g.user_id, names.get(g.user_id, ""))
            label = f"{games.get(g.game_id, '?')}: {g.bracket.label}" if many else g.bracket.label
            m.gear = f"{m.gear}, {label}" if m.gear else label
    for q in s.scalars(select(QuietHours).where(QuietHours.enabled.is_(True), QuietHours.guild_id == guild_id)):
        touch(q.user_id, q.user_name).quiet = "quiet now" if q.state == "quiet" else "on"
    timers_here = select(UserTimer).join(Event).where(Event.game_id.in_(in_server))
    for t in s.scalars(timers_here.where(UserTimer.status == "active")):
        touch(t.user_id, t.user_name).timers += 1
    for r in s.scalars(select(PfInterest).where(PfInterest.status == "active", PfInterest.game_id.in_(in_server))):
        touch(r.user_id, r.user_name).registered = True
    for m, p in s.execute(select(PfPartyMember, PfParty).join(PfParty).where(
            PfParty.status.in_(party.LIVE), PfParty.game_id.in_(in_server))):
        row = touch(m.user_id, m.user_name)
        row.in_party = f"{row.in_party}, {party.party_title(p)}" if row.in_party else party.party_title(p)
    # Names seen anywhere else (finished timers) fill gaps.
    for t in s.scalars(timers_here.order_by(UserTimer.id.desc()).limit(500)):
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


def role_options(s: Session, guild_id: int) -> list[RoleOption]:
    """Every game's class, event and dungeon roles in a server; labels name
    the game when the server runs several."""
    games = {g.id: g.name for g in s.scalars(select(Game).where(Game.guild_id == guild_id)
                                             .order_by(Game.sort_order, Game.id))}
    many = len(games) > 1

    def name(game_id, text):
        return f"{games.get(game_id, '?')}: {text}" if many else text

    opts = [RoleOption(c.role_id, name(c.game_id, f"{c.emoji} {c.name}".strip()), "class",
                       party.TYPE_LABEL[c.class_type])
            for c in party.server_classes(s, guild_id) if c.role_id]
    opts += [RoleOption(c.role_id, name(c.game_id, c.name), "event")
             for c in s.scalars(select(Category).where(Category.role_id != 0, Category.game_id.in_(games))
                                .order_by(Category.game_id, Category.sort_order))]
    opts += [RoleOption(c.role_id, name(c.game_id, party.party_title_for(c)), "dungeon")
             for c in party.server_contents(s, guild_id, False) if c.role_id]
    return opts


def type_roles(s: Session, guild_id: int) -> set[int]:
    return {r for g in s.scalars(select(Game).where(Game.guild_id == guild_id))
            for r in (g.tank_role_id, g.support_role_id, g.dps_role_id) if r}


def managed_role_ids(s: Session, guild_id: int) -> set[int]:
    return {o.role_id for o in role_options(s, guild_id)} | type_roles(s, guild_id)


def desired_roles(s: Session, guild_id: int, chosen: set[int]) -> set[int]:
    """Chosen class and ping roles plus the type roles the classes need
    (each class's own game's Tank, Support or DPS role)."""
    opts = {o.role_id: o for o in role_options(s, guild_id)}
    games = {g.id: party.type_role_ids(g) for g in s.scalars(select(Game).where(Game.guild_id == guild_id))}
    want = {r for r in chosen if r in opts}
    for c in party.server_classes(s, guild_id):
        role = games.get(c.game_id, {}).get(c.class_type)
        if c.role_id in want and role:
            want.add(role)
    return want


def plan_roles(s: Session, guild_id: int, user_id: int, live: set[int],
               chosen: set[int]) -> tuple[set[int], set[int], set[int]]:
    """-> (add in Discord, remove in Discord, dungeon roles to hold as paused).
    During quiet hours, chosen dungeon roles are held instead of granted."""
    want = desired_roles(s, guild_id, chosen)
    managed = managed_role_ids(s, guild_id)
    hold: set[int] = set()
    if quiet.is_quiet(s, guild_id, user_id):
        hold = want & quiet.content_role_ids(s, guild_id)
        want -= hold
    return want - live, (managed & live) - want, hold


def paused_roles(s: Session, guild_id: int, user_id: int) -> set[int]:
    return {r.role_id for r in s.scalars(select(RoleHolder).where(
        RoleHolder.guild_id == guild_id, RoleHolder.user_id == user_id, RoleHolder.suspended.is_(True)))}


def record(s: Session, guild_id: int, user_id: int, name: str, chosen: set[int], hold: set[int]) -> None:
    """Bring role_holders in line with what the admin just set."""
    opts = {o.role_id: o for o in role_options(s, guild_id)}
    rows = {r.role_id: r for r in s.scalars(select(RoleHolder).where(RoleHolder.guild_id == guild_id,
                                                                     RoleHolder.user_id == user_id))}
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
            s.add(RoleHolder(guild_id=guild_id, user_id=user_id, user_name=name, role_id=rid, kind=kind, since=now,
                             suspended=rid in hold))
        else:
            row.suspended = rid in hold
            if name:
                row.user_name = name


def detail(s: Session, guild_id: int, user_id: int) -> dict:
    name = next((m.name for m in known(s, guild_id) if m.user_id == user_id), "")
    in_server = _game_ids(s, guild_id)
    timers = list(s.scalars(select(UserTimer).join(Event).where(
        UserTimer.user_id == user_id, UserTimer.status == "active", Event.game_id.in_(in_server))
        .order_by(UserTimer.fire_at)))
    for t in timers:
        _ = t.event
    games = {g.id: g.name for g in s.scalars(select(Game).where(Game.guild_id == guild_id))}
    many = len(games) > 1
    titles = {c.id: party.party_title_for(c) for c in party.server_contents(s, guild_id, False)}
    registrations = []
    for reg in s.scalars(select(PfInterest).where(PfInterest.user_id == user_id, PfInterest.status == "active",
                                                  PfInterest.game_id.in_(in_server)).order_by(PfInterest.game_id)):
        text = f"{', '.join(titles.get(i, '?') for i in reg.content_ids or [])} as " \
               f"{reg.pf_class.name if reg.pf_class else '?'}"
        registrations.append((reg, f"{games.get(reg.game_id, '?')}: {text}" if many else text))
    parties = [(p, f"{games.get(p.game_id, '?')}: {party.party_title(p)}" if many else party.party_title(p))
               for p in party.live_parties_for(s, user_id, guild_id)]
    for p, _ in parties:
        _ = p.members
    q = quiet.get(s, guild_id, user_id)
    tracked = {r.role_id for r in s.scalars(select(RoleHolder).where(RoleHolder.guild_id == guild_id,
                                                                     RoleHolder.user_id == user_id))}
    return dict(name=name, timers=timers, registrations=registrations, parties=parties, quiet=q,
                quiet_text=quiet.window_text(q) if q else "", tracked=tracked,
                paused=paused_roles(s, guild_id, user_id))


FORMER = "Former member"


@dataclass
class Forgotten:
    name: str
    left_party: str = ""
    removed: dict[str, int] = field(default_factory=dict)


def forget(s: Session, guild_id: int, user_id: int, now: int) -> Forgotten:
    """Erase what Nyerk keeps about one member of one server (a privacy
    request). Their roles, gear score, quiet hours, registrations and past
    party seats are deleted; boss timers they reported and parties they led
    stay for everyone else, without their name; log lines lose their name.
    Other servers they're in are not touched. Discord roles are removed
    separately by the caller, before this runs."""
    name = next((m.name for m in known(s, guild_id) if m.user_id == user_id), "")
    out = Forgotten(name)
    in_server = _game_ids(s, guild_id)
    left = []
    for p in party.live_parties_for(s, user_id, guild_id):
        left.append(party.party_title(p))
        party.remove_member(s, p, user_id, now)
    out.left_party = ", ".join(left)
    parties_here = select(PfParty.id).where(PfParty.game_id.in_(in_server))
    for label, query in (
            ("roles", select(RoleHolder).where(RoleHolder.guild_id == guild_id, RoleHolder.user_id == user_id)),
            ("gear score", select(MemberGear).where(MemberGear.game_id.in_(in_server), MemberGear.user_id == user_id)),
            ("profile", select(MemberProfile).where(MemberProfile.guild_id == guild_id,
                                                    MemberProfile.user_id == user_id)),
            ("quiet hours", select(QuietHours).where(QuietHours.guild_id == guild_id, QuietHours.user_id == user_id)),
            ("registrations", select(PfInterest).where(PfInterest.game_id.in_(in_server),
                                                       PfInterest.user_id == user_id)),
            ("party seats", select(PfPartyMember).where(PfPartyMember.party_id.in_(parties_here),
                                                        PfPartyMember.user_id == user_id))):
        rows = list(s.scalars(query))
        for r in rows:
            s.delete(r)
        if rows:
            out.removed[label] = len(rows)
    timers = list(s.scalars(select(UserTimer).join(Event).where(UserTimer.user_id == user_id,
                                                                Event.game_id.in_(in_server))))
    for t in timers:
        t.user_id, t.user_name = 0, FORMER
    if timers:
        out.removed["boss timers (kept, name removed)"] = len(timers)
    led = list(s.scalars(select(PfParty).where(PfParty.leader_id == user_id, PfParty.game_id.in_(in_server))))
    for p in led:
        p.leader_id, p.leader_name = 0, FORMER
    if led:
        out.removed["parties led (kept, name removed)"] = len(led)
    if name:
        pattern = re.compile(rf"(?<!\w){re.escape(name)}(?!\w)")
        scrubbed = 0
        for row in s.scalars(select(LogEntry).where(LogEntry.guild_id == guild_id, LogEntry.text.contains(name))):
            new = pattern.sub(FORMER.lower(), row.text)
            if new != row.text:
                row.text, scrubbed = new, scrubbed + 1
        if scrubbed:
            out.removed["log lines (name removed)"] = scrubbed
    return out
