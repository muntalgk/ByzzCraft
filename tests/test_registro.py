"""Ogni frase che panel.py scrive in panel.log deve finire nella categoria giusta del Registro.
Se cambi un log(...) in panel.py, aggiorna la riga qui: se il test fallisce, il Registro la mostrerebbe come testo grezzo."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))  # cartella pannello
import i18n  # noqa: E402
import registro as R  # noqa: E402
from accessi import VERBS  # noqa: E402

TS = "2026-10-21 17:00:00  "
# (riga di panel.log, categoria, utente, inizio del testo)
CASES = [
    ("admin logged in from admin@example.com", "login", "admin", "logged in"),
    ("login failed for 'admin' from 100.1.2.3", "login", None, "Login failed for «admin»"),
    ("alice started the server", "server", "alice", "started the server"),
    ("alice changed their password", "login", "alice", "changed their password"),
    ("admin created user bob", "login", "admin", "created user bob"),
    ("admin updated user bob", "login", "admin", "updated user bob"),
    ("admin deleted user bob", "login", "admin", "deleted user bob"),
    ("admin completed the first setup", "sistema", "admin", "completed the first setup"),
    ("panel: start failed (Java is not installed: download it from the Dashboard checks.)", "server", "panel", "the server did not start"),
    ("admin accepted the Minecraft EULA", "impostazioni", "admin", "accepted the Minecraft EULA"),
    ("panel downloaded Java 25", "sistema", "panel", "downloaded Java 25"),
    ("panel downloaded the Minecraft assets", "sistema", "panel", "downloaded the textures"),
    ("java error: no Java 25 for linux/riscv", "sistema", None, "Java error"),
    ("panel (restart) started the server", "server", "panel", "started the server"),
    ("panel stopped the server (empty for 30 minutes)", "server", "panel", "stopped the server"),
    ("admin restarted the server", "server", "admin", "restarted the server"),
    ("admin: stop failed (timed out)", "server", "admin", "could not stop"),
    ("panel: the server crashed (Watching Server)", "server", "panel", "the server crashed"),
    ("panel (after a crash) started the server", "server", "panel", "started the server"),
    ("admin force-stopped the server", "server", "admin", "force-stopped the server"),
    ("panel started on http://127.0.0.1:8765", "sistema", None, "Panel started"),
    ("admin changed view-distance: 24 -> 32", "impostazioni", "admin", "changed a setting"),
    ("alice ran: time set day", "console", "alice", "ran a command"),
    ("admin added the marker 'Base' (100, -200)", "mappa", "admin", "added the marker «Base»"),
    ("admin removed the marker 'Base'", "mappa", "admin", "removed the marker"),
    ("admin moved backup world_2026-10-20_10-00-00.zip to the trash", "backup", "admin", "moved a backup"),
    ("admin started a restore of auto_2026-10-21_04-00-00.zip", "backup", "admin", "started a world restore"),
    ("admin changed the backup schedule", "backup", "admin", "changed the backup schedule"),
    ("admin: manual backup completed", "backup", "admin", "manual backup completed"),
    ("alice: manual backup failed (disk full)", "backup", "alice", "manual backup failed"),
    ("admin: restore of auto_2026-10-21_04-00-00.zip completed", "backup", "admin", "restore completed"),
    ("admin: restore of auto_2026-10-21_04-00-00.zip failed (boom)", "backup", "admin", "restore failed"),
    ("admin changed the panel settings: ram, java_args", "impostazioni", "admin", "changed the panel settings"),
    ("panel: backup before start failed (disk full)", "backup", "panel", "backup before the start failed"),
    ("sampling error: boom", "sistema", None, "Sampling error"),
    ("invalid request /api/x: ValueError", "sistema", None, "Invalid request"),
    ("map: 3 tiles not updated this round, retrying in 5 minutes", "sistema", None, "Map:"),
]
# tutte le azioni del tab Accessi, con le frasi prese da accessi.VERBS come fa panel.py
CASES += [("admin %s Notch" % v, "giocatori", "admin", "") for v in VERBS.values()]
CASES += [("admin banned Notch (griefing)", "giocatori", "admin", "banned Notch")]

ok = fail = 0
for line, cat, user, text in CASES:
    e = R.parse(TS + line)
    good = e and e["cat"] == cat and e["user"] == user and e["text"].startswith(text) and (cat != "giocatori" or "Notch" in e["text"])
    ok, fail = (ok + 1, fail) if good else (ok, fail + 1)
    if not good:
        print("FAIL", line, "->", e and (e["cat"], e["user"], e["text"]))

d = R.parse(TS + "admin changed view-distance: 24 -> 32")["detail"]
assert d == {"key": "view-distance", "old": "24", "new": "32"}, d
assert R.parse(TS + "admin banned Notch (griefing)")["detail"] == "griefing"
assert R.parse("riga senza data") is None
# query: filtri e conteggi
es = [R.parse(TS + l) for l, *_ in CASES]
items, total, counts = R.query(es, cat="giocatori", user="admin")
assert total == len(VERBS) + 1 and counts["login"] == 4, (total, counts)
assert R.query(es, q="NOTCH", limit=2)[0][0]["cat"] == "giocatori"
# in italiano le stesse righe escono tradotte
i18n.load("it")
assert R.parse(TS + "admin logged in from x")["text"] == "è entrato nel pannello"
assert R.parse(TS + "admin opped Notch")["text"] == "ha reso operatore Notch"
assert R.parse(TS + "admin stopped the server")["text"] == "ha spento il server"
assert R.parse(TS + "admin created user bob")["text"] == "ha creato l'utente bob"
i18n.load("en")
print("PASS %d  FAIL %d" % (ok, fail))
sys.exit(1 if fail else 0)
