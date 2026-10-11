"""Run Nyerk: Discord bot, scheduler, web admin and housekeeping in one
process and one event loop, sharing one SQLite database."""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
import sys

import uvicorn

from . import db, housekeeping
from .config import Config
from .db import get_settings
from .logbus import bus
from .seed import seed, seed_party_finder, seed_reaction_groups, upgrade_defaults
from .web.app import create_app, hash_password

log = logging.getLogger("nyerk")


def init_password(cfg: Config) -> None:
    with db.session() as s:
        st = get_settings(s)
        reset = os.environ.get("NYERK_RESET_PASSWORD") in ("1", "true", "yes")
        if st.admin_password_hash and not reset:
            return
        password = cfg.admin_password
        if not password:
            password = secrets.token_urlsafe(12)
            # Printed once to the console and local log only, never to Discord.
            log.warning("No ADMIN_PASSWORD set. Generated admin password: %s", password)
        st.admin_password_hash = hash_password(password)


async def amain(cfg: Config) -> None:
    bus.attach_queue()
    bot = None
    if cfg.web_only:
        log.warning("NYERK_WEB_ONLY set: Discord is disabled.")
    elif not cfg.discord_token:
        log.error("DISCORD_TOKEN is not set: running the web admin only.")
    else:
        from .bot import NyerkBot

        bot = NyerkBot(cfg.guild_id)

    ssl = {}
    if cfg.web_tls == "self-signed":
        cert, key = housekeeping.ensure_self_signed(cfg)
        ssl = {"ssl_certfile": str(cert), "ssl_keyfile": str(key)}
    elif cfg.web_tls == "files":
        ssl = {"ssl_certfile": cfg.tls_cert_file, "ssl_keyfile": cfg.tls_key_file}
    app = create_app(cfg, bot)
    server = uvicorn.Server(uvicorn.Config(
        app, host=cfg.web_host, port=cfg.web_port, log_level="warning", proxy_headers=cfg.web_tls == "off",
        forwarded_allow_ips="*" if cfg.web_tls == "off" else None, **ssl,
    ))
    scheme = "http" if cfg.web_tls == "off" else "https"
    bus.info("startup", f"Nyerk starting. Web admin on {scheme}://{cfg.web_host}:{cfg.web_port}", discord=False)

    tasks = [asyncio.create_task(server.serve(), name="web"),
             asyncio.create_task(housekeeping.loop(cfg), name="housekeeping")]
    if bot is not None:
        tasks.append(asyncio.create_task(bot.start(cfg.discord_token), name="discord"))
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for t in done:
        if t.exception():
            log.error("%s stopped: %r", t.get_name(), t.exception())
    if bot is not None and not bot.is_closed():
        await bot.close()
    server.should_exit = True
    for t in pending:
        t.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    log.info("Nyerk stopped.")


def check_data_dir(cfg: Config) -> None:
    """Fail with one readable line instead of a traceback loop."""
    probe = cfg.data_dir / ".write-test"
    try:
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        (cfg.data_dir / "logs").mkdir(exist_ok=True)
        probe.touch()
        probe.unlink()
    except OSError as exc:
        try:
            st = cfg.data_dir.stat()
            owner = f"owned by {st.st_uid}:{st.st_gid}, mode {st.st_mode & 0o777:o}"
        except OSError:
            owner = "not readable"
        print(
            f"FATAL: cannot write to {cfg.data_dir} ({owner}) as uid {os.getuid()}: {exc}\n"
            f"Fix on the host: chown -R {os.getuid()}:{os.getgid()} <stack folder>/data, "
            "then redeploy. If no 'nyerk-entrypoint:' line appears above, the image is "
            "outdated: run 'docker compose build --no-cache' in the stack folder.",
            file=sys.stderr, flush=True,
        )
        sys.exit(78)


def main() -> None:
    cfg = Config()
    from . import safety
    safety.protect_guild(cfg.guild_id)
    check_data_dir(cfg)
    bus.setup(cfg.log_file)
    db.init_engine(cfg.db_path)
    with db.session() as s:
        if seed(s):
            log.info("Seeded default events and categories.")
        for what in upgrade_defaults(s):
            log.info("Updated default %s.", what)
        seed_reaction_groups(s)
        if seed_party_finder(s):
            log.info("Seeded party finder classes, gear brackets and time windows.")
    init_password(cfg)
    try:
        asyncio.run(amain(cfg))
    except KeyboardInterrupt:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
