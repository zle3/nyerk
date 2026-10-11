"""Process configuration from environment variables.

Everything an admin can change at runtime lives in the database (settings
table). Only secrets and deployment wiring live here.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_ids(name: str) -> frozenset[int]:
    return frozenset(int(x) for x in _env(name).replace(" ", "").split(",") if x.isdigit())


def _env_int(name: str, default: int = 0) -> int:
    raw = _env(name)
    return int(raw) if raw else default


@dataclass(frozen=True)
class Config:
    discord_token: str = field(default_factory=lambda: _env("DISCORD_TOKEN"))
    guild_id: int = field(default_factory=lambda: _env_int("GUILD_ID"))
    data_dir: Path = field(default_factory=lambda: Path(_env("NYERK_DATA_DIR", "./data")))
    admin_password: str = field(default_factory=lambda: _env("ADMIN_PASSWORD"))
    session_secret: str = field(default_factory=lambda: _env("SESSION_SECRET"))
    web_host: str = field(default_factory=lambda: _env("WEB_HOST", "0.0.0.0"))
    web_port: int = field(default_factory=lambda: _env_int("WEB_PORT", 8443))
    # "self-signed" (default): generate a cert in data/tls on first run.
    # "files": use TLS_CERT_FILE / TLS_KEY_FILE.
    # "off": plain HTTP, only when a reverse proxy terminates TLS in front.
    web_tls: str = field(default_factory=lambda: _env("WEB_TLS", "self-signed").lower())
    tls_cert_file: str = field(default_factory=lambda: _env("TLS_CERT_FILE"))
    tls_key_file: str = field(default_factory=lambda: _env("TLS_KEY_FILE"))
    backup_keep: int = field(default_factory=lambda: _env_int("BACKUP_KEEP", 14))
    # Disables the Discord side entirely; useful for working on the web admin.
    web_only: bool = field(default_factory=lambda: _env("NYERK_WEB_ONLY") in ("1", "true", "yes"))

    # -- Public access (Cloudflare Tunnel + Discord login) -------------------
    # e.g. https://nyerk.example.com. Turns on Discord login, secure cookies and HSTS.
    public_url: str = field(default_factory=lambda: _env("PUBLIC_URL").rstrip("/"))
    # Discord application (Developer Portal > OAuth2): client ID and secret.
    discord_client_id: str = field(default_factory=lambda: _env("DISCORD_CLIENT_ID"))
    discord_client_secret: str = field(default_factory=lambda: _env("DISCORD_CLIENT_SECRET"))
    # Full access. Comma-separated Discord user IDs; set here, never in the web page.
    owner_ids: frozenset = field(default_factory=lambda: _env_ids("OWNER_IDS"))
    # Members holding any of these roles get admin access (see README).
    admin_role_ids: frozenset = field(default_factory=lambda: _env_ids("ADMIN_ROLE_IDS"))
    # Password sign-in: "lan" (default; only for requests that did not come
    # through Cloudflare), "on" (anywhere) or "off".
    password_login: str = field(default_factory=lambda: _env("PASSWORD_LOGIN", "lan").lower())

    @property
    def is_public(self) -> bool:
        return self.public_url.startswith("https://")

    @property
    def discord_login(self) -> bool:
        return bool(self.is_public and self.discord_client_id and self.discord_client_secret)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "nyerk.db"

    @property
    def log_file(self) -> Path:
        return self.data_dir / "logs" / "nyerk.log"

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def tls_dir(self) -> Path:
        return self.data_dir / "tls"
