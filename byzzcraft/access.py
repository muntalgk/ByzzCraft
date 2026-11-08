"""Server whitelist, operators and bans (standard library only).

With the server on, changes go through RCON (the server updates its own files); with the server off they are
written straight to whitelist.json / ops.json / banned-players.json, with the UUID asked to Mojang.
"""
import hashlib
import json
import os
import re
import urllib.error
import urllib.request
import uuid as _uuid
from datetime import datetime

from .i18n import t
from .paths import write_json

NAME_RE = re.compile(r"^[A-Za-z0-9_]{3,16}\Z")
FILES = {"whitelist": "whitelist.json", "ops": "ops.json", "bans": "banned-players.json", "ipbans": "banned-ips.json"}
ACTIONS = ("wl_add", "wl_remove", "op", "deop", "ban", "pardon", "kick")
# activity log phrases ("admin <verb> Nick"): activity.py recognises them from here, so change them only here
VERBS = {"wl_add": "whitelisted", "wl_remove": "unwhitelisted", "op": "opped",
         "deop": "deopped", "ban": "banned", "pardon": "pardoned", "kick": "kicked"}
# result shown to the user (online via RCON and offline on the files)
DONE = {"wl_add": "%s added to the whitelist", "wl_remove": "%s removed from the whitelist", "op": "%s is now an operator",
        "deop": "%s is no longer an operator", "ban": "%s banned", "pardon": "%s is no longer banned", "kick": "%s kicked"}


def _read(server_dir, key, strict=False):
    """strict=True (before rewriting the file): only a missing file counts as an empty list; an unreadable or
    damaged file is an error, otherwise it would be overwritten and every entry lost."""
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
    write_json(os.path.join(server_dir, FILES[key]), data, indent=2, ensure_ascii=False)


def snapshot(server_dir):
    return {k: _read(server_dir, k) for k in FILES}


def _find(lst, name):
    n = name.lower()
    return next((e for e in lst if str(e.get("name", "")).lower() == n), None)


def lookup(name):
    """(dashed uuid, exact name) from Mojang, None if the name does not exist."""
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


# only the login thread's own line, from the start of the line: chat can put "]: UUID of player" inside a message
LOG_UUID_RE = re.compile(r"^\[[^\]\n]*\] \[User Authenticator #\d+/INFO\](?: \[[^\]\n]*\])?: UUID of player (\w{1,16}) is ([0-9a-fA-F-]{36})[ \t\r]*$", re.M)


def known_uuid(server_dir, name, known=None):
    """(uuid, exact name) without network: usercache.json, then known {uuid: name}, then the login lines in
    logs/latest.log (the latest wins). None if the server never saw the name."""
    n = name.lower()
    try:
        with open(os.path.join(server_dir, "usercache.json"), encoding="utf-8-sig") as f:
            for e in json.load(f):
                if isinstance(e, dict) and str(e.get("name", "")).lower() == n and e.get("uuid"):
                    return str(e["uuid"]), e["name"]
    except (OSError, ValueError, TypeError):
        pass
    for u, kn in (known or {}).items():
        if kn.lower() == n:
            return u, kn
    try:
        with open(os.path.join(server_dir, "logs", "latest.log"), "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 4_000_000))   # shortcut: only the last 4 MB of the log
            text = f.read().decode("utf-8", "replace")
    except OSError:
        return None
    for m in reversed(list(LOG_UUID_RE.finditer(text))):
        if m.group(1).lower() == n:
            try:
                return str(_uuid.UUID(m.group(2))), m.group(1)
            except ValueError:
                pass
    return None


def _profile(server_dir, name, known, offline=False):
    """UUID from the server files or from players already seen, otherwise from Mojang."""
    for key in ("whitelist", "ops", "bans"):
        e = _find(_read(server_dir, key), name)
        if e and e.get("uuid"):
            return e["uuid"], e["name"]
    for u, n in (known or {}).items():
        if n.lower() == name.lower():
            return u, n
    if offline:  # online-mode=false: the server uses UUID.nameUUIDFromBytes("OfflinePlayer:" + name)
        return str(_uuid.UUID(bytes=hashlib.md5(("OfflinePlayer:" + name).encode("utf-8")).digest(), version=3)), name
    try:
        p = lookup(name)
    except (OSError, ValueError, KeyError) as ex:
        k = known_uuid(server_dir, name, known)   # Mojang down or timed out: the server already knows this player
        if k:
            return k
        raise ValueError(t("Cannot reach Mojang for the UUID of %s (%s)") % (name, ex))
    if not p:
        raise ValueError(t("Player %s does not exist") % name)
    return p


def _reason(r):   # one line: it ends up in an RCON command and in the files
    return re.sub(r"[\r\n\x00-\x1f]+", " ", r).strip()[:100]


def command(action, name, reason=""):
    """Equivalent RCON command (server on)."""
    reason = _reason(reason)
    base = {"wl_add": "whitelist add ", "wl_remove": "whitelist remove ", "op": "op ", "deop": "deop ",
            "ban": "ban ", "pardon": "pardon ", "kick": "kick "}[action] + name
    # concatenated, not with %: a reason like "100% sure" must not break the command
    return base + (" " + reason if reason and action in ("ban", "kick") else "")


def resolve(server_dir, action, name, known=None, offline=False):
    """UUID and exact name for the actions that add someone (outside any lock: Mojang can take seconds)."""
    return _profile(server_dir, name, known, offline) if action in ("wl_add", "op", "ban") else None


def apply_known(server_dir, action, name, known=None, who="panel"):
    """Server on but its own Mojang lookup failed ("That player does not exist"): write the files with the UUID the
    server already knows. Returns (message, needs_reload) or None if the UUID is unknown or the action is not wl_add/op.
    The caller holds the panel LOCK. Duplicates are refused by apply_offline."""
    prof = known_uuid(server_dir, name, known) if action in ("wl_add", "op") else None
    if not prof:
        return None
    msg = apply_offline(server_dir, action, name, who=who, profile=prof)
    return msg, action == "wl_add"


def apply_offline(server_dir, action, name, reason="", who="panel", profile=None):
    """Changes the files with the server off. Returns the message for the user.
    The caller holds the panel LOCK (no parallel changes, no server start halfway through).
    profile: (uuid, name) already resolved with resolve(), so Mojang is not called in here."""
    reason = _reason(reason)
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
