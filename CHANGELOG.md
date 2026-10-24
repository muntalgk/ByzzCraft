# Changelog

## Unreleased

- Docker stops Minecraft cleanly before an update (no more world loss) and uses host networking.
- Timeline shows deaths on new installs.
- A crash after an exception is detected and the server restarts.
- Map and item names fixed after the first assets download.
- `server-ip` is supported (edit it in `server.properties` with the server off: the panel no longer changes it).
- Offline-mode whitelist can be edited with the server off.
- Setup no longer downloads vanilla into a folder that already has a world.
- The systemd unit and the hints work with spaces in paths.
- Automatic RAM can go down to 512 MB.
- Executable start scripts keep their shebang.
- Windows: hints for PowerShell; automatic backups too big for the Recycle Bin are deleted.
- Root warning on the Dashboard.
- Login lock per address only.

Upgrading a Docker install from 1.0.0:
- stop the Minecraft server from the panel first: the old container cannot stop it cleanly yet;
- the setup choice is now respected: if you picked "Only this machine" and open the panel from another PC, run
  `docker compose exec byzzcraft sed -i 's/"listen": "127.0.0.1"/"listen": "0.0.0.0"/' /data/panel-config.json && docker compose restart`.

## 1.0.0

First public release.

- Works with Minecraft Java 1.20.5 and newer: the 26.x world layout and the classic one (1.21 and older),
  vanilla, Paper (and forks) and Fabric. Paper's separate Nether and End folders are shown on the map, backed up
  and restored.
- The panel starts Java by itself, with Aikar's flags, and backs up the world before each start (disk space
  checked, progress on the Dashboard). RAM, Java flags, auto stop and restart after a crash are in
  **Settings → Panel**. A start script of your own still works (`start_script`).
- Crash detection: the reason from the crash report, a notice, and an automatic restart (at most 3 in 10 minutes).
  **Force stop** for a server that hangs.
- Backups and restores check the free disk space first. The Dashboard warns when space runs low and when a new
  ByzzCraft release is out.
- Data in `data/` (or `BYZZ_DATA`), apart from the code; installs from before keep working as they are.
- Sessions last 30 days from the last use. The page can be added to the phone's Home screen.
- Docker image and `docker-compose.yml`.
- Setup from the browser of another PC with a one-time code, or from the shell (`panel.py setup`).
- English and Italian interface, Windows and Linux.
