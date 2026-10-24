# Changelog

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
