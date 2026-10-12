"""Party finder on Discord: panels, buttons, forms and the reconciler.

Handlers change the database through party.py and then call sync(), which
makes #aion-2-party-finder match the database: it posts, edits and deletes
listings, pings, warnings, ready pings, the interest summary and the status
message. The scheduler tick calls sync() too, so anything changed from the
web admin, or missed during a restart, is fixed within 30 seconds.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import discord
from sqlalchemy import select

from . import db, party, safety, tracking
from .db import get_settings, get_state
from .logbus import bus
from .models import PfInterest, PfParty
from .party import PartyError, Who

if TYPE_CHECKING:
    from .bot import NyerkBot

log = logging.getLogger("nyerk.party")
NO_MENTIONS = discord.AllowedMentions.none()


def now() -> int:
    return int(time.time())


def who_of(interaction: discord.Interaction) -> Who:
    user = interaction.user
    roles = {r.id for r in getattr(user, "roles", [])} or set(getattr(user, "_roles", []))
    return Who(user.id, user.display_name, set(roles))


def pf(interaction: discord.Interaction) -> "PartyFinder":
    return interaction.client.pf


async def reply(interaction: discord.Interaction, text: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True, allowed_mentions=NO_MENTIONS)
    else:
        await interaction.response.send_message(text, ephemeral=True, allowed_mentions=NO_MENTIONS)


def _btn(custom_id: str, label: str, style=discord.ButtonStyle.secondary, emoji: str | None = None):
    return discord.ui.Button(custom_id=custom_id, label=label[:80], style=style, emoji=emoji or None)


# ----------------------------------------------------------------------------
# Persistent buttons. One DynamicItem per action, IDs under nyerk:pf:.


class ClassButton(discord.ui.DynamicItem[discord.ui.Button], template=r"nyerk:pf:class:(?P<id>\d+|clear|mine)"):
    def __init__(self, key: str, label: str = "", emoji: str = "") -> None:
        style = {"clear": discord.ButtonStyle.danger, "mine": discord.ButtonStyle.primary}.get(
            key, discord.ButtonStyle.secondary)
        super().__init__(_btn(f"nyerk:pf:class:{key}", label or "Class", style, emoji))
        self.key = key

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["id"])

    async def callback(self, interaction):
        if self.key == "mine":
            await interaction.client.show_my_roles(interaction)
            return
        await pf(interaction).on_class(interaction, None if self.key == "clear" else int(self.key))


class PanelButton(discord.ui.DynamicItem[discord.ui.Button], template=r"nyerk:pf:(?P<what>reg|unreg|create)"):
    LABELS = {"reg": ("Register", discord.ButtonStyle.primary), "unreg": ("Cancel my registration",
              discord.ButtonStyle.secondary), "create": ("Create party", discord.ButtonStyle.primary)}

    def __init__(self, what: str) -> None:
        label, style = self.LABELS[what]
        super().__init__(_btn(f"nyerk:pf:{what}", label, style))
        self.what = what

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["what"])

    async def callback(self, interaction):
        await getattr(pf(interaction), f"on_{self.what}")(interaction)


class PartyButton(discord.ui.DynamicItem[discord.ui.Button],
                  template=r"nyerk:pf:(?P<what>join|leave|close|pass|extend|done):(?P<pid>\d+)"):
    LABELS = {
        "join": ("Join", discord.ButtonStyle.success), "leave": ("Leave", discord.ButtonStyle.secondary),
        "close": ("Close", discord.ButtonStyle.danger), "pass": ("Not this time", discord.ButtonStyle.secondary),
        "extend": ("Extend", discord.ButtonStyle.primary), "done": ("Done", discord.ButtonStyle.secondary),
    }

    def __init__(self, what: str, pid: int, label: str = "") -> None:
        base, style = self.LABELS[what]
        super().__init__(_btn(f"nyerk:pf:{what}:{pid}", label or base, style))
        self.what, self.pid = what, pid

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["what"], int(match["pid"]))

    async def callback(self, interaction):
        await pf(interaction).on_party_action(interaction, self.what, self.pid)


class KickSelect(discord.ui.DynamicItem[discord.ui.Select], template=r"nyerk:pf:kick:(?P<pid>\d+)"):
    def __init__(self, pid: int, members: list[tuple[int, str]] | None = None) -> None:
        options = [discord.SelectOption(label=name[:100], value=str(uid)) for uid, name in (members or [])][:25]
        super().__init__(discord.ui.Select(
            custom_id=f"nyerk:pf:kick:{pid}", placeholder="Leader: remove a member",
            options=options or [discord.SelectOption(label="-", value="0")],
        ))
        self.pid = pid

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["pid"]))

    async def callback(self, interaction):
        values = (interaction.data or {}).get("values") or []
        await pf(interaction).on_kick(interaction, self.pid, int(values[0]) if values else 0)


class GearButton(discord.ui.DynamicItem[discord.ui.Button], template=r"nyerk:roles:gear:(?P<id>\d+|clear)"):
    def __init__(self, key: str, label: str = "") -> None:
        style = discord.ButtonStyle.danger if key == "clear" else discord.ButtonStyle.secondary
        super().__init__(discord.ui.Button(label=(label or "Gear")[:80], style=style,
                                           custom_id=f"nyerk:roles:gear:{key}"))
        self.key = key

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["id"])

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.client.set_gear(interaction, None if self.key == "clear" else int(self.key))




ITEMS = (ClassButton, PanelButton, PartyButton, KickSelect, GearButton)


def gear_view(s) -> discord.ui.View:
    """Gear score buttons; used in the party finder and roles channels."""
    view = discord.ui.View(timeout=None)
    for b in party.brackets(s)[:24]:
        view.add_item(GearButton(str(b.id), b.label))
    view.add_item(GearButton("clear", "Clear"))
    return view


def _view(*items: discord.ui.Item) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for it in items:
        view.add_item(it)
    return view


# ----------------------------------------------------------------------------
# Forms


def _select(placeholder: str, options: list[tuple[str, str]], many: bool = False, default: str | None = None):
    opts = [discord.SelectOption(label=label[:100], value=value, default=value == default) for label, value in options][:25]
    return discord.ui.Select(placeholder=placeholder, options=opts, min_values=1,
                             max_values=len(opts) if many else 1)


class RegisterModal(discord.ui.Modal, title="Register for a group"):
    def __init__(self, window_opts, content_opts, class_opts=None) -> None:
        super().__init__(timeout=600)
        self.cls = _select("Class you'll play", class_opts, default=class_opts[0][1]) if class_opts else None
        if self.cls is not None:
            self.add_item(discord.ui.Label(text="Playing as", component=self.cls,
                                           description="You hold several classes; pick one for this run."))
        self.window = _select("How long you are free", window_opts)
        self.content = _select("Content you want", content_opts, many=True)
        self.add_item(discord.ui.Label(text="Available for", component=self.window,
                                       description="You will be pinged when a party opens for this content."))
        self.add_item(discord.ui.Label(text="Content", component=self.content,
                                       description="Pick one or more."))

    async def on_submit(self, interaction):
        await pf(interaction).submit_register(
            interaction, int(self.window.values[0]), [int(v) for v in self.content.values],
            int(self.cls.values[0]) if self.cls is not None else None)


class CreateModal(discord.ui.Modal, title="Create a party"):
    def __init__(self, content_opts) -> None:
        super().__init__(timeout=600)
        self.content = _select("Content", content_opts)
        self.size = _select("Party size", [("5 players", "5"), ("20 players", "20")], default="5")
        self.req = _select("Requirements", [("Open: any class in any slot", "none"),
                                            ("Standard: 1 tank, 1 support, 3 DPS", "default"),
                                            ("Custom", "custom")], default="none")
        self.custom = discord.ui.TextInput(placeholder="e.g. 2/4/14", required=False, max_length=10)
        self.desc = discord.ui.TextInput(placeholder="e.g. fast clear, know the mechanics", required=False,
                                         max_length=100)
        self.add_item(discord.ui.Label(text="Content", component=self.content))
        self.add_item(discord.ui.Label(text="Party size", component=self.size))
        self.add_item(discord.ui.Label(text="Requirements", component=self.req))
        self.add_item(discord.ui.Label(text="Custom makeup", component=self.custom,
                                       description="Tank/Support/DPS counts. Only used with Custom."))
        self.add_item(discord.ui.Label(text="Description", component=self.desc))

    async def on_submit(self, interaction):
        await pf(interaction).submit_create(
            interaction, int(self.content.values[0]), int(self.size.values[0]), self.req.values[0],
            self.custom.value, self.desc.value)


# ----------------------------------------------------------------------------


class PartyFinder:
    def __init__(self, bot: "NyerkBot") -> None:
        self.bot = bot
        self._sync_lock = asyncio.Lock()
        self._chat_warned = False

    async def _channel(self):
        with db.session() as s:
            cid = get_settings(s).party_channel_id
        return await self.bot._channel(cid)

    async def _delete(self, channel, message_id: int) -> None:
        if channel is None or not message_id:
            return
        try:
            await channel.get_partial_message(message_id).delete()
        except discord.NotFound:
            pass
        except discord.HTTPException as exc:
            log.warning("could not delete %s: %r", message_id, exc)

    # -- handlers ---------------------------------------------------------------

    async def on_class(self, interaction, class_id: int | None) -> None:
        who = who_of(interaction)
        try:
            with db.session() as s:
                add, remove, text, held = party.class_change(s, who, class_id)
                held_roles = {c.role_id for c in held}
        except PartyError as exc:
            await reply(interaction, str(exc))
            return
        member = interaction.user
        try:
            if remove:
                await member.remove_roles(*[discord.Object(r) for r in remove], reason="Nyerk class selector")
            if add:
                await member.add_roles(*[discord.Object(r) for r in add], reason="Nyerk class selector")
        except discord.HTTPException as exc:
            bus.error("party", f"Class change for {who.user_name} failed: {exc!r}")
            await reply(interaction, "I couldn't change your roles. An admin has been told.")
            return
        await reply(interaction, text)
        bus.info("party", f"{who.user_name}: {text}")
        tracking.set_classes(who.user_id, who.user_name, held_roles)

    async def on_reg(self, interaction) -> None:
        with db.session() as s:
            held = party.member_classes(s, who_of(interaction).role_ids)
            has_class = bool(held)
            cls = [(f"{x.name} ({party.TYPE_LABEL[x.class_type]})", str(x.id)) for x in held] if len(held) > 1 else None
            has_gear = party.gear_of(s, interaction.user.id) is not None
            w = [(x.label or f"{x.minutes} min", str(x.id)) for x in party.windows(s)]
            c = [(party.party_title_for(x), str(x.id)) for x in party.contents(s)]
        if not has_class:
            await reply(interaction, "Pick at least one class on the class panel first.")
        elif not has_gear:
            await reply(interaction, "Set your gear score in the roles channel first, then register.")
        elif not (w and c):
            await reply(interaction, "Registration isn't set up yet (no content or time windows).")
        else:
            await interaction.response.send_modal(RegisterModal(w, c, cls))

    async def submit_register(self, interaction, window_id, content_ids, class_id=None) -> None:
        who = who_of(interaction)
        try:
            with db.session() as s:
                row = party.register(s, who, window_id, content_ids, now(), class_id)
                names = ", ".join(party.party_title_for(x) for x in party.contents(s, False) if x.id in row.content_ids)
                until = row.expires_at
        except PartyError as exc:
            await reply(interaction, str(exc))
            return
        await reply(interaction, f"Registered until <t:{until}:t> (<t:{until}:R>). You'll be pinged for: {names}.")
        bus.info("party", f"{who.user_name} registered for {names} until <t:{until}:t>.")
        await self.sync()

    async def on_unreg(self, interaction) -> None:
        who = who_of(interaction)
        with db.session() as s:
            row = party.cancel_registration(s, who.user_id)
        await reply(interaction, "Your registration is cancelled." if row else "You weren't registered.")
        if row:
            bus.info("party", f"{who.user_name} cancelled their registration.")
            await self.sync()

    async def on_create(self, interaction) -> None:
        who = who_of(interaction)
        with db.session() as s:
            has_class = party.member_class(s, who.role_ids) is not None
            busy = party.live_party_for(s, who.user_id) is not None
            c = [(party.party_title_for(x), str(x.id)) for x in party.contents(s)]
        if not has_class:
            await reply(interaction, "Pick at least one class on the class panel first.")
        elif busy:
            await reply(interaction, "You are already in a party. Leave it before starting another.")
        elif not c:
            await reply(interaction, "No content is set up yet.")
        else:
            await interaction.response.send_modal(CreateModal(c))

    async def submit_create(self, interaction, content_id, size, requirement, custom, description) -> None:
        who = who_of(interaction)
        try:
            with db.session() as s:
                p = party.create(s, who, content_id, size, requirement, custom, description, now())
                title, pid = party.party_title(p), p.id
        except PartyError as exc:
            await reply(interaction, str(exc))
            return
        await reply(interaction, f"Party created for {title}. It's listed below; registered members are being pinged.")
        bus.info("party", f"Party #{pid} created by {who.user_name}: {title}, {size} players, {requirement}.")
        await self.sync()

    async def on_party_action(self, interaction, what: str, pid: int) -> None:
        who = who_of(interaction)
        t = now()
        if what == "pass":
            await reply(interaction, "No problem. You stay registered for other parties.")
            return
        try:
            with db.session() as s:
                p = s.get(PfParty, pid)
                if p is None:
                    raise PartyError("That party no longer exists.")
                title = party.party_title(p)
                if what == "join":
                    filled = party.join(s, p, who, t)
                    msg = f"You joined {title}." + (" The party is now full!" if filled else "")
                    log_line = f"{who.user_name} joined party #{pid} ({title})." + (" Party is full." if filled else "")
                elif what == "leave":
                    res = party.remove_member(s, p, who.user_id, t)
                    msg = f"You left {title}."
                    log_line = f"{who.user_name} left party #{pid} ({title})." + _leave_note(res)
                elif what == "close":
                    party.require_leader(p, who.user_id)
                    if p.status != "open":
                        raise PartyError("That party is not open.")
                    party.end(p, "closed", t)
                    msg, log_line = f"Closed {title}.", f"Party #{pid} ({title}) closed by {who.user_name}."
                elif what == "extend":
                    party.require_leader(p, who.user_id)
                    party.extend(s, p, t)
                    msg = f"Extended {title}. It now closes <t:{p.expires_at}:R>."
                    log_line = f"Party #{pid} ({title}) extended by {who.user_name}."
                elif what == "done":
                    party.require_leader(p, who.user_id)
                    if p.status != "full":
                        raise PartyError("That party is not in the status list.")
                    party.end(p, "done", t)
                    msg, log_line = f"Marked {title} as done.", f"Party #{pid} ({title}) marked done by {who.user_name}."
                else:
                    return
        except PartyError as exc:
            await reply(interaction, str(exc))
            return
        await reply(interaction, msg)
        bus.info("party", log_line)
        await self.sync()

    async def on_kick(self, interaction, pid: int, user_id: int) -> None:
        who = who_of(interaction)
        try:
            with db.session() as s:
                p = s.get(PfParty, pid)
                if p is None:
                    raise PartyError("That party no longer exists.")
                party.require_leader(p, who.user_id)
                if user_id == who.user_id or not user_id:
                    raise PartyError("Use Leave to leave your own party.")
                name = next((m.user_name for m in p.members if m.user_id == user_id), "that member")
                res = party.remove_member(s, p, user_id, now())
                title = party.party_title(p)
        except PartyError as exc:
            await reply(interaction, str(exc))
            return
        await reply(interaction, f"Removed {name} from {title}.")
        bus.info("party", f"{who.user_name} removed {name} from party #{pid} ({title})." + _leave_note(res))
        await self.sync()

    # -- panels -------------------------------------------------------------------

    def _class_view(self, s) -> discord.ui.View:
        items = [ClassButton(str(c.id), c.name, c.emoji) for c in party.classes(s) if c.role_id][:23]
        return _view(*items, ClassButton("mine", "My roles", "📋"), ClassButton("clear", "Clear classes"))

    async def post_panels(self, which: str = "all") -> str:
        """Delete and repost panels in order: class, interest, create, status."""
        channel = await self._channel()
        if channel is None:
            return "Party channel not set or not reachable."
        kinds = ["class", "gear", "interest", "create", "status"] if which == "all" else [which]
        async with self._sync_lock:
            for kind in kinds:
                with db.session() as s:
                    state, settings = get_state(s), get_settings(s)
                    attr = {"class": "class_panel_message_id", "gear": "gear_panel_message_id",
                            "interest": "interest_panel_message_id",
                            "create": "party_panel_message_id", "status": "party_status_message_id"}[kind]
                    old = getattr(state, attr)
                    if kind == "class":
                        text, view = party.CLASS_PANEL, self._class_view(s)
                    elif kind == "gear":
                        text, view = party.GEAR_PANEL, gear_view(s)
                    elif kind == "interest":
                        text = party.interest_panel_text(s, now())
                        view = _view(PanelButton("reg"), PanelButton("unreg"))
                    elif kind == "create":
                        text = party.CREATE_PANEL.format(lifetime=settings.party_lifetime_min)
                        view = _view(PanelButton("create"))
                    else:
                        text, view = self._status(s)
                await self._delete(channel, old)
                try:
                    msg = await channel.send(text, view=view, allowed_mentions=NO_MENTIONS)
                finally:
                    view.stop()
                with db.session() as s:
                    state = get_state(s)
                    setattr(state, attr, msg.id)
                    if kind == "interest":
                        state.interest_panel_hash = party.digest(text)
                    if kind == "status":
                        state.party_status_hash = party.digest(text + _status_key(s))
        bus.info("party", f"Party finder panel(s) posted: {', '.join(kinds)}.")
        return f"Posted {', '.join(kinds)}."

    def _status(self, s):
        full = party.live_parties(s, "full")
        buttons = [PartyButton("done", p.id, f"Done: {party.party_title(p)} ({p.leader_name})"[:80]) for p in full[:25]]
        return party.status_text(full, get_settings(s).party_chat_delete_min), _view(*buttons)

    # -- reconciler ---------------------------------------------------------------

    async def tick(self) -> None:
        with db.session() as s:
            res = party.tick(s, now())
        for name in res.expired:
            bus.info("party", f"Party expired: {name}.")
        for name in res.done:
            bus.info("party", f"Party left the status list after the hold time: {name}.")
        if res.interests_expired:
            bus.info("party", f"{res.interests_expired} registration(s) expired.", discord=False)
        await self.sync()
        await self.clean_chat()

    async def clean_chat(self) -> None:
        """Members may chat in the party channel to plan runs; their messages
        are deleted once they are party_chat_delete_min old. Nyerk's own
        messages and pinned messages stay."""
        with db.session() as s:
            minutes = get_settings(s).party_chat_delete_min
        me = getattr(self.bot, "user", None)
        if not minutes or me is None:
            return
        channel = await self._channel()
        if channel is None:
            return
        t = now()
        cutoff = datetime.fromtimestamp(t - minutes * 60, timezone.utc)
        try:
            old = [m async for m in channel.history(limit=100, before=cutoff)
                   if m.author.id != me.id and not m.pinned]
            # Bulk delete only takes messages under 14 days old.
            recent = [m for m in old if m.created_at.timestamp() > t - 13 * 86400]
            if recent:
                await channel.delete_messages(recent)
            for m in old:
                if m not in recent:
                    await m.delete()
        except discord.Forbidden:
            if not self._chat_warned:
                self._chat_warned = True
                bus.error("party", "Can't delete member chat in the party channel: give Nyerk Manage Messages "
                                   "and Read Message History there.")
            return
        except discord.HTTPException as exc:
            log.warning("party chat cleanup failed: %r", exc)
            return
        self._chat_warned = False
        if old:
            bus.info("party", f"Deleted {len(old)} chat message(s) older than {minutes} minutes.", discord=False)

    async def sync(self) -> None:
        async with self._sync_lock:
            channel = await self._channel()
            if channel is None:
                return
            await self._sync_parties(channel)
            await self._sync_panel(channel, "interest")
            await self._sync_panel(channel, "status")

    async def _sync_panel(self, channel, kind: str) -> None:
        with db.session() as s:
            state = get_state(s)
            if kind == "interest":
                mid, old_hash = state.interest_panel_message_id, state.interest_panel_hash
                text, view = party.interest_panel_text(s, now()), _view(PanelButton("reg"), PanelButton("unreg"))
                key = party.digest(text)
            else:
                mid, old_hash = state.party_status_message_id, state.party_status_hash
                text, view = self._status(s)
                key = party.digest(text + _status_key(s))
        try:
            if not mid or key == old_hash:
                return  # panel not posted yet (admin posts it) or nothing changed
            try:
                await channel.get_partial_message(mid).edit(content=text, view=view, allowed_mentions=NO_MENTIONS)
            except discord.NotFound:
                msg = await channel.send(text, view=view, allowed_mentions=NO_MENTIONS)
                mid = msg.id
        except discord.HTTPException as exc:
            log.warning("panel %s update failed: %r", kind, exc)
            return
        finally:
            view.stop()
        with db.session() as s:
            state = get_state(s)
            if kind == "interest":
                state.interest_panel_message_id, state.interest_panel_hash = mid, key
            else:
                state.party_status_message_id, state.party_status_hash = mid, key

    async def _sync_parties(self, channel) -> None:
        with db.session() as s:
            settings = get_settings(s)
            rows = list(s.scalars(select(PfParty).where(
                (PfParty.status.in_(party.LIVE)) | (PfParty.listing_message_id != 0) | (PfParty.ping_message_id != 0)
                | (PfParty.warning_message_id != 0) | (PfParty.ready_message_id != 0)
            ).order_by(PfParty.created_at)))
            ids = [p.id for p in rows]
        for pid in ids:
            await self._sync_party(channel, pid, settings)

    async def _sync_party(self, channel, pid: int, settings) -> None:
        with db.session() as s:
            p = s.get(PfParty, pid)
            _ = p.members, p.content
            status = p.status
            listing_text = party.listing_text(p) if status == "open" else ""
            listing_key = party.digest(listing_text + "|" + ",".join(str(m.user_id) for m in p.members))
            kick = [(m.user_id, m.user_name) for m in p.members if m.user_id != p.leader_id]
            targets = party.ping_targets(s, p, now()) if status == "open" and p.ping_pending else []
            ping_role = safety.pingable(p.content.role_id) if p.content and status == "open" and p.ping_pending else 0
            ping_text = party.ping_text(p, targets, ping_role) if targets or ping_role else ""
            target_ids = [t.user_id for t in targets]
            warn = status == "open" and p.warned_at is not None
            warn_text = party.warning_text(p, settings) if warn else ""
            ready_text = party.ready_text(p) if status == "full" and not p.ready_pinged else ""
            member_ids = [m.user_id for m in p.members]
            leader_id = p.leader_id
            snap = dict(listing=p.listing_message_id, hash=p.listing_hash, ping=p.ping_message_id,
                        pending=p.ping_pending, warning=p.warning_message_id, ready=p.ready_message_id)
        upd: dict = {}

        # Listing: exists only while open; reposted at the bottom when reopened.
        if status != "open":
            for key in ("listing", "ping", "warning"):
                if snap[key]:
                    await self._delete(channel, snap[key])
                    upd[f"{key}_message_id"] = 0
        else:
            items = [PartyButton("join", pid), PartyButton("leave", pid), PartyButton("close", pid)]
            if kick:
                items.append(KickSelect(pid, kick))
            view = _view(*items)
            try:
                if not snap["listing"]:
                    msg = await channel.send(listing_text, view=view, allowed_mentions=NO_MENTIONS)
                    upd.update(listing_message_id=msg.id, listing_hash=listing_key)
                elif snap["hash"] != listing_key:
                    try:
                        await channel.get_partial_message(snap["listing"]).edit(
                            content=listing_text, view=view, allowed_mentions=NO_MENTIONS)
                    except discord.NotFound:
                        msg = await channel.send(listing_text, view=view, allowed_mentions=NO_MENTIONS)
                        upd["listing_message_id"] = msg.id
                    upd["listing_hash"] = listing_key
            except discord.HTTPException as exc:
                bus.error("party", f"Listing for party #{pid} failed: {exc!r}")
            finally:
                view.stop()
            # Interest ping, once, right under a new listing.
            if snap["pending"]:
                upd["ping_pending"] = False
                if ping_text:
                    view = _view(PartyButton("join", pid), PartyButton("pass", pid))
                    try:
                        msg = await channel.send(ping_text, view=view, allowed_mentions=discord.AllowedMentions(
                            everyone=False, users=[discord.Object(u) for u in target_ids],
                            roles=[discord.Object(ping_role)] if ping_role else False))
                        upd["ping_message_id"] = msg.id
                        upd["_pinged"] = target_ids
                    except discord.HTTPException as exc:
                        bus.error("party", f"Interest ping for party #{pid} failed: {exc!r}")
                    finally:
                        view.stop()
            # Leader warning.
            if warn and not snap["warning"]:
                view = _view(PartyButton("extend", pid), PartyButton("close", pid))
                try:
                    msg = await channel.send(warn_text, view=view, allowed_mentions=discord.AllowedMentions(
                        everyone=False, roles=False, users=[discord.Object(leader_id)]))
                    upd["warning_message_id"] = msg.id
                except discord.HTTPException as exc:
                    bus.error("party", f"Warning for party #{pid} failed: {exc!r}")
                finally:
                    view.stop()
            elif not warn and snap["warning"]:
                await self._delete(channel, snap["warning"])
                upd["warning_message_id"] = 0

        # Ready ping while full; removed when the party leaves the status list.
        if status == "full" and ready_text:
            try:
                msg = await channel.send(ready_text, allowed_mentions=discord.AllowedMentions(
                    everyone=False, roles=False, users=[discord.Object(u) for u in member_ids]))
                upd.update(ready_message_id=msg.id, ready_pinged=True)
            except discord.HTTPException as exc:
                bus.error("party", f"Ready ping for party #{pid} failed: {exc!r}")
        elif status != "full" and snap["ready"]:
            await self._delete(channel, snap["ready"])
            upd["ready_message_id"] = 0

        if upd:
            pinged = upd.pop("_pinged", [])
            with db.session() as s:
                p = s.get(PfParty, pid)
                for k, v in upd.items():
                    setattr(p, k, v)
                if pinged:
                    t = now()
                    for r in s.scalars(select(PfInterest).where(PfInterest.user_id.in_(pinged),
                                                                PfInterest.status == "active")):
                        r.last_pinged_at = t


def _leave_note(res: party.LeaveResult) -> str:
    bits = []
    if res.new_leader:
        bits.append(f"{res.new_leader} is now leader")
    if res.reopened:
        bits.append("party reopened")
    if res.closed:
        bits.append("party closed (empty)")
    return (" " + "; ".join(bits) + ".") if bits else ""


def _status_key(s) -> str:
    return ",".join(f"{p.id}:{p.leader_name}" for p in party.live_parties(s, "full"))
