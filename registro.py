"""Registro del pannello (panel.log) reso leggibile: categoria, utente, testo e dettagli per ogni riga."""
import os
import re
from datetime import datetime

from accessi import VERBS

CATS = ("server", "login", "console", "impostazioni", "giocatori", "backup", "mappa", "sistema")
_TS = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)  (.*)$")
_WHO = r"(?P<who>[a-z0-9_.-]+(?: \([^)]*\))?)"

# (regex, categoria, livello, testo) - il testo e' una funzione dei gruppi
_RULES = [
    (_WHO + r" ha fatto l'accesso da (?P<from>.+)$", "login", "info", lambda g: ("è entrato nel pannello", "da " + g["from"])),
    (r"accesso fallito per '(?P<u>.*)' da (?P<from>.+)$", "login", "warn",
     lambda g: ("Accesso fallito" + (" per «%s»" % g["u"] if g["u"] else ""), "da " + g["from"])),
    (_WHO + r" ha acceso il server$", "server", "info", lambda g: ("ha acceso il server", "")),
    (_WHO + r" ha (?P<v>spento|riavviato) il server(?: \((?P<r>.*)\))?$", "server", "info",
     lambda g: ("ha %s il server" % g["v"], g["r"] or "")),
    (_WHO + r": spegnimento fallito \((?P<e>.*)\)$", "server", "err", lambda g: ("spegnimento non riuscito", g["e"])),
    (r"pannello avviato su (?P<u>.+)$", "sistema", "info", lambda g: ("Pannello avviato", g["u"])),
    (_WHO + r" ha cambiato (?P<k>\S+): (?P<a>.*) -> (?P<b>.*)$", "impostazioni", "info",
     lambda g: ("ha cambiato un'impostazione", {"key": g["k"], "old": g["a"], "new": g["b"]})),
    (_WHO + r" ha eseguito: (?P<c>.*)$", "console", "info", lambda g: ("ha eseguito un comando", {"cmd": g["c"]})),
    (_WHO + r" ha (?P<v>aggiunto|tolto) il segnaposto '(?P<n>.*)'(?: \((?P<xz>[-\d, ]+)\))?$", "mappa", "info",
     lambda g: ("ha %s il segnaposto «%s»" % (g["v"], g["n"]), g["xz"] or "")),
    (_WHO + r" ha spostato nel Cestino il backup (?P<n>.+)$", "backup", "info",
     lambda g: ("ha spostato nel Cestino un backup", g["n"])),
    (_WHO + r" ha avviato il ripristino di (?P<n>.+)$", "backup", "warn", lambda g: ("ha avviato un ripristino del mondo", g["n"])),
    (_WHO + r" ha cambiato la programmazione dei backup$", "backup", "info", lambda g: ("ha cambiato la programmazione dei backup", "")),
    (_WHO + r": (?P<w>.+?) completato$", "backup", "info", lambda g: (g["w"] + " completato", "")),
    (_WHO + r": (?P<w>.+?) non riuscito \((?P<e>.*)\)$", "backup", "err", lambda g: (g["w"] + " non riuscito", g["e"])),
    (_WHO + r" ha (?P<v>" + "|".join(map(re.escape, VERBS.values())) + r") (?P<n>\S+)(?: \((?P<r>.*)\))?$",
     "giocatori", "info", lambda g: ("ha %s %s" % (g["v"], g["n"]), g["r"] or "")),
    (r"(?P<x>(?:errore|richiesta non valida|mappa:).*)$", "sistema", "err", lambda g: (g["x"][:1].upper() + g["x"][1:], "")),
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
