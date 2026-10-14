"""The setup guide: where a server stands on each step of getting Nyerk
going, from picking the game to posting everything in Discord.

Steps are worked out from the database every time; nothing is stored. The
Discord side (can Nyerk see its channels, hand out its roles) comes from
the bot as a list of problems, or None when the bot isn't connected.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import party
from .db import get_settings
from .models import Category, Game


@dataclass
class Step:
    key: str
    title: str
    done: bool
    detail: str = ""
    optional: bool = False
    todo: list[str] = field(default_factory=list)


def steps(s: Session, guild_id: int, game: Game, problems: list[str] | None) -> list[Step]:
    st = get_settings(s, guild_id)
    out: list[Step] = [Step("game", "Pick the game", True, f"Nyerk runs {game.name} here. Add more on Games.")]

    channels = {"Timer": game.timer_channel_id, "Party finder": game.party_channel_id,
                "Roles": st.roles_channel_id, "Logs": st.log_channel_id}
    missing = [k for k, v in channels.items() if not v]
    out.append(Step("channels", "Channels", not missing,
                    "All four channels are set." if not missing else
                    "Nyerk can create them with the right permissions, or paste the IDs of your own on Settings.",
                    todo=[f"{k} channel" for k in missing]))

    if problems is None:
        out.append(Step("access", "Nyerk's permissions in Discord", False,
                        "The bot isn't connected, so this can't be checked yet."))
    else:
        out.append(Step("access", "Nyerk's permissions in Discord", not problems,
                        "Nyerk can see its channels and hand out its roles." if not problems else
                        "Fix these in Discord (Server Settings → Roles, or the channel's Permissions), "
                        "then reload this page.", todo=problems))

    cats = list(s.scalars(select(Category).where(Category.game_id == game.id).order_by(Category.sort_order)))
    no_role = [c.name for c in cats if not c.role_id]
    out.append(Step("ping_roles", "Event ping roles", bool(cats) and not no_role,
                    f"{len(cats) - len(no_role)} of {len(cats)} ping categories have a role. Members react in the "
                    "roles channel to get pinged." if cats else "No ping categories yet: add them on Roles.",
                    todo=no_role))

    classes = party.classes_for(s, game)
    dungeons = party.contents_for(s, game)
    types = {"Tank": game.tank_role_id, "Support": game.support_role_id, "DPS": game.dps_role_id}
    todo = [f"{c.name} (class)" for c in classes if not c.role_id] + \
        [f"{k} (type)" for k, v in types.items() if not v] + \
        [f"{party.party_title_for(c)} (dungeon)" for c in dungeons if not c.role_id]
    if game.link_home_id:
        detail = "This game shares another server's party finder: give its classes and dungeons this server's roles."
    elif not dungeons:
        detail = "Add the dungeons people group for on the Party finder page, then give them roles."
    else:
        detail = "Classes and Tank, Support and DPS roles for the class picker; a ping role per dungeon."
    out.append(Step("party_roles", "Party finder roles", bool(classes) and bool(dungeons) and not todo,
                    detail, todo=todo))

    posted = {"Dashboard": game.dashboard_message_id, "Roles channel": st.reaction_messages or
              st.reaction_message_id or game.roles_class_message_id, "Party finder panels": game.class_panel_message_id}
    unposted = [k for k, v in posted.items() if not v]
    out.append(Step("posted", "Post everything in Discord", not unposted,
                    "Everything is posted. After changing roles later, repost the roles channel and the class "
                    "panel." if not unposted else "Posts the dashboard, the roles channel and the party finder "
                    "panels in the channels above.", todo=unposted))

    shared = bool(game.link_home_id) or bool(s.scalar(select(Game.id).where(Game.link_home_id == game.id).limit(1)))
    out.append(Step("share", "Share a party finder with another server", shared,
                    "Optional: run one party finder with a friendly server, each keeping its own roles.",
                    optional=True))
    out.append(Step("admins", "Admin roles", bool(st.admin_role_ids),
                    "Optional: members with Manage Server are admins already; add roles to make more.",
                    optional=True))
    return out


def progress(rows: list[Step]) -> tuple[int, int]:
    """(required steps done, required steps)."""
    req = [x for x in rows if not x.optional]
    return sum(x.done for x in req), len(req)
