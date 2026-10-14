"""A deterministic demo server for screenshots: fake members, fake IDs,
fixed clock. Nothing here touches Discord or real data."""
from __future__ import annotations

import random
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select

from nyerk import db, games, party, quiet, timers, tracking
from nyerk.db import get_settings, get_state
from nyerk.models import Category, Event, LogEntry, PfContent, ReactionGroup
from nyerk.party import Who
from nyerk.seed import seed, upgrade_defaults
from nyerk.web.app import hash_password

NY = ZoneInfo("America/New_York")
# Wednesday afternoon, server time. Everything in the screenshots is relative to this.
NOW = int(datetime(2026, 10, 14, 14, 52, tzinfo=NY).timestamp())
PASSWORD = "screenshots-only"
GUILD_ID = 900000000000000001
OWNER_ID = 900000000000000002
ADMIN_ROLE_ID = 900000000000000003

MEMBERS = ["Raelin", "Kai", "Mira", "Tovan", "Sel", "Orrin", "Vex", "Lyra", "Bram", "Iska", "Juno", "Dax",
           "Nell", "Quill", "Ash", "Pip"]
CONTENT = [("Fire Temple", "🔥", 1600), ("Urugugu Canyon", "🏜️", 1500), ("Vakron Sky Island", "🏝️", 1700),
           ("Ferocious Horn Den", "🐺", 1800), ("Shattered Arkanis", "💠", 1900)]


def uid(i: int) -> int:
    return 910000000000000000 + i


class ids:
    """Obviously fake snowflakes."""
    n = 920000000000000000

    @classmethod
    def next(cls) -> int:
        cls.n += 1
        return cls.n


def build(path) -> dict:
    """Create the demo database at `path`. Returns names for the renderer."""
    rnd = random.Random(14)
    db.init_engine(path)
    with db.session() as s:
        seed(s, GUILD_ID), upgrade_defaults(s)
        get_settings(s, GUILD_ID).guild_name = "Your Server"
        get_state(s).admin_password_hash = hash_password(PASSWORD)
        st, game = get_settings(s, GUILD_ID), games.first(s, GUILD_ID)
        for k in ("roles_channel_id", "log_channel_id"):
            setattr(st, k, ids.next())
        for k in ("timer_channel_id", "party_channel_id", "tank_role_id", "support_role_id", "dps_role_id"):
            setattr(game, k, ids.next())
        for c in s.scalars(select(Category)):
            c.role_id = ids.next()
        for c in party.classes(s, 1):
            c.role_id = ids.next()
        for i, (name, emoji, gear) in enumerate(CONTENT):
            s.add(PfContent(game_id=game.id, name=name, emoji=emoji, min_gear_score=gear, sort_order=i,
                            role_id=ids.next()))
        get_state(s).last_tick_at = NOW - 20

    # Members: classes, pings, gear, a few with quiet hours.
    with db.session() as s:
        classes = [c.role_id for c in party.classes(s, 1)]
        pings = [c.role_id for c in s.scalars(select(Category))][:3] + \
                [c.role_id for c in s.scalars(select(PfContent))]
        brackets = [b.id for b in party.brackets(s, 1)]
    for i, name in enumerate(MEMBERS):
        tracking.set_classes(GUILD_ID, uid(i), name, set(rnd.sample(classes, rnd.choice([1, 1, 2]))))
        for r in rnd.sample(pings, rnd.randint(1, 4)):
            tracking.add_ping(GUILD_ID, uid(i), name, r)
    with db.session() as s:
        for i, name in enumerate(MEMBERS):
            party.set_gear(s, uid(i), name, rnd.choice(brackets), NOW)
        for i in (3, 8):
            quiet.set_hours(s, GUILD_ID, uid(i), MEMBERS[i], 23, 7, "America/New_York", set())

    def who(s, i):
        held = {h for h in tracking_roles(s, uid(i))}
        return Who(uid(i), MEMBERS[i], held)

    # History for Stats: parties and registrations over the last month.
    with db.session() as s:
        contents = party.contents(s, 1)
        windows = party.windows(s, 1)
        for k in range(70):
            day = NOW - rnd.randint(1, 27) * 86400
            t = day - (day % 3600) + rnd.choice([0, 1, 1, 2, 2, 3, -1, -2]) * 3600
            i = rnd.randrange(len(MEMBERS))
            try:
                party.register(s, who(s, i), rnd.choice(windows).id,
                               [c.id for c in rnd.sample(contents, rnd.randint(1, 3))], t)
            except party.PartyError:
                pass
            if k % 2:
                continue
            lead = rnd.randrange(len(MEMBERS))
            if party.live_party_for(s, uid(lead), 1):
                continue
            try:
                p = party.create(s, who(s, lead), rnd.choices(contents, [5, 4, 3, 2, 1])[0].id,
                                 rnd.choice([5, 5, 5, 20]), "none", "", "", t)
            except party.PartyError:
                continue
            for _ in range(rnd.randint(1, 4 if p.size == 5 else 9)):
                j = rnd.randrange(len(MEMBERS))
                try:
                    party.join(s, p, who(s, j), t + rnd.randint(120, 1500))
                except party.PartyError:
                    pass
            party.end(p, "done" if p.status == "full" else rnd.choice(["expired", "expired", "closed"]),
                      t + 3600)

    # Right now: boss timers, an open party with a ping, a full party, registrations.
    with db.session() as s:
        bosses = {e.name: e for e in s.scalars(select(Event).where(Event.schedule_type == "kill_report"))}
        timers.report(s, bosses["Eternal Gartua"], uid(1), "Kai", NOW - 7 * 3600)
        timers.report(s, bosses["High Commander Lagta"], uid(2), "Mira", NOW - 3 * 3600, killed_at="20 min ago")
        fire = next(c for c in party.contents(s, 1) if c.name == "Fire Temple")
        horn = next(c for c in party.contents(s, 1) if c.name == "Ferocious Horn Den")
        for i in (7, 9, 11):
            party.register(s, who(s, i), windows[2].id, [fire.id, horn.id], NOW - 600)
        full = party.create(s, who(s, 4), horn.id, 5, "none", "", "farm run, any experience", NOW - 2400)
        for i in (5, 6, 10, 12):
            try:
                party.join(s, full, who(s, i), NOW - 1800)
            except party.PartyError:
                pass
        open_party = party.create(s, who(s, 0), fire.id, 5, "none", "", "fast clear, know the mechanics",
                                  NOW - 300)
        party.join(s, open_party, who(s, 1), NOW - 120)
        s.flush()
        ctx = dict(open_party_id=open_party.id, full_party_id=full.id)

        for at, kind, text in [
            (NOW - 60, "timer", "Timer created by Mira: High Commander Lagta, killed 20 min ago."),
            (NOW - 300, "party", "Party created by Raelin: 🔥 Fire Temple, 5 players."),
            (NOW - 420, "roles", "Granted Fire Temple to Lyra."),
            (NOW - 1500, "alert", "Sent Spacetime Rift 5 min alert with ping."),
            (NOW - 1800, "party", "Ferocious Horn Den party is full."),
            (NOW - 3600, "startup", "Nyerk online as Nyerk#0000 in Your Server."),
        ]:
            s.add(LogEntry(guild_id=GUILD_ID, at=at, level="INFO", kind=kind, text=text))
    return ctx


def tracking_roles(s, user_id: int) -> set[int]:
    from nyerk.models import RoleHolder
    return {r.role_id for r in s.scalars(select(RoleHolder).where(RoleHolder.user_id == user_id))}


def freeze_clock() -> None:
    """Every time.time() in this process returns NOW, so the screenshots are
    identical run to run. (Timeouts use time.monotonic, which is untouched.)"""
    time.time = lambda: float(NOW)
