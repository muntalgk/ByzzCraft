import ctypes, json, os, shutil, sys, tempfile, time, zipfile, traceback
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))  # panel folder
import backups as B

OK = FAIL = 0
def check(name, cond, extra=""):
    global OK, FAIL
    if cond:
        OK += 1
    else:
        FAIL += 1
        print("FAIL:", name, extra)

def raises(name, exc, fn, *a, **k):
    try:
        fn(*a, **k)
    except exc as e:
        check(name, True)
        return str(e)
    except Exception as e:
        check(name, False, "different exception: %r" % e)
        return ""
    check(name, False, "no exception")
    return ""

if os.name == "nt":
    from ctypes import wintypes
    k32 = ctypes.windll.kernel32
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    def lock_exclusive(path):
        h = k32.CreateFileW(path, 0x80000000 | 0x40000000, 0, None, 3, 0x80, None)  # share=0, OPEN_EXISTING
        assert h not in (None, wintypes.HANDLE(-1).value), "lock failed"
        return h
    unlock = k32.CloseHandle
else:
    # Linux does not lock open files: an unreadable file simulates the same case (not as root)
    def lock_exclusive(path):
        os.chmod(path, 0)
        return path
    def unlock(path):
        os.chmod(path, 0o644)

def write(p, data=b"x"):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(data)

def mkzip(p, names, mtime=None):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with zipfile.ZipFile(p, "w") as z:
        for n in names:
            z.writestr(n, b"data-" + n.encode())
    if mtime:
        os.utime(p, (mtime, mtime))

class Rcon:
    def __init__(self): self.calls = []; self.fail_on = None
    def __call__(self, cmd):
        self.calls.append(cmd)
        if cmd == self.fail_on:
            raise RuntimeError("rcon down")
        return "ok"

def fresh():
    root = tempfile.mkdtemp(prefix="bk_test_")
    s = os.path.join(root, "srv")
    w = os.path.join(s, "world")
    write(os.path.join(w, "level.dat"), b"LEVEL-ORIG")
    write(os.path.join(w, "dimensions", "minecraft", "overworld", "region", "r.0.0.mca"), b"R" * 5000)
    write(os.path.join(w, "players", "data", "a.dat"), b"P")
    os.makedirs(os.path.join(w, "empty"))
    write(os.path.join(w, "session.lock"), b"lock")
    write(os.path.join(w, "locked.dat"), b"locked")
    os.makedirs(os.path.join(s, "backups")); os.makedirs(os.path.join(s, "mondi-vecchi"))
    return root, s, os.path.join(root, "state.json")

changes = []
def mk(s, st):
    changes.clear()
    return B.Backups(s, st, on_change=lambda: changes.append(1))

def run():
    # ---------- list / kind
    root, s, st = fresh()
    t0 = time.time() - 10000
    mkzip(os.path.join(s, "backups", "world_2026-10-19_14-39-35.zip"), ["world/level.dat"], t0 + 1)
    mkzip(os.path.join(s, "backups", "auto_2026-10-19_15-00-00.zip"), ["world/level.dat"], t0 + 2)
    mkzip(os.path.join(s, "backups", "manuale_2026-10-19_16-00-00.zip"), ["world/level.dat"], t0 + 3)
    mkzip(os.path.join(s, "backups", "prima-di-survival_x.zip"), ["world/level.dat"], t0 + 4)
    write(os.path.join(s, "backups", "world_2026-10-19_17-00-00.zip.part"))
    write(os.path.join(s, "mondi-vecchi", "world_old", "level.dat"), b"OLD")
    write(os.path.join(s, "mondi-vecchi", "world_old", "a", "b.bin"), b"1234567890")
    b = mk(s, st)
    l = b.list()
    kinds = {e["name"]: e["kind"] for e in l}
    check("list kinds", kinds == {"world_2026-10-19_14-39-35.zip": "startup", "auto_2026-10-19_15-00-00.zip": "scheduled",
          "manuale_2026-10-19_16-00-00.zip": "manual", "prima-di-survival_x.zip": "other", "world_old": "world"}, kinds)
    check("list ignores .part", not any(e["name"].endswith(".part") for e in l))
    check("list order", l[0]["mtime"] >= l[-1]["mtime"])
    old = [e for e in l if e["kind"] == "world"][0]
    check("folder size", old["size"] == 13 and old["id"] == "mondi-vecchi/world_old", old)
    d = b.disk(); check("disk", d["free"] > 0 and d["total"] >= d["free"])
    check("status base", b.status()["busy"] is None and b.status()["schedule"]["every_minutes"] == 120)

    # ---------- malicious ids
    for bad in ["../x", "C:\\x", "backups/../world", "backups/..", "/etc/passwd", "backups\\x.zip",
                "backups/nonesiste.zip", "backups/a/b.zip", "", None, "world"]:
        raises("malicious id %r" % (bad,), ValueError, b.path, bad)
        raises("malicious id delete %r" % (bad,), ValueError, b.delete, bad)
        raises("malicious id restore %r" % (bad,), ValueError, b.restore, bad, False)
    raises("path folder", ValueError, b.path, "mondi-vecchi/world_old")
    check("path zip", b.path("backups/auto_2026-10-19_15-00-00.zip").endswith("auto_2026-10-19_15-00-00.zip"))

    # ---------- create with the server off (with a locked file)
    hl = lock_exclusive(os.path.join(s, "world", "locked.dat"))
    hs = lock_exclusive(os.path.join(s, "world", "session.lock"))
    try:
        # an unreadable file (other than session.lock) makes the backup fail, it is not skipped
        raises("create with locked file fails", OSError, b.create, "manual", False)
        check("error names the file", "locked.dat" in (b.status()["last_error"] or ""), b.status()["last_error"])
        check("no partial zip", not [x for x in os.listdir(os.path.join(s, "backups")) if x.endswith(".part") or x.startswith(".tmp")])
        r = Rcon(); time.sleep(1.1)
        raises("create on with locked file fails", OSError, b.create, "manual", True, r)
        check("save-on even if the copy fails", r.calls[-1] == "save-on", r.calls)
        unlock(hl); hl = None
        time.sleep(1.1)
        e = b.create("manual", server_on=False, note="nota prova")
        check("create off name", e["name"].startswith("manual_") and e["kind"] == "manual" and e["note"] == "nota prova", e)
        with zipfile.ZipFile(b.path(e["id"])) as z:
            n = z.namelist()
        check("zip ha world/level.dat", "world/level.dat" in n, n)
        check("zip without session.lock", not any("session.lock" in x for x in n), n)
        check("zip with locked.dat (readable now)", "world/locked.dat" in n, n)
        check("zip has region", "world/dimensions/minecraft/overworld/region/r.0.0.mca" in n)
        check("on_change called", len(changes) >= 2)
        check("no .part/.tmp", not [x for x in os.listdir(os.path.join(s, "backups")) if x.endswith(".part") or x.startswith(".tmp")])

        # ---------- create with the server on
        r = Rcon()
        time.sleep(1.1)
        e2 = b.create("scheduled", server_on=True, rcon=r)
        check("rcon order", r.calls == ["save-off", "save-all flush", "save-on"], r.calls)
        check("auto_ name", e2["name"].startswith("auto_") and e2["kind"] == "scheduled", e2)
        with zipfile.ZipFile(b.path(e2["id"])) as z:
            n2 = z.namelist()
        check("zip on: world/level.dat", "world/level.dat" in n2)
        check("zip on: without lock", not any("session.lock" in x for x in n2), n2)
        check("tmp removed", not [x for x in os.listdir(os.path.join(s, "backups")) if x.startswith(".tmp")])

        # zip that fails: save-on anyway, cleanup
        r = Rcon(); b._zip_world = lambda *a, **k: (_ for _ in ()).throw(OSError("zip rotto"))
        time.sleep(1.1)
        raises("create zip failed", OSError, b.create, "manual", True, r)
        check("save-on after failed zip", r.calls == ["save-off", "save-all flush", "save-on"], r.calls)
        check("last_error", "zip rotto" in (b.status()["last_error"] or ""))
        check("busy released", b.status()["busy"] is None)
        check("cleanup after error", not [x for x in os.listdir(os.path.join(s, "backups")) if x.endswith(".part") or x.startswith(".tmp")])
        del b._zip_world
        # flush fails -> save-on anyway
        r = Rcon(); r.fail_on = "save-all flush"
        raises("flush failed", RuntimeError, b.create, "manual", True, r)
        check("save-on after failed flush", r.calls == ["save-off", "save-all flush", "save-on"], r.calls)
        # save-off fails -> save-on anyway
        r = Rcon(); r.fail_on = "save-off"
        raises("save-off failed", RuntimeError, b.create, "manual", True, r)
        check("save-on after failed save-off", r.calls[-1] == "save-on", r.calls)
    finally:
        if hl:
            unlock(hl)
        unlock(hs)

    # busy
    import threading
    b._op_lock.acquire()
    raises("busy", ValueError, b.create)
    raises("busy delete", ValueError, b.delete, "backups/auto_2026-10-19_15-00-00.zip")
    raises("busy restore", ValueError, b.restore, "backups/auto_2026-10-19_15-00-00.zip", False)
    b._op_lock.release()
    raises("invalid kind", ValueError, b.create, "boh")

    # ---------- pin / delete / trash
    ide = "backups/prima-di-survival_x.zip"
    b.pin(ide, True)
    check("pin", [e for e in b.list() if e["id"] == ide][0]["pinned"])
    check("pin saved", B.Backups(s, st).list() and ide in B.Backups(s, st)._state["pinned"])
    msg = raises("delete pinned", ValueError, b.delete, ide)
    check("pinned msg", msg == "Unpin it first", msg)
    b.pin(ide, False)
    b.delete(ide)
    check("delete: gone from disk", not os.path.exists(os.path.join(s, "backups", "prima-di-survival_x.zip")))
    check("delete: gone from list", ide not in [e["id"] for e in b.list()])
    b.delete("mondi-vecchi/world_old")
    check("delete folder", not os.path.exists(os.path.join(s, "mondi-vecchi", "world_old")))

    # ---------- prune
    root, s, st = fresh(); b = mk(s, st)
    base = time.time() - 100000
    for i in range(6):
        ts = datetime.fromtimestamp(base + i * 100).strftime(B.TS_FMT)
        mkzip(os.path.join(s, "backups", "world_%s.zip" % ts), ["world/level.dat"], base + i * 100)
        mkzip(os.path.join(s, "backups", "manuale_%s.zip" % ts), ["world/level.dat"], base + i * 100)
    mkzip(os.path.join(s, "backups", "altro1.zip"), ["world/level.dat"], base)
    write(os.path.join(s, "mondi-vecchi", "w1", "level.dat"))
    names = sorted(e["name"] for e in b.list() if e["kind"] == "startup")
    b.pin("backups/" + names[0], True); b.pin("backups/" + names[1], True)  # the 2 oldest pinned
    cfg = b.set_schedule({"keep": {"startup": 2, "scheduled": 0, "manual": 0}})
    check("set_schedule keep", cfg["keep"]["startup"] == 2 and cfg["every_minutes"] == 120)
    rem = b.prune()
    left = sorted(e["name"] for e in b.list() if e["kind"] == "startup")
    check("prune: 2 removed (6-2 pinned-2 kept)", len(rem) == 2, rem)
    check("prune: keeps pinned + 2 newest", left == names[:2] + names[-2:], (left, names))
    check("prune: manual ones untouched", len([e for e in b.list() if e["kind"] == "manual"]) == 6)
    check("prune: other/world untouched", any(e["kind"] == "other" for e in b.list()) and any(e["kind"] == "world" for e in b.list()))
    check("prune: disco", all(not os.path.exists(os.path.join(s, "backups", os.path.basename(r))) for r in rem))
    b.set_schedule({"keep": {"manual": 3}})
    rem = b.prune(); check("prune manual 3", len(rem) == 3 and len([e for e in b.list() if e["kind"] == "manual"]) == 3, rem)
    check("prune idempotent", b.prune() == [])

    # Windows: a Recycle Bin that refuses (tiny limit) must not stop automatic cleanup, only that
    if os.name == "nt":
        real_limit = B._recycle_limit
        B._recycle_limit = lambda path: (1, False)
        try:
            root, s, st = fresh(); b = mk(s, st)
            for i in range(3):
                ts = datetime.fromtimestamp(base + i * 100).strftime(B.TS_FMT)
                mkzip(os.path.join(s, "backups", "world_%s.zip" % ts), ["world/level.dat"], base + i * 100)
                mkzip(os.path.join(s, "backups", "manuale_%s.zip" % ts), ["world/level.dat"], base + i * 100)
            b.set_schedule({"keep": {"startup": 1, "scheduled": 0, "manual": 0}})
            rem = b.prune()
            check("prune: bin refuses, automatic zips still removed", len(rem) == 2, rem)
            man = next(e for e in b.list() if e["kind"] == "manual")
            raises("manual delete still refused by the bin", OSError, b.delete, man["id"])
            check("manual zip still on disk", os.path.exists(os.path.join(s, "backups", man["name"])))
        finally:
            B._recycle_limit = real_limit

    # ---------- schedule validation
    for bad in [{"every_hours": 49}, {"every_hours": -1}, {"every_hours": "x"}, {"daily_at": "25:00"}, {"daily_at": "4pm"},
                {"keep": {"startup": 201}}, {"keep": {"startup": -1}}, "no", {"every_hours": True}]:
        raises("invalid schedule %r" % (bad,), ValueError, b.set_schedule, bad)
    c = b.set_schedule({"every_hours": 0, "daily_at": "", "only_when_online": False})
    check("schedule normalised", c["every_minutes"] == 0 and c["daily_at"] == [] and c["only_when_online"] is False)
    check("schedule saved", B.Backups(s, st).status()["schedule"] == c)
    # corrupt state
    with open(st, "w") as f: f.write("{non json")
    check("corrupt state -> default", B.Backups(s, st).status()["schedule"]["every_minutes"] == 120)

    # ---------- interval tick
    root, s, st = fresh(); b = mk(s, st); r = Rcon()
    T = 1_800_000_000.0
    b.set_schedule({"every_hours": 2, "daily_at": "", "only_when_online": True})
    def wait():
        th = b._sched_thread
        if th: th.join(20)
    b.tick(T, True, 1, r); wait()
    check("tick: first tick does not run", not [e for e in b.list() if e["kind"] == "scheduled"])
    b.tick(T + 3600, True, 1, r); wait()
    check("tick: not after 1h", not [e for e in b.list() if e["kind"] == "scheduled"])
    b.tick(T + 7300, True, 0, r); wait()
    check("tick: not without players", not [e for e in b.list() if e["kind"] == "scheduled"])
    b.tick(T + 7300, False, 1, r); wait()
    check("tick: not with server off (interval)", not [e for e in b.list() if e["kind"] == "scheduled"])
    b.tick(T + 7300, True, 1, r); wait()
    check("tick: runs after 2h with players", len([e for e in b.list() if e["kind"] == "scheduled"]) == 1)
    check("tick: rcon used", r.calls == ["save-off", "save-all flush", "save-on"], r.calls)
    check("tick: last_scheduled", b.status()["last_scheduled"] == T + 7300)
    time.sleep(1.1)
    b.tick(T + 7300 + 3600, True, 1, r); wait()
    check("tick: not right after", len([e for e in b.list() if e["kind"] == "scheduled"]) == 1)
    check("next_scheduled", abs(b.status()["next_scheduled"] - (T + 7300 + 7200)) < 1)
    b.set_schedule({"only_when_online": False})
    time.sleep(1.1)
    b.tick(T + 7300 + 7300, True, 0, r); wait()
    check("tick: only_when_online off", len([e for e in b.list() if e["kind"] == "scheduled"]) == 2)
    # keep in the tick
    b.set_schedule({"keep": {"scheduled": 1}})
    time.sleep(1.1)
    b.tick(T + 40000, True, 0, r); wait()
    check("tick: prune keeps 1 scheduled", len([e for e in b.list() if e["kind"] == "scheduled"]) == 1,
          [e["name"] for e in b.list()])

    # ---------- daily tick
    root, s, st = fresh(); b = mk(s, st); r = Rcon()
    b.set_schedule({"every_hours": 0, "daily_at": "04:00"})
    d1 = datetime(2026, 10, 21, 3, 0).timestamp()
    b.tick(d1, False, 0, None); wait()
    check("daily: not before the time", not b.list())
    b.tick(datetime(2026, 10, 21, 4, 30).timestamp(), False, 0, None); wait()
    check("daily: after the time with server off", len(b.list()) == 1 and b.list()[0]["kind"] == "scheduled")
    time.sleep(1.1)
    b.tick(datetime(2026, 10, 21, 5, 0).timestamp(), False, 0, None); wait()
    check("daily: not twice on the same day", len(b.list()) == 1)
    b.tick(datetime(2026, 10, 22, 4, 5).timestamp(), False, 0, None); wait()
    check("daily: next day", len(b.list()) == 2)
    b2 = B.Backups(s, st)
    check("daily: saved", b2._state["last_daily"] == "2026-10-22 04:00")

    # ---------- restore from zip
    root, s, st = fresh(); b = mk(s, st)
    mkzip(os.path.join(s, "backups", "world_2026-10-19_14-39-35.zip"),
          ["world/", "world/level.dat", "world/sub/f.txt", "world/session.lock"])
    zid = "backups/world_2026-10-19_14-39-35.zip"
    raises("restore with server on", ValueError, b.restore, zid, True)
    res = b.restore(zid, False)
    check("restore moved_to", res["moved_to"].endswith("_before-restore") and os.path.isdir(os.path.join(s, "old-worlds", res["moved_to"])), res)
    check("restore: new world", open(os.path.join(s, "world", "level.dat"), "rb").read() == b"data-world/level.dat")
    check("restore: old one saved", open(os.path.join(s, "old-worlds", res["moved_to"], "level.dat"), "rb").read() == b"LEVEL-ORIG")
    check("restore: sub", os.path.isfile(os.path.join(s, "world", "sub", "f.txt")))
    check("restore: no tmp", not os.path.exists(os.path.join(s, "world.restore-tmp")))

    # restore from folder (the source stays)
    time.sleep(1.1)
    old_id = "old-worlds/" + res["moved_to"]
    res2 = b.restore(old_id, False)
    check("restore folder", open(os.path.join(s, "world", "level.dat"), "rb").read() == b"LEVEL-ORIG")
    check("restore folder: source stays", os.path.isdir(os.path.join(s, "old-worlds", res["moved_to"])))

    # other layout: a single top-level folder with level.dat
    mkzip(os.path.join(s, "backups", "alt.zip"), ["mondo-x/level.dat", "mondo-x/z/y.txt"])
    time.sleep(1.1)
    b.restore("backups/alt.zip", False)
    check("restore other layout", os.path.isfile(os.path.join(s, "world", "z", "y.txt")))
    # unknown layout
    mkzip(os.path.join(s, "backups", "boh.zip"), ["a/x.txt", "b/y.txt"])
    cur = open(os.path.join(s, "world", "level.dat"), "rb").read()
    nold = len(os.listdir(os.path.join(s, "old-worlds")))
    raises("zip without level.dat", ValueError, b.restore, "backups/boh.zip", False)
    check("failed: world untouched", open(os.path.join(s, "world", "level.dat"), "rb").read() == cur)
    check("failed: nothing moved", len(os.listdir(os.path.join(s, "old-worlds"))) == nold)

    # malicious zips
    for evil in ["world/../../evil", "world/..\\..\\evil2", "world/C:/evil", "/abs/evil"]:
        mkzip(os.path.join(s, "backups", "evil.zip"), ["world/level.dat", evil])
        time.sleep(0.05)
        raises("malicious zip %r" % evil, ValueError, b.restore, "backups/evil.zip", False)
        check("evil: world untouched %r" % evil, open(os.path.join(s, "world", "level.dat"), "rb").read() == cur)
        check("evil: no file outside", not os.path.exists(os.path.join(s, "evil")) and not os.path.exists(os.path.join(os.path.dirname(s), "evil")))
        check("evil: no tmp", not os.path.exists(os.path.join(s, "world.restore-tmp")))
        os.remove(os.path.join(s, "backups", "evil.zip"))
    # corrupt zip
    write(os.path.join(s, "backups", "rotto.zip"), b"non e uno zip")
    raises("corrupt zip", Exception, b.restore, "backups/rotto.zip", False)
    check("corrupt: world untouched", open(os.path.join(s, "world", "level.dat"), "rb").read() == cur)

    # rollback: error during the final rename
    mkzip(os.path.join(s, "backups", "ok.zip"), ["world/level.dat"])
    real_replace = os.replace
    def bad_replace(a, c, *x, **k):
        if str(a).endswith("restore-tmp"):
            raise OSError("rename simulato fallito")
        return real_replace(a, c, *x, **k)
    os.replace = bad_replace
    try:
        raises("rollback rename", OSError, b.restore, "backups/ok.zip", False)
    finally:
        os.replace = real_replace
    check("rollback: original world back", open(os.path.join(s, "world", "level.dat"), "rb").read() == cur)
    check("rollback: tmp removed", not os.path.exists(os.path.join(s, "world.restore-tmp")))
    check("rollback: busy free", b.status()["busy"] is None and "Restore failed" in b.status()["last_error"])
    # no world: restore works, moved_to empty
    shutil.rmtree(os.path.join(s, "world"))
    r3 = b.restore("backups/ok.zip", False)
    check("restore without world", r3["moved_to"] == "" and os.path.isfile(os.path.join(s, "world", "level.dat")))

    # ---------- disk space: stop before writing, with a message
    root, s, st = fresh(); b = mk(s, st)
    mkzip(os.path.join(s, "backups", "x.zip"), ["world/level.dat"])
    real_du = B.shutil.disk_usage
    B.shutil.disk_usage = lambda p: real_du(p)._replace(free=100 * 1024 * 1024)   # 100 MB free
    try:
        msg = raises("backup without space", ValueError, b.create, "manual", False)
        check("space message", "disk space" in msg, msg)
        check("no half-written zip", not [x for x in os.listdir(os.path.join(s, "backups")) if x.endswith(".part")])
        raises("restore without space", ValueError, b.restore, "backups/x.zip", False)
        check("world untouched", open(os.path.join(s, "world", "level.dat"), "rb").read() == b"LEVEL-ORIG")
    finally:
        B.shutil.disk_usage = real_du
    check("with space the backup works", b.create("manual", False)["kind"] == "manual")

    # ---------- Paper: Nether and End next to the world (world_nether, world_the_end)
    root, s, st = fresh(); b = mk(s, st)
    write(os.path.join(s, "world_nether", "DIM-1", "region", "r.0.0.mca"), b"NETHER")
    write(os.path.join(s, "world_the_end", "DIM1", "region", "r.0.0.mca"), b"END")
    for on in (False, True):   # with the server off and on (temporary copy)
        e = b.create("manual", on, Rcon() if on else None)
        names = zipfile.ZipFile(os.path.join(s, "backups", e["name"])).namelist()
        check("paper: zip with three folders (on=%s)" % on, "world_nether/DIM-1/region/r.0.0.mca" in names
              and "world_the_end/DIM1/region/r.0.0.mca" in names and "world/level.dat" in names, names)
    write(os.path.join(s, "world_nether", "DIM-1", "region", "r.0.0.mca"), b"NETHER-NUOVO")
    r = b.restore("backups/" + e["name"], False)
    check("paper: nether restored", open(os.path.join(s, "world_nether", "DIM-1", "region", "r.0.0.mca"), "rb").read() == b"NETHER")
    old = os.path.join(s, "old-worlds", r["moved_to"])
    check("paper: old nether next to the old world", open(old + "_nether/DIM-1/region/r.0.0.mca", "rb").read() == b"NETHER-NUOVO")
    l = [x for x in b.list() if x["folder"] == "old-worlds"]
    check("paper: one old world in the list", [x["name"] for x in l] == [r["moved_to"]], l)
    write(os.path.join(s, "world_nether", "DIM-1", "region", "r.0.0.mca"), b"ALTRO")
    b.restore(l[0]["id"], False)   # from the old world folder: its nether comes back too
    check("paper: restore from folder", open(os.path.join(s, "world_nether", "DIM-1", "region", "r.0.0.mca"), "rb").read() == b"NETHER-NUOVO")
    # a backup without Paper's folders leaves the current ones alone
    mkzip(os.path.join(s, "backups", "vanilla.zip"), ["world/level.dat"])
    b.restore("backups/vanilla.zip", False)
    check("paper: current nether untouched", open(os.path.join(s, "world_nether", "DIM-1", "region", "r.0.0.mca"), "rb").read() == b"NETHER-NUOVO")

    # ---------- state file from before 1.0: Italian kind names in the retention rules
    with open(st, "w", encoding="utf-8") as f:
        json.dump({"schedule": {"keep": {"avvio": 3, "programmato": 4, "manuale": 5}}}, f)
    check("old retention keys", mk(s, st).status()["schedule"]["keep"] == {"startup": 3, "scheduled": 4, "manual": 5})

    # ---------- finer control: world tag, per-world retention, size cap, preview
    root, s, st = fresh(); b = mk(s, st)
    now = time.time(); day = 86400
    def zp(name, age_days, size=10):
        p = os.path.join(s, "backups", name)
        mkzip(p, ["world/level.dat"], now - age_days * day)
        with open(p, "ab") as f: f.write(b"0" * size)
        os.utime(p, (now - age_days * day,) * 2)
        return "backups/" + name
    a1, a2, a3 = zp("world_a1.zip", 3), zp("world_a2.zip", 2), zp("world_a3.zip", 1)
    b1, b2_ = zp("world_b1.zip", 3), zp("world_b2.zip", 1)
    m1 = zp("manual_1.zip", 30)
    with open(st, "w", encoding="utf-8") as f:
        json.dump({"world": {b1: "other", b2_: "other"}}, f)
    b = mk(s, st)
    wl = {e["id"]: e["world"] for e in b.list()}
    check("world: untagged = current, tagged kept", wl[a1] == "world" and wl[b1] == "other", wl)
    b.set_schedule({"keep": {"startup": 1, "scheduled": 0, "manual": 0}})
    check("keep is per world", sorted(b.preview({})["ids"]) == sorted([a1, a2, b1]), b.preview({}))
    b.pin(a1, True)
    check("preview skips pinned", sorted(b.preview({})["ids"]) == sorted([a2, b1]), b.preview({}))
    st_before = open(st, "rb").read()
    pv = b.preview({"keep": {"startup": 0}, "max_days": {"startup": 2}})
    check("preview age", set(pv["ids"]) == {a2, b1}, pv)   # a1 is pinned; a3 and b2 are 1 day old
    check("preview bytes", pv["bytes"] == sum(e["size"] for e in b.list() if e["id"] in pv["ids"]))
    check("preview saves nothing", open(st, "rb").read() == st_before and b.status()["schedule"]["keep"]["startup"] == 1)
    raises("preview validates", ValueError, b.preview, {"every_minutes": 5})
    b.set_schedule({"keep": {"startup": 0}, "max_days": {"manual": 10}})
    check("age keeps the newest of a world and kind", b.preview({})["ids"] == [], b.preview({}))
    b.set_schedule({"max_days": {"manual": 0}})
    b._state["schedule"]["max_gb"] = 1e-7   # the API rounds to 0.1 GB, too coarse for test files
    pl = b.preview({})
    check("size: automatic only, oldest first, pinned, manual and the newest per world never",
          set(pl["ids"]) == {a2, b1} and pl["ids"][0] in (b1, a2), pl)
    rem = b.prune()
    check("preview == prune", rem == pl["ids"], (rem, pl))
    check("prune keeps manual and pinned", os.path.exists(os.path.join(s, "backups", "manual_1.zip")) and os.path.exists(os.path.join(s, "backups", "world_a1.zip")))
    check("recycle drops the world tag", b1 not in b._state["world"])
    check("max_gb round", b.set_schedule({"max_gb": "1.26"})["max_gb"] == 1.3 and b.set_schedule({"max_gb": 2})["max_gb"] == 2)
    # cap that is reachable: only enough automatic zips go
    root, s, st = fresh(); b = mk(s, st)
    ids = [zp("world_%d.zip" % i, 5 - i, 0) for i in range(4)]
    one = {e["id"]: e["size"] for e in b.list()}[ids[0]]
    sch = dict(b.status()["schedule"], max_gb=2.5 * one / 1073741824)
    plan = B._prune_plan(b.list(), sch)
    check("size: oldest first until under", [e["id"] for e, w in plan] == ids[:2] and all(w == "size" for _e, w in plan), plan)

    # ---------- bulk operations, note
    root, s, st = fresh(); b = mk(s, st)
    ids = []
    for i in range(4):
        mkzip(os.path.join(s, "backups", "manual_%d.zip" % i), ["world/level.dat"], time.time() - i * 10)
        ids.append("backups/manual_%d.zip" % i)
    b.pin_many(ids[:1], True)
    check("pin_many", [e["id"] for e in b.list() if e["pinned"]] == ids[:1])
    r = b.delete_many(ids + [ids[1]])
    check("delete_many: skips pinned, dedups", sorted(r["deleted"]) == sorted(ids[1:]) and r["skipped"] == 1 and r["errors"] == [], r)
    raises("delete_many: ids must be strings", ValueError, b.delete_many, [[1]])
    raises("every_minutes inf", ValueError, b.set_schedule, {"every_minutes": float("inf")})
    b.set_note(ids[0], None)
    check("note None clears", not any(e["note"] for e in b.list()))
    check("delete_many: left only the pinned", [e["id"] for e in b.list()] == ids[:1] and b.status()["busy"] is None)
    check("delete_many: only pinned = nothing", b.delete_many(ids[:1]) == {"deleted": [], "skipped": 1, "errors": []})
    raises("delete_many: bad id", ValueError, b.delete_many, ["backups/nope.zip"])
    raises("delete_many: not a list", ValueError, b.delete_many, "backups/x.zip")
    raises("delete_many: too many", ValueError, b.delete_many, ["backups/x%d.zip" % i for i in range(501)])
    b.set_note(ids[0], "  before the Wither  ")
    check("set_note", b.list()[0]["note"] == "before the Wither")
    b.set_note(ids[0], "x" * 200); check("set_note: 80 chars", len(b.list()[0]["note"]) == 80)
    b.set_note(ids[0], "  "); check("set_note: empty clears", b.list()[0]["note"] == "" and ids[0] not in b._state["note"])
    b.world_dir = os.path.join(s, "world")
    e = b.create("manual", False)
    check("create tags the world", e["world"] == "world" and mk(s, st)._state["world"][e["id"]] == "world")

    # ---------- schedule: legacy values and validation
    root, s, st = fresh()
    with open(st, "w", encoding="utf-8") as f:
        json.dump({"schedule": {"every_hours": 3, "daily_at": "05:30", "keep": {"manual": 2}}, "last_daily": "2026-10-20"}, f)
    b = mk(s, st); sc = b.status()["schedule"]
    check("legacy schedule", sc["every_minutes"] == 180 and sc["daily_at"] == ["05:30"] and sc["days"] == list(range(7))
          and "every_hours" not in sc and sc["window"] is None and sc["keep"]["manual"] == 2 and sc["max_gb"] == 0, sc)
    check("legacy last_daily", b._state["last_daily"] == "2026-10-20 23:59")
    check("legacy api keys", b.set_schedule({"every_hours": 1, "daily_at": ""})["every_minutes"] == 60 and b.status()["schedule"]["daily_at"] == [])
    c = b.set_schedule({"every_minutes": 15, "window": {"from": "22:00", "to": "06:00"}, "daily_at": ["18:00", "04:00", "18:00"], "days": [4, 0, 0]})
    check("normalised new keys", c["window"] == {"from": "22:00", "to": "06:00"} and c["daily_at"] == ["04:00", "18:00"] and c["days"] == [0, 4], c)
    check("window off", b.set_schedule({"window": None})["window"] is None)
    for bad in [{"window": {"from": "10:00", "to": "10:00"}}, {"window": {"from": "10:00"}}, {"window": "x"},
                {"daily_at": ["01:00", "02:00", "03:00", "04:00", "05:00", "06:00", "07:00"]}, {"daily_at": ["25:00"]}, {"daily_at": 5},
                {"days": []}, {"days": [7]}, {"days": "1"}, {"every_minutes": 5}, {"every_minutes": 2881},
                {"max_days": {"manual": 3651}}, {"max_gb": -1}, {"max_gb": "x"}, {"max_gb": True}, {"max_gb": float("nan")}]:
        raises("invalid schedule %r" % (bad,), ValueError, b.set_schedule, bad)
    check("rejected values changed nothing", b.status()["schedule"]["daily_at"] == ["04:00", "18:00"] and b.status()["schedule"]["days"] == [0, 4])
    check("days empty is fine without daily times", b.set_schedule({"daily_at": [], "days": []})["days"] == [])
    raises("daily times with no day", ValueError, b.set_schedule, {"daily_at": ["04:00"]})

    # ---------- window across midnight
    W = {"from": "22:00", "to": "06:00"}
    check("window crossing midnight", all(B._in_window(W, datetime(2026, 10, 21, h, m)) == ok for h, m, ok in
          [(22, 0, True), (23, 59, True), (0, 0, True), (5, 59, True), (6, 0, False), (12, 0, False), (21, 59, False)]))
    check("window same day", B._in_window({"from": "09:00", "to": "17:00"}, datetime(2026, 10, 21, 9, 0)) and not B._in_window({"from": "09:00", "to": "17:00"}, datetime(2026, 10, 21, 17, 0)))

    # ---------- interval tick honours the window
    root, s, st = fresh(); b = mk(s, st); r = Rcon()
    b.set_schedule({"every_minutes": 30, "daily_at": [], "only_when_online": False, "window": {"from": "22:00", "to": "06:00"}})
    T0 = datetime(2026, 10, 21, 12, 0).timestamp()
    b.tick(T0, True, 1, r); wait()
    b.tick(T0 + 3600, True, 1, r); wait()
    check("tick: due but outside the window", not b.list())
    check("next_scheduled moves to the window start", abs(b.status()["next_scheduled"] - datetime(2026, 10, 21, 22, 0).timestamp()) < 1, b.status()["next_scheduled"])
    b.tick(datetime(2026, 10, 21, 23, 0).timestamp(), True, 1, r); wait()
    check("tick: inside the window", len(b.list()) == 1)

    # ---------- daily slots: two times, weekday filter
    root, s, st = fresh(); b = mk(s, st)
    # 2026-10-21 is a Wednesday (2)
    b.set_schedule({"every_minutes": 0, "daily_at": ["04:00", "16:00"], "days": [2]})
    def n(): return len(b.list())
    def at(d, h, m=0): return datetime(2026, 10, d, h, m).timestamp()
    b.tick(at(21, 3), False, 0, None); wait(); check("slots: before the first", n() == 0)
    b.tick(at(21, 4, 10), False, 0, None); wait(); check("slots: first due", n() == 1)
    check("slots: last_daily = first slot", b._state["last_daily"] == "2026-10-21 04:00")
    time.sleep(1.1); b.tick(at(21, 10), False, 0, None); wait(); check("slots: not twice", n() == 1)
    time.sleep(1.1); b.tick(at(21, 16, 5), False, 0, None); wait(); check("slots: second due", n() == 2)
    check("slots: last_daily = second slot", b._state["last_daily"] == "2026-10-21 16:00")
    time.sleep(1.1); b.tick(at(22, 5), False, 0, None); wait(); check("slots: wrong weekday", n() == 2)
    time.sleep(1.1); b.tick(at(28, 4, 30), False, 0, None); wait(); check("slots: next Wednesday", n() == 3)
    # one interval run after both slots covers both
    root, s, st = fresh(); b = mk(s, st); r = Rcon()
    b.set_schedule({"every_minutes": 30, "only_when_online": False, "daily_at": ["04:00", "16:00"], "days": [2]})
    b.tick(at(21, 1), True, 1, r); wait()
    b.tick(at(21, 17), True, 1, r); wait()
    check("interval run covers both slots", n() == 1 and b._state["last_daily"] == "2026-10-21 16:00", (n(), b._state["last_daily"]))
    time.sleep(1.1); b.tick(at(21, 17, 5), False, 0, None); wait()
    check("no extra daily after the interval run", n() == 1)
    # next_scheduled with days
    wd = (datetime.now().weekday() + 3) % 7
    b.set_schedule({"every_minutes": 0, "daily_at": ["04:00"], "days": [wd]})
    nx = datetime.fromtimestamp(b.status()["next_scheduled"])
    check("next_scheduled honours days", nx.weekday() == wd and (nx.hour, nx.minute) == (4, 0), nx)
    b.set_schedule({"every_minutes": 0, "daily_at": []})
    check("next_scheduled: nothing on", b.status()["next_scheduled"] is None)

    # ---------- operation running: Busy, not an error that waits 15 minutes
    b._op_lock.acquire()
    try:
        raises("busy", B.Busy, b.create, "manual", False)
        b._run_scheduled("interval", time.time(), False, None)
        check("busy: retried next round", b._retry_at is None)
    finally:
        b._op_lock.release()

    # ---------- real folder, read only
    if not os.environ.get("BYZZ_REAL_SERVER"):
        return
    real = B.Backups(os.environ["BYZZ_REAL_SERVER"], os.path.join(tempfile.gettempdir(), "bk_real_state_unused.json"))
    print("--- REAL FOLDER ---")
    for e in real.list():
        print("%-14s %-8s %10.1f MB  %s" % (e["kind"], e["folder"], e["size"] / 1e6, e["name"]))
    dk = real.disk(); print("disk free %.1f GB / %.1f GB" % (dk["free"] / 1e9, dk["total"] / 1e9))
    print("status", real.status())

try:
    run()
except Exception:
    FAIL += 1
    traceback.print_exc()
print("PASS %d  FAIL %d" % (OK, FAIL))
for d in os.listdir(tempfile.gettempdir()):
    if d.startswith("bk_test_"):
        shutil.rmtree(os.path.join(tempfile.gettempdir(), d), ignore_errors=True)
sys.exit(1 if FAIL else 0)
