"""Settings export and import as one JSON file.

Export holds configuration only: settings, ping categories, events with
their alert slots, and the party finder lists. It never holds the admin
password, timers, parties, registrations or message history. Discord IDs
are written as strings so no tool rounds them.

Import is a merge. Every section and every key is optional, so a short file
that only carries channel and role IDs is a valid import. Rows are matched
by name (brackets by label, windows by minutes); matching rows are updated,
new rows are added, and nothing missing from the file is deleted.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import safety, schedules
from .db import get_settings
from .models import (
    AlertSlot, Category, Event, PfBracket, PfClass, PfContent, PfWindow, ReactionGroup, Settings,
)
from .seed import NONE, make_slots

FORMAT = 1
ID_FIELDS = {
    "timer_channel_id", "roles_channel_id", "log_channel_id", "party_channel_id",
    "tank_role_id", "support_role_id", "dps_role_id", "role_id",
}
# Access controls never travel in a file, so an import or a restore can't change them.
SKIP_SETTINGS = {"id", "admin_password_hash", "admin_locked", "admin_grants"}
CLASS_TYPES = ("tank", "support", "dps")


class ImportError_(ValueError):
    pass


def _out(value, key: str):
    if key in ID_FIELDS:
        return str(value) if value else ""
    return value


def export(s: Session) -> dict:
    st = get_settings(s)
    settings = {c.name: _out(getattr(st, c.name), c.name)
                for c in Settings.__table__.columns if c.name not in SKIP_SETTINGS}
    cats = {c.id: c.name for c in s.scalars(select(Category))}
    gnames = {g.id: g.name for g in s.scalars(select(ReactionGroup))}
    events = []
    for e in s.scalars(select(Event).where(Event.overrides_event_id.is_(None)).order_by(Event.sort_order, Event.id)):
        params = {k: v for k, v in (e.schedule_params or {}).items() if not k.startswith("_")}
        events.append({
            "name": e.name, "category": cats.get(e.category_id, ""), "location": e.location,
            "schedule_type": e.schedule_type, "schedule_params": params,
            "duration_minutes": e.duration_minutes, "respawn_minutes": e.respawn_minutes,
            "enabled": e.enabled, "dashboard": e.dashboard, "dashboard_label": e.dashboard_label,
            "dashboard_label_active": e.dashboard_label_active, "sort_order": e.sort_order,
            "source": e.source, "source_key": e.source_key, "edited_fields": e.edited_fields or [],
            "slots": [{"lead_minutes": sl.lead_minutes, "enabled": sl.enabled, "ping": sl.ping,
                       "template": sl.template} for sl in e.slots],
        })
    return {
        "nyerk_export": FORMAT,
        "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "settings": settings,
        "role_messages": [{"name": g.name, "description": g.description, "sort_order": g.sort_order}
                          for g in s.scalars(select(ReactionGroup).order_by(ReactionGroup.sort_order))],
        "categories": [{"name": c.name, "role_id": _out(c.role_id, "role_id"), "sort_order": c.sort_order,
                        "emoji": c.emoji, "role_message": gnames.get(c.group_id, "")}
                       for c in s.scalars(select(Category).order_by(Category.sort_order))],
        "events": events,
        "party_finder": {
            "classes": [{"name": c.name, "emoji": c.emoji, "role_id": _out(c.role_id, "role_id"),
                         "class_type": c.class_type, "sort_order": c.sort_order}
                        for c in s.scalars(select(PfClass).order_by(PfClass.sort_order))],
            "brackets": [{"label": b.label, "min_score": b.min_score, "max_score": b.max_score,
                          "sort_order": b.sort_order}
                         for b in s.scalars(select(PfBracket).order_by(PfBracket.sort_order))],
            "windows": [{"minutes": w.minutes, "label": w.label}
                        for w in s.scalars(select(PfWindow).order_by(PfWindow.minutes))],
            "content": [{"name": c.name, "emoji": c.emoji, "role_id": _out(c.role_id, "role_id"),
                         "react_emoji": c.react_emoji, "role_message": gnames.get(c.group_id, ""),
                         "min_gear_score": c.min_gear_score,
                         "active": c.active, "sort_order": c.sort_order}
                        for c in s.scalars(select(PfContent).order_by(PfContent.sort_order))],
        },
    }


# ----------------------------------------------------------------------------


def _id(value, where: str) -> int:
    raw = str(value if value is not None else "").strip().strip("<@#&>")
    if raw in ("", "0"):
        return 0
    if not raw.isdigit() or not 15 <= len(raw) <= 21:
        raise ImportError_(f"{where}: {value!r} is not a Discord ID")
    return int(raw)


def _coerce(col, value, where: str):
    if col.name in ID_FIELDS:
        rid = _id(value, where)
        if col.name.endswith("role_id"):
            try:
                safety.check_role(rid)
            except ValueError as exc:
                raise ImportError_(f"{where}: {exc}")
        return rid
    py = col.type.python_type
    if py is bool:
        if isinstance(value, bool):
            return value
        raise ImportError_(f"{where}: expected true/false")
    if py is int:
        if isinstance(value, bool) or not isinstance(value, (int, str)) or not str(value).lstrip("-").isdigit():
            raise ImportError_(f"{where}: expected a whole number")
        return int(value)
    if py is str:
        if not isinstance(value, str):
            raise ImportError_(f"{where}: expected text")
        return value
    return value


def _assign(row, data: dict, fields: tuple[str, ...], label: str, changes: list[str]) -> None:
    for key in fields:
        if key not in data:
            continue
        col = type(row).__table__.columns[key]
        new = _coerce(col, data[key], f"{label} {key}")
        if getattr(row, key) != new:
            shown = new if key not in ID_FIELDS else (new or "cleared")
            changes.append(f"{label}: {key} → {shown}")
            setattr(row, key, new)


def _named(s: Session, model, key: str, value, label: str, changes: list[str], **defaults):
    row = s.scalar(select(model).where(getattr(model, key) == value))
    if row is None:
        row = model(**{key: value}, **defaults)
        s.add(row)
        changes.append(f"{label}: added")
    return row


def apply_import(s: Session, data: dict) -> list[str]:
    """Merge `data` into the database. Returns readable change lines.
    Raises ImportError_ on anything malformed; the caller rolls back."""
    if not isinstance(data, dict) or data.get("nyerk_export") != FORMAT:
        raise ImportError_("Not a Nyerk settings file (missing \"nyerk_export\": 1).")
    changes: list[str] = []

    if "settings" in data:
        if not isinstance(data["settings"], dict):
            raise ImportError_("settings must be an object")
        st = get_settings(s)
        cols = Settings.__table__.columns
        for key, value in data["settings"].items():
            if key in SKIP_SETTINGS:
                continue
            if key not in cols:
                changes.append(f"Settings: ignored unknown key {key!r}")
                continue
            _assign(st, {key: value}, (key,), "Settings", changes)

    for g in _list(data, "role_messages"):
        name = _name(g, "name", "role message")
        row = _named(s, ReactionGroup, "name", name, f"Role message {name}", changes, sort_order=99)
        _assign(row, g, ("description", "sort_order"), f"Role message {name}", changes)
    s.flush()
    groups = {g.name: g.id for g in s.scalars(select(ReactionGroup))}

    for c in _list(data, "categories"):
        name = _name(c, "name", "category")
        row = _named(s, Category, "name", name, f"Category {name}", changes, sort_order=99)
        _assign(row, c, ("role_id", "sort_order", "emoji"), f"Category {name}", changes)
        _set_group(row, c, groups, f"Category {name}", changes)

    s.flush()
    cats = {c.name: c.id for c in s.scalars(select(Category))}
    for e in _list(data, "events"):
        _import_event(s, e, cats, changes)

    pf = data.get("party_finder") or {}
    if not isinstance(pf, dict):
        raise ImportError_("party_finder must be an object")
    for c in _list(pf, "classes"):
        name = _name(c, "name", "class")
        if "class_type" in c and c["class_type"] not in CLASS_TYPES:
            raise ImportError_(f"Class {name}: class_type must be tank, support or dps")
        row = _named(s, PfClass, "name", name, f"Class {name}", changes, class_type="dps", sort_order=99)
        _assign(row, c, ("emoji", "role_id", "class_type", "sort_order"), f"Class {name}", changes)
    for b in _list(pf, "brackets"):
        label = _name(b, "label", "bracket")
        row = _named(s, PfBracket, "label", label, f"Bracket {label}", changes, sort_order=99)
        _assign(row, b, ("min_score", "max_score", "sort_order"), f"Bracket {label}", changes)
    for w in _list(pf, "windows"):
        try:
            minutes = int(w.get("minutes"))
        except (TypeError, ValueError):
            raise ImportError_("Each window needs whole-number minutes")
        row = _named(s, PfWindow, "minutes", minutes, f"Window {minutes} min", changes)
        _assign(row, w, ("label",), f"Window {minutes} min", changes)
    for c in _list(pf, "content"):
        name = _name(c, "name", "content")
        row = _named(s, PfContent, "name", name, f"Content {name}", changes, sort_order=99)
        _assign(row, c, ("emoji", "role_id", "react_emoji", "min_gear_score", "active", "sort_order"),
                f"Content {name}", changes)
        _set_group(row, c, groups, f"Content {name}", changes)
    s.flush()
    return changes


def _set_group(row, data: dict, groups: dict[str, int], label: str, changes: list[str]) -> None:
    if "role_message" not in data:
        return
    name = data["role_message"] or ""
    gid = groups.get(name) if name else None
    if name and gid is None:
        raise ImportError_(f"{label}: unknown role message {name!r}")
    if row.group_id != gid:
        row.group_id = gid
        changes.append(f"{label}: role message → {name or 'default'}")


def _list(data: dict, key: str) -> list[dict]:
    value = data.get(key) or []
    if not isinstance(value, list) or not all(isinstance(x, dict) for x in value):
        raise ImportError_(f"{key} must be a list of objects")
    return value


def _name(row: dict, key: str, what: str) -> str:
    name = str(row.get(key) or "").strip()
    if not name:
        raise ImportError_(f"Every {what} needs a {key}")
    return name


def _import_event(s: Session, e: dict, cats: dict[str, int], changes: list[str]) -> None:
    name = _name(e, "name", "event")
    label = f"Event {name}"
    row = s.scalar(select(Event).where(Event.name == name, Event.overrides_event_id.is_(None)))
    if row is None:
        if "schedule_type" not in e:
            raise ImportError_(f"{label}: new events need a schedule_type")
        row = Event(name=name, schedule_type=e["schedule_type"], schedule_params={}, slots=make_slots(NONE),
                    source="import")
        s.add(row)
        changes.append(f"{label}: added")
    if "category" in e:
        cat_id = cats.get(e["category"]) if e["category"] else None
        if e["category"] and cat_id is None:
            raise ImportError_(f"{label}: unknown category {e['category']!r}")
        if row.category_id != cat_id:
            row.category_id = cat_id
            changes.append(f"{label}: category → {e['category'] or 'none'}")
    if "schedule_type" in e or "schedule_params" in e:
        stype = e.get("schedule_type", row.schedule_type)
        try:
            params = schedules.get(stype).validate(dict(e.get("schedule_params", row.schedule_params) or {}))
        except (schedules.ScheduleError, ValueError) as exc:
            raise ImportError_(f"{label}: {exc}")
        if (row.schedule_type, row.schedule_params) != (stype, params):
            row.schedule_type, row.schedule_params = stype, params
            changes.append(f"{label}: schedule → {schedules.get(stype).describe(params)}")
    _assign(row, e, ("location", "duration_minutes", "respawn_minutes", "enabled", "dashboard", "dashboard_label",
                     "dashboard_label_active", "sort_order", "source", "source_key"), label, changes)
    if "edited_fields" in e and isinstance(e["edited_fields"], list) and row.edited_fields != e["edited_fields"]:
        row.edited_fields = list(e["edited_fields"])
    if "slots" in e:
        by_lead = {sl.lead_minutes: sl for sl in row.slots}
        for sl in e["slots"] or []:
            lead = int(sl.get("lead_minutes", -1))
            if lead < 0:
                raise ImportError_(f"{label}: slot without lead_minutes")
            target = by_lead.get(lead)
            if target is None:
                target = AlertSlot(lead_minutes=lead)
                row.slots.append(target)
                by_lead[lead] = target
            _assign(target, sl, ("enabled", "ping", "template"), f"{label} {lead} min slot", changes)


def summary(s: Session) -> list[tuple[str, str, str]]:
    """(admin tab, what is exported, count) for the Settings page."""
    from sqlalchemy import func

    def n(model, *where):
        return s.scalar(select(func.count()).select_from(model).where(*where)) or 0

    events = n(Event, Event.overrides_event_id.is_(None))
    slots = s.scalar(select(func.count()).select_from(AlertSlot).join(Event)
                     .where(Event.overrides_event_id.is_(None))) or 0
    return [
        ("Settings", "Channels, boss reports, dashboard, party timing, defaults source", "all"),
        ("Events", "Schedules, alert toggles, ping toggles, message templates, dashboard placement",
         f"{events} events, {slots} alert slots"),
        ("Roles", "Ping categories with role IDs, role messages, and each role's message and emoji",
         f"{n(Category)} categories, {n(ReactionGroup)} role messages"),
        ("Party finder", "Classes and role IDs, type roles, standard makeup, content with dungeon roles, gear score options, time windows",
         f"{n(PfClass)} classes, {n(PfContent)} content"),
    ]


def filename() -> str:
    return f"nyerk-settings-{time.strftime('%Y%m%d-%H%M')}.json"
