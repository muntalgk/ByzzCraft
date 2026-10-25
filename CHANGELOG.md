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
- New cartoon "BC" logo and icons; README with logo, badges and a screenshot gallery; new screenshots.
- Language can be changed from **Settings → Panel** (the panel restarts in the new language).
- Potions in the inventory show their colored liquid and their name (they looked like empty bottles).
- Dashboard "Latest events" no longer goes empty after many console commands; no seams between map tiles.
- Skins: offline-mode players and a Mojang outage no longer slow the pages (default skin cached for 6 hours).
- Timeline reads only the new lines of panel.log.
- Windows: no cmd windows flashing; the Dashboard asks for its checks once per page load.
- Force stop while starting: no false "start failed", and a clear message during the pre-start backup.
- BungeeCord/Velocity: real UUIDs kept for whitelist changes with the server off.
- Setup from another device warns that "Only this machine" locks it out.
- Confirmations (stop, force stop, restart, EULA, marker delete) are panel dialogs, not browser pop-ups.
- Backups: retention per world (how many and how many days per kind, plus a size cap for automatic ones),
  interval from 15 minutes with an optional time window, several daily times and weekdays, select many to
  delete or pin, a preview of what the next cleanup removes, notes editable afterwards, filter by world.
- Setup without the code also from the server itself through its LAN address; the code-free setup is refused
  to other web pages (cross-site posts, DNS rebinding).
- Security: login lock no longer bypassed with forged proxy headers behind nginx/Caddy; changing a password
  (also with `panel.py passwd`) ends that user's other sessions; Java flags from the panel only from an
  allowlist; `level-name` cannot leave the server folder; panel-users.json readable only by its owner.
- The server no longer stays stuck in "stopping" after one missed ping; "Stopping server" typed in chat no
  longer turns off the crash restart.
- Backups: automatic cleanup on Linux no longer fills `trash/`; files dated before 1980 no longer break every
  backup; a failed restore puts the Nether and the End back too.
- The page reconnects after a session or proxy error instead of "Reconnecting..." forever.
- Long RCON answers no longer shift every later answer by one command; a crashed server's session is closed in
  the timeline; missing default skins no longer break the Players page; Docker image has a health check.

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
