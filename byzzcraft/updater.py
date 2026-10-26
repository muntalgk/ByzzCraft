"""Updates the panel to the latest GitHub release (standard library only).

How depends on how it was installed:
- git clone: fetch the tags and check out the release tag (refused if tracked files were changed by hand);
- zip (the Windows release zip): download it, check it against the release's SHA256SUMS and replace only the
  code; data/ and anything that is not code stays;
- Docker: the image is updated from outside (docker compose pull).
"""
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import urllib.request
import zipfile

from .i18n import t

ZIP_ASSET = "ByzzCraft-windows.zip"
PREFIX = "ByzzCraft/"   # folder inside the zip (git archive --prefix in ci.yml)
RECORD = ".byzzcraft-files"   # top-level code entries the last zip update installed: the next one removes those gone
# the code of 1.0, before byzzcraft/: removed from zip installs that have no RECORD yet
LEGACY = ("access.py", "activity.py", "assets.py", "backups.py", "i18n.py", "mapgen.py", "paths.py", "players.py",
          "runtime.py", "sysinfo.py", "timeline.py", "app.html", "i18n")
NEVER = {"data", "assets", ".git", RECORD, ".update-new", ".update-old"}   # data, also of installs from before 1.0
_NOWINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


class UpdateError(Exception):
    """The message to show; nothing was changed (or the old version was put back)."""


def _get(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": "byzzcraft", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def vtuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


def kind(root, dockerenv="/.dockerenv"):
    if os.path.exists(dockerenv):
        return "docker"
    return "git" if os.path.isdir(os.path.join(root, ".git")) else "zip"


def latest_release(repo, get=_get):
    d = json.loads(get("https://api.github.com/repos/%s/releases/latest" % repo))
    return {"tag": d["tag_name"], "version": d["tag_name"].lstrip("v"),
            "assets": {a["name"]: a["browser_download_url"] for a in d.get("assets", [])}}


def update(root, repo, current, log=print, get=_get, dockerenv="/.dockerenv"):
    """The new version, or None when already on the latest release. UpdateError with the message to show."""
    k = kind(root, dockerenv)
    if k == "docker":
        raise UpdateError(t("In Docker the image is updated from outside: docker compose pull && docker compose up -d"))
    try:
        rel = latest_release(repo, get)
    except (OSError, ValueError, KeyError) as e:
        raise UpdateError(t("Cannot ask GitHub for the latest release (%s).") % e)
    if vtuple(rel["version"]) <= vtuple(current):
        return None
    log("updating %s -> %s (%s install)" % (current, rel["version"], k))
    (_git if k == "git" else _zip)(root, rel, log, get)
    return rel["version"]


def _git(root, rel, log, get):
    def git(*args):
        try:
            r = subprocess.run(["git", "-C", root] + list(args), capture_output=True, text=True, timeout=180, **_NOWINDOW)
        except (OSError, subprocess.SubprocessError) as e:
            raise UpdateError(t("git does not run (%s).") % e)
        if r.returncode:
            raise UpdateError("git %s: %s" % (args[0], (r.stderr or r.stdout).strip()[-300:]))
        return r.stdout
    if git("status", "--porcelain", "--untracked-files=no").strip():
        raise UpdateError(t("Files of the panel were changed by hand (see git status): commit or stash them, then update again."))
    git("fetch", "--quiet", "--tags", "--force", "origin")
    git("checkout", "--quiet", rel["tag"])   # a release, not the tip of main: a detached HEAD on purpose
    log("git: now on %s" % rel["tag"])


def _record(root):
    try:
        with open(os.path.join(root, RECORD), encoding="utf-8") as f:
            return [n for n in f.read().split() if n and "/" not in n and "\\" not in n and n not in (".", "..")]
    except OSError:
        return None


def _rm(p):
    if os.path.isdir(p) and not os.path.islink(p):
        shutil.rmtree(p)
    elif os.path.lexists(p):
        os.remove(p)


def _zip(root, rel, log, get):
    url, sums = rel["assets"].get(ZIP_ASSET), rel["assets"].get("SHA256SUMS")
    if not url or not sums:
        raise UpdateError(t("Release %s has no %s with its SHA256SUMS: download it by hand from GitHub.") % (rel["tag"], ZIP_ASSET))
    data = get(url, 300)
    want = next((line.split()[0].lower() for line in get(sums).decode("utf-8", "replace").splitlines()
                 if line.split()[-1:] == [ZIP_ASSET] or line.split()[-1:] == ["*" + ZIP_ASSET]), None)
    if not want or hashlib.sha256(data).hexdigest() != want:
        raise UpdateError(t("The downloaded zip does not match SHA256SUMS: nothing was changed."))
    tmp, old = os.path.join(root, ".update-new"), os.path.join(root, ".update-old")
    for d in (tmp, old):
        shutil.rmtree(d, ignore_errors=True)
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = z.namelist()
            if not names or any(not n.startswith(PREFIX) or ".." in n.split("/") or "\\" in n or ":" in n for n in names):
                raise UpdateError(t("The release zip has unexpected paths: nothing was changed."))
            z.extractall(tmp)
    except zipfile.BadZipFile as e:
        raise UpdateError(t("The release zip is damaged (%s): nothing was changed.") % e)
    src = os.path.join(tmp, PREFIX.rstrip("/"))
    new = sorted(n for n in os.listdir(src) if n not in NEVER)
    if "panel.py" not in new or "byzzcraft" not in new:
        shutil.rmtree(tmp, ignore_errors=True)
        raise UpdateError(t("The release zip has unexpected paths: nothing was changed."))
    gone = [n for n in (_record(root) or LEGACY) if n not in new and n not in NEVER]
    os.makedirs(old)
    moved, placed = [], []
    try:
        for n in new + gone:   # the old code aside first, so a failure can put it back
            if os.path.lexists(os.path.join(root, n)):
                os.replace(os.path.join(root, n), os.path.join(old, n))
                moved.append(n)
        for n in new:
            os.replace(os.path.join(src, n), os.path.join(root, n))
            placed.append(n)
    except OSError as e:
        for n in placed:
            _rm(os.path.join(root, n))
        for n in moved:
            os.replace(os.path.join(old, n), os.path.join(root, n))
        shutil.rmtree(old, ignore_errors=True)
        raise UpdateError(t("Could not replace the files (%s): the old version is back.") % e)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    with open(os.path.join(root, RECORD), "w", encoding="utf-8") as f:
        f.write("\n".join(new) + "\n")
    shutil.rmtree(old, ignore_errors=True)
    log("zip: %d entries replaced, %d removed" % (len(new), len([n for n in gone if n in moved])))
