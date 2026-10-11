import asyncio

import pytest
from sqlalchemy import select

from nyerk import engine, safety, shugo, transfer
from nyerk.models import Category
from nyerk.web import security

GUILD = 999_999_999_999_999_999


@pytest.fixture(autouse=True)
def guild():
    safety.FORBIDDEN_ROLE_IDS.clear()
    safety.protect_guild(GUILD)
    yield
    safety.FORBIDDEN_ROLE_IDS.clear()


def test_templates_only_fill_plain_fields():
    out = engine.render_template("{name} {name.__class__} {role[0]} {nope}", {"name": "Gartua", "role": ""})
    assert out == "Gartua {name.__class__} {role[0]} {nope}"


@pytest.mark.parametrize("url,ok", [
    ("https://shugo.gg/timers", True), ("https://www.shugo.gg/timers", True),
    ("http://shugo.gg/timers", False), ("https://192.168.1.1/admin", False),
    ("https://shugo.gg.evil.com/x", False), ("https://shugo.gg:8080/x", False), ("file:///etc/passwd", False),
])
def test_shugo_allowlist(url, ok):
    assert shugo.allowed_url(url) is ok


def test_fetch_refuses_other_hosts():
    with pytest.raises(shugo.ParseError):
        asyncio.run(shugo.fetch("http://10.0.0.1/"))


def test_everyone_role_is_never_accepted_or_pinged(s):
    with pytest.raises(ValueError, match="@everyone"):
        safety.check_role(GUILD)
    assert safety.pingable(GUILD) == 0 and safety.pingable(123) == 123
    with pytest.raises(transfer.ImportError_, match="@everyone"):
        transfer.apply_import(s, {"nyerk_export": 1, "categories": [{"name": "Shugo", "role_id": str(GUILD)}]})


def test_everyone_alert_does_not_ping(s, monkeypatch):
    from .conftest import ny
    from .test_bot import make_bot
    from nyerk.db import get_state
    s.scalar(select(Category).where(Category.name == "Invasion")).role_id = GUILD
    from nyerk.models import Event
    inv = s.scalar(select(Event).where(Event.name == "Dimensional Invasion"))
    for sl in inv.slots:
        sl.ping = True
    clock = [ny(2026, 10, 9, 13, 25, 1)]
    get_state(s).last_tick_at = clock[0] - 30
    s.commit()

    async def run():
        b, ch = make_bot(monkeypatch, clock)
        await b.tick()
        return ch

    ch = asyncio.run(run())
    msg, mentions = next((m, a) for m, a in ch.sent if "Dimensional Invasion starts" in m.content)
    assert f"<@&{GUILD}>" not in msg.content and mentions.roles is False


class _Req:
    def __init__(self, peer, headers):
        self.client = type("C", (), {"host": peer})()
        self.headers = headers


def test_client_ip_trusts_cloudflare_only_from_private_peer():
    assert security.client_ip(_Req("172.18.0.3", {"cf-connecting-ip": "203.0.113.9"})) == "203.0.113.9"
    assert security.client_ip(_Req("8.8.8.8", {"cf-connecting-ip": "1.2.3.4"})) == "8.8.8.8"
    assert security.client_ip(_Req("192.168.1.20", {})) == "192.168.1.20"


def test_rate_limiter_window():
    rl = security.RateLimiter()
    assert all(rl.allow("ip", "b", 3, 60, now=0) for _ in range(3))
    assert not rl.allow("ip", "b", 3, 60, now=1)
    assert rl.allow("ip", "b", 3, 60, now=61)
    assert rl.allow("other", "b", 3, 60, now=1)


def test_middleware_limits_and_headers(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from nyerk import db
    from nyerk.config import Config
    from nyerk.web.app import create_app
    monkeypatch.setenv("NYERK_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("WEB_TLS", "off")
    monkeypatch.setenv("PUBLIC_URL", "https://nyerk.example.com")
    db.init_engine(":memory:")
    c = TestClient(create_app(Config()))
    r = c.get("/login")
    assert r.headers["strict-transport-security"].startswith("max-age=")
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert c.post("/login", content=b"x" * 10, headers={"content-length": str(5_000_000)}).status_code == 413
    codes = [c.get("/login").status_code for _ in range(12)]
    assert 429 in codes
