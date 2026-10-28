"""Every phrase panel.py writes to panel.log must land in the right category of the activity log.
If you change a log(...) in panel.py, update the line here: if this test fails, the Log tab would show it as raw text."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))  # panel folder
from byzzcraft import i18n  # noqa: E402
from byzzcraft import activity as R  # noqa: E402
from byzzcraft.access import VERBS  # noqa: E402

TS = "2026-10-21 17:00:00  "
# (panel.log line, category, user, start of the text)
CASES = [
    ("admin logged in from admin@example.com", "login", "admin", "logged in"),
    ("login failed for 'admin' from 100.1.2.3", "login", None, "Login failed for «admin»"),
    ("alice started the server", "server", "alice", "started the server"),
    ("alice changed their password", "login", "alice", "changed their password"),
    ("admin created user bob", "login", "admin", "created user bob"),
    ("admin updated user bob", "login", "admin", "updated user bob"),
    ("admin deleted user bob", "login", "admin", "deleted user bob"),
    ("admin completed the first setup", "system", "admin", "completed the first setup"),
    ("panel: start failed (Java is not installed: download it from the Dashboard checks.)", "server", "panel", "the server did not start"),
    ("admin accepted the Minecraft EULA", "settings", "admin", "accepted the Minecraft EULA"),
    ("panel downloaded Java 25", "system", "panel", "downloaded Java 25"),
    ("panel downloaded the Minecraft assets", "system", "panel", "downloaded the textures"),
    ("admin updated ByzzCraft to 1.2.0", "system", "admin", "updated ByzzCraft to 1.2.0"),
    ("admin made the panel start at login", "system", "admin", "made the panel start at login"),
    ("java error: no Java 25 for linux/riscv", "system", None, "Java error"),
    ("panel (restart) started the server", "server", "panel", "started the server"),
    ("panel stopped the server (empty for 30 minutes)", "server", "panel", "stopped the server"),
    ("admin restarted the server", "server", "admin", "restarted the server"),
    ("admin: stop failed (timed out)", "server", "admin", "could not stop"),
    ("panel: the server crashed (Watching Server)", "server", "panel", "the server crashed"),
    ("panel (after a crash) started the server", "server", "panel", "started the server"),
    ("admin force-stopped the server", "server", "admin", "force-stopped the server"),
    ("panel started on http://127.0.0.1:8765", "system", None, "Panel started"),
    ("admin changed view-distance: 24 -> 32", "settings", "admin", "changed a setting"),
    ("alice ran: time set day", "console", "alice", "ran a command"),
    ("admin added the marker 'Base' (100, -200)", "map", "admin", "added the marker «Base»"),
    ("admin removed the marker 'Base'", "map", "admin", "removed the marker"),
    ("admin moved backup world_2026-10-20_10-00-00.zip to the trash", "backup", "admin", "moved a backup"),
    ("admin started a restore of auto_2026-10-21_04-00-00.zip", "backup", "admin", "started a world restore"),
    ("admin changed the backup schedule", "backup", "admin", "changed the backup schedule"),
    ("admin: manual backup completed", "backup", "admin", "manual backup completed"),
    ("alice: manual backup failed (disk full)", "backup", "alice", "manual backup failed"),
    ("admin: restore of auto_2026-10-21_04-00-00.zip completed", "backup", "admin", "restore completed"),
    ("admin: restore of auto_2026-10-21_04-00-00.zip failed (boom)", "backup", "admin", "restore failed"),
    ("admin changed the panel settings: ram, java_args", "settings", "admin", "changed the panel settings"),
    ("panel: backup before start failed (disk full)", "backup", "panel", "backup before the start failed"),
    ("sampling error: boom", "system", None, "Sampling error"),
    ("invalid request /api/x: ValueError", "system", None, "Invalid request"),
    ("map: 3 tiles not updated this round, retrying in 5 minutes", "system", None, "Map:"),
]
# every action of the Access tab, with the phrases from access.VERBS as panel.py does
CASES += [("admin %s Notch" % v, "players", "admin", "") for v in VERBS.values()]
CASES += [("admin banned Notch (griefing)", "players", "admin", "banned Notch")]

ok = fail = 0
for line, cat, user, text in CASES:
    e = R.parse(TS + line)
    good = e and e["cat"] == cat and e["user"] == user and e["text"].startswith(text) and (cat != "players" or "Notch" in e["text"])
    ok, fail = (ok + 1, fail) if good else (ok, fail + 1)
    if not good:
        print("FAIL", line, "->", e and (e["cat"], e["user"], e["text"]))

d = R.parse(TS + "admin changed view-distance: 24 -> 32")["detail"]
assert d == {"key": "view-distance", "old": "24", "new": "32"}, d
assert R.parse(TS + "admin banned Notch (griefing)")["detail"] == "griefing"
assert R.parse("riga senza data") is None
# query: filters and counts
es = [R.parse(TS + l) for l, *_ in CASES]
items, total, counts = R.query(es, cat="players", user="admin")
assert total == len(VERBS) + 1 and counts["login"] == 4, (total, counts)
assert R.query(es, q="NOTCH", limit=2)[0][0]["cat"] == "players"
# in Italian the same lines come out translated
i18n.load("it")
assert R.parse(TS + "admin logged in from x")["text"] == "è entrato nel pannello"
assert R.parse(TS + "admin opped Notch")["text"] == "ha reso operatore Notch"
assert R.parse(TS + "admin stopped the server")["text"] == "ha spento il server"
assert R.parse(TS + "admin created user bob")["text"] == "ha creato l'utente bob"
i18n.load("en")
print("PASS %d  FAIL %d" % (ok, fail))
sys.exit(1 if fail else 0)
