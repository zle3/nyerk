"""Generate the README screenshots from a throwaway demo instance.

    python -m tools.screenshots [output_dir]      (default: docs/images)

Builds a demo database with fake members and IDs and a fixed clock,
renders Nyerk's real Discord messages as Discord-style pages, runs the web
page with a stand-in bot, and screenshots everything with Playwright. No
Discord account, no real data, nothing to redact. Needs the dev extras in
requirements-screenshots.txt and `playwright install chromium`.
"""
from __future__ import annotations

import base64
import os
import socket
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools import demo  # noqa: E402

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "docs" / "images"
WORK = Path(tempfile.mkdtemp(prefix="nyerk-shots-"))
os.environ.update(
    NYERK_DATA_DIR=str(WORK), WEB_TLS="self-signed", PUBLIC_URL="https://nyerk.example.com",
    DISCORD_CLIENT_ID="1", DISCORD_CLIENT_SECRET="demo", OWNER_IDS=str(demo.OWNER_ID),
    ADMIN_ROLE_IDS=str(demo.ADMIN_ROLE_ID), GUILD_ID=str(demo.GUILD_ID),
)
demo.freeze_clock()

from sqlalchemy import select  # noqa: E402

from nyerk import db, engine, members, party, reaction_roles  # noqa: E402
from nyerk.db import get_settings  # noqa: E402
from nyerk.models import Category, Event, PfParty, RoleHolder, UserTimer  # noqa: E402
from tools.discord_mock import Button, Message, Select, buttons_in_rows, page  # noqa: E402

NOW = demo.NOW
MEMBER_FOR_ME = 7  # Lyra


# -- Discord channels ------------------------------------------------------------


def names(s):
    roles = {c.role_id: c.name for c in s.scalars(select(Category))}
    roles |= {c.role_id: c.name for c in party.contents(s, False)}
    roles |= {c.role_id: c.name for c in party.classes(s)}
    st = get_settings(s)
    roles |= {st.tank_role_id: "Tank", st.support_role_id: "Support", st.dps_role_id: "DPS"}
    users = {demo.uid(i): n for i, n in enumerate(demo.MEMBERS)}
    return roles, users


def class_panel(s, at):
    btns = [Button(c.name, emoji=c.emoji) for c in party.classes(s) if c.role_id][:23]
    btns += [Button("My roles", "primary", "📋"), Button("Clear classes", "danger")]
    return Message(at, party.CLASS_PANEL, buttons_in_rows(btns))


def gear_panel(s, at):
    btns = [Button(b.label) for b in party.brackets(s)] + [Button("Clear", "danger")]
    return Message(at, party.GEAR_PANEL, buttons_in_rows(btns))


def timer_channel(s):
    events = {e.name: e for e in engine.load_events(s)}

    def alert(name, occ_at, lead, timer=None):
        e = events[name]
        slot = next(sl for sl in e.slots if sl.lead_minutes == lead)
        text, _ = engine.render_alert(engine.Occurrence(e, occ_at, e.duration_minutes, timer), slot)
        return Message(occ_at - lead * 60, text)

    t = lambda h, m: int(datetime(2026, 10, 14, h, m, tzinfo=demo.NY).timestamp())  # noqa: E731
    orrin = UserTimer(user_name="Orrin", user_id=demo.uid(5), mode="kill", fire_at=t(14, 45), status="done",
                      created_at=t(8, 46), armed_at=t(8, 46), event_id=events["Silent Dartan"].id)
    msgs = [
        alert("Spacetime Rift", t(14, 0), 5),
        alert("Shugo Festival", t(14, 0), 5),
        alert("Spacetime Rift", t(14, 0), 0),
        alert("Dimensional Invasion", t(14, 30), 5),
        alert("Silent Dartan", t(14, 45), 15, orrin),
        alert("Silent Dartan", t(14, 45), 0, orrin),
    ]
    dash = engine.build_dashboard(s, get_settings(s), NOW)
    btns = [Button(label, "primary" if bid == 0 else "secondary") for bid, label in dash.bosses]
    msgs.append(Message(t(14, 45), dash.text, buttons_in_rows(btns), edited=True))
    return msgs


def roles_channel(s):
    at = NOW - 2 * 86400
    st = get_settings(s)
    msgs = [Message(at, st.roles_header), class_panel(s, at), gear_panel(s, at)]
    holders = {}
    for h in s.scalars(select(RoleHolder)):
        holders[h.role_id] = holders.get(h.role_id, 0) + 1
    for text, chunk in reaction_roles.messages(s):
        msgs.append(Message(at, text, reactions=[(e.emoji, holders.get(e.role_id, 0) + 1) for e in chunk]))
    msgs.append(Message(at, reaction_roles.CLEAR_TEXT, [[
        Button("My roles", "primary", "📋"), Button("Quiet hours", "primary", "🌙"),
        Button("Turn off quiet hours"), Button("Remove all my ping roles", "danger")]]))
    from nyerk.bot import my_roles_text
    lyra = demo.uid(MEMBER_FOR_ME)
    held = {h.role_id for h in s.scalars(select(RoleHolder).where(RoleHolder.user_id == lyra))}
    msgs.append(Message(NOW, my_roles_text(s, lyra, held, NOW), ephemeral=True))
    return msgs


def party_channel(s, ctx):
    at = NOW - 2 * 86400
    st = get_settings(s)
    full = party.live_parties(s, "full")
    open_p = s.get(PfParty, ctx["open_party_id"])
    msgs = [
        class_panel(s, at), gear_panel(s, at),
        Message(at, party.interest_panel_text(s, NOW), [[Button("Register", "primary"),
                                                          Button("Cancel my registration")]], edited=True),
        Message(at, party.CREATE_PANEL.format(lifetime=st.party_lifetime_min), [[Button("Create party", "primary")]]),
        Message(at, party.status_text(full), buttons_in_rows(
            [Button(f"Done: {party.party_title(p)} ({p.leader_name})") for p in full]), edited=True),
    ]
    kick = [m.user_name for m in open_p.members if m.user_id != open_p.leader_id]
    rows = [[Button("Join", "success"), Button("Leave"), Button("Close", "danger")]]
    if kick:
        rows.append([Select("Leader: remove a member")])
    msgs.append(Message(open_p.created_at, party.listing_text(open_p), rows, edited=True))
    targets = party.ping_targets(s, open_p, NOW)
    role = open_p.content.role_id if open_p.content else 0
    msgs.append(Message(open_p.created_at, party.ping_text(open_p, targets, role),
                        [[Button("Join", "success"), Button("Not this time")]]))
    return msgs


# -- web page --------------------------------------------------------------------


class ShotBot:
    """Stand-in for the Discord bot: online, roles from the demo database."""

    def is_ready(self):
        return True

    def status(self):
        return {"connected": True, "user": "Nyerk#0000", "guild": "Your Server", "latency_ms": 42, "last_tick": NOW}

    def request_refresh(self, repost=False):
        pass

    async def fetch_member(self, user_id):
        if user_id == demo.OWNER_ID:
            return "Owner", set()
        with db.session() as s:
            held = {h.role_id for h in s.scalars(select(RoleHolder).where(RoleHolder.user_id == user_id))}
        name = dict((demo.uid(i), n) for i, n in enumerate(demo.MEMBERS)).get(user_id)
        return (name, held) if name else None

    async def admin_apply_roles(self, uid, add, remove):
        return []

    async def apply_quiet_hours(self, only=None):
        pass


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def start_web(port: int) -> None:
    import uvicorn

    from nyerk.config import Config
    from nyerk.housekeeping import ensure_self_signed
    from nyerk.web import auth
    from nyerk.web.app import create_app

    async def identify(cfg, code):  # simulated Discord sign-in: the code names the user
        return int(code), "member"

    auth.discord_identify = identify
    cfg = Config()
    cert, key = ensure_self_signed(cfg)
    server = uvicorn.Server(uvicorn.Config(create_app(cfg, ShotBot()), host="127.0.0.1", port=port,
                                           log_level="warning", ssl_certfile=str(cert), ssl_keyfile=str(key)))
    server.install_signal_handlers = lambda: None
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), 0.2).close()
            return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("web page did not start")


# -- capture ---------------------------------------------------------------------


def main() -> None:
    from playwright.sync_api import sync_playwright

    ctx = demo.build(WORK / "nyerk.db")
    logo = "data:image/png;base64," + base64.b64encode((ROOT / "nyerk/web/static/favicon.png").read_bytes()).decode()
    raw = WORK / "raw"
    raw.mkdir()
    with db.session() as s:
        roles, users = names(s)
        pages = {
            "discord-timers": page("aion-2-timer", "Boss timers and event alerts", timer_channel(s), NOW, logo, roles, users),
            "discord-roles": page("aion-2-roles", "Pick your classes, gear score and pings", roles_channel(s), NOW, logo, roles, users),
            "discord-party-finder": page("aion-2-party-finder", "Find a group", party_channel(s, ctx), NOW, logo, roles, users),
        }
    port = free_port()
    start_web(port)
    base = f"https://127.0.0.1:{port}"
    frozen = datetime.fromtimestamp(NOW, timezone.utc)

    with sync_playwright() as p:
        browser = p.chromium.launch()

        def new_page(width):
            pg = browser.new_page(viewport={"width": width, "height": 900}, color_scheme="dark",
                                  ignore_https_errors=True, timezone_id="America/New_York", locale="en-US")
            pg.clock.set_fixed_time(frozen)
            return pg

        # Discord channels
        pg = new_page(900)
        for name, doc in pages.items():
            f = WORK / f"{name}.html"
            f.write_text(doc, encoding="utf-8")
            pg.goto(f.as_uri())
            pg.screenshot(path=str(raw / f"{name}.png"), full_page=True)

        # Web page as the owner
        pg = new_page(1280)
        pg.goto(base + "/login")
        pg.fill("#password", demo.PASSWORD)
        pg.click("button[type=submit]")
        pg.wait_for_url("**/")

        def shot(path, name, until=None, height=None, start=None):
            pg.goto(base + path)
            pg.wait_for_load_state("networkidle")
            y0 = int(pg.locator(start).first.bounding_box()["y"]) - 20 if start else 0
            if until:
                y1 = int(pg.locator(until).first.bounding_box()["y"]) + 4
            elif height:
                y1 = y0 + height
            else:
                last = pg.locator(".discord").last.bounding_box()
                y1 = int(last["y"] + last["height"]) + 12
            pg.screenshot(path=str(raw / f"{name}.png"), full_page=True,
                          clip={"x": 0, "y": y0, "width": 1280, "height": y1 - y0})

        shot("/", "web-overview", until="text=Recent activity")
        shot("/stats?range=30", "web-stats", until="text=Top members")
        shot("/members", "web-members", height=760)
        shot("/categories", "web-roles-preview", start="text=Roles channel preview")
        shot("/access", "web-access", until="text=Recent sign-ins")

        # My page as a member, through a simulated Discord sign-in
        member = new_page(1280)

        # Start sign-in, read where Nyerk sends the browser, and come back the
        # way Discord would, with the member's ID as the code.
        start = member.context.request.get(base + "/auth/discord", max_redirects=0)
        state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
        member.goto(f"{base}/auth/callback?code={demo.uid(MEMBER_FOR_ME)}&state={state}")
        member.wait_for_url("**/me")
        member.wait_for_load_state("networkidle")
        member.screenshot(path=str(raw / "web-my-page.png"), full_page=True)
        browser.close()

    frame(raw, OUT)
    print(f"Wrote {len(list(raw.glob('*.png')))} screenshots to {OUT}")


def frame(src_dir: Path, out_dir: Path) -> None:
    """Same width, dark frame, rounded corners and a thin border for every image."""
    from PIL import Image, ImageDraw

    W, PAD, MAXW, R = 1000, 20, 960, 12
    BG, BORDER = (17, 18, 20), (43, 45, 49)
    out_dir.mkdir(parents=True, exist_ok=True)
    for src in sorted(src_dir.glob("*.png")):
        im = Image.open(src).convert("RGB")
        if im.width > MAXW:
            im = im.resize((MAXW, round(im.height * MAXW / im.width)), Image.LANCZOS)
        mask = Image.new("L", im.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, im.width - 1, im.height - 1), radius=R, fill=255)
        canvas = Image.new("RGB", (W, im.height + 2 * PAD), BG)
        x = (W - im.width) // 2
        ImageDraw.Draw(canvas).rounded_rectangle((x - 1, PAD - 1, x + im.width, PAD + im.height),
                                                 radius=R + 1, fill=BORDER)
        canvas.paste(im, (x, PAD), mask)
        canvas.save(out_dir / src.name, optimize=True)


if __name__ == "__main__":
    main()
