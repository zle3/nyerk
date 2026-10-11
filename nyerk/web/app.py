"""LAN-only web admin. Server-rendered pages; it only edits the database and
nudges the bot to refresh."""
from __future__ import annotations

import asyncio
import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, available_timezones

import bcrypt
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape
from sqlalchemy import func, select
from starlette.middleware.sessions import SessionMiddleware

from .. import db, engine, members, party, quiet, reaction_roles, schedules, shugo, stats, timers, transfer
from ..db import get_settings, get_state
from ..housekeeping import run_import_check
from ..logbus import bus
from ..models import (
    AlertSlot, Category, Event, LogEntry, PfBracket, PfClass, PfContent, PfInterest, PfParty, PfWindow,
    QuietHours, ReactionGroup, RoleHolder, UserTimer,
)
from ..seed import make_slots, NONE

HERE = Path(__file__).parent
TS_STYLES = {"t": "Short time", "T": "Long time", "d": "Short date", "D": "Long date",
             "f": "Date and time", "F": "Weekday, date and time", "R": "Relative"}
TEMPLATE_FIELDS = ["name", "role", "minutes", "time_full", "time_relative", "time_short", "location",
                   "reporter", "category", "end_full", "end_relative", "duration"]
COMMON_TZ = ["America/New_York", "UTC", "America/Chicago", "America/Los_Angeles", "Europe/Berlin", "Asia/Seoul"]
MAX_FAILS, LOCKOUT = 5, 60

templates = Jinja2Templates(directory=str(HERE / "templates"))
_TS = re.compile(r"&lt;t:(\d+):([tTdDfFR])&gt;")


def discord_preview(text: str) -> Markup:
    """Turn Discord timestamp tokens into <time> tags the browser formats."""
    html = str(escape(text))
    html = _TS.sub(lambda m: f'<time data-ts="{m[1]}" data-style="{m[2]}">{m[0]}</time>', html)
    html = re.sub(r"&lt;@&amp;(\d+)&gt;", r'<span class="mention">@role \1</span>', html)
    html = re.sub(r"^### (.*)$", r'<span class="h3">\1</span>', html, flags=re.M)
    html = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", html)
    html = re.sub(r"&lt;/(nyerk [a-z]+):\d+&gt;", r'<span class="command">/\1</span>', html)
    html = re.sub(r"`([^`\n]+)`", r'<code class="inline">\1</code>', html)
    html = re.sub(r"^## (.*)$", r'<span class="h2">\1</span>', html, flags=re.M)
    html = re.sub(r"^# (.*)$", r'<span class="h1">\1</span>', html, flags=re.M)
    html = re.sub(r"^-# (.*)$", r'<span class="subtext">\1</span>', html, flags=re.M)
    # Headings are block-level, so the newline after them would double the gap.
    html = re.sub(r'(<span class="(?:h1|h2|h3|subtext)">[^\n]*</span>)\n', r"\1", html)
    return Markup(html)


def fmt_ts(ts: int | None, tz: str = "UTC") -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(ts, ZoneInfo(tz)).strftime("%a %b %d, %H:%M %Z")


templates.env.filters["discord"] = discord_preview
templates.env.filters["fmt_ts"] = fmt_ts
templates.env.globals.update(TS_STYLES=TS_STYLES, DAYS=schedules.DAYS, COMMON_TZ=COMMON_TZ)


class Login(Exception):
    pass


def create_app(cfg, bot=None) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.bot = bot
    app.state.fails: dict[str, tuple[int, float]] = {}
    secret = cfg.session_secret or _persistent_secret(cfg)
    app.add_middleware(SessionMiddleware, secret_key=secret, session_cookie="nyerk_session",
                       max_age=14 * 86400, same_site="strict", https_only=cfg.web_tls != "off")
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    @app.exception_handler(Login)
    async def _to_login(request: Request, exc: Login):
        return RedirectResponse("/login", status_code=303)

    @app.middleware("http")
    async def _headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; form-action 'self'")
        return resp

    # -- helpers ----------------------------------------------------------------

    def require(request: Request) -> None:
        if not request.session.get("auth"):
            raise Login()

    async def form(request: Request) -> dict[str, Any]:
        require(request)
        data = await request.form()
        if not secrets.compare_digest(str(data.get("csrf", "")), request.session.get("csrf", "-")):
            raise HTTPException(400, "Form expired, go back and reload the page.")
        return data

    def flash(request: Request, text: str, kind: str = "ok") -> None:
        request.session.setdefault("flash", []).append([kind, text])

    def render(request: Request, name: str, **ctx) -> HTMLResponse:
        if "csrf" not in request.session:
            request.session["csrf"] = secrets.token_urlsafe(32)
        with db.session() as s:
            pending = get_state(s).pending_import
        ctx.update(
            request=request, csrf=request.session["csrf"], flashes=request.session.pop("flash", []),
            nav=name.split(".")[0], pending_changes=len((pending or {}).get("changes", [])),
            bot_status=app.state.bot.status() if app.state.bot else None,
        )
        return templates.TemplateResponse(request, name, ctx)

    def back(url: str) -> RedirectResponse:
        return RedirectResponse(url, status_code=303)

    def refresh(repost: bool = False) -> None:
        if app.state.bot:
            app.state.bot.request_refresh(repost=repost)

    # -- auth ---------------------------------------------------------------------

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        return render(request, "login.html")

    @app.post("/login")
    async def login(request: Request):
        ip = request.client.host if request.client else "?"
        fails, until = app.state.fails.get(ip, (0, 0.0))
        if until > time.time():
            flash(request, f"Too many attempts. Try again in {int(until - time.time())} s.", "err")
            return back("/login")
        data = await request.form()
        if not secrets.compare_digest(str(data.get("csrf", "")), request.session.get("csrf", "-")):
            return back("/login")
        with db.session() as s:
            hashed = get_settings(s).admin_password_hash
        pw = str(data.get("password", "")).encode()
        if hashed and bcrypt.checkpw(pw, hashed.encode()):
            app.state.fails.pop(ip, None)
            request.session.clear()
            request.session.update(auth=True, csrf=secrets.token_urlsafe(32))
            return back("/")
        fails += 1
        app.state.fails[ip] = (fails, time.time() + LOCKOUT if fails >= MAX_FAILS else 0.0)
        bus.warn("admin", f"Failed admin login from {ip}.", discord=fails >= MAX_FAILS)
        flash(request, "Wrong password.", "err")
        return back("/login")

    @app.post("/logout")
    async def logout(request: Request):
        await form(request)
        request.session.clear()
        return back("/login")

    # -- overview -------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def overview(request: Request):
        require(request)
        with db.session() as s:
            settings, state = get_settings(s), get_state(s)
            dash = engine.build_dashboard(s, settings, int(time.time()))
            logs = list(s.scalars(select(LogEntry).order_by(LogEntry.id.desc()).limit(25)))
            active = s.scalar(select(func.count()).select_from(UserTimer).where(UserTimer.status == "active"))
            ctx = dict(dash=dash, logs=logs, active=active, state=state, settings=settings)
        return render(request, "overview.html", **ctx)

    @app.post("/dashboard/repost")
    async def repost_dashboard(request: Request):
        await form(request)
        refresh(repost=True)
        flash(request, "Dashboard will be reposted in a moment." if app.state.bot else "Bot is not running.")
        return back("/")

    # -- members -----------------------------------------------------------------------

    def _bot_ready():
        bot = app.state.bot
        return bot if bot and bot.is_ready() else None

    @app.get("/members", response_class=HTMLResponse)
    async def members_page(request: Request, q: str = ""):
        require(request)
        with db.session() as s:
            rows = members.known(s, q)
        lookup = q.strip() if re.fullmatch(r"\d{15,21}", q.strip()) else ""
        return render(request, "members.html", rows=rows, q=q, lookup=lookup)

    @app.get("/members/{uid}", response_class=HTMLResponse)
    async def member_page(request: Request, uid: int):
        require(request)
        bot = _bot_ready()
        live = await bot.fetch_member(uid) if bot else None
        with db.session() as s:
            info = members.detail(s, uid)
            options = members.role_options(s)
            default_tz = get_settings(s).default_tz
        if live is None and bot is not None and not info["name"]:
            flash(request, f"No member with ID {uid} in the server.", "err")
            return back("/members")
        held = live[1] if live else info["tracked"]
        info["member_name"] = live[0] if live else (info.pop("name") or f"User {uid}")
        info.pop("name", None)
        with db.session() as s:
            gear = party.gear_of(s, uid)
            info["gear_id"], info["brackets"] = (gear.id if gear else 0), party.brackets(s)
        return render(request, "member.html", uid=uid, live=live is not None, held=held,
                      options=options, zones=quiet.ZONES, default_tz=default_tz,
                      hours=[(h, quiet.hour_label(h)) for h in range(24)], **info)

    @app.post("/members/{uid}/roles")
    async def member_roles(request: Request, uid: int):
        data = await form(request)
        bot = _bot_ready()
        if bot is None:
            flash(request, "The bot must be connected to change roles.", "err")
            return back(f"/members/{uid}")
        live = await bot.fetch_member(uid)
        if live is None:
            flash(request, "That member isn't in the server.", "err")
            return back(f"/members/{uid}")
        name, held = live
        chosen = {int(v) for v in data.getlist("role") if str(v).isdigit()}
        with db.session() as s:
            add, remove, hold = members.plan_roles(s, uid, held, chosen)
            labels = {o.role_id: o.label for o in members.role_options(s)}
        errors = await bot.admin_apply_roles(uid, add, remove)
        with db.session() as s:
            members.record(s, uid, name, chosen, hold)
        changed = [f"+{labels.get(r, 'type role')}" for r in add] + [f"-{labels.get(r, 'type role')}" for r in remove]
        changed += [f"+{labels[r]} (paused for quiet hours)" for r in hold if r not in held and r in labels]
        if changed:
            bus.info("admin", f"Roles for {name} changed by admin: {', '.join(changed)}.")
        if errors:
            flash(request, "Some changes failed: " + "; ".join(errors), "err")
        else:
            flash(request, f"Saved roles for {name}." if changed else "No changes.")
        return back(f"/members/{uid}")

    @app.post("/members/{uid}/gear")
    async def member_gear(request: Request, uid: int):
        data = await form(request)
        raw = str(data.get("bracket", ""))
        with db.session() as s:
            name = members.detail(s, uid)["name"] or f"User {uid}"
            try:
                b = party.set_gear(s, uid, name, int(raw) if raw.isdigit() else None, int(time.time()))
            except party.PartyError as exc:
                flash(request, str(exc), "err")
                return back(f"/members/{uid}#gear")
            label = b.label if b else ""
        bus.info("admin", f"Gear score for {name} set to {label or 'none'} by admin.")
        refresh()
        flash(request, f"Gear score set to {label}." if label else "Gear score cleared.")
        return back(f"/members/{uid}#gear")

    @app.post("/members/{uid}/quiet")
    async def member_quiet(request: Request, uid: int):
        data = await form(request)
        bot = _bot_ready()
        live = await bot.fetch_member(uid) if bot else None
        try:
            with db.session() as s:
                if data.get("enabled"):
                    name = live[0] if live else (members.detail(s, uid)["name"] or f"User {uid}")
                    q = quiet.set_hours(s, uid, name, _int(data, "start", 23, 0, 23), _int(data, "end", 7, 0, 23),
                                        str(data.get("tz", "UTC")), live[1] if live else set())
                    msg = f"Quiet hours for {name}: {quiet.window_text(q)}."
                else:
                    q = quiet.turn_off(s, uid)
                    msg = "Quiet hours turned off." if q else "No quiet hours to turn off."
        except ValueError as exc:
            flash(request, str(exc), "err")
            return back(f"/members/{uid}#quiet")
        bus.info("admin", f"{msg} (admin, member {uid})")
        if bot:
            await bot.apply_quiet_hours(only=uid)
        flash(request, msg)
        return back(f"/members/{uid}#quiet")

    @app.post("/members/{uid}/timers/{tid}/cancel")
    async def member_timer_cancel(request: Request, uid: int, tid: int):
        await form(request)
        with db.session() as s:
            t = s.get(UserTimer, tid)
            if t is None or t.user_id != uid:
                raise HTTPException(404)
            result = timers.cancel(t, "admin")
        if result.ok:
            bus.info("timer", result.log)
            refresh()
        flash(request, result.reply.replace("your ", ""), "ok" if result.ok else "err")
        return back(f"/members/{uid}#activity")

    @app.post("/members/{uid}/registration/cancel")
    async def member_registration_cancel(request: Request, uid: int):
        await form(request)
        with db.session() as s:
            row = party.cancel_registration(s, uid)
            name = row.user_name if row else ""
        if row:
            bus.info("party", f"Registration of {name} cancelled by admin.")
            refresh()
        flash(request, "Registration cancelled." if row else "No active registration.")
        return back(f"/members/{uid}#activity")

    @app.post("/members/{uid}/party/remove")
    async def member_party_remove(request: Request, uid: int):
        await form(request)
        try:
            with db.session() as s:
                p = party.live_party_for(s, uid)
                if p is None:
                    raise party.PartyError("Not in a party.")
                title = party.party_title(p)
                name = next((m.user_name for m in p.members if m.user_id == uid), str(uid))
                party.remove_member(s, p, uid, int(time.time()))
        except party.PartyError as exc:
            flash(request, str(exc), "err")
            return back(f"/members/{uid}#activity")
        bus.info("party", f"{name} removed from {title} by admin.")
        refresh()
        flash(request, f"Removed from {title}. The listing updates within 30 seconds.")
        return back(f"/members/{uid}#activity")

    @app.get("/stats", response_class=HTMLResponse)
    async def stats_page(request: Request, range: str = "30"):
        require(request)
        with db.session() as s:
            data = stats.compute(s, range, int(time.time()), get_settings(s).default_tz)
        return render(request, "stats.html", st=data, ranges=stats.RANGES, days=stats.DAYS,
                      heat_level=stats.heat_level)

    @app.get("/logs", response_class=HTMLResponse)
    async def logs_page(request: Request):
        require(request)
        with db.session() as s:
            logs = list(s.scalars(select(LogEntry).order_by(LogEntry.id.desc()).limit(500)))
        return render(request, "logs.html", logs=logs)

    # -- events ---------------------------------------------------------------------

    @app.get("/events", response_class=HTMLResponse)
    async def events_page(request: Request):
        require(request)
        t = int(time.time())
        with db.session() as s:
            evs = [e for e in engine.load_events(s) if not e.overrides_event_id]
            overrides = engine._overrides(engine.load_events(s))
            rows = []
            for e in evs:
                nxt = engine.next_occurrence(e, t, overrides.get(e.id))
                rows.append(dict(e=e, describe=_describe(e), next=nxt.at if nxt else None,
                                 slots={sl.lead_minutes: sl for sl in e.slots}))
        return render(request, "events.html", rows=rows)

    def _event_ctx(s, e: Event | None, error: str = "", values: dict | None = None):
        cats = list(s.scalars(select(Category).order_by(Category.sort_order)))
        preview = []
        if e is not None and e.id and e.schedule_type != "kill_report":
            t = int(time.time())
            preview = [o.at for o in engine.event_occurrences(e, t, t + 40 * 86400)[:6]]
        defaults = {lead: engine.default_template(e or Event(schedule_type="hourly"), lead) for lead in (15, 5, 0)}
        return dict(e=e, cats=cats, types=schedules.REGISTRY, error=error, preview=preview,
                    template_fields=TEMPLATE_FIELDS, default_templates=defaults, values=values or {},
                    tz_default=get_settings(s).default_tz)

    @app.get("/events/new", response_class=HTMLResponse)
    async def event_new(request: Request):
        require(request)
        with db.session() as s:
            ctx = _event_ctx(s, None)
        return render(request, "event_edit.html", **ctx)

    @app.get("/events/{event_id}", response_class=HTMLResponse)
    async def event_edit(request: Request, event_id: int):
        require(request)
        with db.session() as s:
            e = s.get(Event, event_id)
            if e is None or e.overrides_event_id:
                raise HTTPException(404)
            _ = e.slots, e.category
            ctx = _event_ctx(s, e)
        return render(request, "event_edit.html", **ctx)

    @app.post("/events/new")
    async def event_create(request: Request):
        return await _save_event(request, None)

    @app.post("/events/{event_id}")
    async def event_save(request: Request, event_id: int):
        return await _save_event(request, event_id)

    async def _save_event(request: Request, event_id: int | None):
        data = await form(request)
        with db.session() as s:
            e = s.get(Event, event_id) if event_id else Event(slots=make_slots(NONE), source="manual")
            if e is None:
                raise HTTPException(404)
            try:
                changes = _apply_event_form(s, e, data)
            except ValueError as exc:
                s.rollback()
                e = s.get(Event, event_id) if event_id else None
                if e is not None:
                    _ = e.slots, e.category
                ctx = _event_ctx(s, e, str(exc), dict(data))
                return render(request, "event_edit.html", **ctx)
            if not event_id:
                s.add(e)
            s.flush()
            name, new_id = e.name, e.id
        bus.info("config", f"Event {name} {'updated' if event_id else 'created'}" + (f": {', '.join(changes)}." if changes else "."))
        refresh()
        flash(request, f"Saved {name}.")
        return back(f"/events/{new_id}")

    @app.post("/events/{event_id}/toggle")
    async def event_toggle(request: Request, event_id: int):
        await form(request)
        with db.session() as s:
            e = s.get(Event, event_id) or _404()
            e.enabled = not e.enabled
            name, on = e.name, e.enabled
        bus.info("config", f"Event {name} {'enabled' if on else 'disabled'}.")
        refresh()
        return back("/events")

    @app.post("/events/{event_id}/delete")
    async def event_delete(request: Request, event_id: int):
        await form(request)
        with db.session() as s:
            e = s.get(Event, event_id) or _404()
            name = e.name
            s.delete(e)
        bus.info("config", f"Event {name} deleted.")
        refresh()
        flash(request, f"Deleted {name}.")
        return back("/events")

    # -- categories -------------------------------------------------------------------

    @app.get("/categories", response_class=HTMLResponse)
    async def categories_page(request: Request):
        require(request)
        with db.session() as s:
            cats = list(s.scalars(select(Category).order_by(Category.sort_order, Category.id)))
            counts = dict(s.execute(select(Event.category_id, func.count()).group_by(Event.category_id)).all())
            state = get_state(s)
            groups = reaction_roles.groups(s)
            emoji_of = {(e.kind, e.ref_id): e.emoji for e in reaction_roles.entries(s)}
            default_group = {g.default_for: g.id for g in groups if g.default_for}
            assign = [("category", c.id, c.name, c.role_id, c.emoji, c.group_id or default_group.get("category"))
                      for c in cats]
            assign += [("content", c.id, c.name, c.role_id, c.react_emoji, c.group_id or default_group.get("content"))
                       for c in party.contents(s, False)]
            previews = [text for text, _ in reaction_roles.messages(s)]
            ctx = dict(cats=cats, counts=counts, groups=groups, assign=assign, emoji_of=emoji_of,
                       roles_header=get_settings(s).roles_header,
                       previews=previews, class_preview=party.CLASS_PANEL, clear_preview=reaction_roles.CLEAR_TEXT,
                       gear_preview=party.GEAR_PANEL, gear_labels=[b.label for b in party.brackets(s)],
                       posted=bool(state.reaction_messages or state.reaction_message_id))
        return render(request, "categories.html", **ctx)

    @app.post("/categories")
    async def category_add(request: Request):
        data = await form(request)
        name = str(data.get("name", "")).strip()
        if not name:
            flash(request, "Name is required.", "err")
            return back("/categories")
        with db.session() as s:
            if s.scalar(select(Category).where(Category.name == name)):
                flash(request, f"{name} already exists.", "err")
                return back("/categories")
            top = s.scalar(select(func.max(Category.sort_order))) or 0
            s.add(Category(name=name, role_id=_snowflake(data.get("role_id")), sort_order=top + 1))
        bus.info("config", f"Category {name} added.")
        flash(request, f"Added {name}.")
        return back("/categories")

    @app.post("/roles/header")
    async def roles_header_save(request: Request):
        data = await form(request)
        text = str(data.get("roles_header", "")).replace("\r\n", "\n").strip()
        if len(text) > 500:
            flash(request, "Keep the header under 500 characters.", "err")
            return back("/categories#header")
        with db.session() as s:
            get_settings(s).roles_header = text
        bus.info("config", "Roles channel header saved.")
        flash(request, "Header saved. Repost the roles channel to apply.")
        return back("/categories#header")

    @app.post("/roles/groups")
    async def roles_groups_save(request: Request):
        data = await form(request)
        try:
            with db.session() as s:
                rows = reaction_roles.groups(s)
                names = []
                for g in rows:
                    name = str(data.get(f"gname_{g.id}", "")).strip()[:64]
                    if not name:
                        raise ValueError("Every role message needs a title")
                    names.append(name.lower())
                    g.name = name
                    g.description = str(data.get(f"gdesc_{g.id}", "")).strip()[:200]
                    g.sort_order = _int(data, f"gorder_{g.id}", g.sort_order, -1000, 1000)
                if len(names) != len(set(names)):
                    raise ValueError("Two role messages have the same title")
        except ValueError as exc:
            flash(request, f"Nothing saved: {exc}.", "err")
            return back("/categories#messages")
        bus.info("config", "Role messages saved.")
        flash(request, "Role messages saved. Repost the roles channel to apply.")
        return back("/categories#messages")

    @app.post("/roles/groups/new")
    async def roles_group_add(request: Request):
        data = await form(request)
        name = str(data.get("name", "")).strip()[:64]
        if not name:
            flash(request, "Give the role message a title.", "err")
            return back("/categories#messages")
        with db.session() as s:
            if s.scalar(select(ReactionGroup).where(ReactionGroup.name == name)):
                flash(request, f"{name} already exists.", "err")
                return back("/categories#messages")
            top = s.scalar(select(func.max(ReactionGroup.sort_order))) or 0
            s.add(ReactionGroup(name=name, description=str(data.get("description", "")).strip()[:200],
                                sort_order=top + 1))
        bus.info("config", f"Role message {name} added.")
        flash(request, f"Added {name}. Assign roles to it below.")
        return back("/categories#messages")

    @app.post("/roles/groups/{gid}/delete")
    async def roles_group_delete(request: Request, gid: int):
        await form(request)
        with db.session() as s:
            g = s.get(ReactionGroup, gid) or _404()
            if g.default_for:
                flash(request, f"{g.name} is a default role message and can't be removed. Rename it instead.", "err")
                return back("/categories#messages")
            name = g.name
            s.delete(g)  # its roles fall back to the default message
        bus.info("config", f"Role message {name} removed.")
        return back("/categories#messages")

    @app.post("/roles/assign")
    async def roles_assign(request: Request):
        data = await form(request)
        with db.session() as s:
            valid = {g.id for g in reaction_roles.groups(s)}
            changed = 0
            for model, kind, attr in ((Category, "category", "emoji"), (PfContent, "content", "react_emoji")):
                for row in s.scalars(select(model)):
                    gid = data.get(f"group_{kind}_{row.id}")
                    gid = int(gid) if gid and str(gid).isdigit() and int(gid) in valid else row.group_id
                    emoji = str(data.get(f"emoji_{kind}_{row.id}", getattr(row, attr) or "")).strip()[:64]
                    if (row.group_id, getattr(row, attr)) != (gid, emoji):
                        row.group_id = gid
                        setattr(row, attr, emoji)
                        changed += 1
        bus.info("config", f"Roles channel assignments saved ({changed} changed).")
        flash(request, f"Saved {changed} change{'s' if changed != 1 else ''}. Repost the roles channel to apply."
              if changed else "No changes.")
        return back("/categories#assign")

    # Registered before /categories/{cat_id}, which would otherwise swallow it.
    @app.post("/categories/repost-roles")
    async def repost_roles(request: Request):
        await form(request)
        bot = app.state.bot
        if not bot or not bot.is_ready():
            flash(request, "Bot is not connected.", "err")
            return back("/categories")

        async def _run():
            try:
                await bot.post_roles_message()
            except Exception as exc:
                bus.error("roles", f"Reaction roles repost failed: {exc!r}")

        asyncio.create_task(_run())
        flash(request, "Reposting the roles channel. Reactions are added about 1.5 s apart.")
        return back("/categories")

    @app.post("/categories/{cat_id}")
    async def category_save(request: Request, cat_id: int):
        data = await form(request)
        try:
            role_id = _snowflake(data.get("role_id"))
        except ValueError as exc:
            flash(request, str(exc), "err")
            return back("/categories")
        with db.session() as s:
            c = s.get(Category, cat_id) or _404()
            c.name = str(data.get("name", c.name)).strip() or c.name
            c.role_id = role_id
            name = c.name
        bus.info("config", f"Category {name} saved (role {role_id or 'none'}).")
        refresh()
        flash(request, f"Saved {name}. Repost the roles message to apply.")
        return back("/categories")

    @app.post("/categories/{cat_id}/move")
    async def category_move(request: Request, cat_id: int):
        data = await form(request)
        step = -1 if data.get("dir") == "up" else 1
        with db.session() as s:
            cats = list(s.scalars(select(Category).order_by(Category.sort_order, Category.id)))
            idx = next(i for i, c in enumerate(cats) if c.id == cat_id)
            j = idx + step
            if 0 <= j < len(cats):
                cats[idx], cats[j] = cats[j], cats[idx]
            for i, c in enumerate(cats):
                c.sort_order = i
        return back("/categories")

    @app.post("/categories/{cat_id}/delete")
    async def category_delete(request: Request, cat_id: int):
        await form(request)
        with db.session() as s:
            c = s.get(Category, cat_id) or _404()
            if s.scalar(select(func.count()).select_from(Event).where(Event.category_id == cat_id)):
                flash(request, f"{c.name} still has events. Move them first.", "err")
                return back("/categories")
            name = c.name
            s.delete(c)
        bus.info("config", f"Category {name} deleted.")
        return back("/categories")

    # -- user timers ------------------------------------------------------------------

    @app.get("/timers", response_class=HTMLResponse)
    async def timers_page(request: Request):
        require(request)
        with db.session() as s:
            active = list(s.scalars(select(UserTimer).where(UserTimer.status == "active").order_by(UserTimer.fire_at)))
            recent = list(s.scalars(select(UserTimer).where(UserTimer.status != "active")
                                    .order_by(UserTimer.id.desc()).limit(30)))
            for t in active + recent:
                _ = t.event
            bosses = timers.boss_events(s)
            tz = get_settings(s).default_tz
        return render(request, "timers.html", active=active, recent=recent, bosses=bosses, tz=tz)

    @app.post("/timers/{timer_id}/cancel")
    async def timer_cancel(request: Request, timer_id: int):
        await form(request)
        with db.session() as s:
            t = s.get(UserTimer, timer_id) or _404()
            result = timers.cancel(t, "admin")
        if result.ok:
            bus.info("timer", result.log)
            refresh()
        flash(request, result.reply.replace("your ", ""), "ok" if result.ok else "err")
        return back("/timers")

    # -- maintenance ------------------------------------------------------------------

    def _maint(s) -> Event | None:
        return s.scalar(
            select(Event).join(Category).where(Category.name == "Maintenance", Event.overrides_event_id.is_(None),
                                               Event.schedule_type == "weekly").limit(1)
        )

    @app.get("/maintenance", response_class=HTMLResponse)
    async def maintenance_page(request: Request):
        require(request)
        t = int(time.time())
        with db.session() as s:
            m = _maint(s)
            settings = get_settings(s)
            if m is None:
                return render(request, "maintenance.html", m=None)
            overrides = list(s.scalars(select(Event).where(Event.overrides_event_id == m.id).order_by(Event.id)))
            by_replaces = {int(o.schedule_params.get("replaces", 0)): o for o in overrides}
            weeks = []
            for at in schedules.occurrences_between("weekly", m.schedule_params, t - 86400, t + 42 * 86400)[:6]:
                weeks.append(dict(at=at, end=at + m.duration_minutes * 60, override=by_replaces.get(at)))
            extra = [o for o in overrides if not int(o.schedule_params.get("replaces", 0))]
            start_h, start_m = schedules.parse_hhmm(m.schedule_params["time"])
            end_total = (start_h * 60 + start_m + m.duration_minutes) % 1440
            ctx = dict(m=m, weeks=weeks, extra=extra, end_time="%02d:%02d" % divmod(end_total, 60),
                       tz=settings.default_tz, ot=schedules.get("one_off"))
        return render(request, "maintenance.html", **ctx)

    @app.post("/maintenance/regular")
    async def maintenance_regular(request: Request):
        data = await form(request)
        try:
            params = schedules.get("weekly").validate(
                {"days": [str(data.get("day", "wed"))], "time": data.get("start", ""), "tz": "UTC"})
            sh, sm = schedules.parse_hhmm(params["time"])
            eh, em = schedules.parse_hhmm(str(data.get("end", "")))
        except schedules.ScheduleError as exc:
            flash(request, str(exc), "err")
            return back("/maintenance")
        duration = (eh * 60 + em - sh * 60 - sm) % 1440 or 1440
        with db.session() as s:
            m = _maint(s) or _404()
            m.schedule_params, m.duration_minutes = params, duration
        bus.info("config", f"Regular maintenance set to {params['days'][0].title()} {params['time']} UTC for {duration} min.")
        refresh()
        flash(request, "Regular maintenance window saved.")
        return back("/maintenance")

    @app.post("/maintenance/override")
    async def maintenance_override(request: Request):
        data = await form(request)
        replaces = int(data.get("replaces") or 0)
        action = str(data.get("action", "move"))
        tz = str(data.get("tz") or "UTC")
        with db.session() as s:
            m = _maint(s) or _404()
            for old in s.scalars(select(Event).where(Event.overrides_event_id == m.id)):
                if replaces and int(old.schedule_params.get("replaces", 0)) == replaces:
                    s.delete(old)
            if action == "cancel":
                params = {"at": datetime.fromtimestamp(replaces, timezone.utc).strftime("%Y-%m-%d %H:%M"),
                          "tz": "UTC", "_ts": replaces, "replaces": replaces, "cancelled": True}
                duration = 0
                desc = f"cancelled the week of {fmt_ts(replaces)}"
            else:
                try:
                    p = schedules.get("one_off").validate({"at": data.get("start", ""), "tz": tz})
                    end = schedules.get("one_off").validate({"at": data.get("end", ""), "tz": tz})["_ts"]
                except schedules.ScheduleError as exc:
                    flash(request, str(exc), "err")
                    return back("/maintenance")
                if end <= p["_ts"]:
                    flash(request, "End must be after start.", "err")
                    return back("/maintenance")
                params = {**p, "replaces": replaces}
                duration = (end - p["_ts"]) // 60
                desc = f"{'emergency window' if not replaces else 'override'} {fmt_ts(p['_ts'])} for {duration} min"
            s.add(Event(name=m.name, category_id=m.category_id, schedule_type="one_off", schedule_params=params,
                        duration_minutes=duration, overrides_event_id=m.id, enabled=True, dashboard="hidden",
                        source="manual"))
        bus.info("config", f"Maintenance: {desc}.")
        refresh()
        flash(request, "Maintenance override saved.")
        return back("/maintenance")

    @app.post("/maintenance/override/{event_id}/delete")
    async def maintenance_override_delete(request: Request, event_id: int):
        await form(request)
        with db.session() as s:
            o = s.get(Event, event_id)
            if o is None or not o.overrides_event_id:
                raise HTTPException(404)
            s.delete(o)
        bus.info("config", "Maintenance override removed; that week uses the regular window again.")
        refresh()
        return back("/maintenance")

    # -- settings -----------------------------------------------------------------------

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request):
        require(request)
        with db.session() as s:
            settings = get_settings(s)
            counts = transfer.summary(s)
        return render(request, "settings.html", st=settings, export_counts=counts)

    @app.post("/settings")
    async def settings_save(request: Request):
        data = await form(request)
        try:
            with db.session() as s:
                st = get_settings(s)
                for key in ("timer_channel_id", "roles_channel_id", "log_channel_id", "party_channel_id"):
                    setattr(st, key, _snowflake(data.get(key)))
                for key, lo, hi in (("user_timer_limit", 1, 100), ("report_offset_min", 0, 120),
                                    ("keep_occurrences", 1, 50), ("dashboard_max_oneoffs", 1, 60),
                                    ("party_lifetime_min", 5, 720), ("party_warn_min", 1, 120),
                                    ("party_extend_min", 5, 720), ("party_max_extensions", 0, 10),
                                    ("party_status_hold_min", 5, 1440), ("party_ping_gap_min", 0, 240)):
                    val = int(str(data.get(key, "")).strip() or "x")
                    if not lo <= val <= hi:
                        raise ValueError(f"{key.replace('_', ' ')} must be {lo}-{hi}")
                    setattr(st, key, val)
                for key in ("time_style_near", "time_style_far"):
                    if data.get(key) in TS_STYLES:
                        setattr(st, key, data.get(key))
                tz = str(data.get("default_tz", "")).strip()
                if tz not in available_timezones():
                    raise ValueError(f"Unknown time zone {tz!r}")
                st.default_tz = tz
                st.dashboard_divider = str(data.get("dashboard_divider", "")).strip()[:100]
                st.dashboard_title = str(data.get("dashboard_title", "")).strip()[:100]
                st.dashboard_top_label = str(data.get("dashboard_top_label", "")).strip()[:64]
                st.dashboard_main_label = str(data.get("dashboard_main_label", "")).strip()[:64]
                st.oneoff_label = str(data.get("oneoff_label", "")).strip()[:64]
                st.dashboard_footer = str(data.get("dashboard_footer", "")).strip()[:200]
                st.dashboard_commands_label = str(data.get("dashboard_commands_label", "")).strip()[:200]
                st.shugo_url = str(data.get("shugo_url", "")).strip() or st.shugo_url
                st.shugo_region = str(data.get("shugo_region", "")).strip() or st.shugo_region
        except ValueError as exc:
            flash(request, str(exc).replace("invalid literal for int() with base 10: 'x'", "Numbers are required"), "err")
            return back("/settings")
        bus.info("config", "Settings updated.")
        refresh()
        flash(request, "Settings saved.")
        return back("/settings")

    @app.post("/settings/password")
    async def settings_password(request: Request):
        data = await form(request)
        new, confirm = str(data.get("new", "")), str(data.get("confirm", ""))
        with db.session() as s:
            st = get_settings(s)
            if not bcrypt.checkpw(str(data.get("current", "")).encode(), st.admin_password_hash.encode()):
                flash(request, "Current password is wrong.", "err")
                return back("/settings")
            if len(new) < 10 or new != confirm:
                flash(request, "New password must be at least 10 characters and match the confirmation.", "err")
                return back("/settings")
            st.admin_password_hash = hash_password(new)
        bus.info("admin", "Admin password changed.")
        flash(request, "Password changed.")
        return back("/settings")

    # -- defaults import ------------------------------------------------------------------

    @app.get("/defaults", response_class=HTMLResponse)
    async def defaults_page(request: Request):
        require(request)
        with db.session() as s:
            state, settings = get_state(s), get_settings(s)
            ctx = dict(pending=state.pending_import, last=state.last_import_at, error=state.last_import_error,
                       settings=settings)
        return render(request, "defaults.html", **ctx)

    @app.post("/defaults/check")
    async def defaults_check(request: Request):
        await form(request)
        error = await run_import_check()
        flash(request, f"Check failed, last good values kept: {error}" if error else "Checked shugo.gg.",
              "err" if error else "ok")
        return back("/defaults")

    @app.post("/defaults/apply")
    async def defaults_apply(request: Request):
        data = await form(request)
        keys = set(data.getlist("change"))
        with db.session() as s:
            state = get_state(s)
            pending = state.pending_import or {}
            applied = shugo.apply(s, pending, keys)
            remaining = [c for c in pending.get("changes", []) if c["key"] not in keys]
            state.pending_import = {**pending, "changes": remaining} if remaining else None
        if applied:
            bus.info("defaults", f"Applied from shugo.gg: {', '.join(applied)}.")
            refresh()
        flash(request, f"Applied {len(applied)} change(s)." if applied else "Nothing applied.")
        return back("/defaults")

    @app.post("/defaults/dismiss")
    async def defaults_dismiss(request: Request):
        await form(request)
        with db.session() as s:
            get_state(s).pending_import = None
        flash(request, "Review dismissed.")
        return back("/defaults")

    # -- settings export / import ---------------------------------------------------

    @app.get("/settings/export")
    async def settings_export(request: Request):
        require(request)
        import json
        from fastapi.responses import Response

        with db.session() as s:
            data = transfer.export(s)
        bus.info("admin", "Settings exported.", discord=False)
        return Response(json.dumps(data, indent=2, ensure_ascii=False), media_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="{transfer.filename()}"'})

    @app.post("/settings/import", response_class=HTMLResponse)
    async def settings_import_preview(request: Request):
        data = await form(request)
        import json

        upload = data.get("file")
        raw = ""
        if upload is not None and hasattr(upload, "read"):
            raw = (await upload.read()).decode("utf-8-sig", errors="replace")
        raw = raw or str(data.get("text", ""))
        if not raw.strip():
            flash(request, "Choose a settings file or paste its contents.", "err")
            return back("/settings")
        if len(raw) > 2_000_000:
            flash(request, "That file is too large to be a Nyerk settings file.", "err")
            return back("/settings")
        try:
            parsed = json.loads(raw)
            with db.session() as s:
                changes = transfer.apply_import(s, parsed)
                s.rollback()  # preview only
        except (json.JSONDecodeError, transfer.ImportError_) as exc:
            flash(request, f"Can't import: {exc}", "err")
            return back("/settings")
        return render(request, "import_preview.html", changes=changes, raw=raw)

    @app.post("/settings/import/apply")
    async def settings_import_apply(request: Request):
        data = await form(request)
        import json

        try:
            with db.session() as s:
                changes = transfer.apply_import(s, json.loads(str(data.get("raw", ""))))
        except (json.JSONDecodeError, transfer.ImportError_) as exc:
            flash(request, f"Can't import: {exc}", "err")
            return back("/settings")
        bus.info("config", f"Settings imported: {len(changes)} change(s).")
        refresh()
        flash(request, f"Imported {len(changes)} change(s). Repost panels or the roles message if role IDs changed.")
        return back("/settings")

    # -- party finder ----------------------------------------------------------------

    PF_KINDS = {
        "classes": (PfClass, (("name", "str"), ("emoji", "str"), ("role_id", "id"), ("class_type", "type"),
                              ("sort_order", "int"))),
        "brackets": (PfBracket, (("label", "str"), ("min_score", "int"), ("max_score", "int"), ("sort_order", "int"))),
        "windows": (PfWindow, (("minutes", "int"), ("label", "str"))),
        "content": (PfContent, (("name", "str"), ("emoji", "str"), ("role_id", "id"),
                                ("min_gear_score", "int"), ("active", "bool"), ("sort_order", "int"))),
    }

    def _pf_values(kind: str, data) -> dict:
        model, fields = PF_KINDS[kind]
        out = {}
        for key, typ in fields:
            raw = str(data.get(key, "")).strip()
            if typ == "bool":
                out[key] = bool(data.get(key))
            elif typ == "id":
                out[key] = _snowflake(raw)
            elif typ == "int":
                out[key] = _int(data, key, 0, 0 if key != "sort_order" else -1000, 100000)
            elif typ == "type":
                if raw not in party.TYPES:
                    raise ValueError("Type must be tank, support or dps")
                out[key] = raw
            else:
                out[key] = raw[:80]
        first = fields[0][0]
        if not out[first]:
            raise ValueError(f"{first.replace('_', ' ').capitalize()} is required")
        return out

    @app.get("/party", response_class=HTMLResponse)
    async def party_page(request: Request):
        require(request)
        t = int(time.time())
        with db.session() as s:
            settings, state = get_settings(s), get_state(s)
            parties = party.live_parties(s)
            interests = list(s.scalars(select(PfInterest).where(PfInterest.status == "active",
                                                               PfInterest.expires_at > t)
                                       .order_by(PfInterest.expires_at)))
            for r in interests:
                _ = r.pf_class, r.bracket
            quiet_rows = []
            for q in s.scalars(select(QuietHours).where(QuietHours.enabled.is_(True)).order_by(QuietHours.user_name)):
                paused = s.scalar(select(func.count()).select_from(RoleHolder).where(
                    RoleHolder.user_id == q.user_id, RoleHolder.suspended.is_(True))) or 0
                quiet_rows.append((q, quiet.window_text(q), paused))
            ctx = dict(st=settings, state=state, classes=party.classes(s), brackets=party.brackets(s),
                       quiet_rows=quiet_rows,
                       windows=party.windows(s), contents=party.contents(s, False), parties=parties,
                       interests=interests, content_names={c.id: c.name for c in party.contents(s, False)},
                       TYPE_LABEL=party.TYPE_LABEL)
        return render(request, "party.html", **ctx)

    @app.post("/party/types")
    async def party_types(request: Request):
        data = await form(request)
        try:
            with db.session() as s:
                st = get_settings(s)
                for key in ("tank_role_id", "support_role_id", "dps_role_id"):
                    setattr(st, key, _snowflake(data.get(key)))
                for key in ("party_default_tank", "party_default_support", "party_default_dps"):
                    setattr(st, key, _int(data, key, 0, 0, 5))
                if st.party_default_tank + st.party_default_support + st.party_default_dps > 5:
                    raise ValueError("The default makeup can't need more than 5 players")
        except ValueError as exc:
            flash(request, str(exc), "err")
            return back("/party")
        bus.info("config", "Party finder type roles and default makeup saved.")
        flash(request, "Saved. Repost the class panel if role IDs changed.")
        return back("/party")

    @app.post("/party/classes/bulk")
    async def party_classes_bulk(request: Request):
        data = await form(request)
        try:
            with db.session() as s:
                rows = party.classes(s)
                updates = {}
                for c in rows:
                    sub = {k: data.get(f"{k}_{c.id}", "") for k in ("name", "emoji", "role_id", "class_type",
                                                                    "sort_order")}
                    updates[c.id] = _pf_values("classes", sub)
                names = [u["name"].lower() for u in updates.values()]
                if len(names) != len(set(names)):
                    raise ValueError("Two classes have the same name")
                roles = [u["role_id"] for u in updates.values() if u["role_id"]]
                if len(roles) != len(set(roles)):
                    raise ValueError("Two classes use the same role ID")
                changed = []
                for c in rows:
                    for k, v in updates[c.id].items():
                        if getattr(c, k) != v:
                            setattr(c, k, v)
                            changed.append(c.name)
                changed = sorted(set(changed))
        except ValueError as exc:
            flash(request, f"Nothing saved: {exc}.", "err")
            return back("/party#classes")
        if changed:
            bus.info("config", f"Party finder classes saved: {', '.join(changed)}.")
            refresh()
            flash(request, f"Saved {len(changed)} class{'es' if len(changed) != 1 else ''}. Repost the class selector panel to update its buttons.")
        else:
            flash(request, "No changes.")
        return back("/party#classes")

    @app.post("/party/content/bulk")
    async def party_content_bulk(request: Request):
        data = await form(request)
        try:
            with db.session() as s:
                rows = party.contents(s, False)
                updates = {c.id: _pf_values("content", {k: data.get(f"{k}_{c.id}", "") for k in (
                    "name", "emoji", "role_id", "min_gear_score", "sort_order")}
                    | {"active": data.get(f"active_{c.id}")}) for c in rows}
                names = [u["name"].lower() for u in updates.values()]
                if len(names) != len(set(names)):
                    raise ValueError("Two dungeons have the same name")
                changed = sorted({c.name for c in rows for k, v in updates[c.id].items() if getattr(c, k) != v})
                for c in rows:
                    for k, v in updates[c.id].items():
                        setattr(c, k, v)
        except ValueError as exc:
            flash(request, f"Nothing saved: {exc}.", "err")
            return back("/party#content")
        if changed:
            bus.info("config", f"Party finder content saved: {', '.join(changed)}.")
            refresh()
        flash(request, f"Saved {len(changed)} dungeon{'s' if len(changed) != 1 else ''}. Repost the roles channel if roles changed."
              if changed else "No changes.")
        return back("/party#content")

    @app.post("/party/content/create-roles")
    async def party_content_create_roles(request: Request):
        data = await form(request)
        bot = app.state.bot
        if not bot or not bot.is_ready():
            flash(request, "Bot is not connected.", "err")
            return back("/party#content")
        with db.session() as s:
            only = str(data.get("content_id", "")).strip()
            ids = [c.id for c in party.contents(s, False) if not c.role_id and (not only or str(c.id) == only)]
        if not ids:
            flash(request, "Every dungeon already has a role.")
            return back("/party#content")
        try:
            made = await bot.create_content_roles(ids)
        except Exception as exc:
            bus.error("roles", f"Creating dungeon roles failed: {exc!r}")
            flash(request, f"Discord refused: {exc}. Nyerk needs the Manage Roles permission.", "err")
            return back("/party#content")
        flash(request, f"Created {len(made)} role{'s' if len(made) != 1 else ''}: {', '.join(made)}. "
                       "Post the roles message on the Roles page to offer them as reaction roles.")
        return back("/party#content")

    @app.post("/party/{kind}/new")
    async def party_add(request: Request, kind: str):
        data = await form(request)
        if kind not in PF_KINDS:
            raise HTTPException(404)
        model = PF_KINDS[kind][0]
        try:
            values = _pf_values(kind, data)
            with db.session() as s:
                s.add(model(**values))
        except ValueError as exc:
            flash(request, str(exc), "err")
            return back(f"/party#{kind}")
        except Exception:
            flash(request, "That already exists.", "err")
            return back(f"/party#{kind}")
        bus.info("config", f"Party finder {kind}: added {next(iter(values.values()))}.")
        refresh()
        return back(f"/party#{kind}")

    @app.post("/party/{kind}/{row_id}")
    async def party_save(request: Request, kind: str, row_id: int):
        data = await form(request)
        if kind not in PF_KINDS:
            raise HTTPException(404)
        model = PF_KINDS[kind][0]
        try:
            values = _pf_values(kind, data)
            with db.session() as s:
                row = s.get(model, row_id) or _404()
                for k, v in values.items():
                    setattr(row, k, v)
        except ValueError as exc:
            flash(request, str(exc), "err")
            return back(f"/party#{kind}")
        except HTTPException:
            raise
        except Exception:
            flash(request, "That name is already used.", "err")
            return back(f"/party#{kind}")
        bus.info("config", f"Party finder {kind}: saved {next(iter(values.values()))}.")
        refresh()
        flash(request, "Saved." + (" Repost the class panel to update its buttons." if kind == "classes" else ""))
        return back(f"/party#{kind}")

    @app.post("/party/{kind}/{row_id}/delete")
    async def party_delete(request: Request, kind: str, row_id: int):
        await form(request)
        if kind not in PF_KINDS:
            raise HTTPException(404)
        with db.session() as s:
            row = s.get(PF_KINDS[kind][0], row_id) or _404()
            s.delete(row)
        bus.info("config", f"Party finder {kind}: removed #{row_id}.")
        refresh()
        return back(f"/party#{kind}")

    @app.post("/party-panels")
    async def party_panels(request: Request):
        data = await form(request)
        which = str(data.get("which", "all"))
        bot = app.state.bot
        if which not in ("all", "class", "gear", "interest", "create", "status"):
            raise HTTPException(400)
        if not bot or not bot.is_ready():
            flash(request, "Bot is not connected.", "err")
            return back("/party")

        async def _run():
            try:
                await bot.pf.post_panels(which)
            except Exception as exc:
                bus.error("party", f"Panel post failed: {exc!r}")

        asyncio.create_task(_run())
        flash(request, "Posting panels. Check #aion-2-party-finder in a moment.")
        return back("/party")

    @app.post("/party-parties/{pid}/close")
    async def party_close(request: Request, pid: int):
        await form(request)
        with db.session() as s:
            p = s.get(PfParty, pid) or _404()
            if p.status in party.LIVE:
                party.end(p, "closed", int(time.time()))
                title = party.party_title(p)
            else:
                title = ""
        if title:
            bus.info("party", f"Party #{pid} ({title}) closed by admin.")
            refresh()
        return back("/party")

    @app.post("/party-quiet/{qid}/off")
    async def party_quiet_off(request: Request, qid: int):
        await form(request)
        with db.session() as s:
            q = s.get(QuietHours, qid) or _404()
            q.enabled = False
            name = q.user_name
        bus.info("roles", f"Quiet hours for {name} turned off by admin.")
        refresh()  # the next tick gives paused roles back
        flash(request, f"Turned off quiet hours for {name}. Paused roles come back within 30 seconds.")
        return back("/party#quiet")

    @app.post("/party-interest/{rid}/cancel")
    async def party_interest_cancel(request: Request, rid: int):
        await form(request)
        with db.session() as s:
            r = s.get(PfInterest, rid) or _404()
            name = r.user_name
            if r.status == "active":
                r.status = "cancelled"
        bus.info("party", f"Registration of {name} cancelled by admin.")
        refresh()
        return back("/party")

    return app


# ------------------------------------------------------------------------------------------


def _404():
    raise HTTPException(404)


def _describe(e: Event) -> str:
    try:
        return schedules.get(e.schedule_type).describe(e.schedule_params)
    except (schedules.ScheduleError, KeyError):
        return e.schedule_type


def _snowflake(raw) -> int:
    raw = str(raw or "").strip()
    if not raw:
        return 0
    m = re.fullmatch(r"<?[@#&]*(\d{15,21})>?", raw)
    if not m:
        raise ValueError(f"{raw!r} is not a Discord ID")
    return int(m[1])


def _int(data, key: str, default: int = 0, lo: int = 0, hi: int = 10**6) -> int:
    raw = str(data.get(key, "")).strip()
    if not raw:
        return default
    try:
        val = int(raw)
    except ValueError:
        raise ValueError(f"{key.replace('_', ' ')} must be a whole number")
    if not lo <= val <= hi:
        raise ValueError(f"{key.replace('_', ' ')} must be between {lo} and {hi}")
    return val


def _apply_event_form(s, e: Event, data) -> list[str]:
    """Copy form values onto e. Raises ValueError with a readable message."""
    name = str(data.get("name", "")).strip()
    if not name:
        raise ValueError("Name is required.")
    stype = str(data.get("schedule_type", ""))
    st = schedules.get(stype) if stype in schedules.REGISTRY else None
    if st is None:
        raise ValueError("Pick a schedule type.")
    raw = {}
    for f in st.fields:
        key = f"p_{stype}_{f.name}"
        raw[f.name] = data.getlist(key) if f.kind == "days" else str(data.get(key, "")).strip()
    try:
        params = st.validate(raw)
    except (schedules.ScheduleError, ValueError) as exc:
        raise ValueError(str(exc))

    duration = _int(data, "duration_minutes", 0, 0, 7 * 1440)
    respawn = _int(data, "respawn_minutes", 0, 0, 30 * 1440)
    if stype == "kill_report" and respawn <= 0:
        raise ValueError("World bosses need a respawn time in minutes.")

    changes = []
    if (e.schedule_type, e.schedule_params) != (stype, params):
        changes.append("schedule")
    if e.duration_minutes != duration:
        changes.append("duration")
    edited = set(e.edited_fields or [])
    if e.id:
        if "schedule" in changes:
            edited.add("schedule")
        if "duration" in changes:
            edited.add("duration_minutes")
    for f in ("schedule", "duration_minutes"):
        if data.get(f"unlock_{f}"):
            edited.discard(f)

    cat_id = _int(data, "category_id", 0)
    e.name = name[:100]
    e.category_id = cat_id or None
    e.location = str(data.get("location", "")).strip()[:100]
    e.schedule_type, e.schedule_params = stype, params
    e.duration_minutes, e.respawn_minutes = duration, respawn
    e.enabled = bool(data.get("enabled"))
    e.dashboard = data.get("dashboard") if data.get("dashboard") in ("top", "main", "hidden") else "main"
    e.dashboard_label = str(data.get("dashboard_label", "")).strip()[:100]
    e.dashboard_label_active = str(data.get("dashboard_label_active", "")).strip()[:100]
    e.sort_order = _int(data, "sort_order", e.sort_order or 0, -1000, 1000)
    e.edited_fields = sorted(edited)

    # Alert slots. Existing ones by lead; optional new slot; optional removals.
    by_lead = {sl.lead_minutes: sl for sl in e.slots}
    remove = {int(x) for x in data.getlist("slot_remove")}
    for lead, sl in list(by_lead.items()):
        if lead in remove:
            e.slots.remove(sl)
            continue
        sl.enabled = bool(data.get(f"slot_{lead}_enabled"))
        sl.ping = bool(data.get(f"slot_{lead}_ping"))
        sl.template = str(data.get(f"slot_{lead}_template", "")).replace("\r\n", "\n").strip()[:1500]
    new_lead = str(data.get("new_slot_lead", "")).strip()
    if new_lead:
        lead = _int(data, "new_slot_lead", 0, 0, 7 * 1440)
        if lead in by_lead and lead not in remove:
            raise ValueError(f"A {lead} minute slot already exists.")
        e.slots.append(AlertSlot(lead_minutes=lead, enabled=True, ping=False))
    return changes


def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()


def _persistent_secret(cfg) -> str:
    path = cfg.data_dir / "session.key"
    if path.exists():
        return path.read_text().strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_urlsafe(48)
    path.write_text(key)
    path.chmod(0o600)
    return key
