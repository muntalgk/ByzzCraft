<p align="center"><img src="byzzcraft/static/logo.svg" alt="" width="96"></p>

# ByzzCraft

[![release](https://img.shields.io/github/v/release/muntalgk/ByzzCraft)](https://github.com/muntalgk/ByzzCraft/releases/latest)
[![ci](https://github.com/muntalgk/ByzzCraft/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/muntalgk/ByzzCraft/actions/workflows/ci.yml)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-3776AB)
[![MIT](https://img.shields.io/badge/license-MIT-3fb950)](LICENSE)

A web panel for a Minecraft Java server (vanilla, Paper, Spigot or Fabric) that runs on your own machine. I use it to
start the server from my phone, see who's online and where, check inventories and keep backups of the world.

It's plain Python with no dependencies and no database: it reads the server's own files and talks to it over RCON.
Runs on Windows, Linux and Docker. [Leggimi in italiano](README.it.md).

<p>
  <img src="docs/screenshots/dashboard.jpg" alt="Dashboard" width="49%">
  <img src="docs/screenshots/map.jpg" alt="Map" width="49%">
  <img src="docs/screenshots/settings.jpg" alt="Settings" width="75%">
  <img src="docs/screenshots/phone.jpg" alt="The dashboard on a phone" width="22%">
</p>

## Download

The current version is on the [releases page](https://github.com/muntalgk/ByzzCraft/releases/latest).

- **Windows**: [ByzzCraft-windows.zip](https://github.com/muntalgk/ByzzCraft/releases/latest/download/ByzzCraft-windows.zip).
  Unzip it and double-click `ByzzCraft.bat`.
- **Docker**: `ghcr.io/muntalgk/byzzcraft` (x86 and ARM, so a Raspberry Pi works too): `:latest` is the latest
  release, `:edge` follows `main`. See [Docker](#docker).
- **Linux**: clone the repository, so updating is a `git pull`.

Outside Docker you need Python 3.10 or newer. The Minecraft server, Java and the textures are downloaded by the
panel when they're missing.

## What it does

- Start, stop and restart the server; it stops by itself when nobody has played for a while
- Live dashboard: players, time of day and weather, CPU, RAM, tick time, Java heap
- Map of the explored world with live player positions and shared markers
- Player pages: 3D skin, health, inventory, ender chest, statistics
- Timeline of sessions, deaths, advancements and chat, rebuilt from the server logs
- Console with the live log and RCON commands
- `server.properties` editor, backed up on every save
- Plugins for Paper servers: search and install from Hangar, update and remove
- Scheduled and manual world backups with retention rules, download and restore
- Whitelist, ops, bans and kicks, also with the server off
- One login per friend: admins see everything, players can start the server and look around

Works with Minecraft Java 1.20.5 and newer, English and Italian interface.

## Install

### Linux server over SSH

```bash
git clone https://github.com/muntalgk/byzzcraft.git
cd byzzcraft
python3 panel.py
```

It prints a link with a one-time code, something like `http://192.168.1.20:8765/?setup=Xy3...`. Open it from your
computer: the setup asks for the server folder (or makes a new server), your admin user, who can open the panel
and whether you accept the [Minecraft EULA](https://aka.ms/MinecraftEULA). Without the code nobody else on the
network can open that page. If the link doesn't load, open the port (`sudo ufw allow 8765/tcp`, or
`sudo firewall-cmd --permanent --add-port=8765/tcp && sudo firewall-cmd --reload`). Lost it?
`grep setup= data/logs/byzzcraft.log`.

To keep it running after you log out and after a reboot:

```bash
python3 panel.py service | sudo tee /etc/systemd/system/byzzcraft.service
sudo systemctl daemon-reload && sudo systemctl enable --now byzzcraft
```

You can also skip the browser and do the setup from the shell:
`python3 panel.py setup ~/minecraft --admin alex --new-server --server-type paper --accept-eula` (see `--help`).

### Windows, or Linux with a desktop

Unzip the [Windows zip](https://github.com/muntalgk/ByzzCraft/releases/latest/download/ByzzCraft-windows.zip) and
double-click `ByzzCraft.bat`, or clone the repository and run `python panel.py`. The browser opens on the setup page.
If Python is missing, the `.bat` tells you how to get it (`winget install -e --id Python.Python.3.12`).

`python panel.py service` starts it at every login (on Windows it goes in the Startup folder, no admin needed).

### Docker

On a Linux server, install Docker first if you don't have it (Debian, Ubuntu, Fedora, Raspberry Pi OS...), then
log out and back in so your user can run it without `sudo`:

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
```

Then, in the folder where you want to keep the compose file:

```bash
mkdir byzzcraft && cd byzzcraft
curl -fsSLO https://raw.githubusercontent.com/muntalgk/byzzcraft/main/docker-compose.yml
docker compose up -d
docker compose logs
```

Open the setup link from the logs and leave `/minecraft` as the server folder. The container restarts by itself
after a reboot, so there's no service to set up. Open the ports as in the section above (8765 for the panel if you
pick "home network", 25565 for the players).

The compose file uses host networking, which works as is on Linux. With Docker Desktop on Windows or Mac turn it
on in Settings > Resources > Network, or use the `ports:` lines commented in the file; on Windows type `curl.exe`
instead of `curl`.

To use a server you already have, mount its folder instead of the `minecraft` volume (`- ./minecraft:/minecraft`)
after `sudo chown -R 1000:1000 ./minecraft`. If the container has a memory limit, set the RAM in Settings → Panel.

### After the setup

The panel turns on RCON and downloads the textures. Anything still missing (Java, the EULA) shows up on the
Dashboard with a button to fix it. Add your friends in Account, and open the Minecraft port for them:
`sudo ufw allow 25565/tcp`, or on Windows allow Java when it asks. Forward it on the router if they play from outside.

## Using it from outside your network

The "home network" option in the setup is plain HTTP: fine at home, not on the internet. Otherwise keep "this
machine only" and put something in front:

- **Tailscale**, the easiest: `sudo tailscale serve --bg --https=443 http://127.0.0.1:8765`, then
  `https://<machine>.<tailnet>.ts.net` works on your devices. You can [share the machine](https://tailscale.com/kb/1084/sharing) with friends.
- **nginx or Caddy** with your own domain: start from [`examples/nginx.conf`](examples/nginx.conf) or
  [`examples/Caddyfile`](examples/Caddyfile). The proxy has to run on the same machine.
- **SSH tunnel**: `ssh -L 8765:127.0.0.1:8765 you@server`, then <http://127.0.0.1:8765>.

On the internet use long passwords. Wrong logins lock an address for 15 minutes after 5 attempts.

## Updating

When a new release is out, the Dashboard shows an **Update** button to admins: it installs the release and restarts
the panel, while Minecraft keeps running. From the shell it's `python3 panel.py update` (then
`sudo systemctl restart byzzcraft` on a server).

It works the same way for a git clone (it checks out the release tag, and stops if you changed files by hand) and
for the Windows zip (it checks the download against the release's `SHA256SUMS` and replaces only the code). Users,
settings, backups and caches live in `data/` and are never touched. With Docker it's
`docker compose pull && docker compose up -d`; if a release says so in its notes, download `docker-compose.yml` again.

Updates follow releases, not every change on `main`. To try what's coming, use the `:edge` Docker image or
`git checkout main && git pull`.

## Configuration

Most things are in Settings → Panel. The rest is in `data/panel-config.json` (restart the panel after editing it).

| Key | Default | |
|---|---|---|
| `server_dir` | from the setup | Minecraft server folder |
| `listen`, `port` | `127.0.0.1`, `8765` | `0.0.0.0` opens it to the home network |
| `ram` | automatic | e.g. `6G`; automatic is half the machine, 512 MB to 4 GB |
| `java_args` | | extra Java flags, on top of [Aikar's](https://docs.papermc.io/paper/aikars-flags) |
| `backup_on_start` | `true` | back up the world before each start |
| `auto_stop_minutes` | `30` | stop after this long with nobody on, `0` = never |
| `start_script` | | run your own script instead of Java (see [`examples/`](examples)) |
| `server_jar` | detected | for a jar with an unusual name |
| `mc_address` | | address shown to players |
| `lang`, `title` | `en`, `ByzzCraft` | |
| `update_check` | `true` | ask GitHub once a day for a new release |

## When something goes wrong

The Dashboard says what's missing and why the server didn't start. If that isn't enough:

- `data/logs/server-start.log`: Java's output from the last start
- `data/logs/byzzcraft.log`: the panel's own log (also in the Log tab, or `journalctl -u byzzcraft -f`)
- "RCON unavailable" right after the setup: restart the Minecraft server once so it picks up the RCON settings
- Forgot a password: `python3 panel.py passwd NAME`
- Don't run it as root, or the server files will belong to root

## What it connects to

Only to download things, never to send data about you or your server: Mojang (server jar, textures, skins and
UUIDs), Adoptium (Java, when it's missing) and GitHub (once a day, for new releases; `update_check: false` turns it off).

## Contributing

Tests: `for t in tests/test_*.py; do python3 "$t"; done` (CI runs them on Windows and Linux). Translations are in
`byzzcraft/i18n/`; a new language is one more JSON file. Read [CONTRIBUTING.md](CONTRIBUTING.md) before a pull
request, and [SECURITY.md](SECURITY.md) to report a vulnerability.

## License

MIT. Not an official Minecraft product, not approved by or associated with Mojang or Microsoft.
