"""One place to log: local file, recent-log table for the admin page, and a
queue the bot drains into #logs.

Every line belongs to the Discord server in `current_guild`, which the bot
sets for each click and scheduler step and the web page for each request
(see `for_guild`). Lines with no server (0: startup, backups) are the
host's: they show on the host's Logs page and in no server's #logs.
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
import time
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from sqlalchemy import delete, select

from . import db
from .models import LogEntry

logger = logging.getLogger("nyerk")
KEEP_DB_ROWS = 500  # per server
QUEUE_MAX = 200  # lines waiting for #logs

current_guild: contextvars.ContextVar[int] = contextvars.ContextVar("nyerk_guild", default=0)


@contextlib.contextmanager
def for_guild(guild_id: int | None):
    """Log lines inside belong to this server."""
    token = current_guild.set(int(guild_id or 0))
    try:
        yield
    finally:
        current_guild.reset(token)


class LogBus:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[tuple[int, str]] | None = None
        self.dropped = 0

    def setup(self, log_file: Path, keep_days: int = 14) -> None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        # A new file every day (UTC); files older than keep_days are deleted.
        fh = TimedRotatingFileHandler(log_file, when="midnight", backupCount=keep_days, encoding="utf-8", utc=True)
        prune_old_files(log_file, keep_days)
        fh.setFormatter(fmt)
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root = logging.getLogger()
        root.setLevel(logging.INFO)
        root.addHandler(fh)
        root.addHandler(sh)
        logging.getLogger("discord").setLevel(logging.WARNING)
        logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    def attach_queue(self) -> asyncio.Queue[tuple[int, str]]:
        # Bounded: a flood of log lines can't grow memory or keep the bot
        # posting to #logs for hours. Extra lines still reach the Logs page.
        self.queue = asyncio.Queue(maxsize=QUEUE_MAX)
        return self.queue

    def _emit(self, level: str, kind: str, text: str, discord: bool) -> None:
        guild = current_guild.get()
        getattr(logger, level.lower())(f"[{guild or 'host'}] [{kind}] {text}")
        try:
            with db.session() as s:
                s.add(LogEntry(guild_id=guild, at=int(time.time()), level=level, kind=kind, text=text))
                s.flush()
                cutoff = s.scalar(select(LogEntry.id).where(LogEntry.guild_id == guild)
                                  .order_by(LogEntry.id.desc()).offset(KEEP_DB_ROWS).limit(1))
                if cutoff:
                    s.execute(delete(LogEntry).where(LogEntry.guild_id == guild, LogEntry.id <= cutoff))
        except Exception:  # never let logging take the bot down
            logger.exception("could not store log entry")
        if discord and guild and self.queue is not None:
            prefix = {"INFO": "", "WARNING": "⚠️ ", "ERROR": "❌ "}.get(level, "")
            try:
                self.queue.put_nowait((guild, f"{prefix}**{kind}** {text}"))
            except asyncio.QueueFull:
                self.dropped += 1

    def info(self, kind: str, text: str, discord: bool = True) -> None:
        self._emit("INFO", kind, text, discord)

    def warn(self, kind: str, text: str, discord: bool = True) -> None:
        self._emit("WARNING", kind, text, discord)

    def error(self, kind: str, text: str, discord: bool = True) -> None:
        self._emit("ERROR", kind, text, discord)


    def take_dropped(self) -> int:
        n, self.dropped = self.dropped, 0
        return n


bus = LogBus()


def prune_old_files(log_file: Path, keep_days: int, now: float | None = None) -> int:
    """Delete rotated log files (nyerk.log.<date> and the older size-rotated
    nyerk.log.<n>) last written more than keep_days ago."""
    cutoff = (time.time() if now is None else now) - keep_days * 86400
    gone = 0
    for f in log_file.parent.glob(log_file.name + ".*"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                gone += 1
        except OSError:
            pass
    return gone


def prune_old_rows(keep_days: int, now: float | None = None) -> int:
    """Delete web Logs page entries older than keep_days."""
    cutoff = int((time.time() if now is None else now) - keep_days * 86400)
    with db.session() as s:
        return s.execute(delete(LogEntry).where(LogEntry.at < cutoff)).rowcount or 0
