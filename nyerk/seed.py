"""First-run data from the scope document."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import schedules
from .db import get_settings, get_state
from .models import AlertSlot, Category, Event, PfBracket, PfClass, PfWindow, ReactionGroup

NY = "America/New_York"
LEADS = (15, 5, 0)

CATEGORIES = ["World Boss", "Shugo", "Rift", "Invasion", "Reset", "Maintenance"]

# (enabled, ping) per lead 15, 5, 0
ALL_PING = ((True, True), (True, True), (True, True))
FIVE_ONLY = ((False, False), (True, False), (False, False))
NONE = ((False, False), (False, False), (False, False))

BOSSES = [
    ("Silent Dartan", "Artamia Plateau South", 6 * 60),
    ("Soul Ruler Kashapa", "Artamia Plateau East", 6 * 60),
    ("High Commander Lagta", "Red Forest", 12 * 60),
    ("Eternal Gartua", "Isle of Eternity", 12 * 60),
]

SCHEDULED = [
    # name, category, type, params, duration, slots, dashboard, label, label_active, source_key
    ("Shugo Festival", "Shugo", "hourly", {"minute": 0, "tz": NY}, 10, FIVE_ONLY, "main", "", "", "Shugo Festival"),
    ("Spacetime Rift", "Rift", "daily",
     {"times": ["02:00", "05:00", "08:00", "11:00", "14:00", "17:00", "20:00", "23:00"], "tz": NY},
     60, ALL_PING, "main", "", "", "Spacetime Rift"),
    ("Dimensional Invasion", "Invasion", "hourly", {"minute": 30, "tz": NY}, 15, FIVE_ONLY, "main", "", "", "Dimensional Invasion"),
    ("Daily reset", "Reset", "daily", {"times": ["07:00"], "tz": "UTC"}, 0, NONE, "top", "Daily reset", "", "Daily reset"),
    ("Weekly reset", "Reset", "weekly", {"days": ["wed"], "time": "07:00", "tz": "UTC"}, 0,
     ((True, False), (False, False), (False, False)), "top", "Weekly reset", "", "Weekly reset"),
    ("Maintenance", "Maintenance", "weekly", {"days": ["wed"], "time": "06:30", "tz": "UTC"}, 210,
     ((True, False), (False, False), (True, False)), "top", "Maintenance starts", "Maintenance ends", ""),
]


def make_slots(spec) -> list[AlertSlot]:
    return [AlertSlot(lead_minutes=lead, enabled=en, ping=ping) for lead, (en, ping) in zip(LEADS, spec)]


def seed(s: Session) -> bool:
    """Insert defaults when the database is empty. Returns True if seeded."""
    get_settings(s)
    get_state(s)
    if s.scalar(select(Category).limit(1)) is not None:
        return False

    # Channel and role IDs are never in the code: set them on the Settings
    # page or import a settings file (Settings > Import).

    cats = {name: Category(name=name, sort_order=i) for i, name in enumerate(CATEGORIES)}
    s.add_all(cats.values())
    s.flush()

    order = 0
    for name, location, respawn in BOSSES:
        s.add(Event(
            category_id=cats["World Boss"].id, name=name, location=location, schedule_type="kill_report",
            schedule_params={}, respawn_minutes=respawn, slots=make_slots(ALL_PING), sort_order=order,
            source="scope",
        ))
        order += 1
    for name, cat, stype, params, dur, slots, dash, label, label_active, key in SCHEDULED:
        s.add(Event(
            category_id=cats[cat].id, name=name, schedule_type=stype,
            schedule_params=schedules.get(stype).validate(params), duration_minutes=dur,
            slots=make_slots(slots), dashboard=dash, dashboard_label=label,
            dashboard_label_active=label_active, sort_order=order,
            source="shugo.gg" if key else "scope", source_key=key,
        ))
        order += 1
    return True


PF_CLASSES = [
    ("Gladiator", "⚔️", "dps"), ("Templar", "🛡️", "tank"), ("Assassin", "🗡️", "dps"), ("Ranger", "🏹", "dps"),
    ("Sorcerer", "🪄", "dps"), ("Spiritmaster", "📖", "dps"), ("Cleric", "✝️", "support"), ("Chanter", "🎸", "support"),
]
PF_BRACKETS = [("1400-1599", 1400, 1599), ("1600-1899", 1600, 1899), ("1900-2100", 1900, 2100)]
PF_WINDOWS = [(15, "15 min"), (30, "30 min"), (60, "1 hr"), (120, "2 hr"), (240, "4 hr")]


def upgrade_defaults(s: Session) -> list[str]:
    """Move existing installs off defaults that changed, without touching
    anything the admin customised."""
    st = get_settings(s)
    done = []
    if st.dashboard_title == "⏱️ AION2 Timers":
        st.dashboard_title = "⏱️ AION 2 Timers"
        done.append("dashboard title")
    # Columns added by older upgrades stored "\n" as two characters.
    for col in ("roles_header", "dashboard_footer"):
        value = getattr(st, col) or ""
        if "\\n" in value:
            setattr(st, col, value.replace("\\n", "\n"))
            done.append(col.replace("_", " "))
    return done


def seed_party_finder(s: Session) -> bool:
    """Party finder defaults, separately so existing installs get them too.
    Role IDs stay 0 until set in the admin page or imported."""
    if s.scalar(select(PfClass).limit(1)) is not None:
        return False
    s.add_all(PfClass(name=n, emoji=e, class_type=t, sort_order=i) for i, (n, e, t) in enumerate(PF_CLASSES))
    s.add_all(PfBracket(label=l, min_score=a, max_score=b, sort_order=i) for i, (l, a, b) in enumerate(PF_BRACKETS))
    s.add_all(PfWindow(minutes=m, label=l) for m, l in PF_WINDOWS)
    return True


def seed_reaction_groups(s: Session) -> bool:
    """The two default role messages: event pings and dungeon pings."""
    if s.scalar(select(ReactionGroup).limit(1)) is not None:
        return False
    s.add_all([
        ReactionGroup(name="Event pings", description="Timers and server events.", sort_order=0,
                      default_for="category"),
        ReactionGroup(name="Dungeon pings", description="Get pinged when a party opens for these.", sort_order=1,
                      default_for="content"),
    ])
    return True
