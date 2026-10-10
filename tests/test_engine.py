from sqlalchemy import select

from nyerk import engine, timers
from nyerk.models import Category, Event, SentMessage

from .conftest import ny


def boss(s, name="Soul Ruler Kashapa"):
    return s.scalar(select(Event).where(Event.name == name))


def test_blank_report_assumes_kill_five_minutes_ago(s):
    now = ny(2026, 10, 9, 12, 0)
    r = timers.report(s, boss(s), 1, "Raelin", now)
    assert r.ok
    assert r.timer.fire_at == now + 5 * 3600 + 55 * 60
    assert "Active timers: 1/10" in r.reply


def test_duplicate_denied_and_both_fields_rejected(s):
    now = ny(2026, 10, 9, 12, 0)
    assert timers.report(s, boss(s), 1, "Raelin", now).ok
    dup = timers.report(s, boss(s), 2, "Other", now + 60)
    assert not dup.ok and "Raelin" in dup.reply and dup.log
    both = timers.report(s, boss(s, "Silent Dartan"), 2, "Other", now, "10 min ago", "1h")
    assert not both.ok and "not both" in both.reply


def test_countdown_has_no_offset(s):
    now = ny(2026, 10, 9, 12, 0)
    r = timers.report(s, boss(s), 1, "Raelin", now, respawns_in="1h 20m")
    assert r.timer.fire_at == now + 4800 and r.timer.mode == "countdown"


def test_user_limit(s):
    now = ny(2026, 10, 9, 12, 0)
    from nyerk.db import get_settings
    get_settings(s).user_timer_limit = 1
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
    assert lines[3] == r"Artamia Plateau East | reported by Rae\_lin"


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
    assert lines[9] == "### World bosses and one-offs"
    assert lines[10].startswith("Silent Dartan respawning")
    assert lines[-1].startswith("-# Times are in your own time zone")
    assert len(dash.bosses) == 4


def test_dashboard_blank_headings_fall_back_to_spacing(s):
    st = _settings(s)
    st.dashboard_title = st.dashboard_top_label = st.dashboard_main_label = st.oneoff_label = st.dashboard_footer = ""
    st.dashboard_divider = ""
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
    s.add(Event(name="Maintenance", schedule_type="one_off", overrides_event_id=maint.id, duration_minutes=60,
                schedule_params={**schedules.get("one_off").validate({"at": "2026-10-15 05:00", "tz": "UTC"}),
                                 "replaces": regular}))
    s.flush()
    occ = engine.event_occurrences(maint, ny(2026, 10, 12, 0, 0), ny(2026, 10, 18, 0, 0),
                                   engine._overrides(engine.load_events(s)).get(maint.id))
    assert len(occ) == 1 and occ[0].duration_min == 60 and occ[0].at != regular


def test_cleanup_keeps_two_occurrences(s):
    for i, occ in enumerate([100, 200, 300]):
        s.add(SentMessage(message_id=1000 + i, event_id=7, occurrence_at=occ, slot=5, sent_at=occ))
    s.flush()
    doomed = engine.messages_to_delete(s, keep=2)
    assert [d.message_id for d in doomed] == [1000]


def _settings(s):
    from nyerk.db import get_settings
    return get_settings(s)
