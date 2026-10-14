"""Run Nyerk: Discord bot, scheduler, web admin and housekeeping in one
process and one event loop, sharing one SQLite database."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
import signal
import sys

import uvicorn

from . import db, forgotten, housekeeping, servers
from .config import Config
from .db import get_state
from .logbus import bus
from .seed import upgrade_defaults
from .web.app import create_app, hash_password

log = logging.getLogger("nyerk")


def init_password(cfg: Config) -> None:
    with db.session() as s:
        st = get_state(s)  # the host's password: one for the whole Nyerk
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

    servers = [server]
    if cfg.is_public and cfg.tunnel_port and cfg.tunnel_port != cfg.web_port:
        # For cloudflared on the stack network: plain HTTP, never published.
        # Cloudflare provides HTTPS to visitors.
        # lifespan="off": the app's startup and shutdown run once, in the main server.
        tunnel_server = uvicorn.Server(uvicorn.Config(
            app, host="0.0.0.0", port=cfg.tunnel_port, log_level="warning", proxy_headers=False, lifespan="off"))
        servers.append(tunnel_server)
        bus.info("startup", f"Tunnel listener on http://nyerk:{cfg.tunnel_port} (stack network only)", discord=False)

    async def optional(srv) -> None:
        """The tunnel listener must never take the bot down (e.g. port in use)."""
        try:
            await srv.serve()
        except BaseException as exc:  # uvicorn exits with SystemExit when it can't bind
            if isinstance(exc, asyncio.CancelledError):
                raise
            bus.error("startup", f"Tunnel listener on port {cfg.tunnel_port} failed to start ({exc!r}); "
                                 "the rest of Nyerk keeps running.")
            await asyncio.Event().wait()

    # Nyerk handles SIGTERM/Ctrl-C itself, below. uvicorn would otherwise stop
    # only its own server and then re-raise the signal, killing the process
    # before the party finder and Discord are shut down cleanly.
    for srv in servers:
        srv.capture_signals = contextlib.nullcontext
    stop = asyncio.Event()
    with contextlib.suppress(NotImplementedError):  # Windows: Ctrl-C arrives as KeyboardInterrupt
        for sig in (signal.SIGTERM, signal.SIGINT):
            asyncio.get_running_loop().add_signal_handler(sig, stop.set)

    tasks = [asyncio.create_task(stop.wait(), name="signal")]
    tasks.append(asyncio.create_task(servers[0].serve(), name="web"))
    tasks += [asyncio.create_task(optional(s), name="tunnel-web") for s in servers[1:]]
    tasks.append(asyncio.create_task(housekeeping.loop(cfg), name="housekeeping"))
    if bot is not None:
        tasks.append(asyncio.create_task(bot.start(cfg.discord_token), name="discord"))
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    if stop.is_set():
        log.info("Stopping...")
    for t in done:
        if not t.cancelled() and t.exception():
            log.error("%s stopped: %r", t.get_name(), t.exception())
    # Graceful stop (docker stop / an update): let the party finder finish the
    # Discord update it is in the middle of, so no message is posted without
    # being recorded, then stop the web servers and close Discord cleanly.
    if bot is not None:
        await bot.pf.drain(timeout=10)
    for s in servers:
        s.should_exit = True
    servers_left = [t for t in pending if t.get_name() in ("web", "tunnel-web")]
    if servers_left:
        await asyncio.wait(servers_left, timeout=10)
    if bot is not None and not bot.is_closed():
        await bot.close()
    for t in pending:
        if not t.done():
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
    bus.setup(cfg.log_file, cfg.log_keep_days)
    db.init_engine(cfg.db_path)
    with db.session() as s:
        get_state(s)
        if cfg.guild_id:
            # An install from before servers hands its data to GUILD_ID; otherwise
            # GUILD_ID is simply approved from the start.
            if servers.claim_legacy(s, cfg.guild_id):
                log.info("Data from before servers now belongs to server %s.", cfg.guild_id)
            servers.seen(s, cfg.guild_id, "", auto_approve=True)
        servers.protect_all(s)
        for what in upgrade_defaults(s):
            log.info("Updated default %s.", what)
    init_password(cfg)
    forgotten.reapply(cfg)  # a restored backup must not bring forgotten members back
    try:
        asyncio.run(amain(cfg))
    except KeyboardInterrupt:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
