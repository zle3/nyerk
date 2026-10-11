"""Render Nyerk's real messages as a Discord-looking page, for screenshots.

Text comes straight from Nyerk's own functions (dashboard, alerts, panels,
listings), so the screenshots follow the code. Only the look is imitated:
Discord's dark theme, message grouping, buttons, selects, reactions and
the <t:...> timestamps rendered the way Discord shows them (en-US).
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/New_York")
GROUP_SECONDS = 7 * 60


@dataclass
class Button:
    label: str
    style: str = "secondary"   # secondary | primary | danger | success
    emoji: str = ""


@dataclass
class Select:
    placeholder: str


@dataclass
class Message:
    at: int
    content: str
    rows: list[list] = field(default_factory=list)          # rows of Button/Select
    reactions: list[tuple[str, int]] = field(default_factory=list)
    ephemeral: bool = False
    edited: bool = False


# -- timestamps --------------------------------------------------------------


def _relative(delta: float) -> str:
    """moment.js-style wording, as Discord uses."""
    s = abs(delta)
    m, h, d = s / 60, s / 3600, s / 86400
    if s < 45:
        text = "a few seconds"
    elif s < 90:
        text = "a minute"
    elif m < 45:
        text = f"{round(m)} minutes"
    elif m < 90:
        text = "an hour"
    elif h < 22:
        text = f"{round(h)} hours"
    elif h < 36:
        text = "a day"
    elif d < 26:
        text = f"{round(d)} days"
    elif d < 45:
        text = "a month"
    else:
        text = f"{round(d / 30)} months"
    return f"in {text}" if delta > 0 else f"{text} ago"


def _hm(dt: datetime) -> str:
    return dt.strftime("%I:%M %p").lstrip("0")


def stamp(ts: int, style: str, now: int) -> str:
    dt = datetime.fromtimestamp(ts, TZ)
    date = f"{dt:%B} {dt.day}, {dt.year}"
    return {
        "t": _hm(dt), "T": dt.strftime("%I:%M:%S %p").lstrip("0"),
        "d": f"{dt.month}/{dt.day}/{dt.year}", "D": date, "f": f"{date} {_hm(dt)}",
        "F": f"{dt:%A}, {date} {_hm(dt)}", "R": _relative(ts - now),
    }.get(style, f"{date} {_hm(dt)}")


def sent_label(ts: int, now: int) -> str:
    dt, today = datetime.fromtimestamp(ts, TZ), datetime.fromtimestamp(now, TZ)
    day = "Today" if dt.date() == today.date() else f"{dt.month}/{dt.day}/{dt.year}"
    return f"{day} at {_hm(dt)}"


# -- markdown ------------------------------------------------------------------


def inline(text: str, now: int, roles: dict[int, str], users: dict[int, str]) -> str:
    out = html.escape(text, quote=False)
    out = re.sub(r"&lt;t:(\d+):([tTdDfFR])&gt;",
                 lambda m: f'<span class="ts">{stamp(int(m[1]), m[2], now)}</span>', out)
    out = re.sub(r"&lt;@&amp;(\d+)&gt;", lambda m: f'<span class="mention">@{html.escape(roles.get(int(m[1]), "role"))}</span>', out)
    out = re.sub(r"&lt;@!?(\d+)&gt;", lambda m: f'<span class="mention">@{html.escape(users.get(int(m[1]), "member"))}</span>', out)
    out = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
    out = re.sub(r"\\([\\*_~`|>#\-\[\]()])", r"\1", out)  # Discord markdown escapes
    return out


def markdown(text: str, now: int, roles, users) -> str:
    blocks = []
    for line in text.split("\n"):
        if line.startswith("### "):
            blocks.append(f'<div class="h3">{inline(line[4:], now, roles, users)}</div>')
        elif line.startswith("## "):
            blocks.append(f'<div class="h2">{inline(line[3:], now, roles, users)}</div>')
        elif line.startswith("# "):
            blocks.append(f'<div class="h1">{inline(line[2:], now, roles, users)}</div>')
        elif line.startswith("-# "):
            blocks.append(f'<div class="sub">{inline(line[3:], now, roles, users)}</div>')
        elif not line.strip():
            blocks.append('<div class="gap"></div>')
        else:
            blocks.append(f'<div class="line">{inline(line, now, roles, users)}</div>')
    return "\n".join(blocks)


# -- page ----------------------------------------------------------------------

CSS = """
* { box-sizing: border-box; }
body { margin: 0; background: #313338; color: #dbdee1; width: 900px;
       font: 16px/1.375 "Noto Sans", "Helvetica Neue", Arial, "Noto Color Emoji", sans-serif; }
.bar { height: 48px; display: flex; align-items: center; gap: 8px; padding: 0 16px; border-bottom: 1px solid #1f2023;
       color: #f2f3f5; font-weight: 600; box-shadow: 0 1px 0 rgba(4,4,5,.2); }
.bar .hash { color: #80848e; font-size: 24px; font-weight: 400; }
.bar .topic { color: #949ba4; font-weight: 400; font-size: 14px; border-left: 1px solid #3f4147; padding-left: 10px; margin-left: 4px; }
.feed { padding: 16px 0 24px; }
.msg { position: relative; padding: 2px 48px 2px 72px; min-height: 22px; }
.msg.first { margin-top: 17px; }
.avatar { position: absolute; left: 16px; top: 4px; width: 40px; height: 40px; border-radius: 50%; background: #0f1116;
          display: flex; align-items: center; justify-content: center; overflow: hidden; }
.avatar img { width: 34px; }
.head { display: flex; align-items: center; gap: 6px; }
.name { color: #f2f3f5; font-weight: 500; }
.app { background: #5865f2; color: #fff; font-size: 10px; font-weight: 600; border-radius: 3px; padding: 1px 4px; line-height: 15px; }
.time { color: #949ba4; font-size: 12px; }
.body { color: #dbdee1; white-space: normal; overflow-wrap: anywhere; }
.h1 { font-size: 24px; font-weight: 700; line-height: 1.375; margin: 8px 0 4px; color: #f2f3f5; }
.h2 { font-size: 20px; font-weight: 700; line-height: 1.375; margin: 8px 0 4px; color: #f2f3f5; }
.h3 { font-size: 16px; font-weight: 700; line-height: 1.375; margin: 8px 0 4px; color: #f2f3f5; }
.body > :first-child { margin-top: 0; }
.sub { font-size: 13px; color: #949ba4; }
.gap { height: 8px; }
.ts { background: rgba(255,255,255,.06); border-radius: 3px; padding: 0 2px; }
.mention { background: rgba(88,101,242,.3); color: #c9cdfb; border-radius: 3px; padding: 0 2px; font-weight: 500; }
code { background: #2b2d31; border: 1px solid #1e1f22; border-radius: 4px; padding: 0 3px; font: 14px/1.2 "Noto Sans Mono", Consolas, monospace; }
.edited { color: #949ba4; font-size: 10px; margin-left: 4px; }
.rows { margin-top: 4px; display: flex; flex-direction: column; gap: 8px; padding: 2px 0; }
.row { display: flex; flex-wrap: wrap; gap: 8px; }
.btn { height: 32px; padding: 2px 16px; border-radius: 8px; font-size: 14px; font-weight: 500; color: #fff;
       display: inline-flex; align-items: center; gap: 6px; background: #4e5058; }
.btn.primary { background: #5865f2; } .btn.danger { background: #da373c; } .btn.success { background: #248046; }
.select { width: 400px; height: 40px; background: #1e1f22; border: 1px solid #1e1f22; border-radius: 8px; color: #949ba4;
          display: flex; align-items: center; justify-content: space-between; padding: 0 12px; font-size: 15px; }
.reactions { display: flex; gap: 4px; margin-top: 6px; flex-wrap: wrap; }
.react { background: #2b2d31; border: 1px solid #2b2d31; border-radius: 8px; padding: 2px 6px; display: flex; gap: 6px;
         align-items: center; font-size: 14px; color: #b5bac1; font-weight: 600; }
.react .e { font-size: 16px; }
.eph { background: rgba(88,101,242,.08); border-left: 2px solid #5865f2; }
.ephnote { color: #949ba4; font-size: 12px; margin-top: 4px; }
.ephnote a { color: #00a8fc; }
"""


def page(channel: str, topic: str, messages: list[Message], now: int, logo_uri: str,
         roles: dict[int, str], users: dict[int, str]) -> str:
    parts, last = [], None
    for m in messages:
        first = m.ephemeral or last is None or m.at - last.at > GROUP_SECONDS or last.ephemeral
        cls = "msg" + (" first" if first else "") + (" eph" if m.ephemeral else "")
        head = ""
        if first:
            head = (f'<div class="avatar"><img src="{logo_uri}" alt=""></div>'
                    f'<div class="head"><span class="name">Nyerk</span><span class="app">APP</span>'
                    f'<span class="time">{sent_label(m.at, now)}</span></div>')
        body = markdown(m.content, now, roles, users)
        if m.edited:
            body += '<span class="edited">(edited)</span>'
        rows = ""
        if m.rows:
            rendered = []
            for row in m.rows:
                cells = []
                for c in row:
                    if isinstance(c, Select):
                        cells.append(f'<div class="select">{html.escape(c.placeholder)}<span>⌄</span></div>')
                    else:
                        emoji = f'<span>{c.emoji}</span>' if c.emoji else ""
                        cells.append(f'<div class="btn {c.style}">{emoji}{html.escape(c.label)}</div>')
                rendered.append(f'<div class="row">{"".join(cells)}</div>')
            rows = f'<div class="rows">{"".join(rendered)}</div>'
        reacts = ""
        if m.reactions:
            reacts = '<div class="reactions">' + "".join(
                f'<div class="react"><span class="e">{e}</span>{n}</div>' for e, n in m.reactions) + "</div>"
        note = '<div class="ephnote">👁 Only you can see this · <a>Dismiss message</a></div>' if m.ephemeral else ""
        parts.append(f'<div class="{cls}">{head}<div class="body">{body}</div>{rows}{reacts}{note}</div>')
        last = m
    return (f'<!doctype html><html><head><meta charset="utf-8"><style>{CSS}</style></head><body>'
            f'<div class="bar"><span class="hash">#</span>{html.escape(channel)}'
            f'<span class="topic">{html.escape(topic)}</span></div>'
            f'<div class="feed">{"".join(parts)}</div></body></html>')


def buttons_in_rows(buttons: list, per_row: int = 5) -> list[list]:
    return [buttons[i:i + per_row] for i in range(0, len(buttons), per_row)]
