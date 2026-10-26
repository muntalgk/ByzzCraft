"""panel.py update: zip installs (checksum, stale files, rollback, bad zips), git installs (release tag, local changes),
Docker and "already up to date". No network: GitHub's answers are faked, the git remote is a local folder."""
import hashlib, io, json, os, shutil, subprocess, sys, tempfile, zipfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))  # panel folder
from byzzcraft import updater as U

OK = FAIL = 0


def check(name, cond, extra=""):
    global OK, FAIL
    OK, FAIL = (OK + 1, FAIL) if cond else (OK, FAIL + 1)
    if not cond:
        print("FAIL:", name, extra)


def read(*p):
    with open(os.path.join(*p), encoding="utf-8") as f:
        return f.read()


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def raw_zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, text in files.items():
            z.writestr(name, text)
    return buf.getvalue()


def release_zip(files):   # as ci.yml makes it: everything in ByzzCraft/
    return raw_zip({U.PREFIX + n: v for n, v in files.items()})


def github(version, zip_bytes=None, sums=True, bad_sum=False):
    """A fake get(): the releases/latest answer, the zip and its SHA256SUMS."""
    assets = []
    if zip_bytes is not None:
        assets.append({"name": U.ZIP_ASSET, "browser_download_url": "https://x/zip"})
        if sums:
            assets.append({"name": "SHA256SUMS", "browser_download_url": "https://x/sums"})
    digest = "0" * 64 if bad_sum else hashlib.sha256(zip_bytes or b"").hexdigest()
    pages = {"https://api.github.com/repos/o/r/releases/latest": json.dumps({"tag_name": "v" + version, "assets": assets}).encode(),
             "https://x/zip": zip_bytes, "https://x/sums": ("%s  %s\n" % (digest, U.ZIP_ASSET)).encode()}
    return lambda url, timeout=60: pages[url]


root = tempfile.mkdtemp(prefix="upd_test_")
nodocker = os.path.join(root, "no-dockerenv")
try:
    # kind of install
    inst = os.path.join(root, "inst")
    os.makedirs(inst)
    check("kind: zip", U.kind(inst, nodocker) == "zip")
    write(os.path.join(root, "dockerenv"), "")
    check("kind: docker", U.kind(inst, os.path.join(root, "dockerenv")) == "docker")
    try:
        U.update(inst, "o/r", "1.0.0", log=lambda m: None, get=github("1.2.0"), dockerenv=os.path.join(root, "dockerenv"))
        check("docker: refused", False)
    except U.UpdateError as e:
        check("docker: points to compose", "docker compose pull" in str(e), e)
    check("already on the latest release", U.update(inst, "o/r", "1.2.0", log=lambda m: None, get=github("1.2.0"), dockerenv=nodocker) is None)
    check("newer than the latest release (main): not moved back", U.update(inst, "o/r", "1.3.0", log=lambda m: None, get=github("1.2.0"), dockerenv=nodocker) is None)

    # zip install laid out as 1.0: code in the root, data next to it, a file of the user
    for name, text in {"panel.py": "old", "access.py": "old", "i18n/it.json": "{}", "ByzzCraft.bat": "old",
                       "data/panel-users.json": "users", "assets/block/stone.png": "png", "notes.txt": "mine"}.items():
        write(os.path.join(inst, name), text)
    z1 = release_zip({"panel.py": "new", "byzzcraft/__init__.py": "", "byzzcraft/app.html": "page", "ByzzCraft.bat": "new"})
    quiet = lambda m: None

    for label, kw, extra in (("checksum mismatch", {"bad_sum": True}, {}), ("no SHA256SUMS", {"sums": False}, {})):
        try:
            U.update(inst, "o/r", "1.0.0", log=quiet, get=github("1.2.0", z1, **kw), dockerenv=nodocker)
            check(label + ": refused", False)
        except U.UpdateError:
            check(label + ": nothing changed", read(inst, "panel.py") == "old" and os.path.exists(os.path.join(inst, "access.py")))
    for label, files in (("zip slip", {"ByzzCraft/panel.py": "x", "ByzzCraft/byzzcraft/a.py": "", "ByzzCraft/../evil.txt": "x"}),
                         ("outside the prefix", {"ByzzCraft/panel.py": "x", "ByzzCraft/byzzcraft/a.py": "", "Other/x.py": "x"}),
                         ("no panel.py", {"ByzzCraft/README.md": "x"})):
        data = raw_zip(files)
        try:
            U.update(inst, "o/r", "1.0.0", log=quiet, get=github("1.2.0", data), dockerenv=nodocker)
            check(label + ": refused", False)
        except U.UpdateError:
            check(label + ": nothing changed", read(inst, "panel.py") == "old" and not os.path.exists(os.path.join(root, "evil.txt")))

    # a failure halfway: the old version comes back
    real = os.replace

    def flaky(a, b):
        if os.path.basename(a) == "byzzcraft" and ".update-new" in a:
            raise OSError("disk full")
        return real(a, b)
    U.os.replace = flaky
    try:
        U.update(inst, "o/r", "1.0.0", log=quiet, get=github("1.2.0", z1), dockerenv=nodocker)
        check("rollback: error raised", False)
    except U.UpdateError as e:
        check("rollback: old version back", read(inst, "panel.py") == "old" and read(inst, "access.py") == "old"
              and read(inst, "ByzzCraft.bat") == "old" and not os.path.exists(os.path.join(inst, "byzzcraft")), e)
        check("rollback: no leftovers", not any(n.startswith(".update-") for n in os.listdir(inst)), os.listdir(inst))
    finally:
        U.os.replace = real

    check("zip update", U.update(inst, "o/r", "1.0.0", log=quiet, get=github("1.2.0", z1), dockerenv=nodocker) == "1.2.0")
    check("zip: new code", read(inst, "panel.py") == "new" and read(inst, "byzzcraft", "app.html") == "page" and read(inst, "ByzzCraft.bat") == "new")
    check("zip: 1.0 code removed", not os.path.exists(os.path.join(inst, "access.py")) and not os.path.exists(os.path.join(inst, "i18n")))
    check("zip: data, old assets and the user's file kept", read(inst, "data", "panel-users.json") == "users"
          and read(inst, "assets", "block", "stone.png") == "png" and read(inst, "notes.txt") == "mine")
    check("zip: record written, no leftovers", read(inst, U.RECORD).split() == ["ByzzCraft.bat", "byzzcraft", "panel.py"]
          and not any(n.startswith(".update-") for n in os.listdir(inst)))
    # next release drops a file: the record says it was code, so it goes; the user's file still stays
    z2 = release_zip({"panel.py": "newer", "byzzcraft/__init__.py": "", "README.md": "readme"})
    U.update(inst, "o/r", "1.2.0", log=quiet, get=github("1.3.0", z2), dockerenv=nodocker)
    check("zip: file dropped by a release removed", not os.path.exists(os.path.join(inst, "ByzzCraft.bat"))
          and read(inst, "README.md") == "readme" and read(inst, "notes.txt") == "mine" and read(inst, "data", "panel-users.json") == "users")

    # git install: a clone on the old release moves to the new release tag
    git = lambda cwd, *a: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "init.defaultBranch=main"] + list(a),
                                         cwd=cwd, capture_output=True, text=True, check=True).stdout
    origin, clone = os.path.join(root, "origin"), os.path.join(root, "clone")
    os.makedirs(origin)
    git(origin, "init", "-q")
    write(os.path.join(origin, "panel.py"), "v1")
    git(origin, "add", "panel.py"); git(origin, "commit", "-qm", "v1"); git(origin, "tag", "v1.0.0")
    git(root, "clone", "-q", origin, clone)
    write(os.path.join(origin, "panel.py"), "v2")
    git(origin, "commit", "-qam", "v2"); git(origin, "tag", "v1.2.0")
    write(os.path.join(origin, "panel.py"), "v3 not released")
    git(origin, "commit", "-qam", "v3")
    write(os.path.join(clone, "data", "panel-users.json"), "users")   # untracked data: allowed
    write(os.path.join(clone, "panel.py"), "edited by hand")
    try:
        U.update(clone, "o/r", "1.0.0", log=quiet, get=github("1.2.0"), dockerenv=nodocker)
        check("git: local changes refused", False)
    except U.UpdateError as e:
        check("git: local changes refused", "git status" in str(e) and read(clone, "panel.py") == "edited by hand", e)
    git(clone, "checkout", "-q", "panel.py")
    check("git update", U.update(clone, "o/r", "1.0.0", log=quiet, get=github("1.2.0"), dockerenv=nodocker) == "1.2.0")
    check("git: on the release, not on main", read(clone, "panel.py") == "v2"
          and git(clone, "rev-parse", "HEAD") == git(origin, "rev-parse", "v1.2.0^{commit}"))
    check("git: data kept", read(clone, "data", "panel-users.json") == "users")
finally:
    shutil.rmtree(root, ignore_errors=True)
print("PASS %d  FAIL %d" % (OK, FAIL))
sys.exit(1 if FAIL else 0)
