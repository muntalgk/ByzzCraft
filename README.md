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

It can also set up a new server from nothing: it downloads `server.jar` from Mojang and Java from
[Eclipse Temurin](https://adoptium.net) if they are missing. Vanilla, Paper (and its forks) and Fabric servers
are recognised by their jar; set `server_jar` in `data/panel-config.json` if yours has an unusual name. It works with
Minecraft Java 1.20.5 and newer, including the 26.x world layout and Paper's separate Nether and End folders.

## Install

You need **Python 3.10 or newer**. Everything else (Minecraft server, Java, textures) the panel can download.

### On a server without a screen (Linux, over SSH)

```bash
git clone https://github.com/muntalgk/byzzcraft.git
cd byzzcraft
python3 panel.py
```

No git? `curl -L https://github.com/muntalgk/byzzcraft/archive/refs/heads/main.tar.gz | tar xz && cd byzzcraft-main`

The panel prints a link with a one-time setup code:

```
ByzzCraft is not set up yet.
  From your computer open:  http://192.168.1.64:8765/?setup=Xy3...
```

Open it in the browser of **your computer**. The setup page asks for the server folder (or creates a new
server there), the language, your admin user, who can open the panel (this machine only, or your home
network), and whether you accept the [Minecraft EULA](https://aka.ms/MinecraftEULA). Without the code the
page does not open, so nobody else on the network can take the admin account. If the link does not open at
all, the firewall blocks the port: `sudo ufw allow 8765/tcp`. Lost the link?
`grep setup= data/logs/byzzcraft.log`.

Then keep the panel running after you close SSH and after a reboot (the command prints a systemd unit for
your user and folder):

```bash
python3 panel.py service | sudo tee /etc/systemd/system/byzzcraft.service
sudo systemctl daemon-reload && sudo systemctl enable --now byzzcraft
```

Prefer the shell for everything? Do the setup without a browser:

```bash
python3 panel.py setup ~/minecraft --admin alex --new-server --network --accept-eula
```

(`--lang it`, `--no-java` and `--help` are there too; the password is asked, or read from `PANEL_PASSWORD`.)

### On your own PC (Windows or Linux with a desktop)

```bash
git clone https://github.com/muntalgk/byzzcraft.git
cd byzzcraft
python panel.py
```

Open <http://127.0.0.1:8765> on the same PC: the setup page opens without a code. To start it at every
login: `python panel.py service` (on Windows it adds ByzzCraft to the Startup folder, no admin needed).

### After the setup

The panel turns on RCON in `server.properties` and downloads the textures and names it shows (they belong
to Mojang, so they are not in this repository). If something is still missing (Java, the EULA) the
**Dashboard** shows it with a button to fix it. Press **Start**.

**Friends**: open **Account** and add a user for each of them. Admin users can also use the console,
settings, backups and access lists. For them to join the game, open the Minecraft port: `sudo ufw allow 25565/tcp`
(and forward it on your router if they play from outside).

### How the server starts

The panel starts Java by itself, with [Aikar's flags](https://docs.papermc.io/paper/aikars-flags), and backs up
the world before each start (`backups/world_*.zip`, after checking the free disk space). RAM (automatic: half
of the machine, 1 to 4 GB), extra Java flags, the backup before the start, the auto stop and the address
shown to players are in **Settings → Panel**, so you change them from the browser, without SSH.

Need something the panel does not do? Put your own script in the server folder and set `start_script` in
`data/panel-config.json`: the panel runs it and passes `JAVA` (its own Java, if it downloaded one), `SERVER_JAR`
and `PYTHON`. Examples are in `examples/`.

## Reach it from other devices

"My home network" in the setup opens the panel at `http://<server-ip>:8765`, without HTTPS: fine at home, not
on the internet. For anything else keep "this machine only" and pick one of these:

### Tailscale (easiest, private)

Install [Tailscale](https://tailscale.com) on the server and on your devices, then on the server:

```bash
sudo tailscale serve --bg --https=443 http://127.0.0.1:8765
```

The panel is at `https://<machine>.<tailnet>.ts.net`, only for devices in your tailnet, with HTTPS.
Friends can join your tailnet, or you can [share the machine](https://tailscale.com/kb/1084/sharing) with them.

### nginx (your own domain)

Point a domain to the server, open ports 80 and 443, copy `examples/nginx.conf` and get a certificate with
certbot. The comments in the file list the commands. Keep `proxy_buffering off`: the live updates need it.

### Caddy (your own domain, automatic HTTPS)

Point a domain to the server, open ports 80 and 443, and run Caddy with `examples/Caddyfile`. Caddy gets
the certificate by itself.

### SSH tunnel (no setup at all)

`ssh -L 8765:127.0.0.1:8765 you@your-server`, then open <http://127.0.0.1:8765> on your computer.

With nginx or Caddy the panel is on the internet: use long passwords. Logins are rate limited (5 attempts,
then 15 minutes locked). The proxy must run on the same machine as the panel.

## Logs

| File | What is in it |
|---|---|
| `data/logs/byzzcraft.log` | technical log of the panel: start-up, downloads, errors with tracebacks (rotated, 5 × 1 MB) |
| `data/logs/server-start.log` | output of Java (or of your start script), last start only: why Minecraft did not start |
| `data/panel.log` | who did what in the panel (the **Log** tab) |
| `<server>/logs/latest.log` | Minecraft's own log (the **Console** tab) |

Admins can read the first two in the **Log** tab too, without SSH. Useful commands:

```bash
journalctl -u byzzcraft -f                          # the panel, when it runs as a service
tail -f data/logs/byzzcraft.log data/logs/server-start.log    # same, from the files
sudo systemctl restart byzzcraft                    # restart the panel (Minecraft keeps running)
python3 panel.py passwd NAME                        # forgot a password
python3 panel.py java                               # download the Java that server.jar needs
python3 panel.py assets                             # download textures and names again
```

## Update

```bash
cd byzzcraft && git pull && sudo systemctl restart byzzcraft
```

Settings, users, backups and the map cache stay: they are in `data/`, not in the repository (set `BYZZ_DATA` to
keep them somewhere else). Installs from before 1.0 keep them next to the code and go on working as they are. When a new release is out
the Dashboard tells admins, with this command; the version you run is at the bottom of the **Account** page.

Backups and restores check the free disk space first and stop with a message instead of filling the disk; the
Dashboard warns when less than three backups would fit.

## Configuration

`data/panel-config.json` is written by the setup; you can edit it and restart the panel.

| Key | Default | Meaning |
|---|---|---|
| `server_dir` | chosen at setup | Minecraft server folder |
| `start_script` | empty | empty: the panel starts Java; otherwise a script in the server folder |
| `ram` | empty (automatic) | server RAM, e.g. `6G` (also in **Settings → Panel**) |
| `java_args` | empty | extra Java flags (also in **Settings → Panel**) |
| `backup_on_start` | `true` | back up the world before each start (without `start_script`) |
| `listen`, `port` | `127.0.0.1`, `8765` | where the panel listens (`0.0.0.0` = home network) |
| `lang` | chosen at setup | interface language: `en`, `it` |
| `title` | `ByzzCraft` | name shown in the page |
| `auto_stop_minutes` | `30` | stop the server after N minutes empty (0 = never) |
| `mc_address` | | server address shown to players, with a copy button |
| `java_auto` | chosen at setup | download Java by itself when it is missing |
| `server_jar` | found by itself | jar to start (Fabric launcher, Paper, `server.jar`); a start script gets it as `SERVER_JAR` |
| `update_check` | `true` | once a day ask GitHub for a newer release (one GET, nothing sent) |

## Troubleshooting

The Dashboard lists what is missing, and when the server does not start it says why. If that is not enough,
`data/logs/server-start.log` has the full output.

- **Login does not stick on `http://192.168…`**: update the panel; older versions marked the cookie HTTPS-only.
- **"RCON unavailable"**: restart the Minecraft server once after setup, so it reads the new RCON settings.
- **Port 8765 in use**: another panel is running, or change `port` in `data/panel-config.json`.
- **Do not run it as root**: the server files would belong to root. The panel warns about it in the log.

## Translations

Texts in the code are English. `i18n/it.json` maps them to Italian; a new language is a new
`i18n/<code>.json` with the same keys (missing entries fall back to English).

## Tests

```bash
for t in tests/test_*.py; do python3 "$t"; done
```

## License

MIT, see [LICENSE](LICENSE).

NOT AN OFFICIAL MINECRAFT PRODUCT. NOT APPROVED BY OR ASSOCIATED WITH MOJANG OR MICROSOFT.
