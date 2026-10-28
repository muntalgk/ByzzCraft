"""The panel's activity log (panel.log) made readable: category, user, text and details for every line."""
import os
import re
from datetime import datetime

from .access import VERBS
from .i18n import t

CATS = ("server", "login", "console", "settings", "players", "backup", "map", "system")
_TS = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)  (.*)$")
_WHO = r"(?P<who>[a-z0-9_.-]+(?: \([^)]*\))?)"
_VERB = {v: k for k, v in VERBS.items()}
_EMPTY = re.compile(r"^empty for (\d+) minutes$")   # reason of the automatic stop (panel.py)


def _reason(r):
    m = _EMPTY.match(r or "")
    return t("empty for %s minutes") % m.group(1) if m else (r or "")


_PLAYER_TXT = {"wl_add": "added %s to the whitelist", "wl_remove": "removed %s from the whitelist", "op": "made %s an operator",
               "deop": "removed %s as operator", "ban": "banned %s", "pardon": "unbanned %s", "kick": "kicked %s"}

# (regex, category, level, text) - text is a function of the groups; the phrases are the ones log() writes in panel.py
_RULES = [
    (_WHO + r" logged in from (?P<from>.+)$", "login", "info", lambda g: (t("logged in to the panel"), t("from %s") % g["from"])),
    (r"login failed for '(?P<u>.*)' from (?P<from>.+)$", "login", "warn",
     lambda g: ((t("Login failed for «%s»") % g["u"]) if g["u"] else t("Login failed"), t("from %s") % g["from"])),
    (_WHO + r" changed their password$", "login", "info", lambda g: (t("changed their password"), "")),
    (_WHO + r" (?P<v>created|updated|deleted) user (?P<n>\S+)$", "login", "info", lambda g: (t("%s user %%s" % g["v"]) % g["n"], "")),
    (_WHO + r" completed the first setup$", "system", "info", lambda g: (t("completed the first setup"), "")),
    (_WHO + r": start failed \((?P<e>.*)\)$", "server", "err", lambda g: (t("the server did not start"), t(g["e"]))),
    (_WHO + r" accepted the Minecraft EULA$", "settings", "info", lambda g: (t("accepted the Minecraft EULA"), "")),
    (_WHO + r" downloaded Java (?P<v>\d+)$", "system", "info", lambda g: (t("downloaded Java %s") % g["v"], "")),
    (_WHO + r" downloaded the Minecraft assets$", "system", "info", lambda g: (t("downloaded the textures and names from Mojang"), "")),
    (_WHO + r" updated ByzzCraft to (?P<v>[\w.-]+)$", "system", "info", lambda g: (t("updated ByzzCraft to %s") % g["v"], "")),
    (_WHO + r" made the panel start at login$", "system", "info", lambda g: (t("made the panel start at login"), "")),
    (_WHO + r" started the server$", "server", "info", lambda g: (t("started the server"), "")),
    (_WHO + r" (?P<v>stopped|restarted) the server(?: \((?P<r>.*)\))?$", "server", "info",
     lambda g: (t("%s the server" % g["v"]), _reason(g["r"]))),
    (_WHO + r": stop failed \((?P<e>.*)\)$", "server", "err", lambda g: (t("could not stop the server"), g["e"])),
    (_WHO + r": the server crashed \((?P<e>.*)\)$", "server", "err", lambda g: (t("the server crashed"), g["e"])),
    (_WHO + r" force-stopped the server$", "server", "warn", lambda g: (t("force-stopped the server"), "")),
    (r"panel started on (?P<u>.+)$", "system", "info", lambda g: (t("Panel started"), g["u"])),
    (_WHO + r" changed the panel settings: (?P<k>.*)$", "settings", "info", lambda g: (t("changed the panel settings"), g["k"])),
    (_WHO + r": backup before start failed \((?P<e>.*)\)$", "backup", "err", lambda g: (t("backup before the start failed"), g["e"])),
    (_WHO + r" changed (?P<k>\S+): (?P<a>.*) -> (?P<b>.*)$", "settings", "info",
     lambda g: (t("changed a setting"), {"key": g["k"], "old": g["a"], "new": g["b"]})),
    (_WHO + r" ran: (?P<c>.*)$", "console", "info", lambda g: (t("ran a command"), {"cmd": g["c"]})),
    (_WHO + r" (?P<v>added|removed) the marker '(?P<n>.*)'(?: \((?P<xz>[-\d, ]+)\))?$", "map", "info",
     lambda g: (t("%s the marker «%%s»" % g["v"]) % g["n"], g["xz"] or "")),
    (_WHO + r" moved backup (?P<n>.+) to the trash$", "backup", "info", lambda g: (t("moved a backup to the trash"), g["n"])),
    (_WHO + r" started a restore of (?P<n>.+)$", "backup", "warn", lambda g: (t("started a world restore"), g["n"])),
    (_WHO + r" changed the backup schedule$", "backup", "info", lambda g: (t("changed the backup schedule"), "")),
    (_WHO + r": (?P<w>manual backup) completed$", "backup", "info", lambda g: (t("manual backup completed"), "")),
    (_WHO + r": restore of (?P<n>.+) completed$", "backup", "info", lambda g: (t("restore completed"), g["n"])),
    (_WHO + r": (?P<w>manual backup) failed \((?P<e>.*)\)$", "backup", "err", lambda g: (t("manual backup failed"), g["e"])),
    (_WHO + r": restore of (?P<n>.+) failed \((?P<e>.*)\)$", "backup", "err", lambda g: (t("restore failed"), g["e"])),
    (_WHO + r" (?P<v>" + "|".join(map(re.escape, VERBS.values())) + r") (?P<n>\S+)(?: \((?P<r>.*)\))?$",
     "players", "info", lambda g: (t(_PLAYER_TXT[_VERB[g["v"]]]) % g["n"], g["r"] or "")),
    (r"(?P<x>(?:.* error: |invalid request |map: ).*)$", "system", "err", lambda g: (g["x"][:1].upper() + g["x"][1:], "")),
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
    return {"t": t, "user": None, "cat": "system", "level": "info", "text": msg, "detail": "", "raw": msg}


MAX_LINES = 20000
_CACHE = {}   # path -> (mtime, size, entries): parsed again only when the file changes


def read(path):
    """The last MAX_LINES entries, newest first; "i" = line number (unique key)."""
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
    """(page of entries, filtered total, counts per category with the other filters applied)."""
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
