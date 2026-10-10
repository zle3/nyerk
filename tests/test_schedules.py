from datetime import datetime, timezone

import pytest

from nyerk import schedules
from nyerk.schedules import ScheduleError, occurrences_between

from .conftest import NY, ny


def local(ts):
    return datetime.fromtimestamp(ts, NY).strftime("%Y-%m-%d %H:%M %Z")


def test_hourly_next():
    p = {"minute": 30, "tz": "America/New_York"}
    assert local(schedules.get("hourly").next_after(p, ny(2026, 10, 9, 14, 10))) == "2026-10-09 14:30 EDT"
    assert local(schedules.get("hourly").next_after(p, ny(2026, 10, 9, 14, 30))) == "2026-10-09 15:30 EDT"


def test_hourly_fires_twice_in_repeated_fall_back_hour():
    p = {"minute": 0, "tz": "America/New_York"}
    occ = occurrences_between("hourly", p, ny(2026, 11, 1, 0, 30), ny(2026, 11, 1, 0, 30) + 4 * 3600)
    assert len(occ) == 4  # 01:00 EDT, 01:00 EST, 02:00 EST, 03:00 EST
    assert len(set(occ)) == 4


def test_daily_keeps_new_york_hour_across_dst():
    p = schedules.get("daily").validate({"times": "02:00, 23:00", "tz": "America/New_York"})
    occ = occurrences_between("daily", p, ny(2026, 10, 31, 12, 0), ny(2026, 11, 2, 12, 0))
    assert [local(t) for t in occ] == [
        "2026-10-31 23:00 EDT", "2026-11-01 02:00 EST", "2026-11-01 23:00 EST", "2026-11-02 02:00 EST",
    ]


def test_utc_reset_moves_an_hour_earlier_in_new_york_on_nov_1():
    p = {"times": ["07:00"], "tz": "UTC"}
    occ = occurrences_between("daily", p, ny(2026, 10, 31, 0, 0), ny(2026, 11, 2, 12, 0))
    assert [local(t) for t in occ] == ["2026-10-31 03:00 EDT", "2026-11-01 02:00 EST", "2026-11-02 02:00 EST"]


def test_weekly():
    p = schedules.get("weekly").validate({"days": ["mon", "thu", "sat"], "time": "21:00", "tz": "America/New_York"})
    occ = occurrences_between("weekly", p, ny(2026, 10, 9, 0, 0), ny(2026, 10, 16, 0, 0))
    assert [local(t) for t in occ] == ["2026-10-10 21:00 EDT", "2026-10-12 21:00 EDT", "2026-10-15 21:00 EDT"]


def test_every_n_hours_from_anchor():
    p = schedules.get("every_n_hours").validate({"hours": 3, "anchor": "01:00", "tz": "America/New_York"})
    occ = occurrences_between("every_n_hours", p, ny(2026, 10, 9, 0, 0), ny(2026, 10, 10, 0, 0))
    assert [local(t)[11:16] for t in occ] == ["01:00", "04:00", "07:00", "10:00", "13:00", "16:00", "19:00", "22:00"]


def test_monthly_clamps_to_month_end():
    p = schedules.get("monthly").validate({"day": 31, "time": "12:00", "tz": "UTC"})
    t = schedules.get("monthly").next_after(p, int(datetime(2026, 11, 1, tzinfo=timezone.utc).timestamp()))
    assert datetime.fromtimestamp(t, timezone.utc).date().isoformat() == "2026-11-30"


def test_one_off():
    st = schedules.get("one_off")
    p = st.validate({"at": "2026-12-01 12:00", "tz": "America/New_York"})
    assert st.next_after(p, ny(2026, 11, 1, 0, 0)) == ny(2026, 12, 1, 12, 0)
    assert st.next_after(p, ny(2026, 12, 2, 0, 0)) is None


def test_spring_forward_gap_resolves_after_gap():
    p = {"times": ["02:30"], "tz": "America/New_York"}
    t = schedules.get("daily").next_after(p, ny(2027, 3, 14, 0, 0))
    assert local(t) == "2027-03-14 03:30 EDT"


@pytest.mark.parametrize("stype,params", [
    ("hourly", {"minute": 61}),
    ("daily", {"times": ""}),
    ("weekly", {"days": ["xyz"], "time": "10:00"}),
    ("daily", {"times": "10:00", "tz": "Mars/Base"}),
])
def test_validation_errors(stype, params):
    with pytest.raises(ScheduleError):
        schedules.get(stype).validate(params)
