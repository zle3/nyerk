# Nyerk

AION2 event alerts for one Discord server (NA East, Siel, Elyos). It posts plain-text alerts and role pings to #aion-2-timer, keeps a live dashboard with world boss report buttons as the last message there, runs reaction roles in #aion-2-roles, and logs to #logs. A password-protected web admin on the LAN configures all of it.

The full spec is in `Nyerk Scope Document.md`.

## Run it

Every push to `main` runs the tests in GitHub Actions, builds the image and publishes it as `ghcr.io/zle3/nyerk:latest` (plus a `sha-xxxxxxx` tag, and `1.2.0`-style tags for `v1.2.0` git tags). The server only needs `compose.yaml`; it never sees the source.

### Dockge (or Portainer, Komodo, any compose manager)

1. In Dockge, **+ Compose**, name the stack `nyerk`, paste the contents of `compose.yaml`.
2. In the env editor set `DISCORD_TOKEN`, `GUILD_ID` and `ADMIN_PASSWORD`, then **Deploy**. Dockge pulls the image.
3. Open `https://<homelab-ip>:8443` (type the `https://`). The certificate is self-signed, so the browser asks you to accept it once. Behind an HTTPS reverse proxy, set `WEB_TLS=off` and point the proxy at port 8443.

**Private repo:** the image is private too, so the server must log in once: create a GitHub token (classic) with only `read:packages`, then on the server run `docker login ghcr.io -u zle3` and paste the token as the password. A public repo needs no login.

**Updating:** push to `main`, wait for the Actions run to go green (about 2 minutes), then click **Update** in Dockge. To update automatically, add a cron job on the server (`crontab -e`); it only restarts Nyerk when a new image was pulled:

```
*/15 * * * * cd /opt/stacks/nyerk && docker compose pull -q && docker compose up -d
```

**Rolling back / pinning:** set `NYERK_IMAGE=ghcr.io/zle3/nyerk:sha-abc1234` (or `:1.2.0`) in the env editor and Deploy.

On first start Nyerk seeds the ten launch events, the six categories and the three channel IDs from the scope doc. After that:

1. Create the category roles in Discord and paste each role ID on the **Roles** page.
2. Click **Post roles message**.
3. Check **Settings** and the **Events** alert toggles.

#### Data folder permissions

Data lands in `/opt/stacks/nyerk/data`. On start the container hands it to `PUID:PGID` (default 1000:1000) and drops root. If that can't work — the stack lives on an NFS/SMB share, SELinux blocks the mount, or chown is ignored — it logs a `nyerk-entrypoint:` line saying why and runs as root instead of crashing. To get back to non-root, either keep `data/` on a local disk, or on the host run `chown -R 1000:1000 /opt/stacks/nyerk/data`. On SELinux hosts (Fedora, RHEL, Rocky) change the volume to `./data:/data:z`.

### Build from source instead

```bash
cp .env.example .env        # fill in DISCORD_TOKEN, GUILD_ID, ADMIN_PASSWORD
docker compose -f compose.build.yaml up -d --build
```

### Discord setup

- **Bot invite scopes:** `bot` and `applications.commands`.
- **Permissions in the three channels:** View Channel, Send Messages, Read Message History, Add Reactions, Use Application Commands. Nyerk also needs Manage Roles, and its role must sit above every category role. It also needs Manage Messages in #aion-2-timer to delete old alerts and dashboards.
- **Role pings:** make each category role mentionable, or give Nyerk Mention All Roles.
- **Intents:** none of the privileged ones; Nyerk uses guilds and guild reactions only.
- **Channel:** make #aion-2-timer read-only for members. Buttons and slash commands still work.

## What's where

| Path | Purpose |
| --- | --- |
| `nyerk/schedules.py` | Schedule types (hourly, daily, every N hours, weekly, monthly, one-off, kill-reported). Each type answers "next occurrence after T". To add a type, subclass `ScheduleType` and decorate it with `@register`; the admin form builds itself from `fields`. |
| `nyerk/engine.py` | Due alerts, skip-after-downtime, message templates, dashboard text, cleanup. Has no Discord code. |
| `nyerk/timers.py` | World boss reports, edits, cancels, duplicate and limit rules. |
| `nyerk/bot.py` | The only module that talks to Discord: 30 s scheduler, dashboard repost and buttons, the `/nyerk` commands, reaction roles, #logs. |
| `nyerk/shugo.py` | shugo.gg table parser and the review diff. Hand-edited fields are never overwritten. |
| `nyerk/housekeeping.py` | Nightly SQLite backup (04:00 server time), daily shugo.gg check, self-signed cert. |
| `nyerk/web/` | FastAPI admin with server-rendered pages. |
| `data/` | `nyerk.db`, `backups/`, `logs/nyerk.log`, `tls/`. Back up by copying this folder. |

## Behaviour notes

- **Restarts:** alerts whose send time passed while Nyerk was down are skipped and logged to #logs. They are not sent late. The `sent_messages` ledger prevents double posts after a restart.
- **Kill reports:** a report with no time assumes the kill happened 5 minutes ago (the offset is editable). A `respawns_in` report uses the remaining time exactly as given. Warnings that are already in the past when a timer is reported are skipped silently.
- **Time zones:** each event stores its own time zone. Game events keep their New York hour, while resets and maintenance stay pinned to UTC, so their New York time moves an hour earlier on November 1. Tests cover both cases.
- **Maintenance overrides:** an override replaces one week's regular window. Emergency windows are added on top of the regular schedule.
- **Message templates:** a line containing only `{role}` is dropped when that slot's ping is off. Reporters are shown by display name, and user mentions are always disabled.

## Develop

```bash
uv venv -p 3.12 .venv && uv pip install --python .venv/bin/python -r requirements-dev.txt
.venv/bin/python -m pytest -q
NYERK_WEB_ONLY=1 ADMIN_PASSWORD=devpassword .venv/bin/python -m nyerk   # admin page without Discord
```
