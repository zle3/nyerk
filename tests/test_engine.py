from sqlalchemy import select

from nyerk import engine, timers
from nyerk.models import Category, Event, SentMessage

from .conftest import ny, GUILD, G  # noqa: F401


def boss(s, name="Soul Ruler Kashapa"):
    return s.scalar(select(Event).where(Event.name == name))


def test_blank_report_assumes_kill_one_minute_ago(s):
    now = ny(2026, 10, 9, 12, 0)
    r = timers.report(s, boss(s), 1, "Raelin", now)
    assert r.ok
    assert r.timer.fire_at == now + 5 * 3600 + 59 * 60
    assert "Active timers: 1/10" in r.reply


def test_rereport_replaces_own_timer(s):
    now = ny(2026, 10, 9, 12, 0)
    first = timers.report(s, boss(s), 1, "Raelin", now).timer
    again = timers.report(s, boss(s), 1, "Raelin", now + 600, respawns_in="2h")
    assert again.ok and again.timer.id == first.id and again.timer.fire_at == now + 600 + 7200
    assert "Updated your Soul Ruler Kashapa timer" in again.reply
    assert timers.active_count(s, 1) == 1


def test_someone_elses_timer_needs_confirmed_override(s):
    now = ny(2026, 10, 9, 12, 0)
    theirs = timers.report(s, boss(s), 1, "Raelin", now).timer
    r = timers.report(s, boss(s), 2, "Kai", now + 60)
    assert not r.ok and r.conflict.id == theirs.id
    assert "Raelin" in r.reply and "misuse" in r.reply and theirs.status == "active"
    stale = timers.report(s, boss(s), 2, "Kai", now + 60, override_id=theirs.id + 999)
    assert stale.conflict is not None  # must name the exact timer that was shown
    done = timers.report(s, boss(s), 2, "Kai", now + 60, killed_at="2 min ago", override_id=theirs.id)
    assert done.ok and done.log_level == "warn" and "Overrode timer" in done.log and "Raelin" in done.log
    assert theirs.status == "overridden" and done.timer.user_id == 2


def test_both_fields_rejected(s):
    both = timers.report(s, boss(s, "Silent Dartan"), 2, "Other", ny(2026, 10, 9, 12, 0), "10 min ago", "1h")
    assert not both.ok and "not both" in both.reply


def test_countdown_has_no_offset(s):
    now = ny(2026, 10, 9, 12, 0)
    r = timers.report(s, boss(s), 1, "Raelin", now, respawns_in="1h 20m")
    assert r.timer.fire_at == now + 4800 and r.timer.mode == "countdown"


def test_user_limit(s):
    now = ny(2026, 10, 9, 12, 0)
    from nyerk.db import get_settings
    G(s).user_timer_limit = 1
    assert timers.report(s, boss(s), 1, "R", now).ok
    r = timers.report(s, boss(s, "Silent Dartan"), 1, "R", now)
    assert not r.ok and "1/1" in r.reply


def test_due_slots_send_and_skip(s):
    now = ny(2026, 10, 9, 13, 25)  # Invasion at 13:30, 5 minute warning at 13:25
    due = engine.due_slots(s, now, now - 30)
    names = {(d.occ.event.name, d.slot.lead_minutes, d.action) for d in due}
    assert ("Dimensional Invasion", 5, "send") in names
    # After downtime the same slot is skipped, not sent.
    later = engine.due_slots(s, now + 600, now - 30)
    assert ("Dimensional Invasion", 5, "skip") in {(d.occ.event.name, d.slot.lead_minutes, d.action) for d in later}


def test_sent_ledger_prevents_double_post(s):
    now = ny(2026, 10, 9, 13, 25)
    for d in engine.due_slots(s, now, now - 30):
        s.add(SentMessage(event_id=d.occ.event.id, timer_id=d.occ.timer_id, occurrence_at=d.occ.at,
                          slot=d.slot.lead_minutes, status="sent", sent_at=now))
    s.flush()
    assert engine.due_slots(s, now, now - 30) == []


def test_past_warnings_skipped_for_new_timer(s):
    now = ny(2026, 10, 9, 12, 0)
    timers.report(s, boss(s), 1, "R", now, respawns_in="3m")
    due = engine.due_slots(s, now + 180, now)
    leads = sorted(d.slot.lead_minutes for d in due if d.occ.timer)
    assert leads == [0]


def test_render_alert_drops_role_line_without_ping(s):
    now = ny(2026, 10, 9, 13, 25)
    d = next(d for d in engine.due_slots(s, now, now - 30) if d.occ.event.name == "Dimensional Invasion")
    text, role = engine.render_alert(d.occ, d.slot)
    assert role == 0
    assert text.splitlines()[0] == "Dimensional Invasion starts in 5 minutes"


def test_render_boss_warning_with_role(s):
    cat = s.scalar(select(Category).where(Category.name == "World Boss"))
    cat.role_id = 42
    now = ny(2026, 10, 9, 12, 0)
    t = timers.report(s, boss(s), 1, "Rae_lin", now).timer
    occ = engine.Occurrence(boss(s), t.fire_at, 0, t)
    slot = next(sl for sl in boss(s).slots if sl.lead_minutes == 15)
    text, role = engine.render_alert(occ, slot)
    lines = text.splitlines()
    assert role == 42 and lines[0] == "<@&42>"
    assert lines[1] == "Soul Ruler Kashapa spawns in 15 minutes"
    assert lines[3] == r"Ancient Zumion Temple Site | reported by Rae\_lin"


def test_dashboard_layout(s):
    now = ny(2026, 10, 9, 12, 10)
    timers.report(s, boss(s, "Silent Dartan"), 1, "R", now)
    dash = engine.build_dashboard(s, _settings(s), now)
    lines = dash.text.splitlines()
    assert lines[0] == "━" * 22
    lines = lines[1:]
    assert lines[0] == "## ⏱️ AION 2 Timers"
    assert lines[1] == "### Server"
    assert lines[2].startswith("Daily reset <t:") and lines[4].startswith("Maintenance starts")
    assert lines[5] == "### Upcoming events"
    assert [l.split(" starts")[0] for l in lines[6:9]] == ["Dimensional Invasion", "Shugo Festival", "Spacetime Rift"]
    assert lines[9] == "### World Bosses"
    assert lines[10].startswith("-# Route: Gartua (Eternal Isle) > Lagta")
    assert lines[11].startswith("Silent Dartan respawning")
    assert lines[-2].startswith("💡 Type `/nyerk` for more") and lines[-3] == ""
    assert lines[-1].startswith("-# Times are in your own time zone")
    assert len(dash.bosses) == 5  # four bosses plus Custom time


def test_dashboard_blank_headings_fall_back_to_spacing(s):
    st = _settings(s)
    st.dashboard_title = st.dashboard_top_label = st.dashboard_main_label = st.oneoff_label = st.dashboard_footer = ""
    st.dashboard_divider = ""
    st.dashboard_commands_label = ""
    st.dashboard_boss_note = ""
    dash = engine.build_dashboard(s, st, ny(2026, 10, 9, 12, 10))
    assert "#" not in dash.text
    assert dash.text.split("\n\n")[1].startswith("Dimensional Invasion")


def test_dashboard_shows_maintenance_ends_while_running(s):
    now = ny(2026, 10, 14, 3, 0)  # Wed 07:00 UTC, maintenance 06:30-10:00 UTC
    dash = engine.build_dashboard(s, _settings(s), now)
    assert "Maintenance ends" in dash.text


def test_maintenance_override_replaces_week(s):
    from nyerk import schedules
    maint = s.scalar(select(Event).where(Event.name == "Maintenance"))
    regular = schedules.get("weekly").next_after(maint.schedule_params, ny(2026, 10, 12, 0, 0))
    s.add(Event(game_id=1, name="Maintenance", schedule_type="one_off", overrides_event_id=maint.id, duration_minutes=60,
                schedule_params={**schedules.get("one_off").validate({"at": "2026-10-15 05:00", "tz": "UTC"}),
                                 "replaces": regular}))
    s.flush()
    occ = engine.event_occurrences(maint, ny(2026, 10, 12, 0, 0), ny(2026, 10, 18, 0, 0),
                                   engine._overrides(engine.load_events(s)).get(maint.id))
    assert len(occ) == 1 and occ[0].duration_min == 60 and occ[0].at != regular


def test_cleanup_deletes_alerts_two_hours_after_the_event(s):
    now = 100_000
    rows = [(1000, now - 2 * 3600 - 60, now - 3 * 3600),   # event 2 h 1 m ago: delete
            (1001, now - 2 * 3600 + 60, now - 3 * 3600),   # 1 h 59 m ago: keep
            (1002, now + 600, now - 60)]                   # upcoming event: keep
    for mid, occ, sent in rows:
        s.add(SentMessage(message_id=mid, event_id=7, occurrence_at=occ, slot=5, sent_at=sent))
    s.flush()
    assert [d.message_id for d in engine.messages_to_delete(s, 120, now)] == [1000]
    assert [d.message_id for d in engine.messages_to_delete(s, 30, now)] == [1000, 1001]

def _settings(s):
    return G(s)  # the AION 2 game: dashboard and boss report settings are per game


def test_cleanup_never_touches_recent_alerts(s):
    s.add(SentMessage(message_id=2000, event_id=8, occurrence_at=100, slot=5, sent_at=10_000))
    s.flush()
    assert engine.messages_to_delete(s, 30, now=10_100) == []      # sent under an hour ago
    assert [m.message_id for m in engine.messages_to_delete(s, 30, now=20_000)] == [2000]

def test_upgrade_adds_text_columns_with_real_newlines(tmp_path):
    import sqlite3

    from nyerk import db
    from nyerk.db import get_settings

    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE settings (id INTEGER PRIMARY KEY, timer_channel_id BIGINT)")
    con.execute("INSERT INTO settings (id, timer_channel_id) VALUES (1, 5)")
    con.commit()
    con.close()
    db.init_engine(path)
    with db.session() as s:
        from nyerk import servers
        assert servers.claim_legacy(s, GUILD)  # what the next start does with GUILD_ID set
        assert get_settings(s, GUILD).roles_header.splitlines()[1].startswith("Pick your class")


def test_upgrade_repairs_escaped_newlines(s):
    from nyerk.db import get_settings
    from nyerk.seed import upgrade_defaults
    st = get_settings(s, GUILD)
    st.roles_header = "# Roles\\nPick"
    assert "roles header" in upgrade_defaults(s)
    assert st.roles_header == "# Roles\nPick"


def test_old_default_report_offset_upgraded(s):
    from nyerk.seed import upgrade_defaults
    st = _settings(s)
    st.report_offset_min = 5
    assert any("report offset" in x for x in upgrade_defaults(s)) and st.report_offset_min == 1
    st.report_offset_min = 3  # a customised value is left alone
    upgrade_defaults(s)
    assert st.report_offset_min == 3


def test_2100_plus_bracket_added_once(s):
    from nyerk.models import PfBracket
    from nyerk.seed import upgrade_defaults
    plus = s.scalar(select(PfBracket).where(PfBracket.label == "2100+"))
    s.delete(plus)
    s.flush()
    assert "gear score bracket 2100+" in upgrade_defaults(s)  # existing installs get it
    s.delete(s.scalar(select(PfBracket).where(PfBracket.label == "2100+")))
    s.flush()
    upgrade_defaults(s)  # deleted by the admin afterwards: stays deleted
    assert s.scalar(select(PfBracket).where(PfBracket.label == "2100+")) is None


def test_boss_buttons_order_and_labels(s):
    dash = engine.build_dashboard(s, _settings(s), ny(2026, 10, 9, 12, 0))
    assert [label for _, label in dash.bosses] == [
        "Eternal Gartua died", "High Commander Lagta died", "Soul Ruler Kashapa died", "Silent Dartan died",
        "Custom time"]
    st = _settings(s)
    st.boss_button_label, st.custom_button_label = "💀 {name}", "Other time"
    labels = [label for _, label in engine.build_dashboard(s, st, ny(2026, 10, 9, 12, 0)).bosses]
    assert labels[0] == "💀 Eternal Gartua" and labels[-1] == "Other time"


def test_upgrade_renames_section_and_reorders_bosses_once(s):
    from nyerk.db import get_state
    from nyerk.seed import upgrade_defaults
    st, state = _settings(s), get_state(s)
    st.oneoff_label = "One-offs"
    state.upgrades_done = [x for x in (state.upgrades_done or []) if x != "boss_order_gartua_first"]
    for i, name in enumerate(["Silent Dartan", "Soul Ruler Kashapa", "High Commander Lagta", "Eternal Gartua"]):
        boss(s, name).sort_order = i  # the old order
    s.flush()
    done = upgrade_defaults(s)
    assert "boss button order" in done and st.oneoff_label == "World Bosses"
    assert boss(s, "Eternal Gartua").sort_order == 0 and boss(s, "High Commander Lagta").sort_order == 1
    boss(s, "Silent Dartan").sort_order = -5  # admin reorders later
    upgrade_defaults(s)
    assert boss(s, "Silent Dartan").sort_order == -5


def test_boss_route_note_shows_without_timers(s):
    text = engine.build_dashboard(s, _settings(s), ny(2026, 10, 9, 12, 0)).text
    assert "### World Bosses\n-# Route: Gartua (Eternal Isle) > Lagta (Laborer's Rest Stop) > Kashapa" in text
    for name in ("Eternal Gartua", "High Commander Lagta", "Silent Dartan", "Soul Ruler Kashapa"):
        boss(s, name).enabled = False
    assert "World Bosses" not in engine.build_dashboard(s, _settings(s), ny(2026, 10, 9, 12, 0)).text


def test_new_text_setting_with_apostrophe_reaches_old_databases(tmp_path):
    import sqlite3
    from nyerk import db
    from nyerk.db import get_settings
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE settings (id INTEGER PRIMARY KEY, timer_channel_id BIGINT)")
    con.execute("INSERT INTO settings (id, timer_channel_id) VALUES (1, 5)")
    con.commit()
    con.close()
    db.init_engine(path)
    with db.session() as s:
        from nyerk import servers
        servers.claim_legacy(s, GUILD)
        assert "(Laborer's Rest Stop)" in G(s).dashboard_boss_note


def test_route_update_reorders_and_renames_untouched_locations(s):
    from nyerk.db import get_state
    from nyerk.seed import upgrade_defaults
    state = get_state(s)
    state.upgrades_done = [x for x in (state.upgrades_done or []) if x != "boss_route_2026_10_11"]
    old = {"Eternal Gartua": "Isle of Eternity", "High Commander Lagta": "Red Forest",
           "Soul Ruler Kashapa": "Artamia Plateau East", "Silent Dartan": "My own spot"}
    for i, (name, loc) in enumerate(old.items()):
        boss(s, name).location, boss(s, name).sort_order = loc, 3 - i
    s.flush()
    assert "boss route order and locations" in upgrade_defaults(s)
    assert [boss(s, n).sort_order for n in ("Eternal Gartua", "High Commander Lagta", "Soul Ruler Kashapa",
                                            "Silent Dartan")] == [0, 1, 2, 3]
    assert boss(s, "High Commander Lagta").location == "Laborer's Rest Stop"
    assert boss(s, "Silent Dartan").location == "My own spot"  # edited by the admin: kept
