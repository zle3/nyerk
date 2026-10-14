"""Game templates (AION 2 from the scope document, or a blank game) and
upgrades for installs made before a default changed."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import schedules
from .db import get_settings, get_state
from .models import AlertSlot, Category, Event, Game, PfBracket, PfClass, PfWindow, ReactionGroup

NY = "America/New_York"
LEADS = (15, 5, 0)

CATEGORIES = ["World Boss", "Shugo", "Rift", "Invasion", "Reset", "Maintenance"]

# (enabled, ping) per lead 15, 5, 0
ALL_PING = ((True, True), (True, True), (True, True))
FIVE_ONLY = ((False, False), (True, False), (False, False))
NONE = ((False, False), (False, False), (False, False))

BOSSES = [  # the usual route, which is also the dashboard button order
    ("Eternal Gartua", "Eternal Isle", 12 * 60),
    ("High Commander Lagta", "Laborer's Rest Stop", 12 * 60),
    ("Soul Ruler Kashapa", "Ancient Zumion Temple Site", 6 * 60),
    ("Silent Dartan", "Beast Hunter Campsite", 6 * 60),
]
# Locations first shipped, so the route update only replaces untouched ones.
OLD_BOSS_LOCATIONS = {
    "Eternal Gartua": "Isle of Eternity", "High Commander Lagta": "Red Forest",
    "Soul Ruler Kashapa": "Artamia Plateau East", "Silent Dartan": "Artamia Plateau South",
}
BOSS_ORDER = [name for name, _, _ in BOSSES]

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
    """Make sure the server-wide rows exist and there is at least one game;
    a database with none gets the AION 2 game. For tests and the demo: a
    real first start waits for the owner to pick a game on the web page."""
    from . import games

    get_settings(s)
    get_state(s)
    if games.all_games(s):
        return False
    games.create(s, "AION 2", "aion2")
    return True


def seed_game(s: Session, game: Game) -> None:
    """Fill a new game from its template. Channel and role IDs are never in
    the code: set them on the web page or import a settings file."""
    s.add_all(PfWindow(game_id=game.id, minutes=m, label=l) for m, l in PF_WINDOWS)
    if game.kind != "aion2":
        return
    cats = {name: Category(game_id=game.id, name=name, sort_order=i) for i, name in enumerate(CATEGORIES)}
    s.add_all(cats.values())
    s.flush()
    order = 0
    for name, location, respawn in BOSSES:
        s.add(Event(
            game_id=game.id, category_id=cats["World Boss"].id, name=name, location=location,
            schedule_type="kill_report", schedule_params={}, respawn_minutes=respawn, slots=make_slots(ALL_PING),
            sort_order=order, source="scope",
        ))
        order += 1
    for name, cat, stype, params, dur, slots, dash, label, label_active, key in SCHEDULED:
        s.add(Event(
            game_id=game.id, category_id=cats[cat].id, name=name, schedule_type=stype,
            schedule_params=schedules.get(stype).validate(params), duration_minutes=dur,
            slots=make_slots(slots), dashboard=dash, dashboard_label=label,
            dashboard_label_active=label_active, sort_order=order,
            source="shugo.gg" if key else "scope", source_key=key,
        ))
        order += 1
    s.add_all(PfClass(game_id=game.id, name=n, emoji=e, class_type=t, sort_order=i)
              for i, (n, e, t) in enumerate(PF_CLASSES))
    s.add_all(PfBracket(game_id=game.id, label=l, min_score=a, max_score=b, sort_order=i)
              for i, (l, a, b) in enumerate(PF_BRACKETS))
    s.flush()


PF_CLASSES = [
    ("Gladiator", "⚔️", "dps"), ("Templar", "🛡️", "tank"), ("Assassin", "🗡️", "dps"), ("Ranger", "🏹", "dps"),
    ("Sorcerer", "🪄", "dps"), ("Spiritmaster", "📖", "dps"), ("Cleric", "✝️", "support"), ("Chanter", "🎸", "support"),
]
PF_BRACKETS = [("1400-1599", 1400, 1599), ("1600-1899", 1600, 1899), ("1900-2100", 1900, 2100),
               ("2100+", 2100, 9999)]
PF_WINDOWS = [(15, "15 min"), (30, "30 min"), (60, "1 hr"), (120, "2 hr"), (240, "4 hr")]


def upgrade_defaults(s: Session) -> list[str]:
    """Move existing installs off defaults that changed, without touching
    anything the admin customised."""
    from . import games

    server = get_settings(s)
    done = []
    state = get_state(s)
    applied = list(state.upgrades_done or [])
    if server.roles_header == "# 🎭 Roles\nPick your class, then the pings you want. Change them any time.":
        server.roles_header = "# 🎭 Roles\nPick your classes and gear score, then the pings you want. Change them any time."
        done.append("roles channel header")
    if "\\n" in (server.roles_header or ""):  # older upgrades stored "\n" as two characters
        server.roles_header = server.roles_header.replace("\\n", "\n")
        done.append("roles header")
    for st in games.all_games(s):
        if st.kind == "aion2":
            done += _upgrade_aion2(s, st, applied)
    state.upgrades_done = applied
    return done


def _upgrade_aion2(s: Session, st: Game, applied: list[str]) -> list[str]:
    done = []
    g = st.id
    if st.dashboard_title == "⏱️ AION2 Timers":
        st.dashboard_title = "⏱️ AION 2 Timers"
        done.append("dashboard title")
    if "bracket_2100_plus" not in applied:
        mine = list(s.scalars(select(PfBracket).where(PfBracket.game_id == g)))
        if mine and not any(b.label == "2100+" for b in mine):
            top = max((b.sort_order for b in mine), default=0)
            s.add(PfBracket(game_id=g, label="2100+", min_score=2100, max_score=9999, sort_order=top + 1))
            done.append("gear score bracket 2100+")
        applied.append("bracket_2100_plus")
    if st.oneoff_label in ("One-offs", "World bosses and one-offs"):
        st.oneoff_label = "World Bosses"
        done.append("dashboard boss section name")
    if "boss_order_gartua_first" not in applied:
        bosses = {e.name: e for e in s.scalars(select(Event).where(Event.schedule_type == "kill_report",
                                                                  Event.game_id == g))}
        if bosses:
            ordered = [bosses[n] for n in BOSS_ORDER if n in bosses]
            ordered += sorted((e for n, e in bosses.items() if n not in BOSS_ORDER), key=lambda e: e.sort_order)
            for i, e in enumerate(ordered):
                e.sort_order = i
            done.append("boss button order")
        applied.append("boss_order_gartua_first")
    if "boss_route_2026_10_11" not in applied:
        bosses = {e.name: e for e in s.scalars(select(Event).where(Event.schedule_type == "kill_report",
                                                                  Event.game_id == g))}
        if bosses:
            locations = {name: loc for name, loc, _ in BOSSES}
            for name, e in bosses.items():
                if name in OLD_BOSS_LOCATIONS and e.location == OLD_BOSS_LOCATIONS[name]:
                    e.location = locations[name]
            ordered = [bosses[n] for n in BOSS_ORDER if n in bosses]
            ordered += sorted((e for n, e in bosses.items() if n not in BOSS_ORDER), key=lambda e: e.sort_order)
            for i, e in enumerate(ordered):
                e.sort_order = i
            done.append("boss route order and locations")
        applied.append("boss_route_2026_10_11")
    if st.dashboard_commands_label == "Commands":  # was a heading over a command list
        st.dashboard_commands_label = "💡 Type `/nyerk` for more: report a kill with a custom time, and see, edit or cancel your timers."
        done.append("dashboard /nyerk reminder")
    if st.report_offset_min == 5:  # old default; 1 minute since 2026-10-10
        st.report_offset_min = 1
        done.append("report offset (5 → 1 minute)")
    # Columns added by older upgrades stored "\n" as two characters.
    for col in ("dashboard_footer",):
        value = getattr(st, col) or ""
        if "\\n" in value:
            setattr(st, col, value.replace("\\n", "\n"))
            done.append(col.replace("_", " "))
    return done


def seed_party_finder(s: Session) -> bool:
    """Kept for older callers: party finder defaults now come with each
    game's template (seed_game)."""
    return False


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
