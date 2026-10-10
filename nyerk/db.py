"""Engine, sessions and single-row accessors."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base, BotState, Settings

_engine: Engine | None = None
_Session: sessionmaker[Session] | None = None


def init_engine(db_path: Path | str) -> Engine:
    global _engine, _Session
    url = "sqlite://" if str(db_path) == ":memory:" else f"sqlite:///{db_path}"
    kwargs = {}
    if url == "sqlite://":
        from sqlalchemy.pool import StaticPool

        kwargs = {"poolclass": StaticPool, "connect_args": {"check_same_thread": False}}
    else:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        kwargs = {"connect_args": {"check_same_thread": False}}
    _engine = create_engine(url, **kwargs)

    @event.listens_for(_engine, "connect")
    def _pragmas(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        if url != "sqlite://":
            cur.execute("PRAGMA journal_mode=WAL")
        cur.close()

    Base.metadata.create_all(_engine)
    _add_missing_columns(_engine)
    _Session = sessionmaker(_engine, expire_on_commit=False)
    return _engine


def _add_missing_columns(engine: Engine) -> None:
    """Tiny forward-only migration: add columns that newer code defines."""
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            existing = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing:
                    continue
                ddl_type = col.type.compile(engine.dialect)
                default = ""
                if col.default is not None and not callable(col.default.arg):
                    val = col.default.arg
                    default = f" DEFAULT {int(val) if isinstance(val, bool) else repr(val)}"
                conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN "{col.name}" {ddl_type}{default}'))


def engine() -> Engine:
    assert _engine is not None, "init_engine() not called"
    return _engine


@contextmanager
def session() -> Iterator[Session]:
    assert _Session is not None, "init_engine() not called"
    s = _Session()
    try:
        yield s
        s.commit()
    except BaseException:
        s.rollback()
        raise
    finally:
        s.close()


def get_settings(s: Session) -> Settings:
    row = s.get(Settings, 1)
    if row is None:
        row = Settings(id=1)
        s.add(row)
        s.flush()
    return row


def get_state(s: Session) -> BotState:
    row = s.get(BotState, 1)
    if row is None:
        row = BotState(id=1)
        s.add(row)
        s.flush()
    return row
