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
    report_offset_min: Mapped[int] = mapped_column(Integer, default=1)
    keep_occurrences: Mapped[int] = mapped_column(Integer, default=2)  # no longer used
    # Delete alert messages this long after their event's start / spawn time.
    alert_retention_min: Mapped[int] = mapped_column(Integer, default=120)
    # Max one-off lines on the dashboard before "+N more".
    dashboard_max_oneoffs: Mapped[int] = mapped_column(Integer, default=20)
    # Discord timestamp styles for dashboard lines inside / beyond 24 hours.
    time_style_near: Mapped[str] = mapped_column(String(1), default="t")
    time_style_far: Mapped[str] = mapped_column(String(1), default="f")
    # Dashboard headings (Discord markdown). Blank hides that heading.
    dashboard_title: Mapped[str] = mapped_column(String(100), default="⏱️ AION 2 Timers")
    # Line above the title so the dashboard stands apart from the alerts.
    # Discord has no horizontal rule in plain text, so it's characters.
    dashboard_divider: Mapped[str] = mapped_column(String(100), default="━" * 22)
    dashboard_top_label: Mapped[str] = mapped_column(String(64), default="Server")
    dashboard_main_label: Mapped[str] = mapped_column(String(64), default="Upcoming events")
    oneoff_label: Mapped[str] = mapped_column(String(64), default="World Bosses")
    # Dashboard buttons: "{name}" is the boss name.
    boss_button_label: Mapped[str] = mapped_column(String(80), default="{name} died")
    custom_button_label: Mapped[str] = mapped_column(String(80), default="Custom time")
    # One-line reminder about /nyerk on the dashboard; blank hides it.
    dashboard_commands_label: Mapped[str] = mapped_column(
        String(200), default="💡 Type `/nyerk` for more: report a kill with a custom time, and see, edit or cancel your timers.")
    dashboard_footer: Mapped[str] = mapped_column(
        String(200), default="Times are in your own time zone. Killed a boss? Tap its button below.")
    shugo_url: Mapped[str] = mapped_column(String(255), default="https://shugo.gg/timers")
    shugo_region: Mapped[str] = mapped_column(String(64), default="NA East")
    admin_password_hash: Mapped[str] = mapped_column(String(255), default="")
    # Owner switch: while on, admin-role holders get member access only.
    admin_locked: Mapped[bool] = mapped_column(Boolean, default=False)

    # First message in the roles channel. Blank posts no header.
    roles_header: Mapped[str] = mapped_column(
        String(500), default="# 🎭 Roles\nPick your classes and gear score, then the pings you want. Change them any time.")

    # Party finder
    party_channel_id: Mapped[int] = mapped_column(BigInteger, default=0)
    party_lifetime_min: Mapped[int] = mapped_column(Integer, default=60)
    party_warn_min: Mapped[int] = mapped_column(Integer, default=10)
    party_extend_min: Mapped[int] = mapped_column(Integer, default=60)
    party_max_extensions: Mapped[int] = mapped_column(Integer, default=2)
    party_status_hold_min: Mapped[int] = mapped_column(Integer, default=120)
    party_ping_gap_min: Mapped[int] = mapped_column(Integer, default=15)
    tank_role_id: Mapped[int] = mapped_column(BigInteger, default=0)
    support_role_id: Mapped[int] = mapped_column(BigInteger, default=0)
    dps_role_id: Mapped[int] = mapped_column(BigInteger, default=0)
    # Makeup of the 5 person "Default" requirement.
    party_default_tank: Mapped[int] = mapped_column(Integer, default=1)
    party_default_support: Mapped[int] = mapped_column(Integer, default=1)
    party_default_dps: Mapped[int] = mapped_column(Integer, default=3)


class ReactionGroup(Base):
    """One reaction roles message in the roles channel (split past 20)."""

    __tablename__ = "reaction_groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    description: Mapped[str] = mapped_column(String(200), default="")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    # "category" / "content": where roles without a chosen message go.
    default_for: Mapped[str] = mapped_column(String(16), default="")


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    role_id: Mapped[int] = mapped_column(BigInteger, default=0)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    # Reaction emoji in the roles message; blank = next number emoji.
    emoji: Mapped[str] = mapped_column(String(64), default="")
    group_id: Mapped[int | None] = mapped_column(ForeignKey("reaction_groups.id", ondelete="SET NULL"), nullable=True)

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
    # {"<message id>": {"<emoji>": role_id}} for every roles message posted.
    reaction_messages: Mapped[dict] = mapped_column(JSON, default=dict)
    # Extra messages in the roles channel: class selector copy, remove-all.
    roles_header_message_id: Mapped[int] = mapped_column(BigInteger, default=0)
    roles_gear_message_id: Mapped[int] = mapped_column(BigInteger, default=0)
    # One-time data upgrades already applied, so deleting a seeded row sticks.
    upgrades_done: Mapped[list] = mapped_column(JSON, default=list)
    # Bumped by "Sign everyone out"; sessions from an older epoch are dropped.
    session_epoch: Mapped[int] = mapped_column(Integer, default=0)
    roles_class_message_id: Mapped[int] = mapped_column(BigInteger, default=0)
    roles_clear_message_id: Mapped[int] = mapped_column(BigInteger, default=0)
    last_tick_at: Mapped[int] = mapped_column(BigInteger, default=0)
    last_import_at: Mapped[int] = mapped_column(BigInteger, default=0)
    last_import_error: Mapped[str] = mapped_column(Text, default="")
    pending_import: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    last_backup_day: Mapped[str] = mapped_column(String(10), default="")
    # Party finder panels
    class_panel_message_id: Mapped[int] = mapped_column(BigInteger, default=0)
    gear_panel_message_id: Mapped[int] = mapped_column(BigInteger, default=0)
    interest_panel_message_id: Mapped[int] = mapped_column(BigInteger, default=0)
    interest_panel_hash: Mapped[str] = mapped_column(String(64), default="")
    party_panel_message_id: Mapped[int] = mapped_column(BigInteger, default=0)
    party_status_message_id: Mapped[int] = mapped_column(BigInteger, default=0)
    party_status_hash: Mapped[str] = mapped_column(String(64), default="")


class LogEntry(Base):
    """Recent log lines, shown on the admin overview."""

    __tablename__ = "log_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[int] = mapped_column(BigInteger)
    level: Mapped[str] = mapped_column(String(8))
    kind: Mapped[str] = mapped_column(String(32))
    text: Mapped[str] = mapped_column(Text)


# ----------------------------------------------------------------------------
# Party finder


class PfClass(Base):
    __tablename__ = "pf_classes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    emoji: Mapped[str] = mapped_column(String(64), default="")
    role_id: Mapped[int] = mapped_column(BigInteger, default=0)
    class_type: Mapped[str] = mapped_column(String(16), default="dps")  # tank | support | dps
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class PfBracket(Base):
    __tablename__ = "pf_brackets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    label: Mapped[str] = mapped_column(String(32), unique=True)
    min_score: Mapped[int] = mapped_column(Integer, default=0)
    max_score: Mapped[int] = mapped_column(Integer, default=0)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class PfWindow(Base):
    __tablename__ = "pf_windows"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    minutes: Mapped[int] = mapped_column(Integer, unique=True)
    label: Mapped[str] = mapped_column(String(32), default="")


class PfContent(Base):
    __tablename__ = "pf_content"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    emoji: Mapped[str] = mapped_column(String(64), default="")
    min_gear_score: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    # Dungeon ping role: pinged when a party opens, offered as a reaction role.
    role_id: Mapped[int] = mapped_column(BigInteger, default=0)
    react_emoji: Mapped[str] = mapped_column(String(64), default="")
    group_id: Mapped[int | None] = mapped_column(ForeignKey("reaction_groups.id", ondelete="SET NULL"), nullable=True)


class PfInterest(Base):
    __tablename__ = "pf_interest"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    user_name: Mapped[str] = mapped_column(String(100), default="")
    class_id: Mapped[int | None] = mapped_column(ForeignKey("pf_classes.id", ondelete="SET NULL"), nullable=True)
    class_type: Mapped[str] = mapped_column(String(16), default="")
    bracket_id: Mapped[int | None] = mapped_column(ForeignKey("pf_brackets.id", ondelete="SET NULL"), nullable=True)
    content_ids: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[int] = mapped_column(BigInteger)
    expires_at: Mapped[int] = mapped_column(BigInteger)
    last_pinged_at: Mapped[int] = mapped_column(BigInteger, default=0)
    status: Mapped[str] = mapped_column(String(16), default="active")  # active | cancelled | expired | joined

    pf_class: Mapped[PfClass | None] = relationship()
    bracket: Mapped[PfBracket | None] = relationship()


class PfParty(Base):
    __tablename__ = "pf_parties"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    content_id: Mapped[int | None] = mapped_column(ForeignKey("pf_content.id", ondelete="SET NULL"), nullable=True)
    leader_id: Mapped[int] = mapped_column(BigInteger)
    leader_name: Mapped[str] = mapped_column(String(100), default="")
    size: Mapped[int] = mapped_column(Integer, default=5)
    requirement: Mapped[str] = mapped_column(String(16), default="default")  # default | custom | none
    # {"tank": n, "support": n, "dps": n, "any": n}
    makeup: Mapped[dict] = mapped_column(JSON, default=dict)
    description: Mapped[str] = mapped_column(String(100), default="")
    # open | full | closed | expired | done
    status: Mapped[str] = mapped_column(String(16), default="open")
    created_at: Mapped[int] = mapped_column(BigInteger)
    expires_at: Mapped[int] = mapped_column(BigInteger)
    extensions_used: Mapped[int] = mapped_column(Integer, default=0)
    warned_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    filled_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Seconds left on the listing when it filled; restored if it reopens.
    remaining_s: Mapped[int] = mapped_column(Integer, default=0)
    ended_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Discord messages. The reconciler posts/edits/deletes to match status.
    listing_message_id: Mapped[int] = mapped_column(BigInteger, default=0)
    listing_hash: Mapped[str] = mapped_column(String(64), default="")
    ping_message_id: Mapped[int] = mapped_column(BigInteger, default=0)
    ping_pending: Mapped[bool] = mapped_column(Boolean, default=False)
    warning_message_id: Mapped[int] = mapped_column(BigInteger, default=0)
    ready_message_id: Mapped[int] = mapped_column(BigInteger, default=0)
    ready_pinged: Mapped[bool] = mapped_column(Boolean, default=False)

    content: Mapped[PfContent | None] = relationship()
    members: Mapped[list["PfPartyMember"]] = relationship(
        back_populates="party", cascade="all, delete-orphan", order_by="PfPartyMember.joined_at"
    )


class PfPartyMember(Base):
    __tablename__ = "pf_party_members"
    __table_args__ = (UniqueConstraint("party_id", "user_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    party_id: Mapped[int] = mapped_column(ForeignKey("pf_parties.id", ondelete="CASCADE"))
    user_id: Mapped[int] = mapped_column(BigInteger)
    user_name: Mapped[str] = mapped_column(String(100), default="")
    class_id: Mapped[int | None] = mapped_column(ForeignKey("pf_classes.id", ondelete="SET NULL"), nullable=True)
    class_name: Mapped[str] = mapped_column(String(64), default="")
    class_type: Mapped[str] = mapped_column(String(16), default="")
    slot_type: Mapped[str] = mapped_column(String(16), default="any")
    bracket_label: Mapped[str] = mapped_column(String(32), default="")
    joined_at: Mapped[int] = mapped_column(BigInteger)

    party: Mapped[PfParty] = relationship(back_populates="members")


class RoleHolder(Base):
    """Who holds a class or ping role, as far as Nyerk has seen through its
    own buttons and reactions. Feeds the Stats page."""

    __tablename__ = "role_holders"
    __table_args__ = (UniqueConstraint("user_id", "role_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    user_name: Mapped[str] = mapped_column(String(100), default="")
    role_id: Mapped[int] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(8))  # class | ping
    since: Mapped[int] = mapped_column(BigInteger)
    # Removed in Discord for the member's quiet hours; given back after.
    suspended: Mapped[bool] = mapped_column(Boolean, default=False)


class QuietHours(Base):
    """A member's daily window without dungeon ping roles."""

    __tablename__ = "quiet_hours"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    user_name: Mapped[str] = mapped_column(String(100), default="")
    start_min: Mapped[int] = mapped_column(Integer)   # minutes after midnight, member's time zone
    end_min: Mapped[int] = mapped_column(Integer)
    tz: Mapped[str] = mapped_column(String(64), default="America/New_York")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    state: Mapped[str] = mapped_column(String(8), default="active")  # active | quiet
    updated_at: Mapped[int] = mapped_column(BigInteger, default=0)


class MemberProfile(Base):
    """Per-member settings Nyerk keeps itself (not Discord roles)."""

    __tablename__ = "member_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    user_name: Mapped[str] = mapped_column(String(100), default="")
    bracket_id: Mapped[int | None] = mapped_column(ForeignKey("pf_brackets.id", ondelete="SET NULL"), nullable=True)
    updated_at: Mapped[int] = mapped_column(BigInteger, default=0)

    bracket: Mapped["PfBracket | None"] = relationship()
