"""Ogni frase che panel.py scrive in panel.log deve finire nella categoria giusta del Registro.
Se cambi un log(...) in panel.py, aggiorna la riga qui: se il test fallisce, il Registro la mostrerebbe come testo grezzo."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))  # cartella pannello
import registro as R  # noqa: E402
from accessi import VERBS  # noqa: E402

TS = "2026-10-21 17:00:00  "
# (riga di panel.log, categoria, utente, inizio del testo)
CASES = [
    ("admin ha fatto l'accesso da admin@example.com", "login", "admin", "è entrato"),
    ("accesso fallito per 'admin' da 100.1.2.3", "login", None, "Accesso fallito per «admin»"),
    ("alice ha acceso il server", "server", "alice", "ha acceso"),
    ("pannello (riavvio) ha acceso il server", "server", "pannello", "ha acceso"),
    ("pannello ha spento il server (vuoto da 30 minuti)", "server", "pannello", "ha spento"),
    ("admin ha riavviato il server", "server", "admin", "ha riavviato"),
    ("admin: spegnimento fallito (timed out)", "server", "admin", "spegnimento non riuscito"),
    ("pannello avviato su http://127.0.0.1:8765", "sistema", None, "Pannello avviato"),
    ("admin ha cambiato view-distance: 24 -> 32", "impostazioni", "admin", "ha cambiato"),
    ("alice ha eseguito: time set day", "console", "alice", "ha eseguito"),
    ("admin ha aggiunto il segnaposto 'Base' (100, -200)", "mappa", "admin", "ha aggiunto il segnaposto «Base»"),
    ("admin ha tolto il segnaposto 'Base'", "mappa", "admin", "ha tolto"),
    ("admin ha spostato nel Cestino il backup world_2026-10-20_10-00-00.zip", "backup", "admin", "ha spostato"),
    ("admin ha avviato il ripristino di auto_2026-10-21_04-00-00.zip", "backup", "admin", "ha avviato un ripristino"),
    ("admin ha cambiato la programmazione dei backup", "backup", "admin", "ha cambiato la programmazione"),
    ("admin: backup manuale completato", "backup", "admin", "backup manuale completato"),
    ("alice: backup manuale non riuscito (disco pieno)", "backup", "alice", "backup manuale non riuscito"),
    ("errore campionamento: boom", "sistema", None, "Errore campionamento"),
    ("richiesta non valida /api/x: ValueError", "sistema", None, "Richiesta non valida"),
    ("mappa: 3 tile non aggiornate in questo giro, riprovo tra 5 minuti", "sistema", None, "Mappa:"),
]
# tutte le azioni del tab Accessi, con le frasi prese da accessi.VERBS come fa panel.py
CASES += [("admin ha %s Notch" % v, "giocatori", "admin", "ha %s Notch" % v) for v in VERBS.values()]
CASES += [("admin ha bannato Notch (griefing)", "giocatori", "admin", "ha bannato Notch")]

ok = fail = 0
for line, cat, user, text in CASES:
    e = R.parse(TS + line)
    good = e and e["cat"] == cat and e["user"] == user and e["text"].startswith(text)
    ok, fail = (ok + 1, fail) if good else (ok, fail + 1)
    if not good:
        print("FAIL", line, "->", e and (e["cat"], e["user"], e["text"]))

d = R.parse(TS + "admin ha cambiato view-distance: 24 -> 32")["detail"]
assert d == {"key": "view-distance", "old": "24", "new": "32"}, d
assert R.parse(TS + "admin ha bannato Notch (griefing)")["detail"] == "griefing"
assert R.parse("riga senza data") is None
# query: filtri e conteggi
es = [R.parse(TS + l) for l, *_ in CASES]
items, total, counts = R.query(es, cat="giocatori", user="admin")
assert total == len(VERBS) + 1 and counts["login"] == 1, (total, counts)
assert R.query(es, q="NOTCH", limit=2)[0][0]["cat"] == "giocatori"
print("PASS %d  FAIL %d" % (ok, fail))
sys.exit(1 if fail else 0)
