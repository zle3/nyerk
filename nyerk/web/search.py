"""Settings search for the web page: the full Search page and the live
suggestions under the search box in the top bar.

The index is built once at start from the page templates (every labelled
field and section heading) plus the pages themselves; events, role
categories, dungeons, classes and members are read from the database at
most every DATA_TTL seconds. Matching tolerates typos and knows a few
synonyms. A search is an in-memory scan of a few hundred short strings with
hard caps on the query, so it costs next to nothing; the per-user rate
limits in app.require() bound it further.
"""
from __future__ import annotations

import html
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select

from .. import party
from ..models import Category, Event, Game, MemberProfile, RoleHolder

MAX_QUERY = 80      # characters
MAX_WORDS = 8
MAX_RESULTS = 40
MAX_SUGGESTIONS = 8
MAX_WORD = 30       # longer words are cut before fuzzy matching
DATA_TTL = 30       # seconds the database part of the index is reused
MAX_MEMBERS = 5000

# Words people type -> words the pages use.
SYNONYMS = {
    "ping": ["role", "mention"], "pings": ["role", "mention"], "mention": ["ping", "role"],
    "role": ["ping"], "roles": ["ping"],
    "delete": ["retention", "cleanup", "remove"], "remove": ["delete", "cleanup"],
    "cleanup": ["delete", "retention"], "clean": ["delete", "cleanup"],
    "timezone": ["time zone"], "tz": ["time zone"],
    "dungeon": ["content"], "dungeons": ["content"], "content": ["dungeon"],
    "lfg": ["party"], "group": ["party"], "groups": ["party"],
    "text": ["wording", "label", "template", "heading"], "wording": ["template", "label"],
    "message": ["template", "wording"], "messages": ["template", "wording"],
    "dnd": ["quiet"], "mute": ["quiet"], "sleep": ["quiet"],
    "boss": ["world boss"], "bosses": ["world boss"], "wb": ["world boss"],
    "maint": ["maintenance"], "downtime": ["maintenance"],
    "admin": ["access"], "admins": ["access"], "owner": ["access"], "permission": ["access"],
    "permissions": ["access"], "lock": ["access"],
    "log": ["logs"], "history": ["change history"], "undo": ["history", "restore"],
    "backup": ["export"], "import": ["export"], "gs": ["gear score"], "gear": ["gear score"],
    "class": ["classes"], "tank": ["tank"], "healer": ["support"], "heal": ["support"],
}

# Pages, so typing a page name jumps there.
NAV = [
    ("Overview", "/", "home dashboard status"), ("Events", "/events", "schedule alerts"),
    ("Boss timers", "/timers", "world boss kills dashboard buttons"), ("Maintenance", "/maintenance", ""),
    ("Defaults", "/defaults", "shugo.gg suggestions"), ("Roles", "/categories", "ping roles reaction channel"),
    ("Party finder", "/party", "dungeons classes panels lfg"), ("Members", "/members", "people users"),
    ("Stats", "/stats", "statistics activity charts"), ("Logs", "/logs", "activity"),
    ("Settings", "/settings", "channels"), ("Access and security", "/access", "admins owner permissions"),
    ("Change history", "/history", "undo restore"), ("My page", "/me", "my roles timers"),
    ("Games", "/games", "add switch delete game aion other"),
]

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
    "games.html": ("Games", "/games"),
}

_JINJA = re.compile(r"\{%.*?%\}|\{\{.*?\}\}|\{#.*?#\}", re.S)
_TAG = re.compile(r"<[^>]+>")
_LABEL = re.compile(r'<label for="([^"]+)">(.*?)</label>(.*?)(?=<label for=|<p class="section-label"|</form>|$)', re.S)
# Owner-only even when Settings is opened to admins (see auth.NEVER_OPENED).
OWNER_ONLY = {"Admin password", "Current", "New", "Confirm"}
_HELP = re.compile(r'<div class="help">(.*?)</div>', re.S)
_WORD = re.compile(r"[a-z0-9]+")
_SECTION = re.compile(r'<p class="section-label"(?: id="([\w-]+)")?>(.*?)</p>', re.S)


@dataclass(frozen=True)
class Entry:
    page: str
    url: str
    label: str
    detail: str
    haystack: str
    owner_only: bool = False
    tokens: frozenset[str] = field(default=frozenset(), compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "tokens", frozenset(_WORD.findall(self.haystack)))


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
    for label, url, words in NAV:
        out.append(Entry("Page", url, label, "", f"{label} {words} page".lower()))
    # One result per (page, label): the same field can appear twice in a template.
    seen, unique = set(), []
    for e in out:
        if (e.page, e.label) not in seen:
            seen.add((e.page, e.label))
            unique.append(e)
    return unique


_cache: tuple[float, list[Entry]] = (0.0, [])


def _data_entries(s) -> list[Entry]:
    global _cache
    if time.monotonic() - _cache[0] < DATA_TTL:
        return _cache[1]
    out = []
    names = {g.id: g.name for g in s.scalars(select(Game))}
    many = len(names) > 1

    def page(kind, game_id):  # "Event" or "AION 2 event" when there are several games
        return f"{names.get(game_id, '?')} {kind.lower()}" if many else kind

    for g in s.scalars(select(Game).order_by(Game.sort_order, Game.id)):
        out.append(Entry("Game", f"/settings?game={g.id}#game", g.name, "", f"{g.name} game".lower()))
    for e in s.scalars(select(Event).where(Event.overrides_event_id.is_(None)).order_by(Event.name)):
        out.append(Entry(page("Event", e.game_id), f"/events/{e.id}", e.name, "",
                         f"{e.name} event {names.get(e.game_id, '')}".lower()))
    for c in s.scalars(select(Category).order_by(Category.name)):
        out.append(Entry(page("Ping role", c.game_id), f"/categories?game={c.game_id}", c.name, "",
                         f"{c.name} ping role category {names.get(c.game_id, '')}".lower()))
    for c in party.contents(s, None, False):
        out.append(Entry(page("Dungeon", c.game_id), f"/party?game={c.game_id}#content", c.name, "",
                         f"{c.name} dungeon content {names.get(c.game_id, '')}".lower()))
    for c in party.classes(s, None):
        out.append(Entry(page("Class", c.game_id), f"/party?game={c.game_id}#classes", c.name, "",
                         f"{c.name} class {names.get(c.game_id, '')}".lower()))
    people: dict[int, str] = {}
    for model in (MemberProfile, RoleHolder):
        for uid, name in s.execute(select(model.user_id, model.user_name).limit(MAX_MEMBERS)):
            if name and len(people) < MAX_MEMBERS:
                people.setdefault(uid, name)
    for uid, name in sorted(people.items(), key=lambda x: x[1].lower()):
        out.append(Entry("Member", f"/members/{uid}", name, "", f"{name} {uid} member".lower()))
    _cache = (time.monotonic(), out)
    return out


def forget_data() -> None:
    """For tests: reread the database on the next search."""
    global _cache
    _cache = (0.0, [])


def clean_query(raw: str | None) -> list[str]:
    words = " ".join(str(raw or "")[:MAX_QUERY].lower().split()).split(" ")
    return [w for w in words if w][:MAX_WORDS]


def _close(a: str, b: str, limit: int) -> bool:
    """At most `limit` typos apart: a wrong, missing or extra letter, or two
    letters swapped (optimal string alignment distance). Small strings only."""
    if abs(len(a) - len(b)) > limit:
        return False
    before, prev = None, list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            d = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
            if before is not None and j > 1 and ca == b[j - 2] and a[i - 2] == cb:
                d = min(d, before[j - 2] + 1)
            cur.append(d)
        if min(cur) > limit:
            return False
        before, prev = prev, cur
    return prev[-1] <= limit


def _word_score(word: str, e: Entry, label: str, label_tokens: set[str]) -> int | None:
    """How well one query word matches an entry: lower is better, None is no match."""
    best = None
    for alt in [word] + SYNONYMS.get(word, []):
        penalty = 0 if alt == word else 1
        if alt in label_tokens:
            score = 0
        elif any(t.startswith(alt) for t in label_tokens):
            score = 1
        elif alt in label:
            score = 2
        elif alt in e.haystack:
            score = 4
        else:
            continue
        best = score + penalty if best is None else min(best, score + penalty)
    if best is None and len(word) >= 4:
        limit = 1 if len(word) < 7 else 2
        w = word[:MAX_WORD]
        if any(_close(w, t[:MAX_WORD], limit) for t in label_tokens):
            best = 5
        elif any(_close(w, t[:MAX_WORD], limit) for t in e.tokens):
            best = 7
    return best


def search(s, index: list[Entry], words: list[str], visible, owner: bool = False,
           limit: int = MAX_RESULTS) -> list[Entry]:
    """Entries matching every word (allowing typos and synonyms), best first.
    `visible(url)` hides pages the signed-in person can't open."""
    if not words:
        return []
    scored = []
    for e in index + _data_entries(s):
        if e.owner_only and not owner:
            continue
        label = e.label.lower()
        label_tokens = set(_WORD.findall(label))
        total = 0
        for w in words:
            sc = _word_score(w, e, label, label_tokens)
            if sc is None:
                break
            total += sc
        else:
            if visible(e.url):
                kind = {"Page": 0, "Member": 3}.get(e.page, 1 if e.detail != "Section" else 2)
                scored.append((total, kind, len(label), label, e))
    scored.sort(key=lambda x: x[:4])
    return [x[-1] for x in scored[:limit]]
