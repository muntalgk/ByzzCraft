"""Mods of a NeoForge/Forge/Fabric/Quilt server: installed list, on/off, Modrinth install/update, and the zip of mods players need."""
import contextlib
import hashlib
import http.client
import json
import os
import re
import threading
import time
import urllib.request
import zipfile
import zlib
from urllib.parse import quote, urlencode, urlsplit

from . import assets as AS, backups as BK, paths
from .i18n import t

API = "https://api.modrinth.com/v2"
CDN = "cdn.modrinth.com"
UA = "ByzzCraft"   # panel.py appends "/<version>"
MAX_BYTES = 200 * 1048576
JAR = re.compile(r'(?!\.)[^/\\:*?"<>|\x00-\x1f]+\.jar')
SLUG = re.compile(r"[\w.-]{1,64}")
SIDES = os.path.join(paths.CACHE, "modrinth-sides.json")   # sha1 of a jar -> Modrinth client_side ("" = unknown to Modrinth)
ICONS = os.path.join(paths.CACHE, "modrinth-icons.json")   # sha1 of a jar -> Modrinth icon url ("" = none)
LOGO_MAX = 262144
PNG_SIG = bytes([0x89]) + b"PNG" + bytes([13, 10, 26, 10])
CHECK_EVERY = 6 * 3600
LOADERS = {"neoforge": "NeoForge", "forge": "Forge", "fabric": "Fabric", "quilt": "Quilt"}
ERR = (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError, http.client.HTTPException)
_meta_cache, _sha, _upd = {}, {}, {}   # by (path, size, mtime); sha1 -> (when, loader, mc, newest version or None)
_lock, _pack_lock = threading.Lock(), threading.Lock()


class FsError(Exception):
    """A local file operation failed (jar locked by the running server, trash, disk): not a network problem."""


def _busy():
    return {"ok": False, "message": t("The file is in use: stop the server and retry.")}


def _fail(e):
    return {"ok": False, "message": t("Modrinth is not reachable: %s") % e}


def _api(path, body=None, **q):
    req = urllib.request.Request("%s/%s%s" % (API, path, "?" + urlencode(q) if q else ""), json.dumps(body).encode() if body is not None else None,
                                 {"User-Agent": UA, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def _download(url, sha512, sha1):
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=60) as r:
        data = r.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError(t("File too big (max 200 MB)."))
    if (hashlib.sha512(data).hexdigest() != sha512) if sha512 else (hashlib.sha1(data).hexdigest() != sha1):
        raise OSError("Corrupted download: %s" % url)
    return data


# ---------------------------------------------------------------- what is installed
def _toml(txt):
    """(modId, displayName, version) of the first [[mods]] table: only these keys, no TOML parser."""
    m = re.search(r"^\[\[mods\]\][^\n]*$(.*?)(?=^\s*\[|\Z)", txt, re.M | re.S)
    body = m.group(1) if m else ""

    def g(k):
        x = re.search(r"""^\s*%s\s*=\s*(?:"([^"\n]*)"|'([^'\n]*)')""" % k, body, re.M)
        return (x.group(1) or x.group(2) or "") if x else ""
    return g("modId"), g("displayName"), g("version")


def _meta(path, st):
    """(modId, name, version, kind) of a mod jar, "" where it cannot be told; kind = fabric/quilt/forge. Cached by size and mtime."""
    k = (path, st.st_size, st.st_mtime_ns)
    if k in _meta_cache:
        return _meta_cache[k]
    r = ("", "", "", "", "")
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            rd = lambda n: z.read(n)[:262144].decode("utf-8-sig", "replace")   # noqa: E731
            n = next((x for x in ("META-INF/neoforge.mods.toml", "META-INF/mods.toml") if x in names), None)
            if n:
                i, nm, v = _toml(rd(n))
                if "${" in v:   # "${file.jarVersion}": the build fills it into the manifest
                    mf = re.search(r"^Implementation-Version:\s*(\S+)", rd("META-INF/MANIFEST.MF") if "META-INF/MANIFEST.MF" in names else "", re.M)
                    v = mf.group(1) if mf else ""
                lg = re.search(r"""^\s*logoFile\s*=\s*["']([^"'\n]+)["']""", rd(n), re.M)
                r = (i, nm, v, "forge", lg.group(1) if lg else "")
            elif "fabric.mod.json" in names:
                j = json.loads(rd("fabric.mod.json"), strict=False)
                r = (j.get("id", ""), j.get("name", ""), str(j.get("version", "")), "fabric", j.get("icon") if isinstance(j.get("icon"), str) else "")
            elif "quilt.mod.json" in names:
                j = json.loads(rd("quilt.mod.json"), strict=False)["quilt_loader"]
                md = j.get("metadata") or {}
                r = (j.get("id", ""), md.get("name", ""), str(j.get("version", "")), "quilt", md.get("icon") if isinstance(md.get("icon"), str) else "")
    except (OSError, ValueError, KeyError, TypeError, AttributeError, zipfile.BadZipFile, zlib.error, EOFError):   # a truncated jar must not break the list
        pass
    _meta_cache[k] = r
    return r


def logo(server_dir, name):
    """(png bytes) of the logo inside an installed jar, None if it has none; name must be in the listing, never a path."""
    m = _find(server_dir, name)
    if not m:
        return None
    p = os.path.join(server_dir, "mods", m["file"])
    lg = _meta(p, os.stat(p))[4]
    try:
        with zipfile.ZipFile(p) as z:
            zi = z.getinfo(lg.lstrip("/"))
            if zi.file_size > LOGO_MAX:
                return None
            data = z.read(zi)
    except (OSError, KeyError, ValueError, zipfile.BadZipFile, zlib.error, EOFError):
        return None
    return data if data.startswith(PNG_SIG) else None


def _sha1(path):
    st = os.stat(path)
    k = (path, st.st_size, st.st_mtime_ns)
    if k not in _sha:
        h = hashlib.sha1()
        with open(path, "rb") as f:
            for b in iter(lambda: f.read(1 << 20), b""):
                h.update(b)
        _sha[k] = h.hexdigest()
    return _sha[k]


def sides(path=SIDES):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def installed(server_dir):
    """The jars in mods/ (enabled or .jar.disabled), no network. update: newest version known (checked in the background); server_only: Modrinth says no client needs it."""
    md, sd, ic, out = os.path.join(server_dir, "mods"), sides(), sides(ICONS), []
    try:
        names = sorted(os.listdir(md), key=str.lower)
    except OSError:
        return out
    for n in names:
        p = os.path.join(md, n)
        base = n[:-9] if n.endswith(".disabled") else n
        if not JAR.fullmatch(base) or not os.path.isfile(p):
            continue
        st = os.stat(p)
        i, name, ver, kind, logo = _meta(p, st)
        sha = _sha.get((p, st.st_size, st.st_mtime_ns))
        u = _upd.get(sha, (0, 0, 0, None))[3] if sha else None
        out.append({"file": n, "enabled": base == n, "id": i, "name": name or base[:-4], "version": ver, "kind": kind, "size": st.st_size, "mtime": st.st_mtime,
                    "server_only": bool(sha) and sd.get(sha) == "unsupported", "update": u["number"] if u and u["sha1"] != sha else "",
                    # Modrinth's icon, else the logo inside the jar (served by /api/mods/logo), else the UI shows the initial
                    "icon": ic.get(sha, "") if sha and urlsplit(ic.get(sha, "")).hostname == CDN else "", "logo": bool(logo)})
    return out


def loader(server_dir, jar):
    """(loader, loader version) of the server: NeoForge/Forge from libraries/, Fabric/Quilt from the jar name or the mods; (None, "") if unknown."""
    ml = AS.modloader(server_dir)
    if ml:
        return ml[0], ml[1]
    j = (jar or "").lower()
    for k in ("quilt", "fabric"):
        if j.startswith(k):
            return k, ""
    kinds = {m["kind"] for m in installed(server_dir)}
    return ("fabric" if "fabric" in kinds else "quilt" if "quilt" in kinds else None), ""


# ---------------------------------------------------------------- on / off / remove
def _find(server_dir, name):
    """The listing entry for a name from the client, never a path."""
    return next((m for m in installed(server_dir) if m["file"] == name), None)


def toggle(server_dir, name):
    m = _find(server_dir, name)
    if not m:
        return {"ok": False, "message": t("Mod not found.")}
    md, new = os.path.join(server_dir, "mods"), name[:-9] if name.endswith(".disabled") else name + ".disabled"
    if os.path.exists(os.path.join(md, new)):
        return {"ok": False, "message": t("%s already exists.") % new}
    try:
        os.rename(os.path.join(md, name), os.path.join(md, new))
    except OSError as e:
        return _busy()
    return {"ok": True, "message": t("%s changed. Restart the server to apply it.") % m["name"]}


def remove(server_dir, name):
    if not _find(server_dir, name):
        return {"ok": False, "message": t("Mod not found.")}
    try:
        BK._to_trash_dir(os.path.join(server_dir, "mods", name), os.path.join(server_dir, "trash"))
    except OSError as e:
        return _busy()
    return {"ok": True, "message": t("%s removed. Restart the server to unload it.") % name}


# ---------------------------------------------------------------- Modrinth
def search(q, offset, ld, mc):
    if not ld:
        return {"ok": False, "message": t("Cannot tell which mod loader this server uses.")}
    try:
        facets = [["project_type:mod"], ["categories:" + ld]] + ([["versions:" + mc]] if mc else [])
        j = _api("search", query=q, facets=json.dumps(facets), offset=offset, limit=20, index="relevance" if q else "downloads")
        out = [{"name": h["title"], "slug": h["slug"], "owner": h.get("author", ""), "description": h.get("description") or "", "downloads": h["downloads"],
                "avatar": h["icon_url"] if urlsplit(h.get("icon_url") or "").hostname == CDN else "", "url": "https://modrinth.com/mod/" + quote(h["slug"])}
               for h in j["hits"]]
        return {"ok": True, "results": out, "more": j["offset"] + len(out) < j["total_hits"]}
    except ERR as e:
        return _fail(e)


def _pick(v):
    f = next((x for x in v["files"] if x.get("primary")), v["files"][0])
    h, u = f.get("hashes") or {}, urlsplit(f["url"])
    if u.scheme != "https" or u.hostname != CDN or not JAR.fullmatch(f["filename"]) or not (h.get("sha512") or h.get("sha1")):
        raise ValueError(t("Download refused: unexpected file."))
    return f["filename"], f["url"], h.get("sha512"), h.get("sha1")


def _install(server_dir, v, old=None, guard=None):
    """Downloads and checks the version's file, then (and only then) trashes the jar it replaces and writes it. Returns the file name.
    A disabled old jar stays disabled: the new one is written as .jar.disabled."""
    name, url, s512, s1 = _pick(v)
    if old and old.endswith(".disabled"):
        name += ".disabled"
    data = _download(url, s512, s1)
    md = os.path.join(server_dir, "mods")
    tmp = os.path.join(md, name + ".panel-tmp")
    with guard or contextlib.nullcontext():   # only the file changes wait for the icon build, not the download
        return _write(md, server_dir, name, tmp, data, old)


def _write(md, server_dir, name, tmp, data, old):
    try:
        os.makedirs(md, exist_ok=True)
        with open(tmp, "wb") as f:
            f.write(data)
        for n in {old, name} - {None}:   # old jar leaves only now: if this fails (jar locked) it stays where it is
            if os.path.isfile(os.path.join(md, n)):
                BK._to_trash_dir(os.path.join(md, n), os.path.join(server_dir, "trash"))
        os.replace(tmp, os.path.join(md, name))
    except OSError as e:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise FsError(e) from e
    return name


def install(server_dir, slug, ld, mc, guard=None):
    """{ok, message, file, version, deps}: deps = required mods (by name) that look not installed; reported, never installed."""
    if not SLUG.fullmatch(slug):
        return {"ok": False, "message": t("Bad mod name.")}
    if not ld:
        return {"ok": False, "message": t("Cannot tell which mod loader this server uses.")}
    if not mc:
        return {"ok": False, "message": t("Cannot tell the Minecraft version of this server.")}
    inst = installed(server_dir)   # enabled or .disabled: a second copy of the same mod would crash the server at start
    norm = lambda x: re.sub(r"[-_ ]", "", x.lower())   # noqa: E731  (mod id cloth_config = Modrinth slug cloth-config)
    if any(norm(m["id"]) == norm(slug) for m in inst if m["id"]):
        return {"ok": False, "message": t("%s is already installed.") % slug}
    try:
        vs = _api("project/%s/version" % quote(slug), loaders=json.dumps([ld]), game_versions=json.dumps([mc]))
        if not vs:
            return {"ok": False, "message": t("No version of this mod for %s %s") % (LOADERS[ld], mc)}
        v = next((x for x in vs if x.get("version_type") == "release"), vs[0])
        fn = _pick(v)[0]
        if any(m["file"].replace(".jar.disabled", ".jar") == fn for m in inst):   # same file name = same mod, whatever its mod id
            return {"ok": False, "message": t("%s is already installed.") % slug}
        have = {x for m in inst for x in (m["id"].lower(), m["name"].lower())}
        deps = []
        ids = [d["project_id"] for d in v.get("dependencies", []) if d.get("dependency_type") == "required" and d.get("project_id")]
        if ids:
            for p in _api("projects", ids=json.dumps(ids)):
                if p["slug"].lower() not in have and p["title"].lower() not in have:
                    deps.append(p["title"])
        name = _install(server_dir, v, guard=guard)
        msg = t("%s installed. Restart the server to load it.") % ("%s %s" % (slug, v["version_number"]))
        if v.get("version_type", "release") != "release":   # updates offer releases only: say so now
            msg += " " + t("It is a test version (%s): no automatic updates until the mod has a release.") % v.get("version_type")
        return {"ok": True, "file": name, "version": v["version_number"], "deps": deps, "message": msg}
    except FsError:
        return _busy()
    except ERR as e:
        return _fail(e)


def check_updates(server_dir, ld, mc):
    """Asks Modrinth for newer versions of the installed jars, unless asked in the last CHECK_EVERY; True if the answer is new."""
    if not ld:
        return False
    with _lock:
        now, todo = time.time(), []
        md = os.path.join(server_dir, "mods")
        for m in installed(server_dir):
            if m["enabled"]:
                h = _sha1(os.path.join(md, m["file"]))
                k = _upd.get(h)
                if not k or k[1:3] != (ld, mc) or now - k[0] > CHECK_EVERY:
                    todo.append(h)
        if not todo:
            return False
        try:
            res = _api("version_files/update", {"hashes": todo, "algorithm": "sha1", "loaders": [ld], "game_versions": [mc]})
            for h in todo:
                v = res.get(h)
                v = v if v and v.get("version_type", "release") == "release" else None   # no beta/alpha offered as an update
                _upd[h] = (now, ld, mc, {"number": v["version_number"], "sha1": next((f["hashes"]["sha1"] for f in v["files"] if f.get("primary")), v["files"][0]["hashes"]["sha1"])} if v else None)
        except ERR:
            return False
        return True


def update(server_dir, name, ld, mc, guard=None):
    m = _find(server_dir, name)
    if not m or not ld:
        return {"ok": False, "message": t("Mod not found.")}
    try:
        h = _sha1(os.path.join(server_dir, "mods", name))
        v = _api("version_files/update", {"hashes": [h], "algorithm": "sha1", "loaders": [ld], "game_versions": [mc]}).get(h)
        if not v or v.get("version_type", "release") != "release":   # simplest correct: only releases are updates
            return {"ok": False, "message": t("No newer version found.")}
        new = _install(server_dir, v, old=name, guard=guard)
        _upd.pop(h, None)
        done = "%s %s" % (m["name"], v["version_number"])
        return {"ok": True, "file": new, "version": v["version_number"],
                "message": t("%s updated: it stays disabled.") % done if new.endswith(".disabled") else t("%s installed. Restart the server to load it.") % done}
    except FsError:
        return _busy()
    except ERR as e:
        return _fail(e)


# ---------------------------------------------------------------- client pack
def fetch_sides(shas):
    """Asks Modrinth, for the jars not asked yet, whether clients need the mod; True if anything was added. Offline: nothing is kept (the pack then holds everything)."""
    with _lock:
        d, ic = sides(), sides(ICONS)
        todo = [h for h in shas if h not in d or h not in ic]
        if not todo:
            return False
        try:
            vs, cs = {}, {}
            for i in range(0, len(todo), 500):
                vs.update(_api("version_files", {"hashes": todo[i:i + 500], "algorithm": "sha1"}))
            ids = sorted({v["project_id"] for v in vs.values()})
            for i in range(0, len(ids), 100):
                for p in _api("projects", ids=json.dumps(ids[i:i + 100])):
                    cs[p["id"]] = (p["client_side"], p.get("icon_url") or "")
        except ERR:
            return False
        for h in todo:
            d[h], ic[h] = cs.get((vs.get(h) or {}).get("project_id"), ("", ""))
        paths.write_json(SIDES, d)
        paths.write_json(ICONS, ic)
        return True


def pack(server_dir, ld, lv, mc):
    """(zip path, names of the mods left out as server-only): the enabled mods players need, rebuilt only when the mods change."""
    md = os.path.join(server_dir, "mods")
    mods = [m for m in installed(server_dir) if m["enabled"]]
    sha = {m["file"]: _sha1(os.path.join(md, m["file"])) for m in mods}
    fetch_sides(list(sha.values()))
    sd = sides()
    skip = [m for m in mods if sd.get(sha[m["file"]]) == "unsupported"]
    keep = [m for m in mods if m not in skip]
    key = hashlib.sha1(json.dumps([[m["file"], m["size"], m["mtime"]] for m in keep] + [ld, lv, mc]).encode()).hexdigest()[:16]
    path = os.path.join(paths.CACHE, "client-mods-%s.zip" % key)
    with _pack_lock:
        if not os.path.isfile(path):
            os.makedirs(paths.CACHE, exist_ok=True)
            for n in os.listdir(paths.CACHE):
                if n.startswith("client-mods-"):
                    try:
                        os.remove(os.path.join(paths.CACHE, n))
                    except OSError:
                        pass
            readme = "%s %s, Minecraft %s\r\n\r\n%s\r\n" % (LOADERS.get(ld, "?"), lv, mc, t("Put the files of the mods folder in the mods folder of your Minecraft:")) \
                + "".join("\r\n%s (%s)" % (m["name"], m["file"]) for m in keep)
            with zipfile.ZipFile(path + ".tmp", "w", zipfile.ZIP_STORED, strict_timestamps=False) as z:   # jars are compressed already
                z.writestr("README.txt", readme)
                for m in keep:
                    z.write(os.path.join(md, m["file"]), "mods/" + m["file"])
            os.replace(path + ".tmp", path)
    return path, [m["name"] for m in skip]
