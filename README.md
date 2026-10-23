# ByzzCraft

A self-hosted web panel for a vanilla Minecraft Java server. Start the server from your phone, see who is
online and where, check their inventory, back up the world. One Python file, no dependencies, no database:
it reads the server's own files and talks to it over RCON.

[Leggimi in italiano](README.it.md)

## What it does

- **Start, stop, restart** the server from the browser; it stops by itself when nobody has played for a while.
- **Live dashboard**: players online, time of day, moon and weather, CPU, RAM, tick time, Java heap.
- **Map**: top-down render of the explored world (Overworld, Nether, End), live player positions, shared markers.
- **Players**: 3D skin, health, food, armor, XP, inventory and ender chest, statistics.
- **Timeline**: play sessions, deaths, advancements and chat, rebuilt from the server logs.
- **Console**: live log and RCON commands.
- **Settings**: `server.properties` editor that backs up the file on every save.
- **Backups**: manual and scheduled world backups, retention, download, restore.
- **Access**: whitelist, operators, bans and kicks, even with the server off.
- **Accounts**: friends get their own login; admins see everything, players can start the server and look around.
- English and Italian interface, Windows and Linux.

## Install

You need **Python 3.11 or newer** and a folder with a Minecraft Java server (`server.jar`).

```bash
git clone https://github.com/muntalgk/byzzcraft.git
cd byzzcraft
python panel.py
```

Then open **<http://127.0.0.1:8765>** in a browser **on the same machine**. The first page asks for:

- the Minecraft server folder,
- the language,
- your admin user and password,
- whether you accept the [Minecraft EULA](https://aka.ms/MinecraftEULA) (needed to run the server).

That is all. The panel then turns on RCON in `server.properties`, copies a start script into the server
folder if there is none, and downloads from Mojang the textures and names it shows (they are copyrighted
by Mojang, so they are not part of this repository). Press **Start** on the dashboard.

> The setup page only opens from the machine itself, so nobody else can grab the admin account.
> On a server without a desktop, open an SSH tunnel and use the browser on your PC:
> `ssh -L 8765:127.0.0.1:8765 you@your-server`, then open <http://127.0.0.1:8765>.

**Friends**: open **Account** in the panel and add a user for each of them. Admin users can also use the
console, settings, backups and access lists.

### The start script

The panel starts Minecraft with a script in the server folder: `start.sh` on Linux, `start.bat` on Windows.
Setup copies the one from `examples/` if you have none. It backs up the world before each start
(`backups/world_*.zip`) and writes `logs/gc.log` for the heap chart. Open it to change the RAM (`RAM=4G`) or
the Java path. To use your own script, set `start_script` in `panel-config.json`.

### Keep it running

- **Windows**: press Win+R, type `shell:startup`, and create a shortcut there to
  `pythonw C:\path\to\byzzcraft\panel.py` (`pythonw` runs it without a window).
- **Linux**: use `examples/byzzcraft.service` with systemd. Run it as the user that owns the server files,
  not as root.

The Minecraft server keeps running if you restart the panel.

## Reach it from other devices

The panel listens on `127.0.0.1` only and is never exposed directly. Pick one way to reach it:

### Tailscale (easiest, private)

Install [Tailscale](https://tailscale.com) on the server and on your devices, then on the server:

```bash
tailscale serve --bg --https=443 http://127.0.0.1:8765
```

The panel is at `https://<machine>.<tailnet>.ts.net`, only for devices in your tailnet, with HTTPS.
Friends can join your tailnet, or you can [share the machine](https://tailscale.com/kb/1084/sharing) with them.

### nginx (your own domain)

Point a domain to the server, open ports 80 and 443, copy `examples/nginx.conf` and get a certificate with
certbot. The comments in the file list the commands. Keep `proxy_buffering off`: the live updates need it.

### Caddy (your own domain, automatic HTTPS)

Point a domain to the server, open ports 80 and 443, and run Caddy with `examples/Caddyfile`. Caddy gets
the certificate by itself.

With nginx or Caddy the panel is on the internet: use long passwords. Logins are rate limited (5 attempts,
then 15 minutes locked). The proxy must run on the same machine as the panel.

## Configuration

`panel-config.json` is written by the setup; you can edit it and restart the panel.

| Key | Default | Meaning |
|---|---|---|
| `server_dir` | chosen at setup | Minecraft server folder |
| `start_script` | `start.sh` / `start.bat` | script that starts the server |
| `listen`, `port` | `127.0.0.1`, `8765` | where the panel listens |
| `lang` | chosen at setup | interface language: `en`, `it` |
| `title` | `ByzzCraft` | name shown in the page |
| `auto_stop_minutes` | `30` | stop the server after N minutes empty (0 = never) |
| `mc_address` | | server address shown to players, with a copy button |

## Troubleshooting

- **Forgot the password**: on the server, `python panel.py passwd NAME`.
- **Map and items without pictures**: the download from Mojang failed; run `python panel.py assets`.
- **"RCON unavailable"**: restart the Minecraft server once after setup, so it reads the new RCON settings.
- **Locked out of setup on a remote server**: use the SSH tunnel above.

## Translations

Texts in the code are English. `i18n/it.json` maps them to Italian; a new language is a new
`i18n/<code>.json` with the same keys (missing entries fall back to English).

## Tests

```bash
python tests/test_backups.py
python tests/test_accessi.py
python tests/test_registro.py
python tests/test_assets.py
```

## License

MIT, see [LICENSE](LICENSE).

NOT AN OFFICIAL MINECRAFT PRODUCT. NOT APPROVED BY OR ASSOCIATED WITH MOJANG OR MICROSOFT.
