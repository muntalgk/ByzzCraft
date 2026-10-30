"""Plugins from Hangar (PaperMC's store) for Paper and its forks: search, install/update, list, remove."""
import http.client
import io
import json
import zlib
import os
import re
import threading
import zipfile
from urllib.parse import quote, urlencode, urlsplit

from . import backups as BK, paths
from .assets import _get
from .i18n import t

HANGAR = "https://hangar.papermc.io"
CDN = "hangarcdn.papermc.io"
MAX_BYTES = 100 * 1048576
JAR = re.compile(r"[\w.+-]+\.jar")
SLUG = re.compile(r"[\w.-]{1,64}")
PROJECTS = os.path.join(paths.CACHE, "hangar-projects.json")   # slug -> {icon on the CDN ("" = none), page}: plugins.json only knows the slug
_projects_lock, _projects_tried = threading.Lock(), set()
ERR = (OSError, ValueError, KeyError, TypeError, AttributeError, http.client.HTTPException)   # network, bad JSON or an unexpected shape: a message, never a 500


def _json(path, **q):
    return json.loads(_get("%s/api/v1/%s?%s" % (HANGAR, path, urlencode(q))))


def _cdn(url):
    """The URL if it is on Hangar's CDN (the only image host the page allows), else ""."""
    return url if urlsplit(url or "").hostname == CDN else ""


def _fail(e):
    return {"ok": False, "message": t("Hangar is not reachable: %s") % e}


def search(q, offset, mc):
    try:
        p = dict(q=q, limit=20, offset=offset, sort="-downloads", platform="PAPER")
        if mc:
            p["version"] = mc
        j = _json("projects", **p)
        out = []
        for r in j["result"]:
            ns = r["namespace"]
            out.append({"name": r["name"], "slug": ns["slug"], "owner": ns["owner"], "description": r.get("description") or "",
                        "downloads": r["stats"]["downloads"], "stars": r["stats"]["stars"],
                        "avatar": _cdn(r.get("avatarUrl")), "url": "%s/%s/%s" % (HANGAR, ns["owner"], ns["slug"])})
        pg = j["pagination"]
        return {"ok": True, "results": out, "more": pg["offset"] + len(out) < pg["count"]}
    except ERR as e:
        return _fail(e)


def latest(slug, mc):
    """Newest Release for this Minecraft version, else the newest of any channel; raises ValueError if there is none."""
    if not SLUG.fullmatch(slug):
        raise ValueError("bad project name")
    for channel in ("Release", None):
        p = dict(limit=1, platform="PAPER")
        if mc:
            p["platformVersion"] = mc
        if channel:
            p["channel"] = channel
        res = _json("projects/%s/versions" % quote(slug), **p)["result"]
        if res:
            v = res[0]
            d = v["downloads"]["PAPER"]
            fi = d.get("fileInfo") or {}
            return {"version": v["name"], "channel": v["channel"]["name"], "file": fi.get("name", ""), "size": fi.get("sizeBytes") or 0,
                    "sha": fi.get("sha256Hash"), "url": d.get("downloadUrl"), "ext": d.get("externalUrl"),
                    "deps": [x["name"] for x in v.get("pluginDependencies", {}).get("PAPER", []) if x.get("required")]}
    raise ValueError(t("No version of this plugin for Minecraft %s") % mc)


def _meta(path):
    """(name, version) from the plugin.yml inside the jar; None where it cannot be read."""
    try:
        with zipfile.ZipFile(path) as z:
            n = next(x for x in ("paper-plugin.yml", "plugin.yml") if x in z.namelist())
            txt = z.read(n)[:65536].decode("utf-8", "replace")
    except (OSError, StopIteration, zipfile.BadZipFile, zlib.error, EOFError):   # a truncated jar must not break the list
        return None, None
    g = lambda k: (re.search(r"^%s:\s*['\"]?([^'\"\r\n]+)" % k, txt, re.M) or [None, None])[1]
    return g("name"), g("version")


def _track():
    p = os.path.join(paths.DATA, "plugins.json")
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(d):
    paths.write_json(os.path.join(paths.DATA, "plugins.json"), d)


def projects():
    try:
        with open(PROJECTS, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def fetch_projects(slugs):
    """Asks Hangar for the icon and page not known yet, once per process for each slug (Hangar may be down); True if any was added.
    The page needs the owner too: hangar.papermc.io/<slug> alone is a 404."""
    with _projects_lock:
        d, new = projects(), False
        for slug in slugs:
            if slug in d or slug in _projects_tried or not SLUG.fullmatch(slug):
                continue
            _projects_tried.add(slug)
            try:
                r = _json("projects/%s" % quote(slug))
                ns = r["namespace"]
                d[slug] = {"icon": _cdn(r.get("avatarUrl")), "url": "%s/%s/%s" % (HANGAR, quote(ns["owner"]), quote(ns["slug"]))}
                new = True
            except ERR:
                pass
        if new:
            paths.write_json(PROJECTS, d)
        return new


def installed(server_dir):
    """The jars in plugins/ with their Hangar icon and page; icon None = not asked to Hangar yet (fetch_projects)."""
    pd, tr, hp, out = os.path.join(server_dir, "plugins"), _track(), projects(), []
    try:
        names = sorted(os.listdir(pd), key=str.lower)
    except OSError:
        return out
    for n in names:
        p = os.path.join(pd, n)
        if JAR.fullmatch(n) and os.path.isfile(p):
            name, ver = _meta(p)
            k = tr.get(n) or {}
            h = hp.get(k["slug"], {}) if k.get("slug") else {"icon": ""}   # not from Hangar (copied by hand): nothing to ask
            out.append({"file": n, "name": name or n[:-4], "version": k.get("version") or ver or "", "slug": k.get("slug", ""), "size": os.path.getsize(p),
                        "icon": h.get("icon"), "url": h.get("url", "")})
    return out


def _to_trash(server_dir, name):
    BK._to_trash_dir(os.path.join(server_dir, "plugins", name), os.path.join(server_dir, "trash"))


def install(server_dir, slug, mc):
    """{ok, message, file, version, deps}: deps = required plugins that are not installed (reported, never installed)."""
    try:
        v = latest(slug, mc)
        page = "%s/%s" % (HANGAR, quote(slug))
        u = urlsplit(v["url"] or "")
        if not v["url"]:
            return {"ok": False, "message": t("This plugin is hosted elsewhere: get it from %s") % (v["ext"] or page), "url": v["ext"] or page}
        if u.scheme != "https" or u.hostname != CDN:
            return {"ok": False, "message": t("Download refused: not from Hangar.")}
        if not JAR.fullmatch(v["file"]) or not v["sha"]:
            return {"ok": False, "message": t("Download refused: unexpected file.")}
        if v["size"] > MAX_BYTES:
            return {"ok": False, "message": t("File too big (max 100 MB).")}
        data = _get(v["url"], sha256=v["sha"])
        if len(data) > MAX_BYTES:
            return {"ok": False, "message": t("File too big (max 100 MB).")}
        pd = os.path.join(server_dir, "plugins")
        os.makedirs(pd, exist_ok=True)
        tr = _track()
        for n in {n for n, k in tr.items() if k.get("slug") == slug} | {v["file"]}:   # the old jar of this project goes to the trash
            if JAR.fullmatch(n) and os.path.isfile(os.path.join(pd, n)):
                _to_trash(server_dir, n)
            tr.pop(n, None)
        name = (_meta(io.BytesIO(data))[0] or "").lower()
        for x in installed(server_dir):   # the same plugin under another file name (copied by hand): Paper refuses duplicates
            if name and (_meta(os.path.join(pd, x["file"]))[0] or "").lower() == name:
                _to_trash(server_dir, x["file"])
                tr.pop(x["file"], None)
        have = {(_meta(os.path.join(pd, x["file"]))[0] or "").lower() for x in installed(server_dir)} | {name}
        tmp = os.path.join(pd, v["file"] + ".panel-tmp")
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, os.path.join(pd, v["file"]))
        tr[v["file"]] = {"slug": slug, "version": v["version"]}
        _save(tr)
        ver = v["version"] + ("" if v["channel"] == "Release" else " (%s)" % v["channel"])
        return {"ok": True, "file": v["file"], "version": ver, "deps": [d for d in v["deps"] if d.lower() not in have],
                "message": t("%s installed. Restart the server to load it.") % ("%s %s" % (slug, ver))}
    except ERR as e:
        return _fail(e)


def remove(server_dir, name):
    p = os.path.join(server_dir, "plugins", name)
    if not JAR.fullmatch(name) or not os.path.isfile(p):
        return {"ok": False, "message": t("Plugin not found.")}
    try:
        _to_trash(server_dir, name)
        tr = _track()
        if tr.pop(name, None):
            _save(tr)
    except OSError as e:
        return {"ok": False, "message": str(e)}
    return {"ok": True, "message": t("%s removed. Restart the server to unload it.") % name}
