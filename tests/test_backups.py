import ctypes, os, shutil, sys, tempfile, time, zipfile, traceback
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))  # cartella pannello
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
        check(name, False, "eccezione diversa: %r" % e)
        return ""
    check(name, False, "nessuna eccezione")
    return ""

if os.name == "nt":
    from ctypes import wintypes
    k32 = ctypes.windll.kernel32
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    def lock_exclusive(path):
        h = k32.CreateFileW(path, 0x80000000 | 0x40000000, 0, None, 3, 0x80, None)  # share=0, OPEN_EXISTING
        assert h not in (None, wintypes.HANDLE(-1).value), "lock fallito"
        return h
    unlock = k32.CloseHandle
else:
    # Linux non blocca i file aperti: un file illeggibile simula lo stesso caso (non vale da root)
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
            raise RuntimeError("rcon giù")
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
    check("list kinds", kinds == {"world_2026-10-19_14-39-35.zip": "avvio", "auto_2026-10-19_15-00-00.zip": "programmato",
          "manuale_2026-10-19_16-00-00.zip": "manuale", "prima-di-survival_x.zip": "altro", "world_old": "mondo"}, kinds)
    check("list ignora .part", not any(e["name"].endswith(".part") for e in l))
    check("list ordine", l[0]["mtime"] >= l[-1]["mtime"])
    old = [e for e in l if e["kind"] == "mondo"][0]
    check("size cartella", old["size"] == 13 and old["id"] == "mondi-vecchi/world_old", old)
    d = b.disk(); check("disk", d["free"] > 0 and d["total"] >= d["free"])
    check("status base", b.status()["busy"] is None and b.status()["schedule"]["every_hours"] == 2)

    # ---------- id malevoli
    for bad in ["../x", "C:\\x", "backups/../world", "backups/..", "/etc/passwd", "backups\\x.zip",
                "backups/nonesiste.zip", "backups/a/b.zip", "", None, "world"]:
        raises("id malevolo %r" % (bad,), ValueError, b.path, bad)
        raises("id malevolo delete %r" % (bad,), ValueError, b.delete, bad)
        raises("id malevolo restore %r" % (bad,), ValueError, b.restore, bad, False)
    raises("path cartella", ValueError, b.path, "mondi-vecchi/world_old")
    check("path zip", b.path("backups/auto_2026-10-19_15-00-00.zip").endswith("auto_2026-10-19_15-00-00.zip"))

    # ---------- create server spento (con file bloccato)
    hl = lock_exclusive(os.path.join(s, "world", "locked.dat"))
    hs = lock_exclusive(os.path.join(s, "world", "session.lock"))
    try:
        # un file illeggibile (diverso da session.lock) fa fallire il backup, non lo salta
        raises("create con file bloccato fallisce", OSError, b.create, "manuale", False)
        check("errore nomina il file", "locked.dat" in (b.status()["last_error"] or ""), b.status()["last_error"])
        check("niente zip parziale", not [x for x in os.listdir(os.path.join(s, "backups")) if x.endswith(".part") or x.startswith(".tmp")])
        r = Rcon(); time.sleep(1.1)
        raises("create on con file bloccato fallisce", OSError, b.create, "manuale", True, r)
        check("save-on anche se la copia fallisce", r.calls[-1] == "save-on", r.calls)
        unlock(hl); hl = None
        time.sleep(1.1)
        e = b.create("manuale", server_on=False, note="nota prova")
        check("create off nome", e["name"].startswith("manual_") and e["kind"] == "manuale" and e["note"] == "nota prova", e)
        with zipfile.ZipFile(b.path(e["id"])) as z:
            n = z.namelist()
        check("zip ha world/level.dat", "world/level.dat" in n, n)
        check("zip senza session.lock", not any("session.lock" in x for x in n), n)
        check("zip con locked.dat (ora leggibile)", "world/locked.dat" in n, n)
        check("zip ha regione", "world/dimensions/minecraft/overworld/region/r.0.0.mca" in n)
        check("on_change chiamata", len(changes) >= 2)
        check("nessun .part/.tmp", not [x for x in os.listdir(os.path.join(s, "backups")) if x.endswith(".part") or x.startswith(".tmp")])

        # ---------- create server acceso
        r = Rcon()
        time.sleep(1.1)
        e2 = b.create("programmato", server_on=True, rcon=r)
        check("rcon ordine", r.calls == ["save-off", "save-all flush", "save-on"], r.calls)
        check("nome auto_", e2["name"].startswith("auto_") and e2["kind"] == "programmato", e2)
        with zipfile.ZipFile(b.path(e2["id"])) as z:
            n2 = z.namelist()
        check("zip on: world/level.dat", "world/level.dat" in n2)
        check("zip on: senza lock", not any("session.lock" in x for x in n2), n2)
        check("tmp rimosso", not [x for x in os.listdir(os.path.join(s, "backups")) if x.startswith(".tmp")])

        # zip che fallisce: save-on comunque, pulizia
        r = Rcon(); b._zip_world = lambda *a, **k: (_ for _ in ()).throw(OSError("zip rotto"))
        time.sleep(1.1)
        raises("create zip fallito", OSError, b.create, "manuale", True, r)
        check("save-on dopo zip fallito", r.calls == ["save-off", "save-all flush", "save-on"], r.calls)
        check("last_error", "zip rotto" in (b.status()["last_error"] or ""))
        check("busy rilasciato", b.status()["busy"] is None)
        check("pulizia dopo errore", not [x for x in os.listdir(os.path.join(s, "backups")) if x.endswith(".part") or x.startswith(".tmp")])
        del b._zip_world
        # flush fallisce -> save-on lo stesso
        r = Rcon(); r.fail_on = "save-all flush"
        raises("flush fallito", RuntimeError, b.create, "manuale", True, r)
        check("save-on dopo flush fallito", r.calls == ["save-off", "save-all flush", "save-on"], r.calls)
        # save-off fallisce -> save-on lo stesso
        r = Rcon(); r.fail_on = "save-off"
        raises("save-off fallito", RuntimeError, b.create, "manuale", True, r)
        check("save-on dopo save-off fallito", r.calls[-1] == "save-on", r.calls)
    finally:
        if hl:
            unlock(hl)
        unlock(hs)

    # occupato
    import threading
    b._op_lock.acquire()
    raises("occupato", ValueError, b.create)
    raises("occupato delete", ValueError, b.delete, "backups/auto_2026-10-19_15-00-00.zip")
    raises("occupato restore", ValueError, b.restore, "backups/auto_2026-10-19_15-00-00.zip", False)
    b._op_lock.release()
    raises("tipo non valido", ValueError, b.create, "boh")

    # ---------- pin / delete / cestino
    ide = "backups/prima-di-survival_x.zip"
    b.pin(ide, True)
    check("pin", [e for e in b.list() if e["id"] == ide][0]["pinned"])
    check("pin persistito", B.Backups(s, st).list() and ide in B.Backups(s, st)._state["pinned"])
    msg = raises("delete fissato", ValueError, b.delete, ide)
    check("msg fissato", msg == "Unpin it first", msg)
    b.pin(ide, False)
    b.delete(ide)
    check("delete: sparito dal disco", not os.path.exists(os.path.join(s, "backups", "prima-di-survival_x.zip")))
    check("delete: sparito da list", ide not in [e["id"] for e in b.list()])
    b.delete("mondi-vecchi/world_old")
    check("delete cartella", not os.path.exists(os.path.join(s, "mondi-vecchi", "world_old")))

    # ---------- prune
    root, s, st = fresh(); b = mk(s, st)
    base = time.time() - 100000
    for i in range(6):
        ts = datetime.fromtimestamp(base + i * 100).strftime(B.TS_FMT)
        mkzip(os.path.join(s, "backups", "world_%s.zip" % ts), ["world/level.dat"], base + i * 100)
        mkzip(os.path.join(s, "backups", "manuale_%s.zip" % ts), ["world/level.dat"], base + i * 100)
    mkzip(os.path.join(s, "backups", "altro1.zip"), ["world/level.dat"], base)
    write(os.path.join(s, "mondi-vecchi", "w1", "level.dat"))
    names = sorted(e["name"] for e in b.list() if e["kind"] == "avvio")
    b.pin("backups/" + names[0], True); b.pin("backups/" + names[1], True)  # i 2 piu vecchi fissati
    cfg = b.set_schedule({"keep": {"avvio": 2, "programmato": 0, "manuale": 0}})
    check("set_schedule keep", cfg["keep"]["avvio"] == 2 and cfg["every_hours"] == 2)
    rem = b.prune()
    left = sorted(e["name"] for e in b.list() if e["kind"] == "avvio")
    check("prune: rimossi 2 (6-2 fissati-2 tenuti)", len(rem) == 2, rem)
    check("prune: tiene fissati + 2 recenti", left == names[:2] + names[-2:], (left, names))
    check("prune: manuali intatti", len([e for e in b.list() if e["kind"] == "manuale"]) == 6)
    check("prune: altro/mondo intatti", any(e["kind"] == "altro" for e in b.list()) and any(e["kind"] == "mondo" for e in b.list()))
    check("prune: disco", all(not os.path.exists(os.path.join(s, "backups", os.path.basename(r))) for r in rem))
    b.set_schedule({"keep": {"manuale": 3}})
    rem = b.prune(); check("prune manuale 3", len(rem) == 3 and len([e for e in b.list() if e["kind"] == "manuale"]) == 3, rem)
    check("prune idempotente", b.prune() == [])

    # ---------- validazione schedule
    for bad in [{"every_hours": 49}, {"every_hours": -1}, {"every_hours": "x"}, {"daily_at": "25:00"}, {"daily_at": "4pm"},
                {"keep": {"avvio": 201}}, {"keep": {"avvio": -1}}, "no", {"every_hours": True}]:
        raises("schedule invalido %r" % (bad,), ValueError, b.set_schedule, bad)
    c = b.set_schedule({"every_hours": 0, "daily_at": "", "only_when_online": False})
    check("schedule normalizzato", c["every_hours"] == 0 and c["daily_at"] == "" and c["only_when_online"] is False)
    check("schedule persistito", B.Backups(s, st).status()["schedule"] == c)
    # stato corrotto
    with open(st, "w") as f: f.write("{non json")
    check("stato corrotto -> default", B.Backups(s, st).status()["schedule"]["every_hours"] == 2)

    # ---------- tick intervallo
    root, s, st = fresh(); b = mk(s, st); r = Rcon()
    T = 1_800_000_000.0
    b.set_schedule({"every_hours": 2, "daily_at": "", "only_when_online": True})
    def wait():
        th = b._sched_thread
        if th: th.join(20)
    b.tick(T, True, 1, r); wait()
    check("tick: primo tick non esegue", not [e for e in b.list() if e["kind"] == "programmato"])
    b.tick(T + 3600, True, 1, r); wait()
    check("tick: dopo 1h no", not [e for e in b.list() if e["kind"] == "programmato"])
    b.tick(T + 7300, True, 0, r); wait()
    check("tick: senza giocatori no", not [e for e in b.list() if e["kind"] == "programmato"])
    b.tick(T + 7300, False, 1, r); wait()
    check("tick: server spento no (intervallo)", not [e for e in b.list() if e["kind"] == "programmato"])
    b.tick(T + 7300, True, 1, r); wait()
    check("tick: dopo 2h con giocatori esegue", len([e for e in b.list() if e["kind"] == "programmato"]) == 1)
    check("tick: rcon usato", r.calls == ["save-off", "save-all flush", "save-on"], r.calls)
    check("tick: last_scheduled", b.status()["last_scheduled"] == T + 7300)
    time.sleep(1.1)
    b.tick(T + 7300 + 3600, True, 1, r); wait()
    check("tick: subito dopo no", len([e for e in b.list() if e["kind"] == "programmato"]) == 1)
    check("next_scheduled", abs(b.status()["next_scheduled"] - (T + 7300 + 7200)) < 1)
    b.set_schedule({"only_when_online": False})
    time.sleep(1.1)
    b.tick(T + 7300 + 7300, True, 0, r); wait()
    check("tick: only_when_online off", len([e for e in b.list() if e["kind"] == "programmato"]) == 2)
    # keep nel tick
    b.set_schedule({"keep": {"programmato": 1}})
    time.sleep(1.1)
    b.tick(T + 40000, True, 0, r); wait()
    check("tick: prune tiene 1 programmato", len([e for e in b.list() if e["kind"] == "programmato"]) == 1,
          [e["name"] for e in b.list()])

    # ---------- tick giornaliero
    root, s, st = fresh(); b = mk(s, st); r = Rcon()
    b.set_schedule({"every_hours": 0, "daily_at": "04:00"})
    d1 = datetime(2026, 10, 21, 3, 0).timestamp()
    b.tick(d1, False, 0, None); wait()
    check("daily: prima dell'orario no", not b.list())
    b.tick(datetime(2026, 10, 21, 4, 30).timestamp(), False, 0, None); wait()
    check("daily: dopo l'orario a server spento", len(b.list()) == 1 and b.list()[0]["kind"] == "programmato")
    time.sleep(1.1)
    b.tick(datetime(2026, 10, 21, 5, 0).timestamp(), False, 0, None); wait()
    check("daily: non due volte nello stesso giorno", len(b.list()) == 1)
    b.tick(datetime(2026, 10, 22, 4, 5).timestamp(), False, 0, None); wait()
    check("daily: giorno dopo", len(b.list()) == 2)
    b2 = B.Backups(s, st)
    check("daily: persistito", b2._state["last_daily"] == "2026-10-22")

    # ---------- restore da zip
    root, s, st = fresh(); b = mk(s, st)
    mkzip(os.path.join(s, "backups", "world_2026-10-19_14-39-35.zip"),
          ["world/", "world/level.dat", "world/sub/f.txt", "world/session.lock"])
    zid = "backups/world_2026-10-19_14-39-35.zip"
    raises("restore server acceso", ValueError, b.restore, zid, True)
    res = b.restore(zid, False)
    check("restore moved_to", res["moved_to"].endswith("_before-restore") and os.path.isdir(os.path.join(s, "old-worlds", res["moved_to"])), res)
    check("restore: mondo nuovo", open(os.path.join(s, "world", "level.dat"), "rb").read() == b"data-world/level.dat")
    check("restore: vecchio salvato", open(os.path.join(s, "old-worlds", res["moved_to"], "level.dat"), "rb").read() == b"LEVEL-ORIG")
    check("restore: sub", os.path.isfile(os.path.join(s, "world", "sub", "f.txt")))
    check("restore: no tmp", not os.path.exists(os.path.join(s, "world.restore-tmp")))

    # restore da cartella (sorgente resta)
    time.sleep(1.1)
    old_id = "old-worlds/" + res["moved_to"]
    res2 = b.restore(old_id, False)
    check("restore cartella", open(os.path.join(s, "world", "level.dat"), "rb").read() == b"LEVEL-ORIG")
    check("restore cartella: sorgente resta", os.path.isdir(os.path.join(s, "old-worlds", res["moved_to"])))

    # struttura alternativa: unica cartella di primo livello con level.dat
    mkzip(os.path.join(s, "backups", "alt.zip"), ["mondo-x/level.dat", "mondo-x/z/y.txt"])
    time.sleep(1.1)
    b.restore("backups/alt.zip", False)
    check("restore struttura alternativa", os.path.isfile(os.path.join(s, "world", "z", "y.txt")))
    # struttura sconosciuta
    mkzip(os.path.join(s, "backups", "boh.zip"), ["a/x.txt", "b/y.txt"])
    cur = open(os.path.join(s, "world", "level.dat"), "rb").read()
    nold = len(os.listdir(os.path.join(s, "old-worlds")))
    raises("zip senza level.dat", ValueError, b.restore, "backups/boh.zip", False)
    check("fallito: mondo intatto", open(os.path.join(s, "world", "level.dat"), "rb").read() == cur)
    check("fallito: nulla spostato", len(os.listdir(os.path.join(s, "old-worlds"))) == nold)

    # zip malevoli
    for evil in ["world/../../evil", "world/..\\..\\evil2", "world/C:/evil", "/abs/evil"]:
        mkzip(os.path.join(s, "backups", "evil.zip"), ["world/level.dat", evil])
        time.sleep(0.05)
        raises("zip malevolo %r" % evil, ValueError, b.restore, "backups/evil.zip", False)
        check("evil: mondo intatto %r" % evil, open(os.path.join(s, "world", "level.dat"), "rb").read() == cur)
        check("evil: niente file fuori", not os.path.exists(os.path.join(s, "evil")) and not os.path.exists(os.path.join(os.path.dirname(s), "evil")))
        check("evil: no tmp", not os.path.exists(os.path.join(s, "world.restore-tmp")))
        os.remove(os.path.join(s, "backups", "evil.zip"))
    # zip corrotto
    write(os.path.join(s, "backups", "rotto.zip"), b"non e uno zip")
    raises("zip corrotto", Exception, b.restore, "backups/rotto.zip", False)
    check("corrotto: mondo intatto", open(os.path.join(s, "world", "level.dat"), "rb").read() == cur)

    # rollback: errore durante il rename finale
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
    check("rollback: mondo originale rimesso", open(os.path.join(s, "world", "level.dat"), "rb").read() == cur)
    check("rollback: tmp rimosso", not os.path.exists(os.path.join(s, "world.restore-tmp")))
    check("rollback: busy libero", b.status()["busy"] is None and "Restore failed" in b.status()["last_error"])
    # mondo assente: restore funziona, moved_to vuoto
    shutil.rmtree(os.path.join(s, "world"))
    r3 = b.restore("backups/ok.zip", False)
    check("restore senza mondo", r3["moved_to"] == "" and os.path.isfile(os.path.join(s, "world", "level.dat")))

    # ---------- spazio disco: ci si ferma prima di scrivere, con un messaggio
    root, s, st = fresh(); b = mk(s, st)
    mkzip(os.path.join(s, "backups", "x.zip"), ["world/level.dat"])
    real_du = B.shutil.disk_usage
    B.shutil.disk_usage = lambda p: real_du(p)._replace(free=100 * 1024 * 1024)   # 100 MB liberi
    try:
        msg = raises("backup senza spazio", ValueError, b.create, "manuale", False)
        check("messaggio spazio", "disk space" in msg, msg)
        check("niente zip a meta'", not [x for x in os.listdir(os.path.join(s, "backups")) if x.endswith(".part")])
        raises("ripristino senza spazio", ValueError, b.restore, "backups/x.zip", False)
        check("mondo intatto", open(os.path.join(s, "world", "level.dat"), "rb").read() == b"LEVEL-ORIG")
    finally:
        B.shutil.disk_usage = real_du
    check("con spazio il backup va", b.create("manuale", False)["kind"] == "manuale")

    # ---------- Paper: Nether ed End accanto al mondo (world_nether, world_the_end)
    root, s, st = fresh(); b = mk(s, st)
    write(os.path.join(s, "world_nether", "DIM-1", "region", "r.0.0.mca"), b"NETHER")
    write(os.path.join(s, "world_the_end", "DIM1", "region", "r.0.0.mca"), b"END")
    for on in (False, True):   # a server spento e acceso (copia temporanea)
        e = b.create("manuale", on, Rcon() if on else None)
        names = zipfile.ZipFile(os.path.join(s, "backups", e["name"])).namelist()
        check("paper: zip con tre cartelle (on=%s)" % on, "world_nether/DIM-1/region/r.0.0.mca" in names
              and "world_the_end/DIM1/region/r.0.0.mca" in names and "world/level.dat" in names, names)
    write(os.path.join(s, "world_nether", "DIM-1", "region", "r.0.0.mca"), b"NETHER-NUOVO")
    r = b.restore("backups/" + e["name"], False)
    check("paper: nether ripristinato", open(os.path.join(s, "world_nether", "DIM-1", "region", "r.0.0.mca"), "rb").read() == b"NETHER")
    old = os.path.join(s, "old-worlds", r["moved_to"])
    check("paper: nether vecchio accanto al mondo vecchio", open(old + "_nether/DIM-1/region/r.0.0.mca", "rb").read() == b"NETHER-NUOVO")
    l = [x for x in b.list() if x["folder"] == "old-worlds"]
    check("paper: un solo mondo vecchio in elenco", [x["name"] for x in l] == [r["moved_to"]], l)
    write(os.path.join(s, "world_nether", "DIM-1", "region", "r.0.0.mca"), b"ALTRO")
    b.restore(l[0]["id"], False)   # dalla cartella del mondo vecchio: torna anche il suo nether
    check("paper: ripristino da cartella", open(os.path.join(s, "world_nether", "DIM-1", "region", "r.0.0.mca"), "rb").read() == b"NETHER-NUOVO")
    # un backup senza le cartelle di Paper lascia stare quelle attuali
    mkzip(os.path.join(s, "backups", "vanilla.zip"), ["world/level.dat"])
    b.restore("backups/vanilla.zip", False)
    check("paper: nether attuale intatto", open(os.path.join(s, "world_nether", "DIM-1", "region", "r.0.0.mca"), "rb").read() == b"NETHER-NUOVO")

    # ---------- operazione in corso: Busy, non un errore che rimanda di 15 minuti
    b._op_lock.acquire()
    try:
        raises("busy", B.Busy, b.create, "manuale", False)
        b._run_scheduled("interval", time.time(), False, None)
        check("busy: si riprova al giro dopo", b._retry_at is None)
    finally:
        b._op_lock.release()

    # ---------- dir vera, sola lettura
    if not os.environ.get("BYZZ_REAL_SERVER"):
        return
    real = B.Backups(os.environ["BYZZ_REAL_SERVER"], os.path.join(tempfile.gettempdir(), "bk_real_state_unused.json"))
    print("--- DIR VERA ---")
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
