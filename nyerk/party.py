"""Party finder logic: classes, interest registrations, parties and slots,
plus the plain-text bodies of every party finder message.

No Discord code here. Handlers in party_bot.py change state through these
functions; the reconciler there then makes the channel match the database.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .db import get_settings
from .engine import escape_md, ts
from .models import (
    MemberProfile, PfBracket, PfClass, PfContent, PfInterest, PfParty, PfPartyMember, PfWindow, Settings,
)

TYPES = ("tank", "support", "dps")
TYPE_LABEL = {"tank": "Tank", "support": "Support", "dps": "DPS", "any": "Open"}
LIVE = ("open", "full")
LIMIT = 2000


class PartyError(Exception):
    """A rule stopped the action; the message is shown privately to the user."""


@dataclass
class Who:
    user_id: int
    user_name: str
    role_ids: set[int]


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# ----------------------------------------------------------------------------
# Lookups


def classes(s: Session) -> list[PfClass]:
    return list(s.scalars(select(PfClass).order_by(PfClass.sort_order, PfClass.id)))


def brackets(s: Session) -> list[PfBracket]:
    return list(s.scalars(select(PfBracket).order_by(PfBracket.sort_order, PfBracket.id)))


def windows(s: Session) -> list[PfWindow]:
    return list(s.scalars(select(PfWindow).order_by(PfWindow.minutes)))


def contents(s: Session, active_only: bool = True) -> list[PfContent]:
    q = select(PfContent).order_by(PfContent.sort_order, PfContent.name)
    if active_only:
        q = q.where(PfContent.active.is_(True))
    return list(s.scalars(q))


def member_classes(s: Session, role_ids: set[int]) -> list[PfClass]:
    """A member's classes come from their roles on every click; they can
    hold several."""
    return [c for c in classes(s) if c.role_id and c.role_id in role_ids]


def member_class(s: Session, role_ids: set[int]) -> PfClass | None:
    held = member_classes(s, role_ids)
    return held[0] if held else None


def require_classes(s: Session, who: Who) -> list[PfClass]:
    held = member_classes(s, who.role_ids)
    if not held:
        raise PartyError("Pick at least one class on the class panel first.")
    return held


def require_class(s: Session, who: Who) -> PfClass:
    return require_classes(s, who)[0]


def pick_class_and_slot(party: PfParty, held: list[PfClass]) -> tuple[PfClass | None, str | None]:
    """With several classes, play the one that fills a slot reserved for its
    type first; otherwise any class that fits an open slot."""
    for c in held:
        slot = pick_slot(party, c.class_type)
        if slot == c.class_type:
            return c, slot
    for c in held:
        slot = pick_slot(party, c.class_type)
        if slot:
            return c, slot
    return None, None


def type_role_ids(settings: Settings) -> dict[str, int]:
    return {"tank": settings.tank_role_id, "support": settings.support_role_id, "dps": settings.dps_role_id}


def live_party_for(s: Session, user_id: int) -> PfParty | None:
    return s.scalar(
        select(PfParty).join(PfPartyMember)
        .where(PfPartyMember.user_id == user_id, PfParty.status.in_(LIVE)).limit(1)
    )


def active_interest(s: Session, user_id: int) -> PfInterest | None:
    return s.scalar(select(PfInterest).where(PfInterest.user_id == user_id, PfInterest.status == "active").limit(1))


def live_parties(s: Session, status: str | None = None) -> list[PfParty]:
    q = select(PfParty).options(selectinload(PfParty.members), selectinload(PfParty.content))
    q = q.where(PfParty.status == status) if status else q.where(PfParty.status.in_(LIVE))
    return list(s.scalars(q.order_by(PfParty.created_at)))


# ----------------------------------------------------------------------------
# Class selector


def class_change(s: Session, who: Who, class_id: int | None) -> tuple[set[int], set[int], str, list[PfClass]]:
    """Toggle one class (or clear all with None). Members can hold several
    classes; each type role stays while any held class has that type.
    Returns roles to add, roles to remove, the private reply, and the
    classes held afterwards."""
    settings = get_settings(s)
    all_classes = [c for c in classes(s) if c.role_id]
    type_roles = type_role_ids(settings)
    all_type = {r for r in type_roles.values() if r}
    held = [c for c in all_classes if c.role_id in who.role_ids]
    if class_id is None:
        owned = ({c.role_id for c in all_classes} | all_type) & who.role_ids
        return set(), owned, "All your classes have been cleared.", []
    c = s.get(PfClass, class_id)
    if c is None:
        raise PartyError("That class no longer exists.")
    if not c.role_id:
        raise PartyError(f"{c.name} has no role set up yet. Ask an admin.")
    if c in held:
        held.remove(c)
        verb = f"Removed {c.name}."
    else:
        held.append(c)
        verb = f"Added {c.name}."
    want_types = {type_roles.get(x.class_type, 0) for x in held} - {0}
    want = {x.role_id for x in held} | want_types
    owned = ({x.role_id for x in all_classes} | all_type) & who.role_ids
    add, remove = want - who.role_ids, owned - want
    held.sort(key=lambda x: (x.sort_order, x.id))
    summary = ", ".join(f"{x.name} ({TYPE_LABEL[x.class_type]})" for x in held) or "none"
    missing = sorted({TYPE_LABEL[x.class_type] for x in held if not type_roles.get(x.class_type)})
    note = f" The {', '.join(missing)} role is not set up yet." if missing else ""
    return add, remove, f"{verb} Your classes: {summary}.{note}", held


# ----------------------------------------------------------------------------
# Gear score (kept by Nyerk, not a Discord role)


def gear_of(s: Session, user_id: int) -> PfBracket | None:
    p = s.scalar(select(MemberProfile).where(MemberProfile.user_id == user_id))
    return p.bracket if p else None


def set_gear(s: Session, user_id: int, user_name: str, bracket_id: int | None, now: int) -> PfBracket | None:
    """One gear score per member; picking another replaces it, None clears."""
    bracket = s.get(PfBracket, bracket_id) if bracket_id else None
    if bracket_id and bracket is None:
        raise PartyError("That gear score option no longer exists.")
    p = s.scalar(select(MemberProfile).where(MemberProfile.user_id == user_id))
    if p is None:
        p = MemberProfile(user_id=user_id)
        s.add(p)
    p.user_name, p.bracket_id, p.updated_at = user_name or p.user_name, bracket.id if bracket else None, now
    # Keep an open registration in step, so pings use the new score.
    reg = active_interest(s, user_id)
    if reg:
        reg.bracket_id = p.bracket_id
    s.flush()
    return bracket


GEAR_PANEL = (
    "## Gear score\n"
    "Pick your gear score. It shows on party listings and decides which dungeon pings reach you. "
    "Pick another any time to change it."
)


# ----------------------------------------------------------------------------
# Interest list


def register(s: Session, who: Who, window_id: int, content_ids: list[int], now: int,
             class_id: int | None = None) -> PfInterest:
    held = require_classes(s, who)
    c = next((x for x in held if x.id == class_id), held[0])
    bracket = gear_of(s, who.user_id)
    if bracket is None:
        raise PartyError("Set your gear score in the roles channel first.")
    window = s.get(PfWindow, window_id)
    chosen = [x for x in (s.get(PfContent, i) for i in content_ids) if x is not None and x.active]
    if window is None or not chosen:
        raise PartyError("Pick how long you are free and at least one piece of content.")
    old = active_interest(s, who.user_id)
    if old:
        old.status = "cancelled"  # registering again replaces, never stacks
    row = PfInterest(
        user_id=who.user_id, user_name=who.user_name, class_id=c.id, class_type=c.class_type,
        bracket_id=bracket.id, content_ids=[x.id for x in chosen], created_at=now,
        expires_at=now + window.minutes * 60, last_pinged_at=old.last_pinged_at if old else 0,
    )
    s.add(row)
    s.flush()
    return row


def cancel_registration(s: Session, user_id: int) -> PfInterest | None:
    row = active_interest(s, user_id)
    if row:
        row.status = "cancelled"
    return row


# ----------------------------------------------------------------------------
# Slots


def parse_makeup(settings: Settings, size: int, requirement: str, custom: str) -> dict[str, int]:
    if size not in (5, 20):
        raise PartyError("Party size must be 5 or 20.")
    if requirement == "none":
        return {"tank": 0, "support": 0, "dps": 0, "any": size}
    if requirement == "default":
        if size != 5:
            raise PartyError("Standard makeup is for 5 person parties. Pick Open or Custom for 20.")
        counts = [settings.party_default_tank, settings.party_default_support, settings.party_default_dps]
    elif requirement == "custom":
        m = re.fullmatch(r"\s*(\d{1,2})\s*/\s*(\d{1,2})\s*/\s*(\d{1,2})\s*", custom or "")
        if not m:
            raise PartyError("Custom makeup is Tank/Support/DPS counts, for example 2/4/14.")
        counts = [int(m[1]), int(m[2]), int(m[3])]
    else:
        raise PartyError("Pick a requirement.")
    if sum(counts) > size:
        raise PartyError(f"That makeup needs {sum(counts)} players but the party has {size} slots.")
    return {"tank": counts[0], "support": counts[1], "dps": counts[2], "any": size - sum(counts)}


def used_slots(party: PfParty) -> dict[str, int]:
    used = {k: 0 for k in (*TYPES, "any")}
    for m in party.members:
        used[m.slot_type] = used.get(m.slot_type, 0) + 1
    return used


def pick_slot(party: PfParty, class_type: str) -> str | None:
    used = used_slots(party)
    if used.get(class_type, 0) < party.makeup.get(class_type, 0):
        return class_type
    if used["any"] < party.makeup.get("any", 0):
        return "any"
    return None


def is_full(party: PfParty) -> bool:
    return len(party.members) >= party.size


# ----------------------------------------------------------------------------
# Parties


def _member(s: Session, party: PfParty, who: Who, c: PfClass, slot: str, now: int) -> PfPartyMember:
    reg = active_interest(s, who.user_id)
    gear = gear_of(s, who.user_id) or (reg.bracket if reg else None)
    bracket = gear.label if gear else ""
    if reg:
        reg.status = "joined"  # joining ends the registration
    m = PfPartyMember(user_id=who.user_id, user_name=who.user_name, class_id=c.id, class_name=c.name,
                      class_type=c.class_type, slot_type=slot, bracket_label=bracket, joined_at=now)
    party.members.append(m)
    return m


def create(s: Session, who: Who, content_id: int, size: int, requirement: str, custom: str,
           description: str, now: int) -> PfParty:
    settings = get_settings(s)
    held = require_classes(s, who)
    if live_party_for(s, who.user_id):
        raise PartyError("You are already in a party. Leave it before starting another.")
    content = s.get(PfContent, content_id)
    if content is None or not content.active:
        raise PartyError("Pick an active piece of content.")
    makeup = parse_makeup(settings, size, requirement, custom)
    party = PfParty(
        content_id=content.id, leader_id=who.user_id, leader_name=who.user_name, size=size,
        requirement=requirement, makeup=makeup, description=(description or "").strip()[:100],
        status="open", created_at=now, expires_at=now + settings.party_lifetime_min * 60,
        ping_pending=True, members=[],
    )
    c, slot = pick_class_and_slot(party, held)
    if slot is None:
        types = "/".join(sorted({TYPE_LABEL[x.class_type] for x in held}))
        raise PartyError(f"That makeup has no slot for your type ({types}).")
    s.add(party)
    _member(s, party, who, c, slot, now)
    s.flush()
    return party


def join(s: Session, party: PfParty, who: Who, now: int) -> bool:
    """Instant join. Returns True if this filled the party."""
    if party.status != "open":
        raise PartyError("That party is no longer open.")
    if any(m.user_id == who.user_id for m in party.members):
        raise PartyError("You are already in this party.")
    held = require_classes(s, who)
    if live_party_for(s, who.user_id):
        raise PartyError("You are already in another party. Leave it first.")
    c, slot = pick_class_and_slot(party, held)
    if slot is None:
        types = "/".join(sorted({TYPE_LABEL[x.class_type] for x in held}))
        raise PartyError(f"No open slot for {types} in this party.")
    _member(s, party, who, c, slot, now)
    s.flush()
    if is_full(party):
        mark_full(party, now)
        return True
    return False


def mark_full(party: PfParty, now: int) -> None:
    party.status = "full"
    party.filled_at = now
    party.remaining_s = max(60, party.expires_at - now)
    party.ping_pending = False
    party.ready_pinged = False


def reopen(party: PfParty, now: int) -> None:
    """A member left a full party: back to an open listing at the bottom,
    with its remaining time restored. No new interest ping."""
    party.status = "open"
    party.expires_at = now + max(60, party.remaining_s)
    party.filled_at = None
    party.listing_message_id = 0  # reconciler posts a fresh listing below everything
    party.listing_hash = ""
    party.warned_at = None


@dataclass
class LeaveResult:
    closed: bool = False
    reopened: bool = False
    new_leader: str = ""


def remove_member(s: Session, party: PfParty, user_id: int, now: int) -> LeaveResult:
    member = next((m for m in party.members if m.user_id == user_id), None)
    if member is None or party.status not in LIVE:
        raise PartyError("That member is not in an active party.")
    was_full = party.status == "full"
    party.members.remove(member)
    s.flush()
    out = LeaveResult()
    if not party.members:
        end(party, "closed", now)
        out.closed = True
        return out
    if user_id == party.leader_id:
        heir = min(party.members, key=lambda m: (m.joined_at, m.id))  # longest-standing member
        party.leader_id, party.leader_name = heir.user_id, heir.user_name
        out.new_leader = heir.user_name
    if was_full:
        reopen(party, now)
        out.reopened = True
    return out


def end(party: PfParty, status: str, now: int) -> None:
    """closed / expired / done. The reconciler deletes its messages."""
    party.status = status
    party.ended_at = now
    party.ping_pending = False


def require_leader(party: PfParty, user_id: int) -> None:
    if party.leader_id != user_id:
        raise PartyError("Only the party leader can do that.")


def extend(s: Session, party: PfParty, now: int) -> None:
    settings = get_settings(s)
    if party.status != "open":
        raise PartyError("Only an open party can be extended.")
    if party.extensions_used >= settings.party_max_extensions:
        raise PartyError(f"This party has already been extended {settings.party_max_extensions} times.")
    party.expires_at = max(party.expires_at, now) + settings.party_extend_min * 60
    party.extensions_used += 1
    party.warned_at = None  # restart the warning


def ping_targets(s: Session, party: PfParty, now: int) -> list[PfInterest]:
    """Registrants for this content with an open slot for their type, whose
    bracket reaches the minimum, outside the ping cooldown, not in a party."""
    settings = get_settings(s)
    gap = settings.party_ping_gap_min * 60
    minimum = party.content.min_gear_score if party.content else 0
    out = []
    rows = s.scalars(select(PfInterest).where(PfInterest.status == "active", PfInterest.expires_at > now))
    for r in rows:
        if party.content_id not in (r.content_ids or []) or r.user_id == party.leader_id:
            continue
        gear = gear_of(s, r.user_id) or r.bracket
        if gear and gear.max_score < minimum:
            continue
        if r.last_pinged_at and now - r.last_pinged_at < gap:
            continue
        if pick_slot(party, r.class_type) is None or live_party_for(s, r.user_id):
            continue
        out.append(r)
    return out


# ----------------------------------------------------------------------------
# Time-driven transitions (run from the scheduler tick)


@dataclass
class TickResult:
    expired: list[str]
    done: list[str]
    interests_expired: int


def pause_clocks(s: Session, seconds: int, now: int) -> int:
    """Nyerk was down (an update or restart) for `seconds`. Push back every
    live party's and registration's deadline by that much, so nobody loses
    time they couldn't use: leaders could not extend, members could not join.
    Returns how many deadlines moved."""
    warn = get_settings(s).party_warn_min * 60
    moved = 0
    for p in live_parties(s):
        if p.status == "open":
            p.expires_at += seconds
            if p.warned_at is not None and now < p.expires_at - warn:
                p.warned_at = None  # the warning comes back when it is due again
        elif p.filled_at:
            p.filled_at += seconds
        moved += 1
    for r in s.scalars(select(PfInterest).where(PfInterest.status == "active")):
        r.expires_at += seconds
        moved += 1
    return moved


def leader_note(p: PfParty, user_id: int, name: str, what: str, became_leader: bool = False) -> str:
    """The private note a leader gets when someone joins (`what` = "joined"),
    leaves ("left") or is removed ("removed") from their party."""
    title, count = party_title(p), f"{len(p.members)}/{p.size}"
    if what == "joined":
        m = next((m for m in p.members if m.user_id == user_id), None)
        text = f"🟢 {_who(m) if m else escape_md(name)} joined your **{title}** party. {count}"
        return text + (", full and ready!" if p.status == "full" else "")
    verb = "was removed from" if what == "removed" else "left"
    text = f"🔴 {escape_md(name)} {verb} your **{title}** party. {count}"
    return text + (". You're the leader now." if became_leader else "")


def tick(s: Session, now: int) -> TickResult:
    settings = get_settings(s)
    res = TickResult([], [], 0)
    for p in live_parties(s):
        name = party_title(p)
        if p.status == "open":
            if now >= p.expires_at:
                end(p, "expired", now)
                res.expired.append(name)
            elif p.warned_at is None and now >= p.expires_at - settings.party_warn_min * 60:
                p.warned_at = now  # sent even if late, so the leader can still extend
        elif p.status == "full" and p.filled_at and now >= p.filled_at + settings.party_status_hold_min * 60:
            end(p, "done", now)
            res.done.append(name)
    for r in s.scalars(select(PfInterest).where(PfInterest.status == "active", PfInterest.expires_at <= now)):
        r.status = "expired"
        res.interests_expired += 1
    return res


# ----------------------------------------------------------------------------
# Message bodies (plain text)


def party_title(p: PfParty) -> str:
    name = p.content.name if p.content else "Party"
    return f"{p.content.emoji} {name}".strip() if p.content and p.content.emoji else name


def _who(m: PfPartyMember) -> str:
    bits = [m.class_name] + ([m.bracket_label] if m.bracket_label else [])
    return f"{escape_md(m.user_name)} ({', '.join(bits)})"


def listing_text(p: PfParty) -> str:
    minimum = p.content.min_gear_score if p.content else 0
    head = f"**[Party] {party_title(p)}** | {p.size} players"
    if minimum:
        head += f" | min gear {minimum}"
    head += f" | leader {escape_md(p.leader_name)}"
    lines = [head]
    if p.description:
        lines.append(escape_md(p.description))
    by_slot: dict[str, list[PfPartyMember]] = {}
    for m in p.members:
        by_slot.setdefault(m.slot_type, []).append(m)
    for slot in (*TYPES, "any"):
        cap = p.makeup.get(slot, 0)
        if not cap:
            continue
        who = by_slot.get(slot, [])
        names = ", ".join(_who(m) for m in who) or "open"
        label = "Players" if p.requirement == "none" else TYPE_LABEL[slot]
        lines.append(f"{label} {len(who)}/{cap}: {names}")
    lines.append(f"Closes {ts(p.expires_at, 'R')}")
    return "\n".join(lines)[:LIMIT]


def ping_text(p: PfParty, targets: list[PfInterest], role_id: int = 0) -> str:
    minimum = p.content.min_gear_score if p.content else 0
    mentions = " ".join(([f"<@&{role_id}>"] if role_id else []) + [f"<@{t.user_id}>" for t in targets])
    gear = f" (min gear {minimum})" if minimum else ""
    return f"{mentions}\nA party opened for **{party_title(p)}**{gear}. Join below, or pass if you can't make it."[:LIMIT]


def warning_text(p: PfParty, settings: Settings) -> str:
    left = settings.party_max_extensions - p.extensions_used
    ext = (f"Extend adds {settings.party_extend_min} minutes ({left} left)." if left > 0
           else "It can't be extended again.")
    return f"<@{p.leader_id}> your **{party_title(p)}** party closes {ts(p.expires_at, 'R')}. {ext}"


def ready_text(p: PfParty) -> str:
    mentions = " ".join(f"<@{m.user_id}>" for m in p.members)
    return f"{mentions}\nYour **{party_title(p)}** party is full and ready. Leader: {escape_md(p.leader_name)}."[:LIMIT]


CLASS_PANEL = (
    "## AION 2 Classes\n"
    "Pick every class you play. Each gives the class role and its type role (Tank, Support or DPS); "
    "click a class again to remove it.\n"
    "Parties put you in a slot one of your classes can fill. **My roles** shows what you have."
)

CREATE_PANEL = (
    "## Start a party\n"
    "Pick the content, size and makeup. Members registered for that content get pinged and can join in one click.\n"
    "Parties close after {lifetime} minutes unless the leader extends them."
)


def interest_panel_text(s: Session, now: int) -> str:
    head = (
        "## Looking for a group?\n"
        "Register below and Nyerk will @mention you when a party opens for content you picked.\n"
        "You can join straight from the ping. Your registration ends on its own, or press Cancel."
    )
    rows = list(s.scalars(
        select(PfInterest).where(PfInterest.status == "active", PfInterest.expires_at > now)
        .order_by(PfInterest.expires_at)
    ))
    if not rows:
        return head + "\n-# Nobody is registered right now."
    body: list[str] = []
    for c in contents(s):
        who = [r for r in rows if c.id in (r.content_ids or [])]
        if not who:
            continue
        body.append(f"### {party_title_for(c)}")
        for r in who:
            cls = r.pf_class.name if r.pf_class else "?"
            gear = gear_of(s, r.user_id) or r.bracket
            bracket = gear.label if gear else "?"
            body.append(f"{escape_md(r.user_name)} · {cls} ({TYPE_LABEL.get(r.class_type, '?')}) · {bracket}"
                        f" · until {ts(r.expires_at, 'R')}")
    text = head + "\n" + "\n".join(body)
    while len(text) > LIMIT:
        text = text[: text.rfind("\n")]
    return text


def party_title_for(c: PfContent) -> str:
    return f"{c.emoji} {c.name}".strip() if c.emoji else c.name


def chat_note(minutes: int) -> str:
    """The line under Party status telling members they may chat below."""
    if not minutes:
        return ""
    return f"💬 **Chat below to plan your runs.** Messages here are deleted after {minutes} minutes."


def status_text(parties: list[PfParty], chat_minutes: int = 0) -> str:
    lines = ["## Party status"]
    if not parties:
        lines.append("-# No full parties right now. Full parties show here until the leader presses Done.")
    for p in parties:
        lines.append(f"**{party_title(p)}** | {len(p.members)}/{p.size} | leader {escape_md(p.leader_name)}"
                     f" | ready {ts(p.filled_at or 0, 'R')}")
        lines.append(", ".join(_who(m) for m in p.members))
    note = chat_note(chat_minutes)
    text = "\n".join(lines)
    while len(text) + len(note) + 2 > LIMIT:
        text = text[: text.rfind("\n")]
    return f"{text}\n\n{note}" if note else text
