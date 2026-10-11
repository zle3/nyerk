"""Change history: who changed what in the configuration, with undo.

The web app snapshots the full settings export before and after every
admin/owner form submission. If they differ, the change is recorded with
a readable summary and the "before" snapshot, which Restore re-imports.
"""
from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from . import transfer
from .models import ConfigChange

KEEP = 300
KEYS = ("name", "label", "minutes", "lead_minutes")


def snapshot(s: Session) -> dict:
    data = transfer.export(s)
    data.pop("exported_at", None)
    return data


def _flatten(value, prefix: str, out: dict) -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            _flatten(v, f"{prefix}.{k}" if prefix else str(k), out)
    elif isinstance(value, list) and value and all(isinstance(x, dict) for x in value):
        for item in value:
            key = next((str(item[k]) for k in KEYS if k in item), "?")
            _flatten({k: v for k, v in item.items()}, f"{prefix}[{key}]", out)
    else:
        out[prefix] = value


def diff(before: dict, after: dict) -> list[str]:
    a, b = {}, {}
    _flatten(before, "", a)
    _flatten(after, "", b)
    lines = []
    for key in sorted(set(a) | set(b)):
        if a.get(key) != b.get(key):
            old, new = a.get(key, "—"), b.get(key, "—")
            short = lambda v: (str(v)[:60] + "…") if len(str(v)) > 60 else str(v)
            lines.append(f"{key}: {short(old)} → {short(new)}")
    return lines


def record(s: Session, at: int, user: dict, path: str, before: dict, lines: list[str]) -> ConfigChange:
    row = ConfigChange(at=at, user_id=user.get("uid", 0), user_name=user.get("name", ""),
                       level=user.get("level", ""), path=path[:200],
                       summary="\n".join(lines[:40]) + (f"\n… and {len(lines) - 40} more" if len(lines) > 40 else ""),
                       before=before)
    s.add(row)
    s.flush()
    cutoff = s.scalar(select(ConfigChange.id).order_by(ConfigChange.id.desc()).offset(KEEP).limit(1))
    if cutoff:
        s.execute(delete(ConfigChange).where(ConfigChange.id <= cutoff))
    return row


def restore(s: Session, change_id: int) -> list[str]:
    row = s.get(ConfigChange, change_id)
    if row is None or not row.before:
        raise ValueError("That change can't be restored.")
    data = dict(row.before)
    data["nyerk_export"] = transfer.FORMAT
    return transfer.apply_import(s, data)
