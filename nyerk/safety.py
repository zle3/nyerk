"""Guards shared by the web admin, settings import and the bot."""
from __future__ import annotations

# The @everyone role has the same ID as the server. Pinging it as a
# "category role" would ping the whole server, so it can never be used.
FORBIDDEN_ROLE_IDS: set[int] = set()


def protect_guild(guild_id: int) -> None:
    if guild_id:
        FORBIDDEN_ROLE_IDS.add(int(guild_id))


def check_role(role_id: int) -> int:
    if role_id and role_id in FORBIDDEN_ROLE_IDS:
        raise ValueError("That ID is the server itself (@everyone). Use a normal role instead.")
    return role_id


def pingable(role_id: int) -> int:
    """0 for roles that must never be pinged."""
    return 0 if not role_id or role_id in FORBIDDEN_ROLE_IDS else role_id


# Text people could use to advertise: links, invites, bare domains, mass pings.
import re as _re

_BLOCKED = _re.compile(
    r"https?://|www\.|discord(?:app)?\.(?:gg|com/invite)|dsc\.gg|\.gg/|@everyone|@here"
    r"|\b[\w-]+\.(?:com|net|org|gg|io|xyz|ly|me|co|tv|link|shop|store|app|dev|info|biz|site|online|fun|click|live)\b",
    _re.IGNORECASE,
)


def blocked_text(value: str) -> str:
    """The offending snippet if `value` contains a link, invite or mass
    mention, else ""."""
    m = _BLOCKED.search(value or "")
    return m.group(0) if m else ""
