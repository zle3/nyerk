# Nyerk

**AION 2 event timers, roles and party finder for Discord.** One bot for one server (built for NA East, Siel, Elyos), set up from its own web page. Run it at home; optionally open the web page to your members at your own domain.

- **Timers:** alerts with role pings for events, world boss respawns, resets and maintenance, and a live dashboard with one-click boss kill reports.
- **Roles:** self-serve ping roles, class and gear score buttons, quiet hours and a one-click "remove all my pings".
- **Party finder:** an interest list that pings people when a matching party opens, 5 or 20 player listings, and a channel to chat and plan runs.
- **Web page:** configure everything above, manage members, see stats, undo any change. Optionally open it to members with Discord sign-in.

**[Quick start](#quick-start)** · **[Using it](#using-it)** · **[Updating](#updating)** · **[Wiki](https://github.com/zle3/nyerk/wiki)**

## Screenshots

### In Discord

**Timer channel:** alerts with role pings, then the live dashboard with one-click boss reports.

<img src="docs/images/discord-timers.png" width="760" alt="Timer alerts and the live dashboard in Discord">

**Roles channel:** class buttons, event and dungeon ping roles, and ping settings (My roles, quiet hours).

<img src="docs/images/discord-roles.png" width="760" alt="Roles channel with class buttons, ping roles and ping settings">

**Party finder:** class picker, interest list, create party and party status.

<img src="docs/images/discord-party-finder.png" width="760" alt="Party finder panels in Discord">

### Web page

**Overview:** bot status and a live preview of the dashboard.

<img src="docs/images/web-overview.png" width="760" alt="Web page overview with dashboard preview">

**My page:** what members see after signing in with Discord.

<img src="docs/images/web-my-page.png" width="760" alt="Member page with timers, classes, pings, gear score and quiet hours">

<details>
<summary><b>More: stats, members, roles setup, access and security</b></summary>

**Stats:** party activity by day and hour, content demand and class distribution.

<img src="docs/images/web-stats.png" width="760" alt="Stats page">

**Members:** everyone Nyerk has seen, with their classes, gear score and activity.

<img src="docs/images/web-members.png" width="760" alt="Members page">

**Roles:** a preview of the whole roles channel before posting it.

<img src="docs/images/web-roles-preview.png" width="760" alt="Roles channel preview">

**Access and security:** who can sign in, what each level may do, and the emergency switches.

<img src="docs/images/web-access.png" width="760" alt="Access and security page">

</details>

## Quick start

No coding needed. You need a computer or home server that stays on, with [Docker](https://docs.docker.com/get-docker/) installed, and admin rights on your Discord server. Plan on about 20 minutes.

**1. Create the bot**

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications) → **New Application**, name it (for example Nyerk).
2. **Bot** → **Reset Token** → copy the token. Treat it like a password: never share it.
3. **General Information** → copy the **Application ID**.
4. Invite the bot: open this link with your Application ID pasted in, and pick your server.
   `https://discord.com/oauth2/authorize?client_id=YOUR_APPLICATION_ID&scope=bot+applications.commands&permissions=2415995968`

**2. Prepare your Discord server**

1. Turn on **User Settings → Advanced → Developer Mode**, so you can right-click anything → **Copy ID**.
2. Make four channels (any names): timers, roles, logs, and party finder (optional). Make the timer channel **read-only** for members; buttons and commands still work there. Members may chat in the party finder channel: Nyerk deletes their messages after 15 minutes.
3. Make the roles members will pick: one per event they want pings for (World Boss, Shugo, Rift, …), each set to **Allow anyone to @mention this role**. For the party finder, also one per class and Tank, Support and DPS.
4. In **Server Settings → Roles**, drag **Nyerk's role above all of those**, or it can't hand them out.

**3. Start Nyerk**

Make a folder and open a terminal in it. Download the compose file:

```bash
curl -O https://raw.githubusercontent.com/zle3/nyerk/main/compose.yaml
```

Create a file named `.env` in the same folder:

```
DISCORD_TOKEN=paste-your-bot-token
GUILD_ID=paste-your-server-id
ADMIN_PASSWORD=pick-a-long-password
```

Start it:

```bash
docker compose up -d
```

*Using Dockge?* Create a stack named `nyerk`, paste in the contents of `compose.yaml`, put the three lines in the env box, and click **Deploy**.

**4. Set it up in the web page**

1. Open `https://YOUR-COMPUTER-IP:8443` (type the `https://`). Your browser warns about the certificate; that's expected, so continue.
2. Sign in with your `ADMIN_PASSWORD`.
3. **Settings:** paste your channel IDs → **Save settings**. Within a minute the dashboard appears in your timer channel.
4. **Roles:** paste the role ID next to each event category and click **Save** on that row. When they're all in, click **Post roles channel**.
5. **Party finder** (optional): paste the class role IDs (**Save classes**) and the Tank, Support and DPS role IDs (**Save**). Add the dungeons people group for under **Content**, click **Create missing roles**, then **Post all panels**. Back on **Roles**, click **Post roles channel** again so the dungeon roles appear.

**5. Check it works**

The dashboard is in your timer channel, #logs says *Nyerk online*, and clicking a boss button on the dashboard gives you a private "Timer set" reply. (Remove that test timer with `/nyerk cancel`.)

That's it. Event times, alert toggles, message wording and everything else can be changed in the web page whenever you like. The full list is in the [Setup checklist](https://github.com/zle3/nyerk/wiki/Setup-checklist).

## Using it

- **Timer channel:** the dashboard (always the newest message) shows resets, maintenance, upcoming events and boss timers in each reader's own time zone. Killed a boss? Click its button, or use `/nyerk`.
- **Roles channel:** pick your classes and gear score, react for the pings you want, and set quiet hours under **Ping settings**.
- **Party finder channel:** **Register** to get pinged when a party opens for content you picked, or **Create party**. Chat to plan runs; messages are deleted after 15 minutes.

More in the wiki: [Using Nyerk](https://github.com/zle3/nyerk/wiki/Using-Nyerk).

## Updating

Click **Update** in Dockge, or run `docker compose pull && docker compose up -d`. Your data lives in the `data/` folder next to `compose.yaml`, with nightly backups. Rollbacks, automatic updates and password resets: [Running and updating](https://github.com/zle3/nyerk/wiki/Running-and-updating).

## Wiki

| Setting up | Running it | Using and changing it |
|---|---|---|
| [Setup checklist](https://github.com/zle3/nyerk/wiki/Setup-checklist) | [Running and updating](https://github.com/zle3/nyerk/wiki/Running-and-updating) | [Using Nyerk](https://github.com/zle3/nyerk/wiki/Using-Nyerk) |
| [Discord permissions](https://github.com/zle3/nyerk/wiki/Discord-permissions) | [Public access](https://github.com/zle3/nyerk/wiki/Public-access) (your own domain) | [How Nyerk behaves](https://github.com/zle3/nyerk/wiki/How-Nyerk-behaves) |
| [Settings export and import](https://github.com/zle3/nyerk/wiki/Settings-export-and-import) | [Access levels](https://github.com/zle3/nyerk/wiki/Access-levels) | [Development](https://github.com/zle3/nyerk/wiki/Development) |
| | [Security updates](https://github.com/zle3/nyerk/wiki/Security-updates) | |
