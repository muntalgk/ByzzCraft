"""Gestione backup del mondo Minecraft (solo libreria standard, Windows e Linux).

Usato da panel.py. Un'operazione alla volta (backup / ripristino / eliminazione).
"""
import ctypes
import json
import os
import shutil
import threading
import time
import zipfile
from datetime import datetime, timedelta

from i18n import t as tr   # t e' usato come variabile locale (orari)

if os.name == "nt":
    from ctypes import wintypes
else:
    wintypes = None

TS_FMT = "%Y-%m-%d_%H-%M-%S"
KINDS_KEEP = ("avvio", "programmato", "manuale")
DEFAULT_SCHEDULE = {
    "every_hours": 2,
    "only_when_online": True,
    "daily_at": "04:00",
    "keep": {"avvio": 10, "programmato": 12, "manuale": 0},
}
CHUNK = 1024 * 1024


def _kind_of_zip(name):
    if name.startswith("world_"):
        return "avvio"
    if name.startswith("auto_"):
        return "programmato"
    if name.startswith("manuale_"):
        return "manuale"
    return "altro"


def _rmtree(p):
    shutil.rmtree(p, ignore_errors=True)


def _rmfile(p):
    try:
        os.remove(p)
    except OSError:
        pass


# ---------------------------------------------------------------- Cestino
if wintypes:
    class _SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("wFunc", wintypes.UINT),
            ("pFrom", wintypes.LPCWSTR),
            ("pTo", wintypes.LPCWSTR),
            ("fFlags", ctypes.c_ushort),
            ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings", ctypes.c_void_p),
            ("lpszProgressTitle", wintypes.LPCWSTR),
        ]


def _recycle_limit(path):
    """(byte massimi, elimina-subito) del Cestino del disco di path, letti dal registro; None se ignoti."""
    try:
        import winreg
        buf = ctypes.create_unicode_buffer(80)
        root = os.path.splitdrive(os.path.abspath(path))[0] + "\\"
        if not ctypes.windll.kernel32.GetVolumeNameForVolumeMountPointW(root, buf, 80):
            return None
        guid = buf.value[buf.value.index("{"):buf.value.index("}") + 1]
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Explorer\BitBucket\Volume" + "\\" + guid) as k:
            mx = winreg.QueryValueEx(k, "MaxCapacity")[0]
            try:
                nuke = winreg.QueryValueEx(k, "NukeOnDelete")[0]
            except OSError:
                nuke = 0
        return int(mx) * 1024 * 1024, bool(nuke)
    except (OSError, ValueError):
        return None


def _to_recycle_bin(path, size=None):
    """Sposta path (file o cartella) nel Cestino. Solleva OSError se fallisce.
    Se il Cestino non puo' contenerlo Windows lo cancellerebbe per sempre senza chiedere (abbiamo
    NOCONFIRMATION): in quel caso ci si ferma prima e si lascia la scelta a una persona."""
    path = os.path.abspath(path)
    lim = _recycle_limit(path)
    if lim is None:
        raise OSError(tr("Cannot read the Recycle Bin settings: not deleting anything, to be safe"))
    if lim[1]:
        raise OSError(tr("The Recycle Bin is disabled on this drive: delete the file by hand if you are sure"))
    if size is not None and size >= lim[0]:
        raise OSError(tr("Too big for the Recycle Bin (limit %d MB): delete it by hand if you are sure") % (lim[0] // 1048576))
    FO_DELETE = 3
    FOF_SILENT = 0x4
    FOF_NOCONFIRMATION = 0x10
    FOF_ALLOWUNDO = 0x40
    FOF_NOERRORUI = 0x400
    op = _SHFILEOPSTRUCTW()
    op.hwnd = None
    op.wFunc = FO_DELETE
    op.pFrom = path + "\0\0"
    op.pTo = None
    op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI
    rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if rc != 0 or op.fAnyOperationsAborted:
        raise OSError(tr("Could not move to the Recycle Bin (code %s)") % rc)
    if os.path.exists(path):
        raise OSError(tr("The file is still there after moving it to the Recycle Bin"))


TRASH_DAYS = 7


def _to_trash_dir(path, trash):
    """Linux: niente Cestino di sistema sui server, si sposta in trash/ (stesso disco del server: istantaneo).
    shortcut: chi resta in trash/ piu' di TRASH_DAYS giorni viene eliminato, come fa il Cestino di Windows quando e' pieno."""
    os.makedirs(trash, exist_ok=True)
    limit = time.time() - TRASH_DAYS * 86400
    for n in os.listdir(trash):
        p = os.path.join(trash, n)
        try:
            if os.path.getmtime(p) < limit:
                _rmtree(p) if os.path.isdir(p) else _rmfile(p)
        except OSError:
            pass
    dst = os.path.join(trash, "%s_%s" % (datetime.now().strftime(TS_FMT), os.path.basename(path)))
    shutil.move(path, dst)
    os.utime(dst)   # l'eta' nel cestino conta da adesso, non dalla data del backup
    return dst


# ---------------------------------------------------------------- classe
class Backups:
    def __init__(self, server_dir, state_file, on_change=None):
        self.server_dir = os.path.abspath(server_dir)
        self.state_file = state_file
        self.on_change = on_change
        self.backups_dir = os.path.join(self.server_dir, "backups")
        self.old_dir = os.path.join(self.server_dir, "mondi-vecchi")
        self.world_dir = os.path.join(self.server_dir, "world")
        self._op_lock = threading.Lock()
        self._mu = threading.RLock()  # stato / busy
        self._busy = None
        self._last_error = None
        self._first_tick = None
        self._sched_thread = None
        self._retry_at = None         # dopo un backup programmato fallito: non prima di questo istante
        self._size_cache = {}
        self._last_notify = 0.0
        self._state = self._load_state()

    # ------------------------------------------------------------ stato
    def _load_state(self):
        st = {}
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                st = d
        except (OSError, ValueError):
            st = {}
        pinned = st.get("pinned")
        notes = st.get("note")
        out = {
            "pinned": {x for x in pinned if isinstance(x, str)} if isinstance(pinned, list) else set(),
            "note": {k: v for k, v in notes.items() if isinstance(k, str) and isinstance(v, str)}
            if isinstance(notes, dict) else {},
            "schedule": DEFAULT_SCHEDULE.copy(),
            "last_scheduled": st.get("last_scheduled") if isinstance(st.get("last_scheduled"), (int, float)) else None,
            "last_daily": st.get("last_daily") if isinstance(st.get("last_daily"), str) else None,
        }
        out["schedule"]["keep"] = dict(DEFAULT_SCHEDULE["keep"])
        try:
            out["schedule"] = self._normalize_schedule(st.get("schedule") or {}, out["schedule"])
        except ValueError:
            pass
        return out

    def _save_state(self):
        with self._mu:
            data = {
                "pinned": sorted(self._state["pinned"]),
                "note": self._state["note"],
                "schedule": self._state["schedule"],
                "last_scheduled": self._state["last_scheduled"],
                "last_daily": self._state["last_daily"],
            }
            tmp = self.state_file + ".tmp"
            d = os.path.dirname(os.path.abspath(self.state_file))
            os.makedirs(d, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.state_file)

    # ------------------------------------------------------------ utilità
    def _notify(self, force=True):
        now = time.time()
        if not force and now - self._last_notify < 0.5:
            return
        self._last_notify = now
        if self.on_change:
            try:
                self.on_change()
            except Exception:
                pass

    def _begin(self, op, label):
        if not self._op_lock.acquire(blocking=False):
            raise ValueError(tr("Another operation is in progress"))
        with self._mu:
            self._busy = {"op": op, "label": label, "progress": 0.0, "started": time.time()}
            self._last_error = None
        self._notify()

    def _end(self):
        with self._mu:
            self._busy = None
        self._op_lock.release()
        self._notify()

    def _progress(self, frac):
        with self._mu:
            if self._busy:
                self._busy["progress"] = max(0.0, min(1.0, frac))
        self._notify(force=False)

    def _set_error(self, msg):
        with self._mu:
            self._last_error = msg

    @staticmethod
    def _unique(path):
        if not os.path.exists(path):
            return path
        base, ext = os.path.splitext(path)
        i = 2
        while os.path.exists("%s-%d%s" % (base, i, ext)):
            i += 1
        return "%s-%d%s" % (base, i, ext)

    def _folder_size(self, path, mtime):
        key = (os.path.basename(path), mtime)
        with self._mu:
            if key in self._size_cache:
                return self._size_cache[key]
        total = 0
        for root, _d, files in os.walk(path):
            for fn in files:
                try:
                    total += os.path.getsize(os.path.join(root, fn))
                except OSError:
                    pass
        with self._mu:
            self._size_cache[key] = total
        return total

    # ------------------------------------------------------------ elenco
    def list(self):
        out = []
        with self._mu:
            pinned = set(self._state["pinned"])
            notes = dict(self._state["note"])
        try:
            names = os.listdir(self.backups_dir)
        except OSError:
            names = []
        for n in names:
            if not n.lower().endswith(".zip"):
                continue
            p = os.path.join(self.backups_dir, n)
            try:
                if not os.path.isfile(p):
                    continue
                stt = os.stat(p)
            except OSError:
                continue
            i = "backups/" + n
            out.append({"id": i, "name": n, "folder": "backups", "kind": _kind_of_zip(n),
                        "size": stt.st_size, "mtime": stt.st_mtime,
                        "pinned": i in pinned, "note": notes.get(i, "")})
        try:
            names = os.listdir(self.old_dir)
        except OSError:
            names = []
        for n in names:
            p = os.path.join(self.old_dir, n)
            try:
                if not os.path.isdir(p):
                    continue
                mt = os.stat(p).st_mtime
            except OSError:
                continue
            i = "mondi-vecchi/" + n
            out.append({"id": i, "name": n, "folder": "mondi-vecchi", "kind": "mondo",
                        "size": self._folder_size(p, mt), "mtime": mt,
                        "pinned": i in pinned, "note": notes.get(i, "")})
        out.sort(key=lambda e: e["mtime"], reverse=True)
        return out

    def disk(self):
        u = shutil.disk_usage(self.server_dir)
        return {"free": u.free, "total": u.total}

    def _find(self, id):
        if not isinstance(id, str) or not id:
            raise ValueError(tr("Invalid backup"))
        parts = id.split("/")
        if (len(parts) != 2 or parts[0] not in ("backups", "mondi-vecchi")
                or parts[1] in ("", ".", "..") or "\\" in id or ":" in id):
            raise ValueError(tr("Invalid backup"))
        for e in self.list():
            if e["id"] == id:
                return e
        raise ValueError(tr("Backup not found"))

    def _abs(self, e):
        return os.path.join(self.server_dir, e["folder"], e["name"])

    def path(self, id):
        e = self._find(id)
        if e["folder"] != "backups":
            raise ValueError(tr("Not a zip file: only zips can be downloaded"))
        return self._abs(e)

    # ------------------------------------------------------------ stato pubblico
    def status(self):
        with self._mu:
            busy = dict(self._busy) if self._busy else None
            cfg = json.loads(json.dumps(self._state["schedule"]))
            return {"busy": busy, "last_error": self._last_error, "schedule": cfg,
                    "last_scheduled": self._state["last_scheduled"],
                    "next_scheduled": self._next_scheduled(cfg)}

    def _next_scheduled(self, cfg):
        cands = []
        base = self._state["last_scheduled"] or self._first_tick
        if cfg["every_hours"] > 0 and base:
            cands.append(base + cfg["every_hours"] * 3600)
        if cfg["daily_at"]:
            h, m = (int(x) for x in cfg["daily_at"].split(":"))
            now = datetime.now()
            t = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if self._state["last_daily"] == now.strftime("%Y-%m-%d"):
                t += timedelta(days=1)
            elif t < now:
                t = now
            cands.append(t.timestamp())
        return min(cands) if cands else None

    # ------------------------------------------------------------ pin
    def pin(self, id, pinned):
        e = self._find(id)
        with self._mu:
            if pinned:
                self._state["pinned"].add(e["id"])
            else:
                self._state["pinned"].discard(e["id"])
            self._save_state()
        self._notify()

    # ------------------------------------------------------------ pianificazione
    @staticmethod
    def _int_in(v, lo, hi, what):
        if isinstance(v, bool) or not isinstance(v, (int, float, str)):
            raise ValueError(tr("Invalid %s") % what)
        try:
            n = int(float(v))
        except ValueError:
            raise ValueError(tr("Invalid %s") % what)
        if not lo <= n <= hi:
            raise ValueError(tr("%s must be between %d and %d") % (what, lo, hi))
        return n

    def _normalize_schedule(self, cfg, base):
        if not isinstance(cfg, dict):
            raise ValueError(tr("Invalid configuration"))
        out = {"every_hours": base["every_hours"], "only_when_online": base["only_when_online"],
               "daily_at": base["daily_at"], "keep": dict(base["keep"])}
        if "every_hours" in cfg:
            out["every_hours"] = self._int_in(cfg["every_hours"], 0, 48, tr("Hours between backups"))
        if "only_when_online" in cfg:
            out["only_when_online"] = bool(cfg["only_when_online"])
        if "daily_at" in cfg:
            d = cfg["daily_at"]
            if d is None:
                d = ""
            if not isinstance(d, str):
                raise ValueError(tr("Invalid daily time"))
            d = d.strip()
            if d:
                try:
                    t = datetime.strptime(d, "%H:%M")
                except ValueError:
                    raise ValueError(tr("Invalid daily time (use HH:MM)"))
                d = t.strftime("%H:%M")
            out["daily_at"] = d
        if "keep" in cfg:
            k = cfg["keep"]
            if not isinstance(k, dict):
                raise ValueError(tr("Invalid retention"))
            for kind in KINDS_KEEP:
                if kind in k:
                    out["keep"][kind] = self._int_in(k[kind], 0, 200, tr("How many to keep (%s)") % kind)
        return out

    def set_schedule(self, cfg):
        with self._mu:
            new = self._normalize_schedule(cfg, self._state["schedule"])
            self._state["schedule"] = new
            self._save_state()
            res = json.loads(json.dumps(new))
        self._notify()
        return res

    # ------------------------------------------------------------ creazione
    def create(self, kind="manuale", server_on=False, rcon=None, note=""):
        prefix = {"manuale": "manuale", "programmato": "auto", "auto": "auto"}.get(kind)
        if prefix is None:
            raise ValueError(tr("Invalid backup type"))
        if server_on and rcon is None:
            raise ValueError(tr("RCON is needed to back up while the server is on"))
        self._begin("backup", tr("Scheduled backup") if prefix == "auto" else tr("Manual backup"))
        with self._mu:
            self._last_error = None   # l'avviso vecchio non vale piu' per questo tentativo
        tmpdir = part = None
        try:
            os.makedirs(self.backups_dir, exist_ok=True)
            self._clean_stale()
            if not os.path.isdir(self.world_dir):
                raise ValueError(tr("World folder not found"))
            ts = datetime.now().strftime(TS_FMT)
            final = self._unique(os.path.join(self.backups_dir, "%s_%s.zip" % (prefix, ts)))
            part = final + ".part"
            src = self.world_dir
            total = self._tree_bytes(self.world_dir)
            if server_on:
                tmpdir = os.path.join(self.backups_dir, ".tmp-" + ts)
                _rmtree(tmpdir)
                save_off = False
                try:
                    rcon("save-off")
                    save_off = True
                    rcon("save-all flush")
                    time.sleep(1.0)
                    self._copy_world(self.world_dir, os.path.join(tmpdir, "world"), total)
                finally:
                    # sempre, anche se save-off sembra fallito (la risposta puo' essersi persa)
                    err = None
                    for attempt in range(3):
                        try:
                            rcon("save-on")
                            err = None
                            break
                        except Exception as ex:  # noqa: BLE001
                            err = ex
                            time.sleep(2)
                    if err is not None and save_off:
                        self._set_error(tr("WARNING: save-on failed (%s): type save-on in the Console") % err)
                src = os.path.join(tmpdir, "world")
                base, span = 0.3, 0.7
            else:
                base, span = 0.0, 1.0
            self._zip_world(src, part, base, span)
            os.replace(part, final)
            part = None
            if tmpdir:
                _rmtree(tmpdir)
                tmpdir = None
            eid = "backups/" + os.path.basename(final)
            if note:
                with self._mu:
                    self._state["note"][eid] = str(note)
                    self._save_state()
            return self._find(eid)
        except Exception as ex:
            if part:
                _rmfile(part)
            if tmpdir:
                _rmtree(tmpdir)
            with self._mu:
                if not (self._last_error or "").startswith(tr("WARNING")):
                    self._last_error = tr("Backup failed: %s") % ex
            raise
        finally:
            self._end()

    def _clean_stale(self):
        try:
            for n in os.listdir(self.backups_dir):
                p = os.path.join(self.backups_dir, n)
                if n.startswith(".tmp-") and os.path.isdir(p):
                    _rmtree(p)
                elif n.lower().endswith(".part") and os.path.isfile(p):
                    _rmfile(p)
        except OSError:
            pass

    @staticmethod
    def _tree_bytes(root):
        t = 0
        for r, _d, files in os.walk(root):
            for fn in files:
                if fn == "session.lock":
                    continue
                try:
                    t += os.path.getsize(os.path.join(r, fn))
                except OSError:
                    pass
        return max(t, 1)

    def _copy_world(self, src, dst, total):
        done = 0
        for r, dirs, files in os.walk(src):
            rel = os.path.relpath(r, src)
            tgt = dst if rel == "." else os.path.join(dst, rel)
            os.makedirs(tgt, exist_ok=True)
            for fn in files:
                if fn == "session.lock":
                    continue
                s = os.path.join(r, fn)
                d = os.path.join(tgt, fn)
                try:
                    shutil.copyfile(s, d)
                    done += os.path.getsize(d)
                except OSError as ex:
                    _rmfile(d)   # un backup a cui manca un pezzo non va spacciato per buono
                    raise OSError(tr("unreadable file: %s (%s)") % (os.path.relpath(s, src), ex))
                self._progress(0.3 * done / total)

    def _zip_world(self, src, part, base, span):
        total = self._tree_bytes(src)
        done = 0
        with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as zf:
            for r, dirs, files in os.walk(src):
                rel = os.path.relpath(r, src)
                arc_dir = "world" if rel == "." else "world/" + rel.replace(os.sep, "/")
                if not dirs and not files:
                    zf.writestr(arc_dir + "/", b"")
                for fn in files:
                    if fn == "session.lock":
                        continue
                    s = os.path.join(r, fn)
                    try:
                        zf.write(s, arc_dir + "/" + fn)
                        done += os.path.getsize(s)
                    except OSError as ex:
                        raise OSError(tr("unreadable file: %s (%s)") % (os.path.relpath(s, src), ex))
                    self._progress(base + span * done / total)

    # ------------------------------------------------------------ eliminazione
    def delete(self, id):
        e = self._find(id)
        if e["pinned"]:
            raise ValueError(tr("Unpin it first"))
        self._begin("delete", tr("Deleting %s") % e["name"])
        try:
            self._recycle(e)
        except Exception as ex:
            self._set_error(tr("Delete failed: %s") % ex)
            raise
        finally:
            self._end()

    def _recycle(self, e):
        if os.name == "nt":
            _to_recycle_bin(self._abs(e), e.get("size"))
        else:
            _to_trash_dir(self._abs(e), os.path.join(self.server_dir, "trash"))
        with self._mu:
            self._state["pinned"].discard(e["id"])
            self._state["note"].pop(e["id"], None)
            self._save_state()

    # ------------------------------------------------------------ conservazione
    def prune(self):
        if not self._op_lock.acquire(blocking=False):
            return []
        removed = []
        try:
            with self._mu:
                self._busy = {"op": "delete", "label": tr("Cleaning up old backups"),
                              "progress": 0.0, "started": time.time()}
            keep = self.status()["schedule"]["keep"]
            entries = self.list()
            for kind in KINDS_KEEP:
                n = keep.get(kind, 0)
                if n <= 0:
                    continue
                cand = [e for e in entries if e["kind"] == kind and not e["pinned"]]
                cand.sort(key=lambda e: e["mtime"], reverse=True)
                for e in cand[n:]:
                    try:
                        self._recycle(e)
                        removed.append(e["id"])
                    except Exception as ex:
                        self._set_error(tr("Cleanup failed for %s: %s") % (e["name"], ex))
        finally:
            with self._mu:
                self._busy = None
            self._op_lock.release()
            if removed:
                self._notify()
        return removed

    # ------------------------------------------------------------ tick
    def tick(self, now, server_on, players_online, rcon):
        try:
            with self._mu:
                if self._first_tick is None:
                    self._first_tick = now
                cfg = self._state["schedule"]
                last = self._state["last_scheduled"]
                last_daily = self._state["last_daily"]
            idle = not self._op_lock.locked() and not (self._sched_thread and self._sched_thread.is_alive()) \
                and now >= (self._retry_at or 0)
            if idle:
                start = None
                if (cfg["every_hours"] > 0 and server_on
                        and (players_online > 0 or not cfg["only_when_online"])
                        and now - (last or self._first_tick) >= cfg["every_hours"] * 3600):
                    start = "interval"
                elif cfg["daily_at"]:
                    dt = datetime.fromtimestamp(now)
                    h, m = (int(x) for x in cfg["daily_at"].split(":"))
                    if last_daily != dt.strftime("%Y-%m-%d") and (dt.hour, dt.minute) >= (h, m):
                        start = "daily"
                if start:
                    th = threading.Thread(target=self._run_scheduled,
                                          args=(start, now, server_on, rcon), daemon=True)
                    self._sched_thread = th
                    th.start()
                    return  # la pulizia avverrà a fine backup
            if not self._op_lock.locked():
                self.prune()
        except Exception as ex:
            self._set_error(tr("Scheduling error: %s") % ex)

    def _run_scheduled(self, why, now, server_on, rcon):
        try:
            self.create("programmato", server_on=server_on, rcon=rcon if server_on else None)
        except Exception as ex:  # noqa: BLE001
            # last_error è già salvato da create; non conta come fatto: si riprova tra 15 minuti
            if "in corso" not in str(ex):
                with self._mu:
                    self._retry_at = now + 900
            return
        with self._mu:
            self._retry_at = None
            self._state["last_scheduled"] = now
            dt, daily = datetime.fromtimestamp(now), self._state["schedule"]["daily_at"]
            # un backup a intervallo fatto dopo l'orario del giornaliero vale anche come giornaliero
            if why == "daily" or (daily and (dt.hour, dt.minute) >= tuple(int(x) for x in daily.split(":"))):
                self._state["last_daily"] = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
            try:
                self._save_state()
            except OSError:
                pass
        self._notify()
        try:
            self.prune()
        except Exception as ex:
            self._set_error(tr("Cleanup error: %s") % ex)

    # ------------------------------------------------------------ ripristino
    def restore(self, id, server_on):
        if server_on:
            raise ValueError(tr("Stop the server first"))
        e = self._find(id)
        self._begin("restore", tr("Restoring %s") % e["name"])
        tmp = os.path.join(self.server_dir, "world.ripristino-tmp")
        try:
            _rmtree(tmp)
            src = self._abs(e)
            if e["folder"] == "backups":
                self._extract_zip(src, tmp)
            else:
                self._copy_folder(src, tmp)
            if not os.path.isfile(os.path.join(tmp, "level.dat")):
                raise ValueError(tr("The backup does not contain level.dat"))
            os.makedirs(self.old_dir, exist_ok=True)
            old = None
            if os.path.isdir(self.world_dir):
                old = self._unique(os.path.join(
                    self.old_dir, "world_%s_prima-del-ripristino" % datetime.now().strftime(TS_FMT)))
                os.replace(self.world_dir, old)
            try:
                os.replace(tmp, self.world_dir)
            except Exception:
                if old:
                    os.replace(old, self.world_dir)
                raise
            self._notify()
            return {"moved_to": os.path.basename(old) if old else ""}
        except Exception as ex:
            _rmtree(tmp)
            self._set_error(tr("Restore failed: %s") % ex)
            raise
        finally:
            self._end()

    @staticmethod
    def _split_name(name):
        n = name.replace("\\", "/")
        if n.startswith("/") or (len(n) > 1 and n[1] == ":"):
            raise ValueError(tr("Unsafe zip: absolute path (%s)") % name)
        parts = [p for p in n.split("/") if p not in ("", ".")]
        for p in parts:
            if p == ".." or ":" in p:
                raise ValueError(tr("Unsafe zip: invalid path (%s)") % name)
        return parts

    def _extract_zip(self, zpath, dest):
        with zipfile.ZipFile(zpath) as zf:
            names = {i.filename.replace("\\", "/") for i in zf.infolist()}
            if "world/level.dat" in names:
                prefix = ["world"]
            elif "level.dat" in names:
                prefix = []
            else:
                tops = {n.split("/")[0] for n in names if "/" in n}
                cands = [t for t in tops if (t + "/level.dat") in names]
                if len(cands) != 1:
                    raise ValueError(tr("Unrecognised zip layout (world/level.dat missing)"))
                prefix = [cands[0]]
            members = []
            for info in zf.infolist():
                parts = self._split_name(info.filename)  # valida TUTTI i membri
                if parts[:len(prefix)] == prefix and len(parts) > len(prefix):
                    members.append((info, parts[len(prefix):]))
            total = max(sum(i.file_size for i, _ in members), 1)
            dest_real = os.path.realpath(dest)
            os.makedirs(dest_real, exist_ok=True)
            done = 0
            for info, rel in members:
                target = os.path.realpath(os.path.join(dest_real, *rel))
                if os.path.commonpath([dest_real, target]) != dest_real:
                    raise ValueError(tr("Unsafe zip: entry outside the destination (%s)") % info.filename)
                if info.is_dir() or info.filename.endswith(("/", "\\")):
                    os.makedirs(target, exist_ok=True)
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with zf.open(info) as s, open(target, "wb") as d:
                    while True:
                        b = s.read(CHUNK)
                        if not b:
                            break
                        d.write(b)
                        done += len(b)
                        self._progress(done / total)

    def _copy_folder(self, src, dest):
        total = self._tree_bytes(src)
        done = 0
        for r, _dirs, files in os.walk(src):
            rel = os.path.relpath(r, src)
            tgt = dest if rel == "." else os.path.join(dest, rel)
            os.makedirs(tgt, exist_ok=True)
            for fn in files:
                if fn == "session.lock":
                    continue
                s = os.path.join(r, fn)
                shutil.copyfile(s, os.path.join(tgt, fn))
                done += os.path.getsize(s)
                self._progress(done / total)
