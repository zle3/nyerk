"""One place to log: local file, recent-log table for the admin page, and a
queue the bot drains into #logs."""
from __future__ import annotations

import asyncio
import logging
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from sqlalchemy import delete, select

from . import db
from .models import LogEntry

logger = logging.getLogger("nyerk")
KEEP_DB_ROWS = 500
QUEUE_MAX = 200  # lines waiting for #logs


class LogBus:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[str] | None = None
        self.dropped = 0

    def setup(self, log_file: Path) -> None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        fh = RotatingFileHandler(log_file, maxBytes=2_000_000, backupCount=5, encoding="utf-8")
        fh.setFormatter(fmt)
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root = logging.getLogger()
        root.setLevel(logging.INFO)
        root.addHandler(fh)
        root.addHandler(sh)
        logging.getLogger("discord").setLevel(logging.WARNING)
        logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    def attach_queue(self) -> asyncio.Queue[str]:
        # Bounded: a flood of log lines can't grow memory or keep the bot
        # posting to #logs for hours. Extra lines still reach the Logs page.
        self.queue = asyncio.Queue(maxsize=QUEUE_MAX)
        return self.queue

    def _emit(self, level: str, kind: str, text: str, discord: bool) -> None:
        getattr(logger, level.lower())(f"[{kind}] {text}")
        try:
            with db.session() as s:
                s.add(LogEntry(at=int(time.time()), level=level, kind=kind, text=text))
                s.flush()
                cutoff = s.scalar(select(LogEntry.id).order_by(LogEntry.id.desc()).offset(KEEP_DB_ROWS).limit(1))
                if cutoff:
                    s.execute(delete(LogEntry).where(LogEntry.id <= cutoff))
        except Exception:  # never let logging take the bot down
            logger.exception("could not store log entry")
        if discord and self.queue is not None:
            prefix = {"INFO": "", "WARNING": "⚠️ ", "ERROR": "❌ "}.get(level, "")
            try:
                self.queue.put_nowait(f"{prefix}**{kind}** {text}")
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
