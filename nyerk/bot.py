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

from . import db, engine, games, linking, party, party_bot, quiet, reaction_roles, safety, servers, timers, tracking
from .db import get_settings, get_state
from .logbus import bus, current_guild, for_guild
from .models import Event, Game, PfBracket, PfClass, PfContent, SentMessage, UserTimer
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
    def __init__(self, event_id: int, label: str = "") -> None:
        super().__init__(
            discord.ui.Button(
                label=(label or "Boss died")[:80],
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
    def __init__(self, label: str = "Custom time") -> None:
        super().__init__(
            discord.ui.Button(label=label[:80], style=discord.ButtonStyle.primary, custom_id="nyerk:custom")
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls()

    async def callback(self, interaction: discord.Interaction) -> None:
        with db.session() as s:
            game = games.by_timer_channel(s, interaction.channel_id, interaction.guild_id)  # this dashboard's game
            bosses = [(b.id, b.name) for b in timers.boss_events(s, game_id=game.id)][:25] if game else []
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


class OverrideView(discord.ui.View):
    """Private confirmation before replacing someone else's boss timer."""

    def __init__(self, client, user_id: int, event_id: int, timer_id: int, killed_at: str, respawns_in: str):
        super().__init__(timeout=120)
        self.client, self.user_id, self.event_id, self.timer_id = client, user_id, event_id, timer_id
        self.killed_at, self.respawns_in = killed_at, respawns_in

    @discord.ui.button(label="Override their timer", style=discord.ButtonStyle.danger)
    async def override(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return
        self.stop()
        await self.client.confirm_override(interaction, self.event_id, self.timer_id, self.killed_at,
                                           self.respawns_in)

    @discord.ui.button(label="Keep theirs", style=discord.ButtonStyle.secondary)
    async def keep(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(content="Kept the existing timer. Nothing changed.", view=None)


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
    for event_id, label in bosses[:25]:
        view.add_item(CustomTimeButton(label) if event_id == 0 else BossButton(event_id, label))
    return view


# ----------------------------------------------------------------------------
# Slash commands


nyerk_group = app_commands.Group(name="nyerk", description="Boss timers", guild_only=True)


def boss_choices(s, guild_id: int) -> list[tuple[str, str]]:
    """(label, value) for every enabled boss in a server; labels name the
    game when it runs several, values are event IDs so same-named bosses differ."""
    names = {g.id: g.name for g in games.all_games(s, guild_id, enabled_only=True)}
    many = len(names) > 1
    return [(f"{names[b.game_id]}: {b.name}" if many else b.name, str(b.id))
            for b in timers.boss_events(s) if b.game_id in names]


async def boss_autocomplete(interaction: discord.Interaction, current: str):
    with db.session() as s:
        choices = boss_choices(s, interaction.guild_id or 0)
    cur = current.lower()
    return [app_commands.Choice(name=n[:100], value=v) for n, v in choices if cur in n.lower()][:25]


async def my_timer_autocomplete(interaction: discord.Interaction, current: str):
    with db.session() as s:
        rows = timers.user_timers(s, interaction.user.id)
        t = now()
        choices = [(f"{r.event.name} — in {humanize(r.fire_at - t)}", str(r.id)) for r in rows]
    cur = current.lower()
    return [app_commands.Choice(name=n[:100], value=v) for n, v in choices if cur in n.lower()][:25]


@nyerk_group.command(name="worldboss", description="Report a world boss kill and start its respawn timer (replaces your own)")
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
        all_games = games.all_games(s, interaction.guild_id or 0)
        rows = [r for r in timers.user_timers(s, interaction.user.id) if r.event.game_id in {g.id for g in all_games}]
        many = len(all_games) > 1
        names = {g.id: g.name for g in all_games}
        lines = [f"{names.get(r.event.game_id, '?') + ': ' if many else ''}{r.event.name}: "
                 f"{engine.ts(r.fire_at, 'F')} ({engine.ts(r.fire_at, 'R')})" for r in rows]
        limit = f"/{all_games[0].user_timer_limit}" if len(all_games) == 1 else ""
    text = "\n".join(lines) if lines else "You have no active timers."
    await interaction.response.send_message(f"{text}\nActive timers: {len(lines)}{limit}.",
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
        self._force_repost: set[int] = set()  # game IDs whose dashboard is reposted next tick
        self._lock = asyncio.Lock()
        self._tasks: list[asyncio.Task] = []
        self._started = False
        self.last_tick_ok: int = 0
        self.pf = party_bot.PartyFinder(self)
        self._clearing: set[int] = set()

    # -- lifecycle ---------------------------------------------------------

    async def setup_hook(self) -> None:
        self.add_dynamic_items(BossButton, CustomTimeButton, PingSettingsButton, *party_bot.ITEMS)
        self.tree.interaction_check = self._approved_only
        await self.tree.sync()  # global commands; approved servers also get a copy at once (on_ready)
        if bus.queue is None:
            bus.attach_queue()
        self._tasks.append(asyncio.create_task(self._log_drain(), name="nyerk-logs"))

    async def on_ready(self) -> None:
        if self._started:
            bus.info("startup", "Reconnected to Discord.", discord=False)
            self.request_refresh()
            return
        self._started = True
        with db.session() as s:
            # A pre-servers install without GUILD_ID belongs to the one server the bot is in.
            if len(self.guilds) == 1 and servers.claim_legacy(s, self.guilds[0].id, self.guilds[0].name):
                bus.info("startup", f"Data from before servers now belongs to {self.guilds[0].name}.", discord=False)
            for g in self.guilds:
                servers.seen(s, g.id, g.name, auto_approve=g.id == self.guild_id)
            approved = servers.approved_ids(s)
        names = ", ".join(g.name for g in self.guilds) or "no servers"
        bus.info("startup", f"Nyerk online as {self.user} in {names}.", discord=False)
        for g in self.guilds:
            if g.id in approved:
                await self.sync_commands(g.id)
        self._tasks.append(asyncio.create_task(self._scheduler(), name="nyerk-scheduler"))

    async def sync_commands(self, guild_id: int) -> None:
        """Commands show up in a server at once this way (global ones take a while)."""
        try:
            guild = discord.Object(guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        except discord.HTTPException as exc:
            log.warning("command sync for %s failed: %r", guild_id, exc)

    async def on_guild_join(self, guild) -> None:
        with db.session() as s:
            row = servers.seen(s, guild.id, guild.name, auto_approve=guild.id == self.guild_id)
            ok = row.status == servers.APPROVED
        if ok:
            await self.sync_commands(guild.id)
        bus.warn("servers", f"Nyerk was added to {guild.name} ({guild.id})" +
                 ("." if ok else ": it's waiting for the host to approve it on the Servers page."), discord=False)

    async def on_guild_remove(self, guild) -> None:
        bus.warn("servers", f"Nyerk was removed from {guild.name} ({guild.id}). Its data stays until the host "
                            "deletes it on the Servers page.", discord=False)

    async def on_guild_update(self, before, after) -> None:
        if before.name != after.name:
            with db.session() as s:
                row = servers.get(s, after.id)
                if row is not None:
                    row.guild_name = after.name[:100]

    async def _approved_only(self, interaction: discord.Interaction) -> bool:
        """Slash commands only work in servers the host approved."""
        current_guild.set(interaction.guild_id or 0)
        with db.session() as s:
            ok = servers.is_approved(s, interaction.guild_id)
        if not ok:
            await interaction.response.send_message(
                "Nyerk isn't set up in this server yet: its host needs to approve it.", ephemeral=True)
        return ok

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

    def request_refresh(self, repost: bool = False, game_id: int | None = None) -> None:
        """Wake the scheduler now (timer or config changed). repost moves the
        dashboard of game_id (or every game's) to the bottom of its channel."""
        if repost:
            if game_id is None:
                with db.session() as s:
                    self._force_repost |= {g.id for g in games.all_games(s, None)}
            else:
                self._force_repost.add(game_id)
        self._wake.set()  # web admin and bot share one event loop

    def status(self, guild_id: int | None = None) -> dict:
        guild = self.get_guild(guild_id) if guild_id else None
        return {
            "connected": self.is_ready() and not self.is_closed(),
            "user": str(self.user) if self.user else "",
            "guild": guild.name if guild else "",
            "guilds": len(self.guilds),
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
        """Each server's lines go to its own #logs, batched."""
        assert bus.queue is not None
        while True:
            batch = [await bus.queue.get()]
            await asyncio.sleep(1.0)  # batch bursts into one message per server
            while not bus.queue.empty() and len(batch) < 100:
                batch.append(bus.queue.get_nowait())
            dropped = bus.take_dropped()
            by_guild: dict[int, list[str]] = {}
            for guild_id, line in batch:
                by_guild.setdefault(guild_id, []).append(line)
            for guild_id, lines in by_guild.items():
                if dropped:
                    lines.append(f"⚠️ **logs** some lines were skipped here; see the Logs page.")
                with db.session() as s:
                    row = servers.get(s, guild_id)
                    channel_id = row.log_channel_id if row and row.status == servers.APPROVED else 0
                ch = await self._channel(channel_id) if self.is_ready() else None
                if ch is None:
                    continue
                for chunk in _chunks(lines, 1900):
                    try:
                        await ch.send(chunk, allowed_mentions=NO_MENTIONS)
                    except discord.HTTPException as exc:
                        log.warning("could not write to #logs: %r", exc)

    # -- reports -------------------------------------------------------------

    async def handle_boss_report(self, interaction: discord.Interaction, boss: str,
                                 killed_at: str = "", respawns_in: str = "") -> None:
        name = interaction.user.display_name
        current_guild.set(interaction.guild_id or 0)
        with db.session() as s:
            event = timers.find_boss(s, boss, interaction.guild_id or 0)
            if event is None:
                await interaction.response.send_message(f"No enabled world boss called {boss!r}.", ephemeral=True,
                                                        allowed_mentions=NO_MENTIONS)
                return
            result = timers.report(s, event, interaction.user.id, name, now(), killed_at, respawns_in)
            conflict = (result.conflict.id, result.conflict.user_name) if result.conflict else None
            event_id = event.id
        if conflict:
            view = OverrideView(self, interaction.user.id, event_id, conflict[0], killed_at, respawns_in)
            await interaction.response.send_message(result.reply, view=view, ephemeral=True,
                                                    allowed_mentions=NO_MENTIONS)
            return
        await interaction.response.send_message(result.reply, ephemeral=True, allowed_mentions=NO_MENTIONS)
        if result.log:
            (bus.warn if result.log_level == "warn" or not result.ok else bus.info)("timer", result.log)
        if result.ok:
            self.request_refresh()

    async def confirm_override(self, interaction: discord.Interaction, event_id: int, timer_id: int,
                               killed_at: str, respawns_in: str) -> None:
        name = interaction.user.display_name
        current_guild.set(interaction.guild_id or 0)
        with db.session() as s:
            event = s.get(Event, event_id)
            if event is not None and s.get(Game, event.game_id).guild_id != interaction.guild_id:
                event = None
            if event is None:
                await interaction.response.edit_message(content="That boss no longer exists.", view=None)
                return
            result = timers.report(s, event, interaction.user.id, name, now(), killed_at, respawns_in,
                                   override_id=timer_id)
            if result.conflict:  # the timer changed while they were deciding
                await interaction.response.edit_message(
                    content="That timer changed in the meantime. Report again to see the current one.", view=None)
                return
        await interaction.response.edit_message(content=result.reply, view=None)
        if result.log:
            (bus.warn if result.log_level == "warn" or not result.ok else bus.info)("timer", result.log)
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
            state = get_state(s)
            live_games = games.all_games(s, None)  # approved servers' games
            channels = {g.id: g.timer_channel_id for g in live_games}
            retention = {g.id: g.alert_retention_min for g in live_games}
            guild_of = {g.id: g.guild_id for g in live_games}
            last = state.last_tick_at or (t - TICK_SECONDS)
            if last > t:
                last = t - TICK_SECONDS
            due = engine.due_slots(s, t, last)
            to_send = []
            skipped = []
            held = []
            for d in due:
                if d.action == "maintenance":  # the game is down then: never sent, not a missed alert
                    s.add(SentMessage(event_id=d.occ.event.id, timer_id=d.occ.timer_id, occurrence_at=d.occ.at,
                                      slot=d.slot.lead_minutes, status="skipped", sent_at=t))
                    held.append((guild_of.get(d.occ.event.game_id, 0),
                                 f"{d.occ.event.name} ({_slot_name(d.slot.lead_minutes)})"))
                elif d.action == "skip":
                    s.add(SentMessage(event_id=d.occ.event.id, timer_id=d.occ.timer_id, occurrence_at=d.occ.at,
                                      slot=d.slot.lead_minutes, status="skipped", sent_at=t))
                    skipped.append((guild_of.get(d.occ.event.game_id, 0),
                                    f"{d.occ.event.name} ({_slot_name(d.slot.lead_minutes)}, "
                                    f"due {engine.ts(d.send_at, 'f')})"))
                else:
                    content, role_id = engine.render_alert(d.occ, d.slot)
                    if role_id and not safety.pingable(role_id):  # @everyone: never ping it
                        content, role_id = content.replace(f"<@&{role_id}>", "").strip(), 0
                    to_send.append((d.occ.event.game_id, d.occ.event.id, d.occ.event.name, d.occ.timer_id,
                                    d.occ.at, d.slot.lead_minutes, content, role_id))

        for guild_id in sorted({g for g, _ in held}):
            with for_guild(guild_id):
                bus.info("maintenance", "Not sent, the game is in maintenance then: " +
                         "; ".join(line for g, line in held if g == guild_id), discord=False)
        for guild_id in sorted({g for g, _ in skipped}):
            mine = [line for g, line in skipped if g == guild_id]
            with for_guild(guild_id):
                bus.warn("skipped", f"Skipped {len(mine)} missed alert(s): " + "; ".join(mine[:15]) +
                         (" …" if len(mine) > 15 else ""))

        posted: set[int] = set()  # games that got an alert: their dashboard moves below it
        for game_id, event_id, name, timer_id, occ_at, lead, content, role_id in to_send:
            current_guild.set(guild_of.get(game_id, 0))  # this alert's log lines belong to its server
            channel_id = channels.get(game_id, 0)
            channel = await self._channel(channel_id)
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
                    posted.add(game_id)
                    # Scheduled alerts (Shugo, Rift…) stay on the web Logs page; boss alerts
                    # come from member reports and also go to #logs.
                    bus.info("alert", f"Sent {name} {_slot_name(lead)} alert" + (" with ping." if role_id else "."),
                             discord=bool(timer_id))
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
                      for gid, minutes in retention.items() for m in engine.messages_to_delete(s, minutes, t, gid)]

        current_guild.set(0)
        await self._cleanup(doomed)
        repost = posted | self._force_repost
        self._force_repost = set()
        await self.update_dashboards(repost)
        try:
            await self.apply_quiet_hours()
        except Exception as exc:
            log.exception("quiet hours failed")
            bus.error("roles", f"Quiet hours update failed: {exc!r}")
        try:
            # Offline longer than a missed tick or two: an update or restart.
            await self.pf.tick(downtime=max(0, t - last - TICK_SECONDS) if t - last > 3 * TICK_SECONDS else 0)
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
                    bus.info("cleanup", f"Deleted {what} (message {message_id}): event ended over the "
                                        f"retention time.", discord=False)
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
            dashes = [(g.id, g.name, g.timer_channel_id, g.dashboard_message_id)
                      for g in games.all_games(s, None, enabled_only=True) if g.timer_channel_id]
        for game_id, name, channel_id, dash_id in dashes:
            channel = await self._channel(channel_id)
            if channel is None:
                bus.error("error", f"{name} timer channel {channel_id} not reachable.")
                continue
            last_id = getattr(channel, "last_message_id", None)
            if not dash_id or (last_id is not None and last_id != dash_id):
                self._force_repost.add(game_id)

    @staticmethod
    def _is_alert(message_id: int) -> bool:
        with db.session() as s:
            return s.scalar(select(SentMessage.id).where(SentMessage.message_id == message_id).limit(1)) is not None

    async def update_dashboards(self, repost: set[int] | bool = frozenset()) -> None:
        """Every enabled game's dashboard; repost (True = all) moves those to
        the bottom of their channel."""
        with db.session() as s:
            ids = [(g.id, g.guild_id) for g in games.all_games(s, None, enabled_only=True)]
        for game_id, guild_id in ids:
            with for_guild(guild_id):
                await self.update_dashboard(game_id, repost=repost is True or (bool(repost) and game_id in repost))

    async def update_dashboard(self, game_id: int, repost: bool = False) -> None:
        with db.session() as s:
            game = s.get(Game, game_id)
            if game is None:
                return
            dash = engine.build_dashboard(s, game, now())
            channel_id, old_id, old_hash = game.timer_channel_id, game.dashboard_message_id, game.dashboard_hash
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
            game = s.get(Game, game_id)
            if game is not None:
                game.dashboard_message_id, game.dashboard_hash = new_id, dash.digest

    # -- reaction roles --------------------------------------------------------

    def _guild(self, guild_id: int):
        return self.get_guild(guild_id) if guild_id else None

    async def post_roles_message(self, guild_id: int) -> str:
        """Repost everything in the roles channel, top to bottom: the header,
        a copy of the class selector, one message per role message group
        (split past 20 reactions), then the Remove all pings message."""
        guild = self._guild(guild_id)
        current_guild.set(guild_id)
        with db.session() as s:
            settings = state = get_settings(s, guild_id)  # the roles channel's messages live on the server row
            everything = reaction_roles.entries(s, guild_id)
            missing = {e.role_id: e.label for e in everything if guild is None or guild.get_role(e.role_id) is None}
            plan = reaction_roles.messages(s, guild_id, skip=set(missing))
            live = games.all_games(s, guild_id, enabled_only=True)
            many = len(live) > 1
            # Each game's class and gear score selectors, when it has classes with roles.
            panels = [(g.id, party.class_panel_text(g), self.pf._class_view(s, g.id),
                       party.gear_panel_text(g, many), party_bot.gear_view(s, g.id)) for g in live
                      if party.classes_for(s, g)]
            header = (settings.roles_header or "").strip()
            channel_id = settings.roles_channel_id
            old_ids = {int(k) for k in (state.reaction_messages or {})} | {
                x for x in (state.reaction_message_id, state.roles_header_message_id, state.roles_clear_message_id)
                if x} | {x for g in games.all_games(s, guild_id)
                         for x in (g.roles_class_message_id, g.roles_gear_message_id) if x}
        if missing:
            bus.warn("roles", f"Skipped entries whose role does not exist: {', '.join(sorted(missing.values()))}.")
        channel = await self._channel(channel_id)
        if channel is None:
            for _, _, cv, _, gv in panels:
                cv.stop()
                gv.stop()
            return "Roles channel not reachable."
        for old in old_ids:
            try:
                await channel.get_partial_message(old).delete()
            except discord.HTTPException:
                pass
        header_msg = await channel.send(header[:2000], allowed_mentions=NO_MENTIONS) if header else None
        panel_ids: dict[int, tuple[int, int]] = {}
        for game_id, class_text, class_view, gear_text, gear_v in panels:
            try:
                class_msg = await channel.send(class_text, view=class_view, allowed_mentions=NO_MENTIONS)
            finally:
                class_view.stop()
            gear_id = 0
            try:
                if gear_v is not None:
                    gear_id = (await channel.send(gear_text, view=gear_v, allowed_mentions=NO_MENTIONS)).id
            finally:
                if gear_v is not None:
                    gear_v.stop()
            panel_ids[game_id] = (class_msg.id, gear_id)
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
            state = get_settings(s, guild_id)
            state.reaction_messages = posted
            state.reaction_message_id, state.reaction_map = 0, {}
            state.roles_header_message_id = header_msg.id if header_msg else 0
            state.roles_clear_message_id = clear_msg.id
            for g in games.all_games(s, guild_id):
                g.roles_class_message_id, g.roles_gear_message_id = panel_ids.get(g.id, (0, 0))
        for msg, chunk in messages:
            for e in chunk:
                try:
                    await msg.add_reaction(e.emoji)
                except discord.HTTPException as exc:
                    bus.error("roles", f"Could not add reaction {e.emoji} for {e.label}: {exc!r}")
                await asyncio.sleep(REACTION_PAUSE)  # keeps the reactions in order
        roles = sum(len(c) for _, c in messages)
        bus.info("roles", f"Roles channel posted: {'header, ' if header else ''}class selector, gear score, "
                          f"{roles} ping roles in {len(messages)} message(s), remove-all.")
        return f"Posted {roles} ping roles in {len(messages)} message(s)."

    async def clear_pings(self, interaction: discord.Interaction) -> None:
        """Remove every event and dungeon ping role from the clicker, and
        their reactions on the role messages so they can react again."""
        member, guild_id = interaction.user, interaction.guild_id or 0
        current_guild.set(guild_id)
        with db.session() as s:
            if not servers.is_approved(s, guild_id):
                return
            server = get_settings(s, guild_id)
            ping_ids = reaction_roles.ping_role_ids(s, guild_id)
            reaction_msgs = dict(server.reaction_messages or {})
            channel_id = server.roles_channel_id
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
        tracking.remove_pings(guild_id, member.id, held)
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

    async def delete_messages(self, messages: list[tuple[int, int]]) -> None:
        """Best effort: take down Nyerk's messages for a deleted game."""
        for channel_id, message_id in messages:
            channel = await self._channel(channel_id)
            if channel is None:
                continue
            try:
                await channel.get_partial_message(message_id).delete()
            except discord.HTTPException:
                pass

    async def create_content_roles(self, guild_id: int, content_ids: list[int], game_id: int | None = None) -> list[str]:
        """Create a mentionable role for each of a server's dungeons without
        one. game_id: a linked game, whose dungeons are another server's;
        the new roles are this server's roles for them."""
        return await self._create_pf_roles(guild_id, "content", content_ids, game_id)

    async def create_class_roles(self, guild_id: int, class_ids: list[int], game_id: int | None = None) -> list[str]:
        """Create a role for each of a server's classes without one (not
        mentionable: class roles are never pinged). game_id as above."""
        return await self._create_pf_roles(guild_id, "class", class_ids, game_id)

    async def _create_pf_roles(self, guild_id: int, kind: str, ids: list[int], game_id: int | None) -> list[str]:
        model = PfContent if kind == "content" else PfClass
        guild = self._guild(guild_id)
        if guild is None:
            raise RuntimeError("Not connected to a server")
        made = []
        for rid in ids:
            with db.session() as s:
                row = s.get(model, rid)
                g = s.get(Game, game_id) if game_id else None
                if row is None:
                    continue
                if g is not None:
                    seen = party.contents_for(s, g, False) if kind == "content" else party.classes_for(s, g)
                    if g.guild_id != guild_id or g.link_home_id != row.game_id or \
                            any(x.id == rid and x.role_id for x in seen):
                        continue
                elif row.role_id or s.get(Game, row.game_id).guild_id != guild_id:
                    continue
                name = row.name
            what = "dungeon ping role" if kind == "content" else "class role"
            role = await guild.create_role(name=name[:100], mentionable=kind == "content",
                                           reason=f"Nyerk {what} (party finder)")
            with db.session() as s:
                row = s.get(model, rid)
                if game_id:
                    linking.set_role(s, s.get(Game, game_id), kind, rid, role.id)
                elif row is not None:
                    row.role_id = role.id
            made.append(name)
            bus.info("roles", f"Created {what} {name} ({role.id}).")
        return made

    async def create_channels(self, guild_id: int, game_id: int, kinds: list[str]) -> list[str]:
        """Create the channels Nyerk needs that aren't set yet (CHANNELS),
        in a "Nyerk" category, with members' permissions set for each, and
        save their IDs. Returns the names made."""
        guild = self._guild(guild_id)
        if guild is None:
            raise RuntimeError("Not connected to a server")
        with db.session() as s:
            st, g = get_settings(s, guild_id), s.get(Game, game_id)
            if g is None or g.guild_id != guild_id:
                return []
            current = {"roles": st.roles_channel_id, "logs": st.log_channel_id,
                       "timer": g.timer_channel_id, "party": g.party_channel_id}
            many = len(games.all_games(s, guild_id)) > 1
            game_name = g.name
            admin_roles = {int(x) for x in (st.admin_role_ids or "").replace(" ", "").split(",") if x.isdigit()}
        todo = [k for k in CHANNELS if k in kinds and not current[k]]
        if not todo:
            return []
        category = discord.utils.get(guild.categories, name="Nyerk") or \
            await guild.create_category("Nyerk", reason="Nyerk channels")
        made = []
        for kind in todo:
            name, members_post, private = CHANNELS[kind]
            if kind in ("timer", "party") and many:
                name = f"{_slug(game_name)}-{name}"
            everyone = discord.PermissionOverwrite(view_channel=not private, read_message_history=not private,
                                                   send_messages=members_post, add_reactions=False)
            if kind == "party":
                everyone.add_reactions = None  # members' chat: the server's usual reactions
            overwrites = {guild.default_role: everyone,
                          guild.me: discord.PermissionOverwrite(
                              view_channel=True, send_messages=True, read_message_history=True,
                              manage_messages=True, add_reactions=True, embed_links=True)}
            if private:
                for rid in admin_roles:
                    role = guild.get_role(rid)
                    if role is not None:
                        overwrites[role] = discord.PermissionOverwrite(view_channel=True, read_message_history=True)
            channel = await guild.create_text_channel(name, category=category, overwrites=overwrites,
                                                      reason="Nyerk channel")
            with db.session() as s:
                if kind in ("roles", "logs"):
                    setattr(get_settings(s, guild_id), "roles_channel_id" if kind == "roles" else "log_channel_id",
                            channel.id)
                else:
                    setattr(s.get(Game, game_id), f"{kind}_channel_id", channel.id)
            made.append(f"#{channel.name}")
            bus.info("config", f"Created channel #{channel.name} ({kind}).")
        return made

    async def fetch_member(self, guild_id: int, user_id: int) -> tuple[str, set[int]] | None:
        """Display name and role IDs for one member of a server, straight from
        Discord. Uses a normal API lookup, so no privileged intent is needed."""
        guild = self._guild(guild_id)
        if guild is None:
            return None
        try:
            m = guild.get_member(user_id) or await guild.fetch_member(user_id)
        except discord.NotFound:
            return None
        return m.display_name, {r.id for r in m.roles}

    async def member_access(self, guild_id: int, user_id: int) -> tuple[str, set[int], bool, bool] | None:
        """(display name, role IDs, is the server's owner, has Manage Server)
        for one member of a server; None when they aren't in it."""
        guild = self._guild(guild_id)
        if guild is None:
            return None
        try:
            m = guild.get_member(user_id) or await guild.fetch_member(user_id)
        except discord.HTTPException:
            return None
        return m.display_name, {r.id for r in m.roles}, guild.owner_id == user_id, m.guild_permissions.manage_guild

    def server_ids(self) -> set[int]:
        return {g.id for g in self.guilds}

    async def admin_apply_roles(self, guild_id: int, user_id: int, add: set[int], remove: set[int]) -> list[str]:
        """Add and remove roles for one member of a server; returns error lines."""
        guild = self._guild(guild_id)
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

    async def set_gear(self, interaction: discord.Interaction, bracket_id: int | None,
                       game_id: int | None = None) -> None:
        """Pick a gear score bracket, or clear the game's (bracket_id None)."""
        member = interaction.user
        current_guild.set(interaction.guild_id or 0)
        try:
            with db.session() as s:
                b = s.get(PfBracket, bracket_id) if bracket_id else None
                gid = b.game_id if b else game_id
                g = linking.view_in_guild(s, gid, interaction.guild_id) if gid else None
                if g is None:
                    raise party.PartyError("That gear score option isn't part of this server.")
                bracket = party.set_gear(s, member.id, member.display_name, bracket_id, now(), party.pf_id(g),
                                         guild_id=interaction.guild_id)
                label = bracket.label if bracket else ""
        except party.PartyError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        text = f"Gear score set to **{label}**." if label else "Gear score cleared."
        await interaction.response.send_message(text, ephemeral=True)
        bus.info("party", f"{member.display_name}: gear score {label or 'cleared'}.", discord=False)
        await self.pf.sync()  # listings and the interest panel show the new score

    async def show_my_roles(self, interaction: discord.Interaction) -> None:
        """Private overview: classes, ping roles, quiet hours, registration."""
        member = interaction.user
        held = {r.id for r in getattr(member, "roles", [])}
        with db.session() as s:
            text = my_roles_text(s, interaction.guild_id or 0, member.id, held, now())
        await interaction.response.send_message(text, ephemeral=True, allowed_mentions=NO_MENTIONS)

    # -- quiet hours -------------------------------------------------------------

    async def open_quiet_hours(self, interaction: discord.Interaction) -> None:
        guild_id = interaction.guild_id or 0
        with db.session() as s:
            q = quiet.get(s, guild_id, interaction.user.id)
            default_tz = get_settings(s, guild_id).default_tz
            if not quiet.content_role_ids(s, guild_id):
                await interaction.response.send_message("There are no dungeon ping roles yet.", ephemeral=True)
                return
            start, end = (q.start_min // 60, q.end_min // 60) if q else (23, 8)
            tz = q.tz if q else default_tz
        if tz not in dict(quiet.ZONES):
            tz = "America/New_York"
        await interaction.response.send_modal(QuietHoursModal(start, end, tz))

    async def save_quiet_hours(self, interaction: discord.Interaction, start_h: int, end_h: int, tz: str) -> None:
        member, guild_id = interaction.user, interaction.guild_id or 0
        current_guild.set(guild_id)
        held = {r.id for r in getattr(member, "roles", [])}
        try:
            with db.session() as s:
                q = quiet.set_hours(s, guild_id, member.id, member.display_name, start_h, end_h, tz, held)
                text = quiet.window_text(q)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(
            f"Quiet hours set: {text}. Your dungeon ping roles are paused during that time and come back after. "
            "Event pings are not affected.", ephemeral=True)
        bus.info("roles", f"{member.display_name} set quiet hours: {text}.")
        await self.apply_quiet_hours(only=(guild_id, member.id))

    async def quiet_hours_off(self, interaction: discord.Interaction) -> None:
        guild_id = interaction.guild_id or 0
        current_guild.set(guild_id)
        with db.session() as s:
            q = quiet.turn_off(s, guild_id, interaction.user.id)
        if not q:
            await interaction.response.send_message("You don't have quiet hours set.", ephemeral=True)
            return
        await interaction.response.send_message("Quiet hours turned off. Any paused dungeon roles are back.",
                                                ephemeral=True)
        bus.info("roles", f"{interaction.user.display_name} turned off quiet hours.")
        await self.apply_quiet_hours(only=(guild_id, interaction.user.id))

    async def apply_quiet_hours(self, only: tuple[int, int] | None = None) -> None:
        """Pause or give back dungeon ping roles; `only` = (server, member)."""
        with db.session() as s:
            approved = servers.approved_ids(s)
            changes = [c for c in quiet.transitions(s, now(), approved)
                       if only is None or (c.guild_id, c.user_id) == only]
        for c in changes:
            guild = self._guild(c.guild_id)
            if guild is None:
                continue
            current_guild.set(c.guild_id)
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
                quiet.confirm(s, c.guild_id, c.user_id, c.to_state, done)
            if done or c.role_ids == set():
                verb = "paused" if c.to_state == "quiet" else "restored"
                bus.info("roles", f"Quiet hours: {verb} {len(done)} dungeon role(s) for {c.user_name}.", discord=False)

    async def _reaction(self, payload: discord.RawReactionActionEvent, add: bool) -> None:
        if self.user and payload.user_id == self.user.id or payload.guild_id is None:
            return
        if not add and payload.user_id in self._clearing:
            return  # Remove all pings is clearing this member's reactions
        current_guild.set(payload.guild_id)
        with db.session() as s:
            state = servers.get(s, payload.guild_id)
            if state is None or state.status != servers.APPROVED:
                return
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
                hold = quiet.is_quiet(s, payload.guild_id, payload.user_id) and \
                    int(role_id) in quiet.content_role_ids(s, payload.guild_id)
            if hold:  # granted when the member's quiet hours end
                tracking.add_ping(payload.guild_id, payload.user_id, payload.member.display_name if payload.member else "",
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
            tracking.add_ping(payload.guild_id, payload.user_id,
                              payload.member.display_name if payload.member else "", int(role_id))
        else:
            tracking.remove_pings(payload.guild_id, payload.user_id, {int(role_id)})
        bus.info("roles", f"{'Granted' if add else 'Removed'} {role.name if role else role_id} "
                          f"{'to' if add else 'from'} {who}.")

    async def on_raw_reaction_add(self, payload):
        await self._reaction(payload, True)

    async def on_raw_reaction_remove(self, payload):
        await self._reaction(payload, False)


def _chunks(lines: list[str], size: int) -> list[str]:
    """Lines joined into messages of at most `size` characters."""
    out, cur = [], ""
    for line in lines:
        line = line[:size]
        if cur and len(cur) + 1 + len(line) > size:
            out.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    return out + ([cur] if cur else [])


# Channels Nyerk can create: kind -> (name, members may post, private).
# The timer and roles channels are read-only for members (buttons, reactions
# on Nyerk's own reactions and commands still work); members chat in the
# party channel; the log channel is for admins only.
CHANNELS = {
    "timer": ("timers", False, False),
    "party": ("party-finder", True, False),
    "roles": ("roles", False, False),
    "logs": ("nyerk-logs", False, True),
}


def _slug(name: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "game"


def _slot_name(lead: int) -> str:
    return f"{lead} min" if lead else "event-time"



def my_roles_text(s, guild_id: int, user_id: int, held: set[int], t: int) -> str:
    from .models import Category, PfInterest, RoleHolder

    lines = ["## Your roles"]
    all_games = games.all_games(s, guild_id, enabled_only=True)
    many = len(all_games) > 1
    paused = {r.role_id for r in s.scalars(select(RoleHolder).where(RoleHolder.guild_id == guild_id,
                                                                    RoleHolder.user_id == user_id,
                                                                    RoleHolder.suspended.is_(True)))}
    for g in all_games:
        if many:
            lines.append(f"### {g.name}")
        classes = party.member_classes(s, g.id, held)
        lines.append("**Classes:** " + (", ".join(f"{c.emoji} {c.name} ({party.TYPE_LABEL[c.class_type]})".strip()
                                                 for c in classes) or "none yet. Pick them on the class panel."))
        hid = party.pf_id(g)
        if party.brackets(s, hid):
            gear = party.gear_of(s, user_id, hid)
            lines.append("**Gear score:** " + (gear.label if gear else "not set. Pick it in the roles channel."))
        events = [c.name for c in s.scalars(select(Category).where(Category.role_id != 0, Category.game_id == g.id)
                                            .order_by(Category.sort_order)) if c.role_id in held]
        lines.append("**Event pings:** " + (", ".join(events) or "none"))
        dungeons = []
        for c in party.contents_for(s, g, False):
            if c.role_id and c.role_id in held:
                dungeons.append(party.party_title_for(c))
            elif c.role_id and c.role_id in paused:
                dungeons.append(f"{party.party_title_for(c)} (paused)")
        lines.append("**Dungeon pings:** " + (", ".join(dungeons) or "none"))
        if not many:
            lines.append(_quiet_line(s, guild_id, user_id))  # quiet hours cover every game's dungeon pings
        reg = s.scalar(select(PfInterest).where(PfInterest.user_id == user_id, PfInterest.status == "active",
                                                PfInterest.expires_at > t, PfInterest.game_id == hid))
        if reg:
            names = ", ".join(party.party_title_for(c) for c in party.contents(s, hid, False)
                              if c.id in (reg.content_ids or []))
            cls = reg.pf_class.name if reg.pf_class else "?"
            lines.append(f"**Looking for a group:** {names} as {cls}, until <t:{reg.expires_at}:t> "
                         f"(<t:{reg.expires_at}:R>)")
        p = party.live_party_for(s, user_id, hid)
        if p:
            lines.append(f"**In a party:** {party.party_title(p)} ({p.status}, leader {p.leader_name})")
    if many:
        lines.append("### Every game")
        lines.append(_quiet_line(s, guild_id, user_id))
    return "\n".join(lines)


def _quiet_line(s, guild_id: int, user_id: int) -> str:
    q = quiet.get(s, guild_id, user_id)
    if q and q.enabled:
        return f"**Quiet hours:** {quiet.window_text(q)}" + (" · quiet now" if q.state == "quiet" else "")
    return "**Quiet hours:** off"
