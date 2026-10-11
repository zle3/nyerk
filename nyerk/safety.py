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
