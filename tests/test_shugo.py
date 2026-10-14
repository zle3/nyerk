from pathlib import Path

import pytest
from sqlalchemy import select

from nyerk import shugo
from nyerk.models import Event

from .conftest import ny

HTML = (Path(__file__).parent / "fixtures" / "shugo_timers.html").read_text(encoding="utf-8")


def test_parse_na_east():
    items = {i["name"]: i for i in shugo.parse(HTML, "NA East")}
    assert items["Spacetime Rift"]["schedule_params"]["times"][0] == "02:00"
    assert items["Spacetime Rift"]["duration_minutes"] == 60
    assert items["Shugo Festival"]["schedule_type"] == "hourly"
    assert items["Dimensional Invasion"]["schedule_params"]["minute"] == 30
    assert items["Artifact Siege"]["schedule_params"]["days"] == ["mon", "thu", "sat"]
    assert items["Daily reset"]["schedule_params"]["tz"] == "Asia/Seoul"


def test_parse_failure():
    with pytest.raises(shugo.ParseError):
        shugo.parse("<html><h2>Nope</h2></html>", "NA East")


def test_diff_seeded_db_only_offers_new_events(s):
    changes = shugo.diff(s, shugo.parse(HTML, "NA East"), ny(2026, 10, 9, 12, 0), 1)
    kinds = {c["name"]: c["kind"] for c in changes}
    # Resets at 16:00 KST equal 07:00 UTC, so no spurious update.
    assert "Daily reset" not in kinds and "Weekly reset" not in kinds and "Spacetime Rift" not in kinds
    assert kinds["Artifact Siege"] == "new"


def test_hand_edited_field_is_never_overwritten(s):
    rift = s.scalar(select(Event).where(Event.name == "Spacetime Rift"))
    rift.duration_minutes = 45
    rift.edited_fields = ["duration_minutes"]
    s.flush()
    changes = shugo.diff(s, shugo.parse(HTML, "NA East"), ny(2026, 10, 9, 12, 0), 1)
    ch = next(c for c in changes if c["name"] == "Spacetime Rift")
    assert ch["fields"]["duration_minutes"]["locked"]
    shugo.apply(s, {"changes": changes}, {ch["key"]}, 1)
    assert rift.duration_minutes == 45
