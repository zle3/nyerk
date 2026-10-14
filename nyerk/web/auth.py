"""Who may see what. Levels are decided from Discord each time, never
stored as accounts, and apply per Discord server:

- host:   whoever runs this Nyerk: Discord user IDs in OWNER_IDS (env), or
          the LAN password login. Every server, plus the Servers page.
- owner:  the Discord server's owner, in that server.
- admin:  members with Manage Server in that server, or a role its owner
          made an admin role (or ADMIN_ROLE_IDS in the env), unless the
          owner has locked admin access.
- member: anyone else in the server; only their own /me page.

Pages are protected by one path policy (`needed_level`), applied in
`require()` which every page and form already calls.
"""
from __future__ import annotations

import time
from urllib.parse import urlencode

import httpx

LEVELS = {"member": 1, "admin": 2, "owner": 3, "host": 4}
RECHECK_SECONDS = 300          # re-read roles from Discord this often
FRESH_SECONDS = 3600           # owner-only changes need a sign-in this recent
DISCORD_API = "https://discord.com/api/v10"

# Path prefix -> level needed. First match wins; everything else needs admin.
# Prefixes end at a path boundary: "/me" must not match "/members".
MEMBER_PATHS = ("/me", "/auth", "/logout", "/server/use")
HOST_PATHS = ("/servers", "/settings/password")  # the password is the host's, for every server
OWNER_PATHS = ("/settings", "/history", "/access", "/games", "/setup")
# Admins may view these pages and use the listed actions; other changes are owner-only.
ADMIN_POSTS_UNDER_OWNER_PAGES = ("/categories/repost-roles",)
OWNER_POSTS = ("/categories", "/roles/", "/party/types", "/party/classes")


# Owner-only areas the owner can open to admins on the Access page, so they
# can be more hands off. The Access page and the owner password never open up:
# otherwise an admin could grant themselves the rest.
AREAS = {
    "settings": ("Settings and games", "Channels, timing, dashboard text, link preview, export; add, switch off or delete games"),
    "wording": ("Message wording", "Alert templates, dashboard line labels, boss button text"),
    "roles": ("Roles and role IDs", "Roles channel setup and role messages, classes, type roles, dungeon role IDs"),
    "history": ("Change history", "See every change; restoring also needs the three areas above"),
}
NEVER_OPENED = ("/access", "/settings/password")
# These rewrite everything at once, so they need every area they could touch.
ALL_AREAS = frozenset(("settings", "wording", "roles"))


def areas_for(path: str, method: str) -> frozenset[str] | None:
    """For a path that needs the owner: the areas an admin must hold to use it
    anyway. None means owner only, always."""
    if _under(path, NEVER_OPENED):
        return None
    if method == "POST" and _under(path, ("/settings/import",)):
        return ALL_AREAS
    if method == "POST" and _under(path, ("/history",)):
        return ALL_AREAS | {"history"}
    if _under(path, ("/settings", "/games", "/setup")):
        return frozenset(("settings",))
    if _under(path, ("/history",)):
        return frozenset(("history",))
    return frozenset(("roles",))  # OWNER_POSTS: roles channel, classes, type roles


def parse_grants(raw: str | None) -> frozenset[str]:
    return frozenset(a for a in (raw or "").split(",") if a in AREAS)


def may_use(level: str | None, need: str, path: str, method: str, grants: frozenset[str]) -> bool:
    if allows(level, need):
        return True
    if need != "owner" or level != "admin":
        return False
    areas = areas_for(path, method)
    return areas is not None and areas <= grants


def _under(path: str, prefixes) -> bool:
    return any(path == p.rstrip("/") or path.startswith(p.rstrip("/") + "/") for p in prefixes)


def needed_level(path: str, method: str) -> str:
    if _under(path, MEMBER_PATHS):
        return "member"
    if _under(path, HOST_PATHS):
        return "host"
    if _under(path, OWNER_PATHS):
        return "owner"
    if method == "POST" and _under(path, OWNER_POSTS) and not _under(path, ADMIN_POSTS_UNDER_OWNER_PAGES):
        return "owner"
    return "admin"


def allows(have: str | None, need: str) -> bool:
    return LEVELS.get(have or "", 0) >= LEVELS[need]


def server_level(is_server_owner: bool, can_manage: bool, role_ids: set[int], admin_roles: set[int]) -> str:
    """Someone's level in one server, before the owner's admin lock."""
    if is_server_owner:
        return "owner"
    if can_manage or role_ids & admin_roles:
        return "admin"
    return "member"


def effective(base: str | None, locked: bool) -> str | None:
    """The admin lock turns admins into members until the owner unlocks."""
    return "member" if base == "admin" and locked else base


def parse_ids(raw: str | None) -> set[int]:
    return {int(x) for x in str(raw or "").replace(" ", "").split(",") if x.isdigit()}


def authorize_url(cfg, state: str) -> str:
    return "https://discord.com/oauth2/authorize?" + urlencode({
        "client_id": cfg.discord_client_id, "redirect_uri": f"{cfg.public_url}/auth/callback",
        "response_type": "code", "scope": "identify", "state": state, "prompt": "none",
    })


async def discord_identify(cfg, code: str) -> tuple[int, str]:
    """Exchange the OAuth code for the user's ID and name. Only the
    `identify` scope is used and the token is not kept."""
    async with httpx.AsyncClient(timeout=15) as client:
        tok = await client.post(f"{DISCORD_API}/oauth2/token", data={
            "grant_type": "authorization_code", "code": code,
            "redirect_uri": f"{cfg.public_url}/auth/callback",
            "client_id": cfg.discord_client_id, "client_secret": cfg.discord_client_secret,
        }, headers={"Content-Type": "application/x-www-form-urlencoded"})
        tok.raise_for_status()
        access = tok.json()["access_token"]
        me = await client.get(f"{DISCORD_API}/users/@me", headers={"Authorization": f"Bearer {access}"})
        me.raise_for_status()
        data = me.json()
        try:  # best effort: we don't need the token after this
            await client.post(f"{DISCORD_API}/oauth2/token/revoke", data={
                "token": access, "client_id": cfg.discord_client_id, "client_secret": cfg.discord_client_secret})
        except httpx.HTTPError:
            pass
    return int(data["id"]), data.get("global_name") or data.get("username") or "member"


def new_session(uid: int, name: str, host: bool, servers: dict[str, str], epochs: dict[str, int], via: str,
                epoch: int) -> dict:
    """servers: {"<guild id>": base level} for every approved server they're
    in; epochs: each server's sign-everyone-out counter at sign-in."""
    now = int(time.time())
    return {"uid": uid, "name": name, "host": host, "servers": servers, "epochs": epochs, "level": "host" if host
            else "member", "via": via, "issued": now, "checked": now, "epoch": epoch}
