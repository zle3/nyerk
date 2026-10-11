"""The Discord side: scheduler loop, dashboard, commands, buttons, reaction
roles and #logs. This is the only module that talks to Discord."""
from __future__ import annotations

import asyncio
import logging
import sys
import time
from typing import Optional

import discord
from discord import app_commands
from sqlalchemy import select

from . import db, engine, party, party_bot, quiet, reaction_roles, timers, tracking
from .db import get_settings, get_state
from .logbus import bus
from .models import PfContent, SentMessage, UserTimer
from .timeparse import humanize

log = logging.getLogger("nyerk.bot")

TICK_SECONDS = 30
REACTION_PAUSE = 1.5
NO_MENTIONS = discord.AllowedMentions.none()


def now() -> int:
    return int(time.time())


# ----------------------------------------------------------------------------
# Dashboard buttons. Fixed custom IDs, so they keep working after a restart.


class BossButton(discord.ui.DynamicItem[discord.ui.Button], template=r"nyerk:boss:(?P<id>\d+)"):
    def __init__(self, event_id: int, name: str = "") -> None:
        super().__init__(
            discord.ui.Button(
                label=f"{name} died"[:80] if name else "Boss died",
                style=discord.ButtonStyle.secondary,
                custom_id=f"nyerk:boss:{event_id}",
            )
        )
        self.event_id = event_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.client.handle_boss_report(interaction, str(self.event_id))


class CustomTimeButton(discord.ui.DynamicItem[discord.ui.Button], template=r"nyerk:custom"):
    def __init__(self) -> None:
        super().__init__(
            discord.ui.Button(label="Custom time", style=discord.ButtonStyle.primary, custom_id="nyerk:custom")
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls()

    async def callback(self, interaction: discord.Interaction) -> None:
        with db.session() as s:
            bosses = [(b.id, b.name) for b in timers.boss_events(s)][:25]
        if not bosses:
            await interaction.response.send_message("No world bosses are enabled.", ephemeral=True)
            return
        await interaction.response.send_modal(CustomTimeModal(bosses))


class PingSettingsButton(discord.ui.DynamicItem[discord.ui.Button],
                         template=r"nyerk:roles:(?P<what>clear|quiet|quietoff|mine)"):
    LABELS = {
        "mine": ("My roles", discord.ButtonStyle.primary, "📋"),
        "quiet": ("Quiet hours", discord.ButtonStyle.primary, "🌙"),
        "quietoff": ("Turn off quiet hours", discord.ButtonStyle.secondary, None),
        "clear": ("Remove all my ping roles", discord.ButtonStyle.danger, None),
    }

    def __init__(self, what: str) -> None:
        label, style, emoji = self.LABELS[what]
        super().__init__(discord.ui.Button(label=label, style=style, emoji=emoji, custom_id=f"nyerk:roles:{what}"))
        self.what = what

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["what"])

    async def callback(self, interaction: discord.Interaction) -> None:
        client = interaction.client
        await {"clear": client.clear_pings, "quiet": client.open_quiet_hours,
               "quietoff": client.quiet_hours_off, "mine": client.show_my_roles}[self.what](interaction)


class QuietHoursModal(discord.ui.Modal, title="Quiet hours"):
    def __init__(self, start_h: int, end_h: int, tz: str) -> None:
        super().__init__(timeout=600)
        hours = [(quiet.hour_label(h), str(h)) for h in range(24)]
        self.start = discord.ui.Select(options=[discord.SelectOption(label=l, value=v, default=v == str(start_h))
                                                for l, v in hours])
        self.end = discord.ui.Select(options=[discord.SelectOption(label=l, value=v, default=v == str(end_h))
                                              for l, v in hours])
        self.tz = discord.ui.Select(options=[discord.SelectOption(label=l, value=v, default=v == tz)
                                             for v, l in quiet.ZONES])
        self.add_item(discord.ui.Label(text="Quiet from", component=self.start,
                                       description="Your dungeon ping roles are removed at this hour."))
        self.add_item(discord.ui.Label(text="Until", component=self.end,
                                       description="And given back at this hour, every day."))
        self.add_item(discord.ui.Label(text="Your time zone", component=self.tz))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.client.save_quiet_hours(interaction, int(self.start.values[0]), int(self.end.values[0]),
                                                  self.tz.values[0])


class CustomTimeModal(discord.ui.Modal, title="Report a world boss"):
    def __init__(self, bosses: list[tuple[int, str]]) -> None:
        super().__init__(timeout=600)
        self.boss = discord.ui.Select(
            placeholder="Choose the boss",
            options=[discord.SelectOption(label=name, value=str(i)) for i, name in bosses],
        )
        self.killed_at = discord.ui.TextInput(placeholder="e.g. 10 min ago or 14:30", required=False, max_length=40)
        self.respawns_in = discord.ui.TextInput(placeholder="e.g. 1h 20m", required=False, max_length=40)
        self.add_item(discord.ui.Label(text="Boss", component=self.boss))
        self.add_item(discord.ui.Label(
            text="Killed at", description="When it died. Leave both blank if it just died.", component=self.killed_at))
        self.add_item(discord.ui.Label(
            text="Or respawns in", description="Time left on the respawn. Fill one field, not both.",
            component=self.respawns_in))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.client.handle_boss_report(
            interaction, self.boss.values[0], self.killed_at.value, self.respawns_in.value
        )


def dashboard_view(bosses: list[tuple[int, str]]) -> discord.ui.View | None:
    if not bosses:
        return None
    view = discord.ui.View(timeout=None)
    for event_id, name in bosses[:24]:
        view.add_item(BossButton(event_id, name))
    view.add_item(CustomTimeButton())
    return view


# ----------------------------------------------------------------------------
# Slash commands


nyerk_group = app_commands.Group(name="nyerk", description="AION2 timers", guild_only=True)


async def boss_autocomplete(interaction: discord.Interaction, current: str):
    with db.session() as s:
        names = [b.name for b in timers.boss_events(s)]
    cur = current.lower()
    return [app_commands.Choice(name=n, value=n) for n in names if cur in n.lower()][:25]


async def my_timer_autocomplete(interaction: discord.Interaction, current: str):
    with db.session() as s:
        rows = timers.user_timers(s, interaction.user.id)
        t = now()
        choices = [(f"{r.event.name} — in {humanize(r.fire_at - t)}", str(r.id)) for r in rows]
    cur = current.lower()
    return [app_commands.Choice(name=n[:100], value=v) for n, v in choices if cur in n.lower()][:25]


@nyerk_group.command(name="worldboss", description="Report a world boss kill and start its respawn timer")
@app_commands.describe(
    boss="The boss that was killed",
    killed_at="When it died, e.g. '10 min ago' or '14:30'. Blank = it just died.",
    respawns_in="Time left on its respawn, e.g. '1h 20m'. Use instead of killed_at.",
)
@app_commands.autocomplete(boss=boss_autocomplete)
async def cmd_worldboss(interaction: discord.Interaction, boss: str,
                        killed_at: Optional[str] = None, respawns_in: Optional[str] = None):
    await interaction.client.handle_boss_report(interaction, boss, killed_at or "", respawns_in or "")


@nyerk_group.command(name="mytimers", description="List your active boss timers")
async def cmd_mytimers(interaction: discord.Interaction):
    with db.session() as s:
        rows = timers.user_timers(s, interaction.user.id)
        limit = get_settings(s).user_timer_limit
        lines = [f"{r.event.name}: {engine.ts(r.fire_at, 'F')} ({engine.ts(r.fire_at, 'R')})" for r in rows]
    text = "\n".join(lines) if lines else "You have no active timers."
    await interaction.response.send_message(f"{text}\nActive timers: {len(lines)}/{limit}.",
                                            ephemeral=True, allowed_mentions=NO_MENTIONS)


@nyerk_group.command(name="cancel", description="Cancel one of your boss timers")
@app_commands.autocomplete(timer=my_timer_autocomplete)
async def cmd_cancel(interaction: discord.Interaction, timer: str):
    client: NyerkBot = interaction.client
    with db.session() as s:
        row = _own_timer(s, interaction.user.id, timer)
        if row is None:
            await interaction.response.send_message("That is not one of your active timers.", ephemeral=True)
            return
        result = timers.cancel(row, interaction.user.display_name)
    await interaction.response.send_message(result.reply, ephemeral=True, allowed_mentions=NO_MENTIONS)
    if result.ok:
        bus.info("timer", result.log)
        client.request_refresh()


@nyerk_group.command(name="edit", description="Change the kill time or remaining countdown of your timer")
@app_commands.describe(killed_at="New kill time, e.g. '10 min ago'", respawns_in="New time left, e.g. '45m'")
@app_commands.autocomplete(timer=my_timer_autocomplete)
async def cmd_edit(interaction: discord.Interaction, timer: str,
                   killed_at: Optional[str] = None, respawns_in: Optional[str] = None):
    client: NyerkBot = interaction.client
    with db.session() as s:
        row = _own_timer(s, interaction.user.id, timer)
        if row is None:
            await interaction.response.send_message("That is not one of your active timers.", ephemeral=True)
            return
        result = timers.edit(s, row, now(), killed_at or "", respawns_in or "", interaction.user.display_name)
    await interaction.response.send_message(result.reply, ephemeral=True, allowed_mentions=NO_MENTIONS)
    if result.ok:
        bus.info("timer", result.log)
        client.request_refresh()


def _own_timer(s, user_id: int, raw: str) -> UserTimer | None:
    try:
        row = s.get(UserTimer, int(raw))
    except ValueError:
        return None
    if row is None or row.user_id != user_id or row.status != "active":
        return None
    return row


# ----------------------------------------------------------------------------
# The bot


class NyerkBot(discord.Client):
    def __init__(self, guild_id: int) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.guild_reactions = True
        super().__init__(intents=intents, allowed_mentions=NO_MENTIONS)
        self.guild_id = guild_id
        self.tree = app_commands.CommandTree(self)
        self.tree.add_command(nyerk_group)
        self.tree.on_error = self._on_app_command_error
        self._wake = asyncio.Event()
        self._force_repost = False
        self._lock = asyncio.Lock()
        self._tasks: list[asyncio.Task] = []
        self._started = False
        self.last_tick_ok: int = 0
        self.pf = party_bot.PartyFinder(self)
        self._clearing: set[int] = set()

    # -- lifecycle ---------------------------------------------------------

    async def setup_hook(self) -> None:
        self.add_dynamic_items(BossButton, CustomTimeButton, PingSettingsButton, *party_bot.ITEMS)
        if self.guild_id:
            guild = discord.Object(self.guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        if bus.queue is None:
            bus.attach_queue()
        self._tasks.append(asyncio.create_task(self._log_drain(), name="nyerk-logs"))

    async def on_ready(self) -> None:
        if self._started:
            bus.info("startup", "Reconnected to Discord.", discord=False)
            self.request_refresh()
            return
        self._started = True
        names = ", ".join(g.name for g in self.guilds) or "no servers"
        bus.info("startup", f"Nyerk online as {self.user} in {names}.")
        self._tasks.append(asyncio.create_task(self._scheduler(), name="nyerk-scheduler"))

    async def close(self) -> None:
        for t in self._tasks:
            t.cancel()
        await super().close()

    async def on_error(self, event_method: str, *args, **kwargs) -> None:
        log.exception("error in %s", event_method)
        bus.error("error", f"{event_method}: {sys.exc_info()[1]!r}")

    async def _on_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        bus.error("error", f"/{interaction.command.qualified_name if interaction.command else '?'}: {error!r}")
        try:
            if interaction.response.is_done():
                await interaction.followup.send("Something went wrong. It has been logged.", ephemeral=True)
            else:
                await interaction.response.send_message("Something went wrong. It has been logged.", ephemeral=True)
        except discord.HTTPException:
            pass

    # -- requests from the web admin -----------------------------------------

    def request_refresh(self, repost: bool = False) -> None:
        """Wake the scheduler now (timer or config changed)."""
        self._force_repost = self._force_repost or repost
        self._wake.set()  # web admin and bot share one event loop

    def status(self) -> dict:
        guild = self.get_guild(self.guild_id) if self.guild_id else (self.guilds[0] if self.guilds else None)
        return {
            "connected": self.is_ready() and not self.is_closed(),
            "user": str(self.user) if self.user else "",
            "guild": guild.name if guild else "",
            "latency_ms": int(self.latency * 1000) if self.is_ready() and self.latency == self.latency else None,
            "last_tick": self.last_tick_ok,
        }

    # -- channels ------------------------------------------------------------

    async def _channel(self, channel_id: int) -> discord.abc.Messageable | None:
        if not channel_id:
            return None
        ch = self.get_channel(channel_id)
        if ch is None:
            try:
                ch = await self.fetch_channel(channel_id)
            except discord.HTTPException:
                return None
        return ch

    async def _log_drain(self) -> None:
        assert bus.queue is not None
        while True:
            lines = [await bus.queue.get()]
            await asyncio.sleep(1.0)  # batch bursts into one message
            while not bus.queue.empty() and sum(len(x) + 1 for x in lines) < 1700:
                lines.append(bus.queue.get_nowait())
            with db.session() as s:
                channel_id = get_settings(s).log_channel_id
            ch = await self._channel(channel_id) if self.is_ready() else None
            if ch is None:
                continue
            try:
                await ch.send("\n".join(lines)[:2000], allowed_mentions=NO_MENTIONS)
            except discord.HTTPException as exc:
                log.warning("could not write to #logs: %r", exc)

    # -- reports -------------------------------------------------------------

    async def handle_boss_report(self, interaction: discord.Interaction, boss: str,
                                 killed_at: str = "", respawns_in: str = "") -> None:
        name = interaction.user.display_name
        with db.session() as s:
            event = timers.find_boss(s, boss)
            if event is None:
                await interaction.response.send_message(f"No enabled world boss called {boss!r}.", ephemeral=True,
                                                        allowed_mentions=NO_MENTIONS)
                return
            result = timers.report(s, event, interaction.user.id, name, now(), killed_at, respawns_in)
        await interaction.response.send_message(result.reply, ephemeral=True, allowed_mentions=NO_MENTIONS)
        if result.log:
            (bus.info if result.ok else bus.warn)("timer", result.log)
        if result.ok:
            self.request_refresh()

    # -- scheduler -----------------------------------------------------------

    async def _scheduler(self) -> None:
        await self._ensure_dashboard_is_last()
        while not self.is_closed():
            try:
                async with self._lock:
                    await self.tick()
                self.last_tick_ok = now()
            except Exception as exc:  # keep the loop alive no matter what
                log.exception("tick failed")
                bus.error("error", f"Scheduler tick failed: {exc!r}")
            # Wake on the next :00/:30 boundary, or sooner when asked.
            delay = TICK_SECONDS - (time.time() % TICK_SECONDS) + 0.5
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    async def tick(self) -> None:
        t = now()
        with db.session() as s:
            settings = get_settings(s)
            state = get_state(s)
            last = state.last_tick_at or (t - TICK_SECONDS)
            if last > t:
                last = t - TICK_SECONDS
            due = engine.due_slots(s, t, last)
            to_send = []
            skipped = []
            for d in due:
                if d.action == "skip":
                    s.add(SentMessage(event_id=d.occ.event.id, timer_id=d.occ.timer_id, occurrence_at=d.occ.at,
                                      slot=d.slot.lead_minutes, status="skipped", sent_at=t))
                    skipped.append(f"{d.occ.event.name} ({_slot_name(d.slot.lead_minutes)}, due {engine.ts(d.send_at, 'f')})")
                else:
                    content, role_id = engine.render_alert(d.occ, d.slot)
                    to_send.append((d.occ.event.id, d.occ.event.name, d.occ.timer_id, d.occ.at,
                                    d.slot.lead_minutes, content, role_id))
            channel_id = settings.timer_channel_id
            keep = settings.keep_occurrences

        if skipped:
            head = f"Skipped {len(skipped)} missed alert(s): "
            bus.warn("skipped", head + "; ".join(skipped[:15]) + (" …" if len(skipped) > 15 else ""))

        posted = 0
        channel = await self._channel(channel_id)
        for event_id, name, timer_id, occ_at, lead, content, role_id in to_send:
            msg_id, status = None, "sent"
            if channel is None:
                status = "failed"
                bus.error("error", f"Timer channel {channel_id} not reachable; {name} alert not sent.")
            else:
                mentions = discord.AllowedMentions(
                    everyone=False, users=False, replied_user=False,
                    roles=[discord.Object(role_id)] if role_id else False,
                )
                try:
                    msg = await channel.send(content, allowed_mentions=mentions)
                    msg_id = msg.id
                    posted += 1
                    bus.info("alert", f"Sent {name} {_slot_name(lead)} alert" + (" with ping." if role_id else "."))
                except discord.HTTPException as exc:
                    status = "failed"
                    bus.error("error", f"Could not send {name} alert: {exc!r}")
            with db.session() as s:
                s.add(SentMessage(message_id=msg_id, channel_id=channel_id if msg_id else None, event_id=event_id,
                                  timer_id=timer_id, occurrence_at=occ_at, slot=lead, status=status, sent_at=now()))

        with db.session() as s:
            for timer in s.scalars(select(UserTimer).where(UserTimer.status == "active", UserTimer.fire_at <= t)):
                timer.status = "done"
            get_state(s).last_tick_at = t
            names = {e.id: e.name for e in engine.load_events(s)}
            doomed = [(m.id, m.channel_id, m.message_id,
                       f"{names.get(m.event_id, f'event {m.event_id}')} {_slot_name(m.slot)} alert "
                       f"for {engine.ts(m.occurrence_at, 'f')}")
                      for m in engine.messages_to_delete(s, keep, t)]

        await self._cleanup(doomed)
        repost = posted > 0 or self._force_repost
        self._force_repost = False
        await self.update_dashboard(repost=repost)
        try:
            await self.apply_quiet_hours()
        except Exception as exc:
            log.exception("quiet hours failed")
            bus.error("roles", f"Quiet hours update failed: {exc!r}")
        try:
            await self.pf.tick()
        except Exception as exc:  # the party finder must never stop timer alerts
            log.exception("party finder tick failed")
            bus.error("party", f"Party finder tick failed: {exc!r}")

    async def _cleanup(self, doomed: list[tuple[int, int | None, int, str]]) -> None:
        for row_id, channel_id, message_id, what in doomed:
            channel = await self._channel(channel_id or 0)
            ok = True
            if channel is not None:
                try:
                    await channel.get_partial_message(message_id).delete()
                    bus.info("cleanup", f"Deleted {what} (message {message_id}): older than the last "
                                        f"occurrences kept.", discord=False)
                except discord.NotFound:
                    pass
                except discord.HTTPException as exc:
                    ok = False
                    log.warning("cleanup failed for %s: %r", message_id, exc)
            if ok:
                with db.session() as s:
                    row = s.get(SentMessage, row_id)
                    if row:
                        row.deleted = True

    # -- dashboard -----------------------------------------------------------

    async def _ensure_dashboard_is_last(self) -> None:
        with db.session() as s:
            settings, state = get_settings(s), get_state(s)
            channel_id, dash_id = settings.timer_channel_id, state.dashboard_message_id
        channel = await self._channel(channel_id)
        if channel is None:
            bus.error("error", f"Timer channel {channel_id} not reachable.")
            return
        last_id = getattr(channel, "last_message_id", None)
        self._force_repost = not dash_id or (last_id is not None and last_id != dash_id)

    @staticmethod
    def _is_alert(message_id: int) -> bool:
        with db.session() as s:
            return s.scalar(select(SentMessage.id).where(SentMessage.message_id == message_id).limit(1)) is not None

    async def update_dashboard(self, repost: bool = False) -> None:
        with db.session() as s:
            settings, state = get_settings(s), get_state(s)
            dash = engine.build_dashboard(s, settings, now())
            channel_id, old_id, old_hash = settings.timer_channel_id, state.dashboard_message_id, state.dashboard_hash
        if not repost and old_id and dash.digest == old_hash:
            return
        channel = await self._channel(channel_id)
        if channel is None:
            return
        view = dashboard_view(dash.bosses)
        new_id = old_id
        try:
            if repost or not old_id:
                if old_id and self._is_alert(old_id):
                    bus.error("error", f"Stored dashboard ID {old_id} is an alert message; not deleting it.")
                elif old_id:
                    try:
                        await channel.get_partial_message(old_id).delete()
                        log.info("deleted previous dashboard %s", old_id)
                    except discord.NotFound:
                        pass
                msg = await channel.send(dash.text, view=view, allowed_mentions=NO_MENTIONS)
                new_id = msg.id
            else:
                try:
                    await channel.get_partial_message(old_id).edit(content=dash.text, view=view,
                                                                   allowed_mentions=NO_MENTIONS)
                except discord.NotFound:
                    msg = await channel.send(dash.text, view=view, allowed_mentions=NO_MENTIONS)
                    new_id = msg.id
        except discord.HTTPException as exc:
            bus.error("error", f"Dashboard update failed: {exc!r}")
            return
        finally:
            if view is not None:
                view.stop()  # buttons are served by the dynamic items, not this view
        with db.session() as s:
            state = get_state(s)
            state.dashboard_message_id, state.dashboard_hash = new_id, dash.digest

    # -- reaction roles --------------------------------------------------------

    def _guild(self):
        return self.get_guild(self.guild_id) if self.guild_id else (self.guilds[0] if self.guilds else None)

    async def post_roles_message(self) -> str:
        """Repost everything in the roles channel, top to bottom: the header,
        a copy of the class selector, one message per role message group
        (split past 20 reactions), then the Remove all pings message."""
        guild = self._guild()
        with db.session() as s:
            settings, state = get_settings(s), get_state(s)
            everything = reaction_roles.entries(s)
            missing = {e.role_id: e.label for e in everything if guild is None or guild.get_role(e.role_id) is None}
            plan = reaction_roles.messages(s, skip=set(missing))
            class_text, class_view = party.CLASS_PANEL, self.pf._class_view(s)
            header = (settings.roles_header or "").strip()
            channel_id = settings.roles_channel_id
            old_ids = {int(k) for k in (state.reaction_messages or {})} | {
                x for x in (state.reaction_message_id, state.roles_header_message_id, state.roles_class_message_id,
                            state.roles_clear_message_id) if x}
        if missing:
            bus.warn("roles", f"Skipped entries whose role does not exist: {', '.join(sorted(missing.values()))}.")
        channel = await self._channel(channel_id)
        if channel is None:
            class_view.stop()
            return "Roles channel not reachable."
        for old in old_ids:
            try:
                await channel.get_partial_message(old).delete()
            except discord.HTTPException:
                pass
        header_msg = await channel.send(header[:2000], allowed_mentions=NO_MENTIONS) if header else None
        try:
            class_msg = await channel.send(class_text, view=class_view, allowed_mentions=NO_MENTIONS)
        finally:
            class_view.stop()
        posted: dict[str, dict[str, int]] = {}
        messages = []
        for text, chunk in plan:
            msg = await channel.send(text, allowed_mentions=NO_MENTIONS)
            posted[str(msg.id)] = {e.emoji: e.role_id for e in chunk}
            messages.append((msg, chunk))
        clear_view = discord.ui.View(timeout=None)
        for what in ("mine", "quiet", "quietoff", "clear"):
            clear_view.add_item(PingSettingsButton(what))
        try:
            clear_msg = await channel.send(reaction_roles.CLEAR_TEXT, view=clear_view, allowed_mentions=NO_MENTIONS)
        finally:
            clear_view.stop()
        with db.session() as s:
            state = get_state(s)
            state.reaction_messages = posted
            state.reaction_message_id, state.reaction_map = 0, {}
            state.roles_header_message_id = header_msg.id if header_msg else 0
            state.roles_class_message_id, state.roles_clear_message_id = class_msg.id, clear_msg.id
        for msg, chunk in messages:
            for e in chunk:
                try:
                    await msg.add_reaction(e.emoji)
                except discord.HTTPException as exc:
                    bus.error("roles", f"Could not add reaction {e.emoji} for {e.label}: {exc!r}")
                await asyncio.sleep(REACTION_PAUSE)  # keeps the reactions in order
        roles = sum(len(c) for _, c in messages)
        bus.info("roles", f"Roles channel posted: {'header, ' if header else ''}class selector, "
                          f"{roles} ping roles in {len(messages)} message(s), remove-all.")
        return f"Posted {roles} ping roles in {len(messages)} message(s)."

    async def clear_pings(self, interaction: discord.Interaction) -> None:
        """Remove every event and dungeon ping role from the clicker, and
        their reactions on the role messages so they can react again."""
        member = interaction.user
        with db.session() as s:
            ping_ids = reaction_roles.ping_role_ids(s)
            reaction_msgs = dict(get_state(s).reaction_messages or {})
            channel_id = get_settings(s).roles_channel_id
        held = {r.id for r in getattr(member, "roles", [])} & ping_ids
        if not held:
            await interaction.response.send_message("You don't have any ping roles.", ephemeral=True)
            return
        try:
            await member.remove_roles(*[discord.Object(r) for r in held], reason="Nyerk: remove all pings")
        except discord.HTTPException as exc:
            bus.error("roles", f"Remove all pings for {member.display_name} failed: {exc!r}")
            await interaction.response.send_message("I couldn't remove your roles. An admin has been told.",
                                                    ephemeral=True)
            return
        await interaction.response.send_message(f"Removed {len(held)} ping role{'s' if len(held) != 1 else ''}.",
                                                ephemeral=True)
        bus.info("roles", f"{member.display_name} removed all their ping roles ({len(held)}).")
        tracking.remove_pings(member.id, held)
        channel = await self._channel(channel_id)
        if channel is None:
            return
        self._clearing.add(member.id)
        try:
            for mid, mapping in reaction_msgs.items():
                for emoji, role_id in mapping.items():
                    if int(role_id) in held:
                        try:
                            await channel.get_partial_message(int(mid)).remove_reaction(emoji, member)
                        except discord.HTTPException:
                            pass  # needs Manage Messages in the roles channel; roles are gone either way
        finally:
            self._clearing.discard(member.id)

    async def create_content_roles(self, content_ids: list[int]) -> list[str]:
        """Create a mentionable role for each dungeon without one."""
        guild = self._guild()
        if guild is None:
            raise RuntimeError("Not connected to a server")
        made = []
        for cid in content_ids:
            with db.session() as s:
                c = s.get(PfContent, cid)
                if c is None or c.role_id:
                    continue
                name = c.name
            role = await guild.create_role(name=name[:100], mentionable=True,
                                           reason="Nyerk dungeon ping role (party finder)")
            with db.session() as s:
                c = s.get(PfContent, cid)
                if c is not None:
                    c.role_id = role.id
            made.append(name)
            bus.info("roles", f"Created dungeon role {name} ({role.id}).")
        return made

    async def fetch_member(self, user_id: int) -> tuple[str, set[int]] | None:
        """Display name and role IDs for one member, straight from Discord.
        Uses a normal API lookup, so no privileged intent is needed."""
        guild = self._guild()
        if guild is None:
            return None
        try:
            m = guild.get_member(user_id) or await guild.fetch_member(user_id)
        except discord.NotFound:
            return None
        return m.display_name, {r.id for r in m.roles}

    async def admin_apply_roles(self, user_id: int, add: set[int], remove: set[int]) -> list[str]:
        """Add and remove roles for one member; returns error lines."""
        guild = self._guild()
        if guild is None:
            return ["Not connected to the server."]
        errors = []
        for rid, adding in [(r, False) for r in remove] + [(r, True) for r in add]:
            try:
                if adding:
                    await self.http.add_role(guild.id, user_id, rid, reason="Nyerk admin: member page")
                else:
                    await self.http.remove_role(guild.id, user_id, rid, reason="Nyerk admin: member page")
            except discord.HTTPException as exc:
                role = guild.get_role(rid)
                errors.append(f"{'Add' if adding else 'Remove'} {role.name if role else rid}: {exc.text or exc}")
        return errors

    async def show_my_roles(self, interaction: discord.Interaction) -> None:
        """Private overview: classes, ping roles, quiet hours, registration."""
        member = interaction.user
        held = {r.id for r in getattr(member, "roles", [])}
        with db.session() as s:
            text = my_roles_text(s, member.id, held, now())
        await interaction.response.send_message(text, ephemeral=True, allowed_mentions=NO_MENTIONS)

    # -- quiet hours -------------------------------------------------------------

    async def open_quiet_hours(self, interaction: discord.Interaction) -> None:
        with db.session() as s:
            q = quiet.get(s, interaction.user.id)
            default_tz = get_settings(s).default_tz
            if not quiet.content_role_ids(s):
                await interaction.response.send_message("There are no dungeon ping roles yet.", ephemeral=True)
                return
            start, end = (q.start_min // 60, q.end_min // 60) if q else (23, 8)
            tz = q.tz if q else default_tz
        if tz not in dict(quiet.ZONES):
            tz = "America/New_York"
        await interaction.response.send_modal(QuietHoursModal(start, end, tz))

    async def save_quiet_hours(self, interaction: discord.Interaction, start_h: int, end_h: int, tz: str) -> None:
        member = interaction.user
        held = {r.id for r in getattr(member, "roles", [])}
        try:
            with db.session() as s:
                q = quiet.set_hours(s, member.id, member.display_name, start_h, end_h, tz, held)
                text = quiet.window_text(q)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(
            f"Quiet hours set: {text}. Your dungeon ping roles are paused during that time and come back after. "
            "Event pings are not affected.", ephemeral=True)
        bus.info("roles", f"{member.display_name} set quiet hours: {text}.")
        await self.apply_quiet_hours(only=member.id)

    async def quiet_hours_off(self, interaction: discord.Interaction) -> None:
        with db.session() as s:
            q = quiet.turn_off(s, interaction.user.id)
        if not q:
            await interaction.response.send_message("You don't have quiet hours set.", ephemeral=True)
            return
        await interaction.response.send_message("Quiet hours turned off. Any paused dungeon roles are back.",
                                                ephemeral=True)
        bus.info("roles", f"{interaction.user.display_name} turned off quiet hours.")
        await self.apply_quiet_hours(only=interaction.user.id)

    async def apply_quiet_hours(self, only: int | None = None) -> None:
        guild = self._guild()
        if guild is None:
            return
        with db.session() as s:
            changes = [c for c in quiet.transitions(s, now()) if only is None or c.user_id == only]
        for c in changes:
            done: set[int] = set()
            for role_id in c.role_ids:
                try:
                    if c.to_state == "quiet":
                        await self.http.remove_role(guild.id, c.user_id, role_id, reason="Nyerk quiet hours")
                    else:
                        await self.http.add_role(guild.id, c.user_id, role_id, reason="Nyerk quiet hours ended")
                    done.add(role_id)
                except discord.NotFound:
                    done.add(role_id)  # member or role gone; nothing to retry
                except discord.HTTPException as exc:
                    bus.error("roles", f"Quiet hours for {c.user_name}: role {role_id} failed: {exc!r}")
            with db.session() as s:
                quiet.confirm(s, c.user_id, c.to_state, done)
            if done or c.role_ids == set():
                verb = "paused" if c.to_state == "quiet" else "restored"
                bus.info("roles", f"Quiet hours: {verb} {len(done)} dungeon role(s) for {c.user_name}.", discord=False)

    async def _reaction(self, payload: discord.RawReactionActionEvent, add: bool) -> None:
        if self.user and payload.user_id == self.user.id or payload.guild_id is None:
            return
        if not add and payload.user_id in self._clearing:
            return  # Remove all pings is clearing this member's reactions
        with db.session() as s:
            state = get_state(s)
            mapping = (state.reaction_messages or {}).get(str(payload.message_id))
            if mapping is None and payload.message_id == state.reaction_message_id:
                mapping = state.reaction_map or {}  # message posted by an older version
            if mapping is None:
                return
            role_id = mapping.get(str(payload.emoji))
        if not role_id:
            return
        guild = self.get_guild(payload.guild_id)
        role = guild.get_role(int(role_id)) if guild else None
        if add:
            with db.session() as s:
                hold = quiet.is_quiet(s, payload.user_id) and int(role_id) in quiet.content_role_ids(s)
            if hold:  # granted when the member's quiet hours end
                tracking.add_ping(payload.user_id, payload.member.display_name if payload.member else "",
                                  int(role_id), suspended=True)
                bus.info("roles", f"Held {role.name if role else role_id} for "
                                  f"{payload.member.display_name if payload.member else payload.user_id} "
                                  f"until their quiet hours end.", discord=False)
                return
        try:
            if add:
                await self.http.add_role(payload.guild_id, payload.user_id, int(role_id), reason="Nyerk reaction role")
            else:
                await self.http.remove_role(payload.guild_id, payload.user_id, int(role_id), reason="Nyerk reaction role")
        except discord.HTTPException as exc:
            bus.error("roles", f"Could not {'grant' if add else 'remove'} role {role_id}: {exc!r}")
            return
        who = payload.member.display_name if payload.member else f"user {payload.user_id}"
        if add:
            tracking.add_ping(payload.user_id, payload.member.display_name if payload.member else "", int(role_id))
        else:
            tracking.remove_pings(payload.user_id, {int(role_id)})
        bus.info("roles", f"{'Granted' if add else 'Removed'} {role.name if role else role_id} "
                          f"{'to' if add else 'from'} {who}.")

    async def on_raw_reaction_add(self, payload):
        await self._reaction(payload, True)

    async def on_raw_reaction_remove(self, payload):
        await self._reaction(payload, False)


def _slot_name(lead: int) -> str:
    return f"{lead} min" if lead else "event-time"



def my_roles_text(s, user_id: int, held: set[int], t: int) -> str:
    from .models import Category, PfInterest, RoleHolder

    lines = ["## Your roles"]
    classes = party.member_classes(s, held)
    lines.append("**Classes:** " + (", ".join(f"{c.emoji} {c.name} ({party.TYPE_LABEL[c.class_type]})".strip()
                                             for c in classes) or "none yet. Pick them on the class panel."))
    events = [c.name for c in s.scalars(select(Category).where(Category.role_id != 0)
                                        .order_by(Category.sort_order)) if c.role_id in held]
    lines.append("**Event pings:** " + (", ".join(events) or "none"))
    paused = {r.role_id for r in s.scalars(select(RoleHolder).where(RoleHolder.user_id == user_id,
                                                                    RoleHolder.suspended.is_(True)))}
    dungeons = []
    for c in party.contents(s, False):
        if c.role_id and c.role_id in held:
            dungeons.append(party.party_title_for(c))
        elif c.role_id and c.role_id in paused:
            dungeons.append(f"{party.party_title_for(c)} (paused)")
    lines.append("**Dungeon pings:** " + (", ".join(dungeons) or "none"))
    q = quiet.get(s, user_id)
    if q and q.enabled:
        lines.append(f"**Quiet hours:** {quiet.window_text(q)}" + (" · quiet now" if q.state == "quiet" else ""))
    else:
        lines.append("**Quiet hours:** off")
    reg = s.scalar(select(PfInterest).where(PfInterest.user_id == user_id, PfInterest.status == "active",
                                            PfInterest.expires_at > t))
    if reg:
        names = ", ".join(party.party_title_for(c) for c in party.contents(s, False) if c.id in (reg.content_ids or []))
        cls = reg.pf_class.name if reg.pf_class else "?"
        lines.append(f"**Looking for a group:** {names} as {cls}, until <t:{reg.expires_at}:t> (<t:{reg.expires_at}:R>)")
    p = party.live_party_for(s, user_id)
    if p:
        lines.append(f"**In a party:** {party.party_title(p)} ({p.status}, leader {p.leader_name})")
    return "\n".join(lines)
