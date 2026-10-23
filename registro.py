"""Registro del pannello (panel.log) reso leggibile: categoria, utente, testo e dettagli per ogni riga."""
import os
import re
from datetime import datetime

from accessi import VERBS
from i18n import t

CATS = ("server", "login", "console", "impostazioni", "giocatori", "backup", "mappa", "sistema")
_TS = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)  (.*)$")
_WHO = r"(?P<who>[a-z0-9_.-]+(?: \([^)]*\))?)"
_VERB = {v: k for k, v in VERBS.items()}
_PLAYER_TXT = {"wl_add": "added %s to the whitelist", "wl_remove": "removed %s from the whitelist", "op": "made %s an operator",
               "deop": "removed %s as operator", "ban": "banned %s", "pardon": "unbanned %s", "kick": "kicked %s"}

# (regex, categoria, livello, testo) - il testo e' una funzione dei gruppi; le frasi sono quelle scritte da log() in panel.py
_RULES = [
    (_WHO + r" logged in from (?P<from>.+)$", "login", "info", lambda g: (t("logged in to the panel"), t("from %s") % g["from"])),
    (r"login failed for '(?P<u>.*)' from (?P<from>.+)$", "login", "warn",
     lambda g: ((t("Login failed for «%s»") % g["u"]) if g["u"] else t("Login failed"), t("from %s") % g["from"])),
    (_WHO + r" started the server$", "server", "info", lambda g: (t("started the server"), "")),
    (_WHO + r" (?P<v>stopped|restarted) the server(?: \((?P<r>.*)\))?$", "server", "info",
     lambda g: (t("%s the server" % g["v"]), g["r"] or "")),
    (_WHO + r": stop failed \((?P<e>.*)\)$", "server", "err", lambda g: (t("could not stop the server"), g["e"])),
    (r"panel started on (?P<u>.+)$", "sistema", "info", lambda g: (t("Panel started"), g["u"])),
    (_WHO + r" changed (?P<k>\S+): (?P<a>.*) -> (?P<b>.*)$", "impostazioni", "info",
     lambda g: (t("changed a setting"), {"key": g["k"], "old": g["a"], "new": g["b"]})),
    (_WHO + r" ran: (?P<c>.*)$", "console", "info", lambda g: (t("ran a command"), {"cmd": g["c"]})),
    (_WHO + r" (?P<v>added|removed) the marker '(?P<n>.*)'(?: \((?P<xz>[-\d, ]+)\))?$", "mappa", "info",
     lambda g: (t("%s the marker «%%s»" % g["v"]) % g["n"], g["xz"] or "")),
    (_WHO + r" moved backup (?P<n>.+) to the trash$", "backup", "info", lambda g: (t("moved a backup to the trash"), g["n"])),
    (_WHO + r" started a restore of (?P<n>.+)$", "backup", "warn", lambda g: (t("started a world restore"), g["n"])),
    (_WHO + r" changed the backup schedule$", "backup", "info", lambda g: (t("changed the backup schedule"), "")),
    (_WHO + r": (?P<w>manual backup) completed$", "backup", "info", lambda g: (t("manual backup completed"), "")),
    (_WHO + r": restore of (?P<n>.+) completed$", "backup", "info", lambda g: (t("restore completed"), g["n"])),
    (_WHO + r": (?P<w>manual backup) failed \((?P<e>.*)\)$", "backup", "err", lambda g: (t("manual backup failed"), g["e"])),
    (_WHO + r": restore of (?P<n>.+) failed \((?P<e>.*)\)$", "backup", "err", lambda g: (t("restore failed"), g["e"])),
    (_WHO + r" (?P<v>" + "|".join(map(re.escape, VERBS.values())) + r") (?P<n>\S+)(?: \((?P<r>.*)\))?$",
     "giocatori", "info", lambda g: (t(_PLAYER_TXT[_VERB[g["v"]]]) % g["n"], g["r"] or "")),
    (r"(?P<x>(?:.* error: |invalid request |map: ).*)$", "sistema", "err", lambda g: (g["x"][:1].upper() + g["x"][1:], "")),
]
_RULES = [(re.compile("^" + rx), c, lv, f) for rx, c, lv, f in _RULES]


def parse(line):
    m = _TS.match(line.rstrip("\n"))
    if not m:
        return None
    try:
        t = int(datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp() * 1000)
    except ValueError:
        return None
    msg = m.group(2)
    for rx, cat, level, fn in _RULES:
        g = rx.match(msg)
        if g:
            d = g.groupdict()
            who = (d.get("who") or "").split(" ")[0] or None
            text, detail = fn(d)
            return {"t": t, "user": who, "cat": cat, "level": level, "text": text, "detail": detail, "raw": msg}
    return {"t": t, "user": None, "cat": "sistema", "level": "info", "text": msg, "detail": "", "raw": msg}


MAX_LINES = 20000
_CACHE = {}   # path -> (mtime, size, voci): si riclassifica solo quando il file cambia


def read(path):
    """Le ultime MAX_LINES voci, la piu' recente per prima; "i" = numero di riga (chiave univoca)."""
    try:
        st = os.stat(path)
    except OSError:
        return []
    c = _CACHE.get(path)
    if c and c[0] == st.st_mtime and c[1] == st.st_size:
        return c[2]
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return []
    start = max(0, len(lines) - MAX_LINES)
    out = []
    for i in range(start, len(lines)):
        e = parse(lines[i])
        if e:
            e["i"] = i
            out.append(e)
    out.reverse()
    _CACHE[path] = (st.st_mtime, st.st_size, out)
    return out


def query(entries, cat=None, user=None, q=None, offset=0, limit=100):
    """(pagina di voci, totale filtrato, conteggi per categoria con gli altri filtri applicati)."""
    cats = set(cat.split(",")) if cat else None
    ql = (q or "").lower().strip()
    counts = dict.fromkeys(CATS, 0)
    res = []
    for e in entries:
        if user and e["user"] != user:
            continue
        if ql and ql not in e["raw"].lower():
            continue
        counts[e["cat"]] = counts.get(e["cat"], 0) + 1
        if cats and e["cat"] not in cats:
            continue
        res.append(e)
    return res[offset:offset + limit], len(res), counts
