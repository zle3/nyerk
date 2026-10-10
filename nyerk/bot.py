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

from . import db, engine, party_bot, timers
from .db import get_settings, get_state
from .logbus import bus
from .models import Category, SentMessage, UserTimer
from .timeparse import humanize

log = logging.getLogger("nyerk.bot")

TICK_SECONDS = 30
NUMBER_EMOJI = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
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
            text="Killed at", description="When it died. Leave both blank for 5 minutes ago.", component=self.killed_at))
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
    killed_at="When it died, e.g. '10 min ago' or '14:30'. Blank = 5 minutes ago.",
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

    # -- lifecycle ---------------------------------------------------------

    async def setup_hook(self) -> None:
        self.add_dynamic_items(BossButton, CustomTimeButton, *party_bot.ITEMS)
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
            doomed = [(m.id, m.channel_id, m.message_id) for m in engine.messages_to_delete(s, keep)]

        await self._cleanup(doomed)
        repost = posted > 0 or self._force_repost
        self._force_repost = False
        await self.update_dashboard(repost=repost)
        try:
            await self.pf.tick()
        except Exception as exc:  # the party finder must never stop timer alerts
            log.exception("party finder tick failed")
            bus.error("party", f"Party finder tick failed: {exc!r}")

    async def _cleanup(self, doomed: list[tuple[int, int | None, int]]) -> None:
        for row_id, channel_id, message_id in doomed:
            channel = await self._channel(channel_id or 0)
            ok = True
            if channel is not None:
                try:
                    await channel.get_partial_message(message_id).delete()
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
                if old_id:
                    try:
                        await channel.get_partial_message(old_id).delete()
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

    async def post_roles_message(self) -> str:
        with db.session() as s:
            settings, state = get_settings(s), get_state(s)
            cats = list(s.scalars(select(Category).where(Category.role_id != 0).order_by(Category.sort_order, Category.id)))
            pairs = [(c.name, c.role_id) for c in cats]
            channel_id, old_id = settings.roles_channel_id, state.reaction_message_id
        guild = self.get_guild(self.guild_id) if self.guild_id else (self.guilds[0] if self.guilds else None)
        missing = [n for n, r in pairs if guild is None or guild.get_role(r) is None]
        pairs = [(n, r) for n, r in pairs if n not in missing][: len(NUMBER_EMOJI)]
        if missing:
            bus.warn("roles", f"Skipped categories whose role does not exist: {', '.join(missing)}.")
        channel = await self._channel(channel_id)
        if channel is None:
            return "Roles channel not reachable."
        if not pairs:
            return "No category has a valid role yet."
        if old_id:
            try:
                await channel.get_partial_message(old_id).delete()
            except discord.HTTPException:
                pass
        lines = ["React to get pinged for an event category. Remove your reaction to stop."]
        lines += [f"{NUMBER_EMOJI[i]} {name}" for i, (name, _) in enumerate(pairs)]
        msg = await channel.send("\n".join(lines), allowed_mentions=NO_MENTIONS)
        mapping = {NUMBER_EMOJI[i]: role_id for i, (_, role_id) in enumerate(pairs)}
        with db.session() as s:
            state = get_state(s)
            state.reaction_message_id, state.reaction_map = msg.id, mapping
        for i in range(len(pairs)):
            await msg.add_reaction(NUMBER_EMOJI[i])
            await asyncio.sleep(REACTION_PAUSE)  # keeps the reactions in numeric order
        bus.info("roles", f"Reaction roles message posted with {len(pairs)} categories.")
        return f"Posted with {len(pairs)} categories."

    async def _reaction(self, payload: discord.RawReactionActionEvent, add: bool) -> None:
        if self.user and payload.user_id == self.user.id or payload.guild_id is None:
            return
        with db.session() as s:
            state = get_state(s)
            if payload.message_id != state.reaction_message_id:
                return
            role_id = (state.reaction_map or {}).get(str(payload.emoji))
        if not role_id:
            return
        guild = self.get_guild(payload.guild_id)
        role = guild.get_role(int(role_id)) if guild else None
        try:
            if add:
                await self.http.add_role(payload.guild_id, payload.user_id, int(role_id), reason="Nyerk reaction role")
            else:
                await self.http.remove_role(payload.guild_id, payload.user_id, int(role_id), reason="Nyerk reaction role")
        except discord.HTTPException as exc:
            bus.error("roles", f"Could not {'grant' if add else 'remove'} role {role_id}: {exc!r}")
            return
        who = payload.member.display_name if payload.member else f"user {payload.user_id}"
        bus.info("roles", f"{'Granted' if add else 'Removed'} {role.name if role else role_id} "
                          f"{'to' if add else 'from'} {who}.")

    async def on_raw_reaction_add(self, payload):
        await self._reaction(payload, True)

    async def on_raw_reaction_remove(self, payload):
        await self._reaction(payload, False)


def _slot_name(lead: int) -> str:
    return f"{lead} min" if lead else "event-time"
