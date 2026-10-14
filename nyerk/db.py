"""Engine, sessions and single-row accessors."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, event, inspect, select, text
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
    _migrate(_engine, db_path)
    _add_missing_columns(_engine)
    _Session = sessionmaker(_engine, expire_on_commit=False)
    return _engine


# -- Schema migrations ---------------------------------------------------------
# PRAGMA user_version counts the migrations applied. Each runs once, in order.

GAME_TABLES = ("categories", "events", "pf_classes", "pf_brackets", "pf_windows", "pf_content",
               "pf_interest", "pf_parties")
# Settings and bot_state columns that moved to games (version 1).
MOVED_SETTINGS = (
    "timer_channel_id", "user_timer_limit", "report_offset_min", "alert_retention_min", "dashboard_max_oneoffs",
    "time_style_near", "time_style_far", "dashboard_title", "dashboard_divider", "dashboard_top_label",
    "dashboard_main_label", "oneoff_label", "dashboard_boss_note", "boss_button_label", "custom_button_label",
    "dashboard_commands_label", "dashboard_footer", "shugo_url", "shugo_region", "party_channel_id",
    "party_lifetime_min", "party_warn_min", "party_extend_min", "party_max_extensions", "party_status_hold_min",
    "party_ping_gap_min", "party_chat_delete_min", "tank_role_id", "support_role_id", "dps_role_id",
    "party_default_tank", "party_default_support", "party_default_dps",
)
MOVED_STATE = (
    "dashboard_message_id", "dashboard_hash", "roles_class_message_id", "roles_gear_message_id",
    "class_panel_message_id", "gear_panel_message_id", "interest_panel_message_id", "interest_panel_hash",
    "party_panel_message_id", "party_status_message_id", "party_status_hash", "last_import_at",
    "last_import_error", "pending_import",
)


def _migrate(engine: Engine, db_path: Path | str) -> None:
    with engine.connect() as conn:
        version = conn.exec_driver_sql("PRAGMA user_version").scalar() or 0
        has_data = conn.exec_driver_sql("SELECT COUNT(*) FROM settings").scalar()
    for target, step in ((1, _migrate_games), (2, _migrate_servers)):
        if version >= target:
            continue
        if has_data:
            _copy_before(db_path, target)
        step(engine)
        version = target


def _copy_before(db_path: Path | str, version: int) -> None:
    """A copy first: SQLite commits schema changes as it goes, so a crash
    mid-upgrade can't be rolled back. It stays until removed by hand."""
    if str(db_path) == ":memory:":
        return
    import sqlite3
    copy = Path(db_path).with_name(Path(db_path).stem + f"-before-v{version}.db")
    if copy.exists():
        return
    src, dst = sqlite3.connect(db_path), sqlite3.connect(copy)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()


def _columns(conn, table: str) -> list[str]:
    return [r[1] for r in conn.exec_driver_sql(f'PRAGMA table_info("{table}")')]


def _migrate_games(engine: Engine) -> None:
    """Version 1: everything belongs to a game. An install from before games
    becomes one game, "AION 2", holding all its events, roles and party
    finder setup; its per-game settings and message IDs move onto it. Tables
    whose names are now unique per game are rebuilt (SQLite can't change a
    unique constraint in place)."""
    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        conn.commit()
        legacy = "timer_channel_id" in _columns(conn, "settings") and \
            conn.exec_driver_sql("SELECT COUNT(*) FROM settings").scalar()
        if legacy and not conn.exec_driver_sql("SELECT COUNT(*) FROM games").scalar():
            st_cols = [c for c in MOVED_SETTINGS if c in _columns(conn, "settings")]
            bs_cols = [c for c in MOVED_STATE if c in _columns(conn, "bot_state")]
            st = conn.exec_driver_sql(f"SELECT {', '.join(st_cols)} FROM settings WHERE id = 1").mappings().first() or {}
            bs = conn.exec_driver_sql(f"SELECT {', '.join(bs_cols)} FROM bot_state WHERE id = 1").mappings().first() \
                if bs_cols else {}
            game_table = Base.metadata.tables["games"]
            # Every column's default first: an old database may lack newer settings.
            values = {c.name: c.default.arg for c in game_table.columns
                      if c.default is not None and not callable(c.default.arg)}
            from .games import AION2_SETTINGS
            values.update(AION2_SETTINGS)  # it was an AION 2 install; newer settings get the AION 2 defaults
            values.update({k: v for k, v in {**dict(st), **dict(bs or {})}.items() if v is not None})
            values.update(id=1, guild_id=0, name="AION 2", kind="aion2", enabled=1, sort_order=0)
            game_cols = set(_columns(conn, "games"))
            values = {k: v for k, v in values.items() if k in game_cols}
            names = ", ".join(f'"{k}"' for k in values)
            marks = ", ".join(f":{k}" for k in values)
            conn.execute(text(f"INSERT INTO games ({names}) VALUES ({marks})"), values)
        for name in GAME_TABLES:
            if "game_id" not in _columns(conn, name):
                _rebuild(conn, Base.metadata.tables[name], extra={"game_id": "1"})
        # Gear score moves from member_profiles to member_gear, per game.
        if "bracket_id" in _columns(conn, "member_profiles"):
            conn.exec_driver_sql(
                "INSERT OR IGNORE INTO member_gear (user_id, game_id, bracket_id, updated_at) "
                "SELECT user_id, 1, bracket_id, updated_at FROM member_profiles WHERE bracket_id IS NOT NULL")
        bad = conn.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
        if bad:
            conn.rollback()
            raise RuntimeError(f"Database upgrade to games failed a foreign key check: {bad[:5]}")
        conn.exec_driver_sql("PRAGMA user_version = 1")
        conn.commit()
        conn.exec_driver_sql("PRAGMA foreign_keys=ON")
        conn.commit()


# Tables that belong to a Discord server from version 2 on.
SERVER_TABLES = ("settings", "reaction_groups", "log_entries", "role_holders", "quiet_hours", "member_profiles",
                 "config_changes")
# bot_state columns that became per server (onto the settings row).
MOVED_TO_SERVER = ("reaction_message_id", "reaction_map", "reaction_messages", "roles_header_message_id",
                   "roles_clear_message_id")


def _migrate_servers(engine: Engine) -> None:
    """Version 2: one Nyerk, many Discord servers. Every row of an existing
    install belongs to server 0, a placeholder that is claimed as the real
    server (GUILD_ID, or the one server the bot is in) on the next start.
    The roles channel's message IDs move from bot_state onto the server's
    settings row, and the admin password from settings to bot_state: it is
    the host's, not one server's."""
    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        conn.commit()
        legacy = "guild_id" not in _columns(conn, "settings")
        password = ""
        moved: dict = {}
        if legacy:
            if "admin_password_hash" in _columns(conn, "settings"):
                password = conn.exec_driver_sql("SELECT admin_password_hash FROM settings ORDER BY id LIMIT 1") \
                    .scalar() or ""
            have = [c for c in MOVED_TO_SERVER if c in _columns(conn, "bot_state")]
            if have:
                row = conn.exec_driver_sql(f"SELECT {', '.join(have)} FROM bot_state WHERE id = 1").mappings().first()
                moved = {k: v for k, v in (row or {}).items() if v is not None}
        for name in SERVER_TABLES:
            if "guild_id" not in _columns(conn, name):
                extra = {"guild_id": "0"}
                if name == "settings":
                    extra["status"] = "'approved'"  # the install's own server keeps working
                _rebuild(conn, Base.metadata.tables[name], extra=extra)
        from sqlalchemy.schema import CreateIndex
        for index in Base.metadata.tables["games"].indexes:  # games.guild_id existed, its index didn't
            conn.exec_driver_sql(str(CreateIndex(index).compile(conn)).replace(
                "CREATE INDEX", "CREATE INDEX IF NOT EXISTS", 1))
        if legacy:
            if "admin_password_hash" not in _columns(conn, "bot_state"):
                conn.exec_driver_sql("ALTER TABLE bot_state ADD COLUMN admin_password_hash VARCHAR(255) DEFAULT ''")
            if password:
                conn.execute(text("UPDATE bot_state SET admin_password_hash = :p WHERE id = 1"), {"p": password})
            if moved:
                import json
                sets = ", ".join(f'"{k}" = :{k}' for k in moved)
                conn.execute(text(f"UPDATE settings SET {sets} WHERE guild_id = 0"),
                             {k: json.dumps(v) if isinstance(v, (dict, list)) else v for k, v in moved.items()})
        conn.exec_driver_sql("PRAGMA user_version = 2")
        conn.commit()
        conn.exec_driver_sql("PRAGMA foreign_keys=ON")
        conn.commit()


def _rebuild(conn, table, extra: dict[str, str]) -> None:
    """Recreate `table` from the current model and copy its rows across:
    the SQLite way to change constraints. `extra` fills new columns."""
    from sqlalchemy.schema import CreateIndex, CreateTable

    old = _columns(conn, table.name)
    tmp = f"_new_{table.name}"
    ddl = str(CreateTable(table).compile(conn)).replace(f'TABLE {table.name} ', f'TABLE {tmp} ', 1)
    conn.exec_driver_sql(ddl)
    keep = [c.name for c in table.columns if c.name in old]
    fill = {k: v for k, v in extra.items() if k not in old}
    for c in table.columns:  # new required columns the old table lacks get the model's default
        if c.name in old or c.name in fill or c.nullable or c.primary_key:
            continue
        arg = c.default.arg if c.default is not None else None
        if callable(arg):  # JSON columns: default=dict or list, wrapped by SQLAlchemy to take a context
            import json
            fill[c.name] = _sql_literal(json.dumps(arg(None)))
        elif arg is not None:
            fill[c.name] = _sql_literal(arg)
    cols = ", ".join(f'"{c}"' for c in keep + list(fill))
    vals = ", ".join([f'"{c}"' for c in keep] + list(fill.values()))
    conn.exec_driver_sql(f'INSERT INTO {tmp} ({cols}) SELECT {vals} FROM "{table.name}"')
    conn.exec_driver_sql(f'DROP TABLE "{table.name}"')
    conn.exec_driver_sql(f'ALTER TABLE {tmp} RENAME TO "{table.name}"')
    for index in table.indexes:
        conn.exec_driver_sql(str(CreateIndex(index).compile(conn)).replace("CREATE INDEX", "CREATE INDEX IF NOT EXISTS", 1))


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
                    default = f" DEFAULT {_sql_literal(val)}"
                conn.exec_driver_sql(f'ALTER TABLE {table.name} ADD COLUMN "{col.name}" {ddl_type}{default}')


def _sql_literal(val) -> str:
    if isinstance(val, bool):
        return str(int(val))
    if isinstance(val, (int, float)):
        return repr(val)
    return "'" + str(val).replace("'", "''") + "'"  # real newlines, not Python escapes


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


class NoServer(LookupError):
    """Nyerk has no settings for that Discord server (not joined, or removed)."""


def get_settings(s: Session, guild_id: int) -> Settings:
    """One server's settings. Rows are made when the bot sees a server
    (servers.seen), never here, so a stray ID can't create one."""
    row = s.scalar(select(Settings).where(Settings.guild_id == int(guild_id)))
    if row is None:
        raise NoServer(f"No server {guild_id}")
    return row


def get_state(s: Session) -> BotState:
    row = s.get(BotState, 1)
    if row is None:
        row = BotState(id=1)
        s.add(row)
        s.flush()
    return row
