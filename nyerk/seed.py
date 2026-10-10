"""First-run data from the scope document."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import schedules
from .db import get_settings, get_state
from .models import AlertSlot, Category, Event

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
    settings = get_settings(s)
    get_state(s)
    if s.scalar(select(Category).limit(1)) is not None:
        return False

    settings.timer_channel_id = 100000000000000001
    settings.roles_channel_id = 100000000000000002
    settings.log_channel_id = 100000000000000003

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
