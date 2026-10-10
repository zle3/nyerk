import pytest

from nyerk.timeparse import TimeParseError, humanize, parse_duration, parse_moment

from .conftest import ny


@pytest.mark.parametrize("text,seconds", [
    ("1h 20m", 4800), ("1h20m", 4800), ("1h20", 4800), ("80m", 4800), ("80", 4800),
    ("1:20", 4800), ("2 hours and 5 minutes", 7500), ("45 min", 2700), ("1d", 86400),
])
def test_durations(text, seconds):
    assert parse_duration(text) == seconds


@pytest.mark.parametrize("text", ["", "soon", "1x", "h", "-5m"])
def test_bad_durations(text):
    with pytest.raises(TimeParseError):
        parse_duration(text)


def test_moments():
    now = ny(2026, 10, 9, 14, 0)
    assert parse_moment("10 min ago", now, "America/New_York") == now - 600
    assert parse_moment("13:30", now, "America/New_York") == ny(2026, 10, 9, 13, 30)
    assert parse_moment("1:30pm", now, "America/New_York") == ny(2026, 10, 9, 13, 30)
    # A clock time later than now means yesterday.
    assert parse_moment("15:00", now, "America/New_York") == ny(2026, 10, 8, 15, 0)
    assert parse_moment("<t:1791574020:F>", now, "America/New_York") == 1791574020
    assert parse_moment("2026-10-09 09:15", now, "America/New_York") == ny(2026, 10, 9, 9, 15)


def test_humanize():
    assert humanize(3 * 3600 + 600) == "3h 10m"
    assert humanize(30) == "0m"
