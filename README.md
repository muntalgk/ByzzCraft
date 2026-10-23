# ByzzCraft

A self-hosted web panel for a vanilla Minecraft Java server. One Python file to run, no dependencies,
no database: it reads the server's own files and talks to it over RCON.

[Leggimi in italiano](README.it.md)

## What it does

- **Start, stop, restart** the server from the browser; it can stop itself when nobody has played for a while.
- **Live dashboard**: players online, time of day, moon phase and weather, CPU, RAM, tick time, Java heap.
- **Map**: top-down render of the explored world (Overworld, Nether, End), live player positions, shared markers.
- **Players**: 3D skin, health, food, armor, XP, inventory and ender chest, statistics.
- **Timeline**: play sessions, deaths, advancements and chat, rebuilt from the server logs.
- **Console**: live log and RCON commands.
- **Settings**: `server.properties` editor with a backup of the file on every save.
- **Backups**: manual and scheduled world backups, retention, download, restore.
- **Access**: whitelist, operators, bans and kicks, even with the server off.
- **Activity log**: who did what in the panel.
- Users with admin or player role, English and Italian interface.

## Requirements

- Python 3.11 or newer (standard library only).
- Windows or Linux.
- A vanilla Minecraft Java server (`server.jar`) that you start with a script.

## Install

```bash
git clone https://github.com/<you>/byzzcraft.git
cd byzzcraft
python panel.py init /path/to/minecraft-server
python panel.py assets
python panel.py
```

1. `init` saves `panel-config.json`, turns on RCON in `server.properties` with a random password
   and creates the first admin user. Run the Minecraft server once before, so `server.properties` exists.
2. `assets` downloads the client jar of your server's version from Mojang and extracts the textures and
   English names the panel shows. They are copyrighted by Mojang, so they are not part of this repository.
3. Copy `examples/start.sh` (Linux) or `examples/start.bat` (Windows) into the server folder and adjust
   RAM and Java. The panel relies on what it does: a world backup before each start and `logs/gc.log`.
4. Open <http://127.0.0.1:8765> and log in.

More users: `python panel.py adduser NAME [--admin]`, `passwd NAME`, `deluser NAME`, `users`.

### Configuration

`panel-config.json` (see `panel-config.example.json`):

| Key | Default | Meaning |
|---|---|---|
| `server_dir` | parent folder | Minecraft server folder |
| `start_script` | `start.sh` / `start.bat` | script that starts the server |
| `listen`, `port` | `127.0.0.1`, `8765` | where the panel listens |
| `lang` | `en` | interface language: `en`, `it` |
| `title` | `ByzzCraft` | name shown in the page |
| `auto_stop_minutes` | `30` | stop the server after N minutes empty (0 = never) |
| `mc_address` | | server address shown to players |
| `user_mc` | `{}` | panel user → Minecraft name, for the face in the sidebar |

### Run it as a service

- **Linux**: `examples/byzzcraft.service` (systemd). Run it as the user that owns the server files, not root.
- **Windows**: put a shortcut to `pythonw panel.py` in the Startup folder (`shell:startup`).

## Security

The panel listens on `127.0.0.1` only. To reach it from other devices put it behind something that adds
HTTPS and keeps strangers out, for example [Tailscale Serve](https://tailscale.com/kb/1312/serve)
(`tailscale serve --bg --https=443 http://127.0.0.1:8765`) or a reverse proxy such as Caddy or nginx.
Do not expose port 8765 directly to the internet.

Passwords are stored as PBKDF2 hashes, sessions are server-side, logins are rate limited, and every
change is written to the activity log.

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
