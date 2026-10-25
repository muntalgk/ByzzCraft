"""Java for the Minecraft server: finds the installed one or downloads one (Eclipse Temurin) into the panel's data.

Usage:  python panel.py java      (downloads the version the server jar asks for)
"""
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import tarfile
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
from paths import RUNTIME  # noqa: E402
API = "https://api.adoptium.net/v3/assets/latest/%d/hotspot?os=%s&architecture=%s&image_type=jre&vendor=eclipse"
EXE = "java.exe" if os.name == "nt" else "java"


def _open(url):
    # the Adoptium API answers 403 to Python's default User-Agent
    return urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "byzzcraft (+https://github.com/muntalgk/byzzcraft)"}), timeout=60)


def required(server_dir, jar=None):
    """Java version the server jar asks for (vanilla, Paper, Fabric launcher), None if unknown."""
    import assets   # here and not at the top: assets loads mapgen, which runtime.py alone does not need
    return assets.jar_info(server_dir, jar or assets.find_jar(server_dir))[1]


def parse_version(text):
    """Major version from `java -version`: 'openjdk version "25.0.1"' -> 25, 'java version "1.8.0_401"' -> 8."""
    m = re.search(r'version "(\d+)(?:\.(\d+))?', text)
    if not m:
        return None
    major = int(m.group(1))
    return int(m.group(2) or 0) if major == 1 else major


_VER = {}   # (java path, mtime) -> major: `java -version` takes seconds, health and settings ask often


def version_of(java):
    exe = shutil.which(java) or java
    try:
        key = (exe, os.path.getmtime(exe))
    except OSError:
        key = None
    if key in _VER:
        return _VER[key]
    v = _run_version(java)
    if key and v:
        _VER[key] = v
    return v


def _run_version(java):
    try:
        # no console window flashing on Windows when the panel runs without one (pythonw)
        r = subprocess.run([java, "-version"], capture_output=True, text=True, timeout=30,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired):
        return None
    return parse_version(r.stderr + r.stdout)


def bundled(major):
    """java downloaded by the panel for this version, if there is one."""
    for root, _, files in os.walk(os.path.join(RUNTIME, "java-%d" % major)):
        if EXE in files and os.path.basename(root) == "bin":
            return os.path.join(root, EXE)
    return None


def java_for(major):
    """(java to use or None, version found in PATH or None)."""
    if major and bundled(major):
        return bundled(major), major
    v = version_of("java")
    return ("java" if v and (not major or v >= major) else None), v


def _platform():
    m = platform.machine().lower()
    arch = {"amd64": "x64", "x86_64": "x64", "aarch64": "aarch64", "arm64": "aarch64"}.get(m)
    if not arch:
        raise OSError("no Java download for this CPU (%s): install Java by hand" % m)
    return ("windows" if os.name == "nt" else "linux"), arch


def _extract(path, dest):
    """Extracts a tar.gz or zip into dest without leaving it (no absolute paths, .. or links pointing outside)."""
    root = os.path.realpath(dest)

    def safe(name):
        p = os.path.realpath(os.path.join(root, name))
        return p == root or p.startswith(root + os.sep)
    if path.endswith(".zip"):
        with zipfile.ZipFile(path) as z:
            bad = [n for n in z.namelist() if not safe(n)]
            if bad:
                raise OSError("unsafe archive entry: %s" % bad[0])
            z.extractall(root)
        return
    with tarfile.open(path) as tf:
        if hasattr(tarfile, "data_filter"):   # Python 3.12 and 3.11.4+
            tf.extractall(root, filter="data")
            return
        for m in tf.getmembers():
            link = m.issym() and not safe(os.path.join(os.path.dirname(m.name), m.linkname))
            if not safe(m.name) or link or m.islnk() or m.isdev():
                raise OSError("unsafe archive entry: %s" % m.name)
        tf.extractall(root)


def install(major, log=print):
    """Downloads Java `major` into runtime/java-<major>/ and returns the path of the executable."""
    osn, arch = _platform()
    with _open(API % (major, osn, arch)) as r:
        builds = json.loads(r.read())
    if not builds:
        raise OSError("no Java %d for %s/%s" % (major, osn, arch))
    pkg = builds[0]["binary"]["package"]
    log("Downloading Java %d (%s, %d MB)..." % (major, pkg["name"], pkg["size"] // 1048576))
    tmp = os.path.join(RUNTIME, "download-tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    archive = os.path.join(tmp, pkg["name"])
    h = hashlib.sha256()
    with _open(pkg["link"]) as r, open(archive, "wb") as f:
        for chunk in iter(lambda: r.read(1 << 20), b""):
            h.update(chunk)
            f.write(chunk)
    if h.hexdigest() != pkg["checksum"]:
        raise OSError("corrupted Java download")
    out = os.path.join(tmp, "out")
    _extract(archive, out)
    dest = os.path.join(RUNTIME, "java-%d" % major)
    shutil.rmtree(dest, ignore_errors=True)
    os.replace(out, dest)
    shutil.rmtree(tmp, ignore_errors=True)
    java = bundled(major)
    if not java or (version_of(java) or 0) < major:
        raise OSError("the downloaded Java does not start")
    log("Java %d installed in %s" % (major, os.path.dirname(os.path.dirname(java))))
    return java
