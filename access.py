"""Whitelist, operatori e ban del server (solo libreria standard).

A server acceso le modifiche passano da RCON (il server aggiorna i suoi file); a server spento si
scrivono direttamente whitelist.json / ops.json / banned-players.json, con l'UUID chiesto a Mojang.
"""
import json
import os
import re
import urllib.error
import urllib.request
import uuid as _uuid
from datetime import datetime

from i18n import t

NAME_RE = re.compile(r"^[A-Za-z0-9_]{3,16}$")
FILES = {"whitelist": "whitelist.json", "ops": "ops.json", "bans": "banned-players.json", "ipbans": "banned-ips.json"}
ACTIONS = ("wl_add", "wl_remove", "op", "deop", "ban", "pardon", "kick")
# frasi del registro ("admin <verbo> Nick"): registro.py le riconosce da qui, quindi si cambiano solo qui
VERBS = {"wl_add": "whitelisted", "wl_remove": "unwhitelisted", "op": "opped",
         "deop": "deopped", "ban": "banned", "pardon": "pardoned", "kick": "kicked"}
# esito mostrato all'utente (online via RCON e offline sui file)
DONE = {"wl_add": "%s added to the whitelist", "wl_remove": "%s removed from the whitelist", "op": "%s is now an operator",
        "deop": "%s is no longer an operator", "ban": "%s banned", "pardon": "%s is no longer banned", "kick": "%s kicked"}


def _read(server_dir, key, strict=False):
    """strict=True (prima di riscrivere il file): solo un file assente vale come lista vuota; un file
    illeggibile o rovinato e' un errore, altrimenti lo si sovrascriverebbe perdendo tutte le voci."""
    try:
        with open(os.path.join(server_dir, FILES[key]), encoding="utf-8-sig") as f:
            data = json.load(f)
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as e:
        if strict:
            raise ValueError(t("Cannot read %s (%s): leaving it untouched") % (FILES[key], e))
        return []
    if not isinstance(data, list):
        if strict:
            raise ValueError(t("%s is not in the expected format: leaving it untouched") % FILES[key])
        return []
    return [e for e in data if isinstance(e, dict)]


def _write(server_dir, key, data):
    p = os.path.join(server_dir, FILES[key])
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, p)


def snapshot(server_dir):
    return {k: _read(server_dir, k) for k in FILES}


def _find(lst, name):
    n = name.lower()
    return next((e for e in lst if str(e.get("name", "")).lower() == n), None)


def lookup(name):
    """(uuid con trattini, nome esatto) da Mojang, None se il nome non esiste."""
    req = urllib.request.Request("https://api.mojang.com/users/profiles/minecraft/" + name,
                                 headers={"User-Agent": "byzzcraft"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            body = r.read()
    except urllib.error.HTTPError as e:
        if e.code in (204, 404):
            return None
        raise
    if not body:
        return None
    d = json.loads(body)
    return str(_uuid.UUID(d["id"])), d["name"]


def _profile(server_dir, name, known):
    """UUID dai file del server o dai giocatori gia' visti, altrimenti da Mojang."""
    for key in ("whitelist", "ops", "bans"):
        e = _find(_read(server_dir, key), name)
        if e and e.get("uuid"):
            return e["uuid"], e["name"]
    for u, n in (known or {}).items():
        if n.lower() == name.lower():
            return u, n
    try:
        p = lookup(name)
    except (OSError, ValueError, KeyError) as ex:
        raise ValueError(t("Cannot reach Mojang for the UUID of %s (%s)") % (name, ex))
    if not p:
        raise ValueError(t("Player %s does not exist") % name)
    return p


def command(action, name, reason=""):
    """Comando RCON equivalente (server acceso)."""
    reason = re.sub(r"[\r\n\x00-\x1f]+", " ", reason).strip()[:100]
    base = {"wl_add": "whitelist add ", "wl_remove": "whitelist remove ", "op": "op ", "deop": "deop ",
            "ban": "ban ", "pardon": "pardon ", "kick": "kick "}[action] + name
    # concatenato, non con %: un motivo come "100% sicuro" non deve rompere il comando
    return base + (" " + reason if reason and action in ("ban", "kick") else "")


def resolve(server_dir, action, name, known=None):
    """UUID e nome esatto per le azioni che aggiungono (fuori da ogni lock: Mojang puo' metterci secondi)."""
    return _profile(server_dir, name, known) if action in ("wl_add", "op", "ban") else None


def apply_offline(server_dir, action, name, reason="", who="panel", profile=None):
    """Modifica i file a server spento. Ritorna il messaggio per l'utente.
    Chi chiama tiene il LOCK del pannello (niente modifiche in parallelo, niente avvio del server a meta').
    profile: (uuid, nome) gia' risolto con resolve(), per non chiamare Mojang qui dentro."""
    reason = re.sub(r"[\r\n\x00-\x1f]+", " ", reason).strip()[:100]
    if action == "kick":
        raise ValueError(t("The server is off: nobody to kick"))
    key = {"wl_add": "whitelist", "wl_remove": "whitelist", "op": "ops", "deop": "ops",
           "ban": "bans", "pardon": "bans"}[action]
    lst = _read(server_dir, key, strict=True)
    cur = _find(lst, name)
    if action in ("wl_remove", "deop", "pardon"):
        if not cur:
            raise ValueError(t({"wl_remove": "%s is not whitelisted", "deop": "%s is not an operator",
                                "pardon": "%s is not banned"}[action]) % name)
        lst.remove(cur)
        _write(server_dir, key, lst)
        return t(DONE[action]) % cur.get("name", name)
    if cur:
        raise ValueError(t({"wl_add": "%s is already whitelisted", "op": "%s is already an operator",
                            "ban": "%s is already banned"}[action]) % cur.get("name", name))
    uid, exact = profile or _profile(server_dir, name, None)
    if action == "wl_add":
        lst.append({"uuid": uid, "name": exact})
    elif action == "op":
        lst.append({"uuid": uid, "name": exact, "level": 4, "bypassesPlayerLimit": False})
    else:
        lst.append({"uuid": uid, "name": exact, "created": datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z"),
                    "source": "ByzzCraft (%s)" % who, "expires": "forever",
                    "reason": reason or "Banned by an operator."})
    _write(server_dir, key, lst)
    return t(DONE[action]) % exact
