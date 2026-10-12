"""The forgotten list: members erased with "Forget this member", kept so a
restored backup can't bring them back.

It lives in data/forgotten.json, outside the database, so restoring an older
nyerk.db doesn't restore an older list with it. Each entry holds only a
random ID, the Discord user ID and when they were forgotten. The database
records which entries it has already applied (BotState.forgets_applied): a
restored copy from before a forget lacks that entry, so the forget runs
again on the next start. The live database already has it, so someone who
comes back and uses Nyerk after being forgotten keeps their new data.

Entries are dropped once every backup that could hold the old data has aged
out: BACKUP_KEEP nightly backups, so BACKUP_KEEP days (14 by default).
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import time
from pathlib import Path

from . import db, members
from .config import Config
from .db import get_state
from .logbus import bus

log = logging.getLogger("nyerk.forgotten")
DAY = 86400


def _path(cfg: Config) -> Path:
    return cfg.data_dir / "forgotten.json"


def _keep_seconds(cfg: Config) -> int:
    return (max(cfg.backup_keep, 1) + 1) * DAY  # +1: the backup written the night of the forget


def load(cfg: Config) -> list[dict]:
    try:
        data = json.loads(_path(cfg).read_text(encoding="utf-8"))
        return [e for e in data if isinstance(e, dict) and {"id", "user_id", "at"} <= set(e)]
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        log.error("forgotten.json unreadable: %r", exc)
        return []


def _save(cfg: Config, entries: list[dict]) -> None:
    path = _path(cfg)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(entries, indent=1), encoding="utf-8")
    os.replace(tmp, path)  # never a half-written list


def record(cfg: Config, s, user_id: int, now: int) -> None:
    """After a forget: add it to the list and mark it applied to this database."""
    entry = {"id": secrets.token_hex(8), "user_id": int(user_id), "at": int(now)}
    _save(cfg, load(cfg) + [entry])
    state = get_state(s)
    state.forgets_applied = list(state.forgets_applied or []) + [entry["id"]]


def reapply(cfg: Config, now: int | None = None) -> int:
    """Drop expired entries, then erase anyone on the list this database
    hasn't forgotten yet (it was restored from an older copy). Returns how
    many forgets ran."""
    now = int(time.time()) if now is None else now
    entries = load(cfg)
    fresh = [e for e in entries if now - int(e["at"]) < _keep_seconds(cfg)]
    if len(fresh) != len(entries):
        _save(cfg, fresh)
    ran = 0
    with db.session() as s:
        state = get_state(s)
        applied = set(state.forgets_applied or [])
        for e in fresh:
            if e["id"] not in applied:
                members.forget(s, int(e["user_id"]), now)
                applied.add(e["id"])
                ran += 1
        keep = {e["id"] for e in fresh}
        state.forgets_applied = sorted(applied & keep)
    if ran:
        bus.warn("admin", f"Re-applied {ran} Forget member request(s) to a restored database.")
    return ran
