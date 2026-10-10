"""Defaults import from the shugo.gg timer table.

shugo.gg has no API, so this reads the server-rendered page: an <h2> per
region ("NA East — server time America/New_York") followed by a table of
Event / Schedule / Duration / What it is. Changes are never applied
directly; they become a pending review the admin approves.
"""
from __future__ import annotations

import re
import time

import httpx
from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import schedules
from .models import Category, Event
from .seed import NONE, make_slots

USER_AGENT = "Nyerk/1.0 (AION2 Discord timer bot; home lab)"
TZ_SUFFIX = {"KST": "Asia/Seoul", "UTC": "UTC"}
DAY_NAMES = {d: d for d in schedules.DAYS}
# Schedule fields an admin edit protects from import.
GUARDED = ("schedule", "duration_minutes")


class ParseError(RuntimeError):
    pass


def _clean_name(raw: str) -> str:
    return re.sub(r"^[^\w]+", "", raw.strip()).strip()


def parse_schedule(text: str, tz: str) -> tuple[str, dict] | None:
    text = " ".join(text.split())
    if "Group" in text:
        return None  # per-matching-group listings (Korea) are ambiguous
    m = re.fullmatch(r"Every hour at :(\d{2})", text)
    if m:
        return "hourly", {"minute": int(m[1]), "tz": tz}
    m = re.fullmatch(r"(.+?) · ((?:\d{1,2}:\d{2}(?:, )?)+)(?: (\w{3}))?", text)
    if not m:
        return None
    when, times, suffix = m[1], [t.strip() for t in m[2].split(",") if t.strip()], m[3]
    if suffix:
        if suffix not in TZ_SUFFIX:
            return None
        tz = TZ_SUFFIX[suffix]
    if when == "Daily":
        return "daily", {"times": times, "tz": tz}
    days = [d.strip().lower()[:3] for d in when.split(",")]
    if all(d in DAY_NAMES for d in days) and len(times) == 1:
        return "weekly", {"days": days, "time": times[0], "tz": tz}
    return None


def parse(html: str, region: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    heading = next((h for h in soup.find_all("h2") if h.get_text(strip=True).startswith(region)), None)
    if heading is None:
        raise ParseError(f"Region heading {region!r} not found")
    m = re.search(r"server time ([A-Za-z_]+/[A-Za-z_]+|UTC)", heading.get_text())
    tz = m[1] if m else "UTC"
    table = heading.find_next("table")
    if table is None:
        raise ParseError("Timer table not found under region heading")
    out = []
    for row in table.select("tbody tr"):
        cells = [c.get_text(" ", strip=True) for c in row.find_all("td")]
        if len(cells) < 3:
            continue
        parsed = parse_schedule(cells[1], tz)
        if parsed is None:
            continue
        stype, params = parsed
        dm = re.match(r"(\d+)\s*min", cells[2])
        out.append({
            "name": _clean_name(cells[0]),
            "schedule_type": stype,
            "schedule_params": schedules.get(stype).validate(params),
            "duration_minutes": int(dm[1]) if dm else 0,
            "schedule_text": cells[1],
        })
    if not out:
        raise ParseError("No events could be parsed from the timer table")
    return out


async def fetch(url: str) -> str:
    async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.text


def _same_schedule(a_type: str, a_params: dict, b_type: str, b_params: dict, now: int) -> bool:
    """Compare by the next two weeks of instants, so 16:00 KST == 07:00 UTC."""
    end = now + 14 * 86400
    return schedules.occurrences_between(a_type, a_params, now, end) == schedules.occurrences_between(
        b_type, b_params, now, end
    )


def diff(s: Session, items: list[dict], now: int) -> list[dict]:
    events = list(s.scalars(select(Event).where(Event.overrides_event_id.is_(None))))
    by_key = {e.source_key.lower(): e for e in events if e.source_key}
    by_name = {e.name.lower(): e for e in events}
    changes = []
    for item in items:
        e = by_key.get(item["name"].lower()) or by_name.get(item["name"].lower())
        new_sched = schedules.get(item["schedule_type"]).describe(item["schedule_params"])
        if e is None:
            changes.append({
                "key": f"new:{item['name']}", "kind": "new", "event_id": None, "name": item["name"],
                "fields": {"schedule": {"old": "", "new": new_sched, "locked": False},
                           "duration_minutes": {"old": "", "new": item["duration_minutes"], "locked": False}},
                "item": item,
            })
            continue
        if e.schedule_type == "kill_report":
            continue
        fields = {}
        if not _same_schedule(e.schedule_type, e.schedule_params, item["schedule_type"], item["schedule_params"], now):
            fields["schedule"] = {
                "old": schedules.get(e.schedule_type).describe(e.schedule_params), "new": new_sched,
                "locked": "schedule" in (e.edited_fields or []),
            }
        if e.duration_minutes != item["duration_minutes"]:
            fields["duration_minutes"] = {
                "old": e.duration_minutes, "new": item["duration_minutes"],
                "locked": "duration_minutes" in (e.edited_fields or []),
            }
        if fields:
            changes.append({"key": f"event:{e.id}", "kind": "update", "event_id": e.id, "name": e.name,
                            "fields": fields, "item": item})
    return changes


def apply(s: Session, pending: dict, keys: set[str]) -> list[str]:
    """Apply selected changes. Locked (hand-edited) fields are never written."""
    applied = []
    for ch in pending.get("changes", []):
        if ch["key"] not in keys:
            continue
        item = ch["item"]
        if ch["kind"] == "new":
            cat = s.scalar(select(Category).order_by(Category.sort_order).where(Category.name == "Other"))
            if cat is None:
                cat = Category(name="Other", sort_order=99)
                s.add(cat)
                s.flush()
            s.add(Event(
                category_id=cat.id, name=item["name"], schedule_type=item["schedule_type"],
                schedule_params=item["schedule_params"], duration_minutes=item["duration_minutes"],
                slots=make_slots(NONE), enabled=False, source="shugo.gg", source_key=item["name"], sort_order=50,
            ))
            applied.append(f"added {item['name']} (disabled)")
            continue
        e = s.get(Event, ch["event_id"])
        if e is None:
            continue
        edited = set(e.edited_fields or [])
        if "schedule" in ch["fields"] and "schedule" not in edited:
            e.schedule_type, e.schedule_params = item["schedule_type"], item["schedule_params"]
            applied.append(f"{e.name} schedule")
        if "duration_minutes" in ch["fields"] and "duration_minutes" not in edited:
            e.duration_minutes = item["duration_minutes"]
            applied.append(f"{e.name} duration")
    return applied


async def check(s_factory, url: str, region: str) -> tuple[dict | None, str]:
    """Fetch and diff. Returns (pending or None, error)."""
    try:
        html = await fetch(url)
        items = parse(html, region)
    except (httpx.HTTPError, ParseError) as exc:
        return None, f"{type(exc).__name__}: {exc}"
    now = int(time.time())
    with s_factory() as s:
        changes = diff(s, items, now)
    return {"fetched_at": now, "changes": changes, "parsed": len(items)}, ""
