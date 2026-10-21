"""The private app API: world boss timers read from the game."""
import time

from fastapi.testclient import TestClient
from sqlalchemy import select

from nyerk import db
from nyerk.config import Config
from nyerk.models import UserTimer
from nyerk.seed import seed
from nyerk.web.app import create_app
from .conftest import GUILD  # noqa: F401


def make(tmp_path, monkeypatch, key="k3y"):
    monkeypatch.setenv("NYERK_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("WEB_TLS", "off")
    monkeypatch.setenv("GUILD_ID", str(GUILD))
    if key:
        monkeypatch.setenv("APP_API_KEY", key)
    db.init_engine(":memory:")
    with db.session() as s:
        seed(s, GUILD)
    return TestClient(create_app(Config()))


def test_off_without_a_key_and_needs_the_key(tmp_path, monkeypatch):
    c = make(tmp_path, monkeypatch, key="")
    assert c.get("/api/app/timers").status_code == 404
    c = make(tmp_path, monkeypatch)
    assert c.get("/api/app/timers").status_code == 401
    assert c.get("/api/app/timers", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_game_times_set_and_replace_timers(tmp_path, monkeypatch):
    c = make(tmp_path, monkeypatch)
    auth = {"Authorization": "Bearer k3y"}
    fire = int(time.time()) + 5 * 3600
    r = c.post("/api/app/timers", headers=auth, json={"timers": [
        {"boss": "Eternal Gartua", "fire_at": fire, "by": "Alias", "user_id": 42},
        {"boss": "Nobody", "fire_at": fire},
        {"boss": "Silent Dartan", "fire_at": 5},
    ]})
    assert r.status_code == 200
    assert r.json() == {"set": ["Eternal Gartua"], "same": [], "unknown": ["Nobody", "Silent Dartan"]}
    got = c.get("/api/app/timers", headers=auth).json()["timers"]
    assert got == [{"boss": "Eternal Gartua", "fire_at": fire, "by": "Alias (game)"}]
    # Within a minute: left alone. A new kill: replaced.
    r = c.post("/api/app/timers", headers=auth, json={"timers": [{"boss": "eternal gartua", "fire_at": fire + 30}]})
    assert r.json()["same"] == ["Eternal Gartua"]
    r = c.post("/api/app/timers", headers=auth, json={"timers": [{"boss": "Eternal Gartua", "fire_at": fire + 3600}]})
    assert r.json()["set"] == ["Eternal Gartua"]
    with db.session() as s:
        rows = list(s.scalars(select(UserTimer).order_by(UserTimer.id)))
        assert [t.status for t in rows] == ["overridden", "active"]
        assert rows[1].fire_at == fire + 3600


def test_events_come_with_the_timers(tmp_path, monkeypatch):
    c = make(tmp_path, monkeypatch)
    got = c.get("/api/app/timers", headers={"Authorization": "Bearer k3y"}).json()
    names = [e["name"] for e in got["events"]]
    # The AION 2 template's scheduled events (bosses are timers, not events).
    assert "Shugo Festival" in names and "Daily reset" in names
    assert "Eternal Gartua" not in names
    now = int(time.time())
    shugo = next(e for e in got["events"] if e["name"] == "Shugo Festival")
    assert shugo["category"] == "Shugo"
    # Hourly: on now, or due within the hour (unless maintenance is on).
    assert shugo["running_until"] or (shugo["next_at"] and shugo["next_at"] - now <= 3600 + 4 * 3600)
