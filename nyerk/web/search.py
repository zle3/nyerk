"""Settings search for the web page.

The index is built once at start from the page templates (every labelled
field and section heading) plus, on each search, the names of events,
role categories, dungeons and classes. Searching is a plain in-memory scan
of a few hundred short strings with hard caps on the query, so it costs
next to nothing however often it is called; the per-user rate limits in
app.require() bound it further.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select

from .. import party
from ..models import Category, Event

MAX_QUERY = 80      # characters
MAX_WORDS = 8
MAX_RESULTS = 40

# template -> (page title, URL). Pages with one form per row (events) link
# to the list, since a field lives on every event.
PAGES = {
    "settings.html": ("Settings", "/settings"),
    "access.html": ("Access and security", "/access"),
    "party.html": ("Party finder", "/party"),
    "timers.html": ("Boss timers", "/timers"),
    "categories.html": ("Roles", "/categories"),
    "maintenance.html": ("Maintenance", "/maintenance"),
    "defaults.html": ("Defaults", "/defaults"),
    "events.html": ("Events", "/events"),
    "event_edit.html": ("Events › any event", "/events"),
}

_JINJA = re.compile(r"\{%.*?%\}|\{\{.*?\}\}|\{#.*?#\}", re.S)
_TAG = re.compile(r"<[^>]+>")
_LABEL = re.compile(r'<label for="([^"]+)">(.*?)</label>(.*?)(?=<label for=|<p class="section-label"|</form>|$)', re.S)
# Owner-only even when Settings is opened to admins (see auth.NEVER_OPENED).
OWNER_ONLY = {"Admin password", "Current", "New", "Confirm"}
_HELP = re.compile(r'<div class="help">(.*?)</div>', re.S)
_SECTION = re.compile(r'<p class="section-label"(?: id="([\w-]+)")?>(.*?)</p>', re.S)


@dataclass(frozen=True)
class Entry:
    page: str
    url: str
    label: str
    detail: str
    haystack: str
    owner_only: bool = False


def _text(fragment: str) -> str:
    return " ".join(html.unescape(_TAG.sub(" ", _JINJA.sub(" ", fragment))).split())


def build_index(template_dir: Path) -> list[Entry]:
    out: list[Entry] = []
    for name, (page, url) in PAGES.items():
        src = (template_dir / name).read_text(encoding="utf-8")
        for anchor, raw in _SECTION.findall(src):
            label = _text(raw)
            if label:
                link = f"{url}#{anchor}" if anchor and url != "/events" else url
                out.append(Entry(page, link, label, "Section", f"{label} {page}".lower(), label in OWNER_ONLY))
        for anchor, raw, rest in _LABEL.findall(src):
            label = _text(raw).replace("Owner only", "").strip()
            if not label:
                continue
            m = _HELP.search(rest[:1500])
            detail = _text(m.group(1)) if m else ""
            link = url if url == "/events" or "{" in anchor else f"{url}#{anchor}"
            out.append(Entry(page, link, label, detail, f"{label} {detail} {page}".lower(),
                             name == "settings.html" and label in OWNER_ONLY))
    # One result per (page, label): the same field can appear twice in a template.
    seen, unique = set(), []
    for e in out:
        if (e.page, e.label) not in seen:
            seen.add((e.page, e.label))
            unique.append(e)
    return unique


def _data_entries(s) -> list[Entry]:
    out = []
    for e in s.scalars(select(Event).order_by(Event.name)):
        out.append(Entry("Event", f"/events/{e.id}", e.name, "", f"{e.name} event".lower()))
    for c in s.scalars(select(Category).order_by(Category.name)):
        out.append(Entry("Ping role", "/categories", c.name, "", f"{c.name} ping role category".lower()))
    for c in party.contents(s, False):
        out.append(Entry("Dungeon", "/party#content", c.name, "", f"{c.name} dungeon content".lower()))
    for c in party.classes(s):
        out.append(Entry("Class", "/party#classes", c.name, "", f"{c.name} class".lower()))
    return out


def clean_query(raw: str | None) -> list[str]:
    words = " ".join(str(raw or "")[:MAX_QUERY].lower().split()).split(" ")
    return [w for w in words if w][:MAX_WORDS]


def search(s, index: list[Entry], words: list[str], visible, owner: bool = False) -> list[Entry]:
    """Entries whose text holds every word, best first. `visible(url)` hides
    pages the signed-in person can't open."""
    if not words:
        return []
    scored = []
    for e in _data_entries(s) + index:
        if all(w in e.haystack for w in words) and (owner or not e.owner_only) and visible(e.url):
            label = e.label.lower()
            rank = 0 if label.startswith(words[0]) else 1 if all(w in label for w in words) else 2
            scored.append((rank, e.page != "Settings", e.label.lower(), e))
    scored.sort(key=lambda x: x[:3])
    return [e for *_, e in scored[:MAX_RESULTS]]
