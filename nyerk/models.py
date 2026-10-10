"""Database schema. All instants are stored as integer Unix seconds (UTC)."""
from __future__ import annotations

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Settings(Base):
    __tablename__ = "settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    timer_channel_id: Mapped[int] = mapped_column(BigInteger, default=0)
    roles_channel_id: Mapped[int] = mapped_column(BigInteger, default=0)
    log_channel_id: Mapped[int] = mapped_column(BigInteger, default=0)
    default_tz: Mapped[str] = mapped_column(String(64), default="America/New_York")
    user_timer_limit: Mapped[int] = mapped_column(Integer, default=10)
    report_offset_min: Mapped[int] = mapped_column(Integer, default=5)
    keep_occurrences: Mapped[int] = mapped_column(Integer, default=2)
    # Max one-off lines on the dashboard before "+N more".
    dashboard_max_oneoffs: Mapped[int] = mapped_column(Integer, default=20)
    # Discord timestamp styles for dashboard lines inside / beyond 24 hours.
    time_style_near: Mapped[str] = mapped_column(String(1), default="t")
    time_style_far: Mapped[str] = mapped_column(String(1), default="f")
    # Dashboard headings (Discord markdown). Blank hides that heading.
    dashboard_title: Mapped[str] = mapped_column(String(100), default="⏱️ AION2 Timers")
    dashboard_top_label: Mapped[str] = mapped_column(String(64), default="Server")
    dashboard_main_label: Mapped[str] = mapped_column(String(64), default="Upcoming events")
    oneoff_label: Mapped[str] = mapped_column(String(64), default="World bosses and one-offs")
    dashboard_footer: Mapped[str] = mapped_column(
        String(200), default="Times are in your own time zone. Killed a boss? Tap its button below.")
    shugo_url: Mapped[str] = mapped_column(String(255), default="https://shugo.gg/timers")
    shugo_region: Mapped[str] = mapped_column(String(64), default="NA East")
    admin_password_hash: Mapped[str] = mapped_column(String(255), default="")


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    role_id: Mapped[int] = mapped_column(BigInteger, default=0)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    events: Mapped[list["Event"]] = relationship(back_populates="category")


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id", ondelete="SET NULL"))
    name: Mapped[str] = mapped_column(String(100))
    location: Mapped[str] = mapped_column(String(100), default="")
    schedule_type: Mapped[str] = mapped_column(String(32))
    # Includes the schedule's own time zone under "tz".
    schedule_params: Mapped[dict] = mapped_column(JSON, default=dict)
    duration_minutes: Mapped[int] = mapped_column(Integer, default=0)
    respawn_minutes: Mapped[int] = mapped_column(Integer, default=0)
    # Maintenance week overrides point at the regular maintenance event.
    overrides_event_id: Mapped[int | None] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # "top" pins the line above everything (resets, maintenance), "main" is the
    # normal look-ahead section, "hidden" keeps it off the dashboard.
    dashboard: Mapped[str] = mapped_column(String(8), default="main")
    dashboard_label: Mapped[str] = mapped_column(String(100), default="")
    dashboard_label_active: Mapped[str] = mapped_column(String(100), default="")
    source: Mapped[str] = mapped_column(String(32), default="manual")
    source_key: Mapped[str] = mapped_column(String(100), default="")
    edited_fields: Mapped[list] = mapped_column(JSON, default=list)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    category: Mapped[Category | None] = relationship(back_populates="events")
    slots: Mapped[list["AlertSlot"]] = relationship(
        back_populates="event", cascade="all, delete-orphan", order_by="AlertSlot.lead_minutes.desc()"
    )


class AlertSlot(Base):
    __tablename__ = "alert_slots"
    __table_args__ = (UniqueConstraint("event_id", "lead_minutes"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"))
    lead_minutes: Mapped[int] = mapped_column(Integer)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    ping: Mapped[bool] = mapped_column(Boolean, default=False)
    # Blank means "use the built-in default for this kind of slot".
    template: Mapped[str] = mapped_column(Text, default="")

    event: Mapped[Event] = relationship(back_populates="slots")


class UserTimer(Base):
    __tablename__ = "user_timers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"))
    user_id: Mapped[int] = mapped_column(BigInteger)
    user_name: Mapped[str] = mapped_column(String(100), default="")
    mode: Mapped[str] = mapped_column(String(16))  # kill | countdown
    killed_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    fire_at: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(16), default="active")  # active | done | cancelled
    created_at: Mapped[int] = mapped_column(BigInteger)
    # Alerts whose send time is before this are never sent (created or edited).
    armed_at: Mapped[int] = mapped_column(BigInteger)

    event: Mapped[Event] = relationship()


class SentMessage(Base):
    """One row per alert slot occurrence: sent or skipped. Doubles as the
    dedupe ledger that prevents double posts after a restart."""

    __tablename__ = "sent_messages"
    __table_args__ = (UniqueConstraint("event_id", "timer_id", "occurrence_at", "slot"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    event_id: Mapped[int] = mapped_column(Integer)
    timer_id: Mapped[int] = mapped_column(Integer, default=0)  # 0 = not a user timer
    occurrence_at: Mapped[int] = mapped_column(BigInteger)
    slot: Mapped[int] = mapped_column(Integer)  # lead minutes
    status: Mapped[str] = mapped_column(String(16), default="sent")  # sent | skipped | failed
    sent_at: Mapped[int] = mapped_column(BigInteger)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)


class BotState(Base):
    __tablename__ = "bot_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    dashboard_message_id: Mapped[int] = mapped_column(BigInteger, default=0)
    dashboard_hash: Mapped[str] = mapped_column(String(64), default="")
    reaction_message_id: Mapped[int] = mapped_column(BigInteger, default=0)
    # {"1️⃣": role_id, ...} as posted, so reordering in the admin page does not
    # silently change what an existing reaction grants.
    reaction_map: Mapped[dict] = mapped_column(JSON, default=dict)
    last_tick_at: Mapped[int] = mapped_column(BigInteger, default=0)
    last_import_at: Mapped[int] = mapped_column(BigInteger, default=0)
    last_import_error: Mapped[str] = mapped_column(Text, default="")
    pending_import: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    last_backup_day: Mapped[str] = mapped_column(String(10), default="")


class LogEntry(Base):
    """Recent log lines, shown on the admin overview."""

    __tablename__ = "log_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[int] = mapped_column(BigInteger)
    level: Mapped[str] = mapped_column(String(8))
    kind: Mapped[str] = mapped_column(String(32))
    text: Mapped[str] = mapped_column(Text)
