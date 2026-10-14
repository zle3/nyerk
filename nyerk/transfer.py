"""Settings export and import as one JSON file.

Export holds configuration only: server settings, role messages, and for
each game its settings, ping categories, events with their alert slots, and
party finder lists. It never holds the admin password, timers, parties,
registrations or message history. Discord IDs are written as strings so no
tool rounds them.

Import is a merge. Every section and every key is optional, so a short file
that only carries channel and role IDs is a valid import. Games are matched
by name, and rows inside a game by name (brackets by label, windows by
minutes); matching rows are updated, new ones added, and nothing missing
from the file is deleted. A format 1 file (from before games) imports into
the game passed as `game_id`.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import safety, schedules
from .db import get_settings
from .models import (
    AlertSlot, Category, Event, Game, PfBracket, PfClass, PfContent, PfWindow, ReactionGroup, Settings,
)
from .seed import NONE, make_slots

FORMAT = 2
ID_FIELDS = {
    "timer_channel_id", "roles_channel_id", "log_channel_id", "party_channel_id",
    "tank_role_id", "support_role_id", "dps_role_id", "role_id",
}
# Access controls never travel in a file, so an import or a restore can't change them.
SKIP_SETTINGS = {"id", "admin_locked", "admin_grants", "admin_role_ids", "session_epoch",
                 # which server this is, and the roles channel's Discord messages
                 "guild_id", "guild_name", "status", "joined_at", "reaction_message_id", "reaction_map",
                 "reaction_messages", "roles_header_message_id", "roles_clear_message_id"}
# Game columns that are identity or Discord bookkeeping, not settings.
SKIP_GAME = {"id", "guild_id", "name", "kind", "enabled", "sort_order", "dashboard_message_id", "dashboard_hash",
             "roles_class_message_id", "roles_gear_message_id", "class_panel_message_id", "gear_panel_message_id",
             "interest_panel_message_id", "interest_panel_hash", "party_panel_message_id",
             "party_status_message_id", "party_status_hash", "last_import_at", "last_import_error", "pending_import",
             # links to other servers are made with a code, never by import
             "link_home_id", "link_code", "link_code_expires"}
GAME_SETTINGS = tuple(c.name for c in Game.__table__.columns if c.name not in SKIP_GAME)
CLASS_TYPES = ("tank", "support", "dps")


class ImportError_(ValueError):
    pass


def _out(value, key: str):
    if key in ID_FIELDS:
        return str(value) if value else ""
    return value


def export(s: Session, guild_id: int) -> dict:
    """One Discord server's configuration."""
    st = get_settings(s, guild_id)
    settings = {c.name: _out(getattr(st, c.name), c.name)
                for c in Settings.__table__.columns if c.name not in SKIP_SETTINGS}
    gnames = {g.id: g.name for g in s.scalars(select(ReactionGroup).where(ReactionGroup.guild_id == guild_id))}
    return {
        "nyerk_export": FORMAT,
        "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "settings": settings,
        "role_messages": [{"name": g.name, "description": g.description, "sort_order": g.sort_order}
                          for g in s.scalars(select(ReactionGroup).where(ReactionGroup.guild_id == guild_id)
                                             .order_by(ReactionGroup.sort_order))],
        "games": [_export_game(s, g, gnames) for g in s.scalars(select(Game).where(Game.guild_id == guild_id)
                                                                 .order_by(Game.sort_order, Game.id))],
    }


def _export_game(s: Session, g: Game, gnames: dict[int, str]) -> dict:
    cats = {c.id: c.name for c in s.scalars(select(Category).where(Category.game_id == g.id))}
    events = []
    for e in s.scalars(select(Event).where(Event.overrides_event_id.is_(None), Event.game_id == g.id)
                       .order_by(Event.sort_order, Event.id)):
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
    mine = lambda model: select(model).where(model.game_id == g.id)  # noqa: E731
    return {
        "name": g.name, "kind": g.kind, "enabled": g.enabled, "sort_order": g.sort_order,
        "settings": {k: _out(getattr(g, k), k) for k in GAME_SETTINGS},
        "categories": [{"name": c.name, "role_id": _out(c.role_id, "role_id"), "sort_order": c.sort_order,
                        "emoji": c.emoji, "role_message": gnames.get(c.group_id, "")}
                       for c in s.scalars(mine(Category).order_by(Category.sort_order))],
        "events": events,
        "party_finder": {
            "classes": [{"name": c.name, "emoji": c.emoji, "role_id": _out(c.role_id, "role_id"),
                         "class_type": c.class_type, "sort_order": c.sort_order}
                        for c in s.scalars(mine(PfClass).order_by(PfClass.sort_order))],
            "brackets": [{"label": b.label, "min_score": b.min_score, "max_score": b.max_score,
                          "sort_order": b.sort_order}
                         for b in s.scalars(mine(PfBracket).order_by(PfBracket.sort_order))],
            "windows": [{"minutes": w.minutes, "label": w.label}
                        for w in s.scalars(mine(PfWindow).order_by(PfWindow.minutes))],
            "content": [{"name": c.name, "emoji": c.emoji, "role_id": _out(c.role_id, "role_id"),
                         "react_emoji": c.react_emoji, "role_message": gnames.get(c.group_id, ""),
                         "min_gear_score": c.min_gear_score,
                         "active": c.active, "sort_order": c.sort_order}
                        for c in s.scalars(mine(PfContent).order_by(PfContent.sort_order))],
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
    q = select(model).where(getattr(model, key) == value)
    for scope in ("game_id", "guild_id"):  # a row only ever matches inside its own game or server
        if scope in defaults:
            q = q.where(getattr(model, scope) == defaults[scope])
    row = s.scalar(q)
    if row is None:
        row = model(**{key: value}, **defaults)
        s.add(row)
        changes.append(f"{label}: added")
    return row


def apply_import(s: Session, data: dict, guild_id: int, game_id: int | None = None) -> list[str]:
    """Merge `data` into one Discord server's configuration. Returns
    readable change lines. Raises ImportError_ on anything malformed; the
    caller rolls back."""
    version = data.get("nyerk_export") if isinstance(data, dict) else None
    if version not in (1, FORMAT):
        raise ImportError_("Not a Nyerk settings file (missing \"nyerk_export\").")
    changes: list[str] = []

    if "settings" in data:
        if not isinstance(data["settings"], dict):
            raise ImportError_("settings must be an object")
        st = get_settings(s, guild_id)
        cols = Settings.__table__.columns
        legacy_game: dict = {}
        for key, value in data["settings"].items():
            if key in SKIP_SETTINGS:
                continue
            if key in cols:
                _assign(st, {key: value}, (key,), "Settings", changes)
            elif version == 1 and key in GAME_SETTINGS:
                legacy_game[key] = value  # from before games: belongs to the target game
            else:
                changes.append(f"Settings: ignored unknown key {key!r}")

    for g in _list(data, "role_messages"):
        name = _name(g, "name", "role message")
        row = _named(s, ReactionGroup, "name", name, f"Role message {name}", changes, sort_order=99,
                     guild_id=guild_id)
        _assign(row, g, ("description", "sort_order"), f"Role message {name}", changes)
    s.flush()
    groups = {g.name: g.id for g in s.scalars(select(ReactionGroup).where(ReactionGroup.guild_id == guild_id))}

    if version == 1:
        game = s.get(Game, game_id) if game_id else None
        if game is None or game.guild_id != guild_id:
            raise ImportError_("Pick the game to import this older settings file into.")
        block = {k: data[k] for k in ("categories", "events", "party_finder") if k in data}
        if "settings" in data:
            block["settings"] = legacy_game
        _import_game(s, game, block, groups, changes, prefix="")
    else:
        mine = select(Game).where(Game.guild_id == guild_id)
        many = len(_list(data, "games")) > 1 or len(list(s.scalars(mine))) > 1
        for gdata in _list(data, "games"):
            name = _name(gdata, "name", "game")
            game = s.scalar(mine.where(Game.name == name))
            if game is None:
                kind = gdata.get("kind") if gdata.get("kind") in ("aion2", "custom") else "custom"
                order = max((g.sort_order for g in s.scalars(mine)), default=-1) + 1
                game = Game(guild_id=guild_id, name=name, kind=kind, sort_order=order)
                s.add(game)
                s.flush()
                changes.append(f"Game {name}: added")
            _assign(game, gdata, ("enabled", "sort_order"), f"Game {name}", changes)
            _import_game(s, game, gdata, groups, changes, prefix=f"{name} › " if many else "")
    s.flush()
    return changes


def _import_game(s: Session, game: Game, data: dict, groups: dict[str, int], changes: list[str],
                 prefix: str) -> None:
    gid = game.id
    if data.get("settings"):
        if not isinstance(data["settings"], dict):
            raise ImportError_("settings must be an object")
        for key, value in data["settings"].items():
            if key in GAME_SETTINGS:
                _assign(game, {key: value}, (key,), f"{prefix}Settings", changes)
            else:
                changes.append(f"{prefix}Settings: ignored unknown key {key!r}")

    for c in _list(data, "categories"):
        name = _name(c, "name", "category")
        label = f"{prefix}Category {name}"
        row = _named(s, Category, "name", name, label, changes, sort_order=99, game_id=gid)
        _assign(row, c, ("role_id", "sort_order", "emoji"), label, changes)
        _set_group(row, c, groups, label, changes)

    s.flush()
    cats = {c.name: c.id for c in s.scalars(select(Category).where(Category.game_id == gid))}
    for e in _list(data, "events"):
        _import_event(s, e, cats, changes, gid, prefix)

    pf = data.get("party_finder") or {}
    if not isinstance(pf, dict):
        raise ImportError_("party_finder must be an object")
    for c in _list(pf, "classes"):
        name = _name(c, "name", "class")
        if "class_type" in c and c["class_type"] not in CLASS_TYPES:
            raise ImportError_(f"Class {name}: class_type must be tank, support or dps")
        label = f"{prefix}Class {name}"
        row = _named(s, PfClass, "name", name, label, changes, class_type="dps", sort_order=99, game_id=gid)
        _assign(row, c, ("emoji", "role_id", "class_type", "sort_order"), label, changes)
    for b in _list(pf, "brackets"):
        name = _name(b, "label", "bracket")
        label = f"{prefix}Bracket {name}"
        row = _named(s, PfBracket, "label", name, label, changes, sort_order=99, game_id=gid)
        _assign(row, b, ("min_score", "max_score", "sort_order"), label, changes)
    for w in _list(pf, "windows"):
        try:
            minutes = int(w.get("minutes"))
        except (TypeError, ValueError):
            raise ImportError_("Each window needs whole-number minutes")
        label = f"{prefix}Window {minutes} min"
        row = _named(s, PfWindow, "minutes", minutes, label, changes, game_id=gid)
        _assign(row, w, ("label",), label, changes)
    for c in _list(pf, "content"):
        name = _name(c, "name", "content")
        label = f"{prefix}Content {name}"
        row = _named(s, PfContent, "name", name, label, changes, sort_order=99, game_id=gid)
        _assign(row, c, ("emoji", "role_id", "react_emoji", "min_gear_score", "active", "sort_order"),
                label, changes)
        _set_group(row, c, groups, label, changes)


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


def _import_event(s: Session, e: dict, cats: dict[str, int], changes: list[str], game_id: int,
                  prefix: str = "") -> None:
    name = _name(e, "name", "event")
    label = f"{prefix}Event {name}"
    row = s.scalar(select(Event).where(Event.name == name, Event.overrides_event_id.is_(None),
                                       Event.game_id == game_id))
    if row is None:
        if "schedule_type" not in e:
            raise ImportError_(f"{label}: new events need a schedule_type")
        row = Event(game_id=game_id, name=name, schedule_type=e["schedule_type"], schedule_params={},
                    slots=make_slots(NONE), source="import")
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


def summary(s: Session, guild_id: int) -> list[tuple[str, str, str]]:
    """(admin tab, what is exported, count) for the Settings page."""
    from sqlalchemy import func

    games = select(Game.id).where(Game.guild_id == guild_id)

    def n(model, *where):
        return s.scalar(select(func.count()).select_from(model).where(*where)) or 0

    events = n(Event, Event.overrides_event_id.is_(None), Event.game_id.in_(games))
    slots = s.scalar(select(func.count()).select_from(AlertSlot).join(Event)
                     .where(Event.overrides_event_id.is_(None), Event.game_id.in_(games))) or 0
    game_count = n(Game, Game.guild_id == guild_id)
    return [
        ("Games", "Each game's channels, boss reports, dashboard, party timing and defaults source",
         f"{game_count} game{'s' if game_count != 1 else ''}"),
        ("Settings", "Roles and logs channels, time zone, link preview", "all"),
        ("Events", "Schedules, alert toggles, ping toggles, message templates, dashboard placement",
         f"{events} events, {slots} alert slots"),
        ("Roles", "Ping categories with role IDs, role messages, and each role's message and emoji",
         f"{n(Category, Category.game_id.in_(games))} categories, "
         f"{n(ReactionGroup, ReactionGroup.guild_id == guild_id)} role messages"),
        ("Party finder", "Classes and role IDs, type roles, standard makeup, content with dungeon roles, gear score options, time windows",
         f"{n(PfClass, PfClass.game_id.in_(games))} classes, {n(PfContent, PfContent.game_id.in_(games))} content"),
    ]


def filename() -> str:
    return f"nyerk-settings-{time.strftime('%Y%m%d-%H%M')}.json"
