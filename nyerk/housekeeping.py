"""Jobs that run without Discord: nightly SQLite backup, daily shugo.gg
check, and the self-signed certificate for the admin page."""
from __future__ import annotations

import asyncio
import datetime as dt
import ipaddress
import logging
import socket
import sqlite3
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from . import db, shugo
from .config import Config
from .db import get_settings, get_state
from .logbus import bus

log = logging.getLogger("nyerk.housekeeping")
BACKUP_HOUR = 4  # local server time
IMPORT_EVERY = 86400


def backup_now(cfg: Config) -> Path:
    cfg.backup_dir.mkdir(parents=True, exist_ok=True)
    target = cfg.backup_dir / f"nyerk-{dt.datetime.now().strftime('%Y-%m-%d-%H%M')}.db"
    src = sqlite3.connect(cfg.db_path)
    dst = sqlite3.connect(target)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    backups = sorted(cfg.backup_dir.glob("nyerk-*.db"))
    for old in backups[: max(0, len(backups) - cfg.backup_keep)]:
        old.unlink(missing_ok=True)
    return target


async def run_import_check() -> str:
    with db.session() as s:
        settings = get_settings(s)
        url, region = settings.shugo_url, settings.shugo_region
    pending, error = await shugo.check(db.session, url, region)
    with db.session() as s:
        state = get_state(s)
        state.last_import_at = int(time.time())
        if error:
            state.last_import_error = error
        else:
            state.last_import_error = ""
            state.pending_import = pending if pending["changes"] else None
    if error:
        bus.warn("defaults", f"shugo.gg check failed, keeping last good values. {error}")
        return error
    n = len(pending["changes"])
    bus.info("defaults", f"shugo.gg check: {pending['parsed']} events read, {n} change(s) to review.",
             discord=n > 0)
    return ""


async def loop(cfg: Config) -> None:
    while True:
        try:
            with db.session() as s:
                settings, state = get_settings(s), get_state(s)
                tz, last_backup, last_import = settings.default_tz, state.last_backup_day, state.last_import_at
            local = dt.datetime.now(ZoneInfo(tz))
            today = local.date().isoformat()
            if local.hour >= BACKUP_HOUR and last_backup != today and cfg.db_path.exists():
                path = await asyncio.to_thread(backup_now, cfg)
                with db.session() as s:
                    get_state(s).last_backup_day = today
                bus.info("backup", f"Nightly backup written: {path.name}", discord=False)
            if time.time() - last_import > IMPORT_EVERY:
                await run_import_check()
        except Exception as exc:
            log.exception("housekeeping failed")
            bus.error("error", f"Housekeeping failed: {exc!r}")
        await asyncio.sleep(300)


def ensure_self_signed(cfg: Config) -> tuple[Path, Path]:
    cert, key = cfg.tls_dir / "cert.pem", cfg.tls_dir / "key.pem"
    if cert.exists() and key.exists():
        return cert, key
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    cfg.tls_dir.mkdir(parents=True, exist_ok=True)
    pk = ec.generate_private_key(ec.SECP256R1())
    host = socket.gethostname()
    names: list[x509.GeneralName] = [x509.DNSName("localhost"), x509.DNSName(host)]
    for ip in {"127.0.0.1", _lan_ip()} - {None}:
        names.append(x509.IPAddress(ipaddress.ip_address(ip)))
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Nyerk admin")])
    now = dt.datetime.now(dt.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject).issuer_name(subject).public_key(pk.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName(names), critical=False)
        .sign(pk, hashes.SHA256())
    )
    key.write_bytes(pk.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                     serialization.NoEncryption()))
    key.chmod(0o600)
    cert.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    return cert, key


def _lan_ip() -> str | None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("10.255.255.255", 1))
            return sock.getsockname()[0]
    except OSError:
        return None
