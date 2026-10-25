"""Minecraft world backups (standard library only, Windows and Linux).

Used by panel.py. One operation at a time (backup / restore / delete).
"""
import ctypes
import json
import os
import shutil
import threading
import time
import zipfile
from datetime import datetime, timedelta

from i18n import t as tr   # t is used as a local variable (times)

if os.name == "nt":
    from ctypes import wintypes
else:
    wintypes = None

TS_FMT = "%Y-%m-%d_%H-%M-%S"
OLD_DIRS = ("old-worlds", "mondi-vecchi")   # where replaced worlds go; the second is the name from before 1.0, only read
KINDS_KEEP = ("startup", "scheduled", "manual")
OLD_KINDS = {"avvio": "startup", "programmato": "scheduled", "manuale": "manual"}   # names in state files from before 1.0
DEFAULT_SCHEDULE = {
    "every_minutes": 120,
    "only_when_online": True,
    "window": None,   # {"from": "HH:MM", "to": "HH:MM"}: interval backups only inside it (from > to crosses midnight)
    "daily_at": ["04:00"],
    "days": [0, 1, 2, 3, 4, 5, 6],   # Monday = 0, for the daily times
    "keep": {"startup": 10, "scheduled": 12, "manual": 0},
    "max_days": {"startup": 0, "scheduled": 0, "manual": 0},
    "max_gb": 0,
}
MAX_DAILY = 6
SLOT_FMT = "%Y-%m-%d %H:%M"
CHUNK = 1024 * 1024
SPACE_MARGIN = 512 * 1024 * 1024   # always left free: the server must still be able to save the world after a backup


EXTRA = ("_nether", "_the_end")   # Paper and Spigot keep the Nether and the End next to the world: world_nether, world_the_end


class Busy(ValueError):
    """Another operation is running: not a backup error, it is retried at the next round."""


def _extras(world):
    return [(sfx, world + sfx) for sfx in EXTRA if os.path.isdir(world + sfx)]


def _mb(n):
    return "%d MB" % (n // 1048576) if n < 10 * 1073741824 else "%.1f GB" % (n / 1073741824)


def _kind_of_zip(name):
    if name.startswith("world_"):
        return "startup"
    if name.startswith("auto_"):
        return "scheduled"
    if name.startswith(("manual_", "manuale_")):
        return "manual"
    return "other"


def _rmtree(p):
    shutil.rmtree(p, ignore_errors=True)


def _rmfile(p):
    try:
        os.remove(p)
    except OSError:
        pass


# ---------------------------------------------------------------- Recycle Bin
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
    """(max bytes, delete-right-away) of the Recycle Bin of path's disk, read from the registry; None if unknown."""
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


class _BinRefused(OSError):
    """The Recycle Bin is disabled or too small for the file (not a read or move failure)."""


def _to_recycle_bin(path, size=None):
    """Moves path (file or folder) to the Recycle Bin. Raises OSError if that fails.
    If the Recycle Bin cannot hold it Windows would delete it for good without asking (we pass
    NOCONFIRMATION): in that case we stop first and leave the choice to a person."""
    path = os.path.abspath(path)
    lim = _recycle_limit(path)
    if lim is None:
        raise OSError(tr("Cannot read the Recycle Bin settings: not deleting anything, to be safe"))
    if lim[1]:
        raise _BinRefused(tr("The Recycle Bin is disabled on this drive: delete the file by hand if you are sure"))
    if size is not None and size >= lim[0]:
        raise _BinRefused(tr("Too big for the Recycle Bin (limit %d MB): delete it by hand if you are sure") % (lim[0] // 1048576))
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
    """Linux: servers have no system trash, so it is moved to trash/ (same disk as the server: instant).
    shortcut: what stays in trash/ more than TRASH_DAYS days is deleted, as the Windows Recycle Bin does when full."""
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
    os.utime(dst)   # the age in the trash counts from now, not from the backup date
    return dst


def _in_window(win, dt):
    if not win:
        return True
    m = dt.hour * 60 + dt.minute
    f, t = (int(x[:2]) * 60 + int(x[3:]) for x in (win["from"], win["to"]))
    return f <= m < t if f < t else (m >= f or m < t)


def _slots(cfg, dt):
    """Today's daily slots as "YYYY-MM-DD HH:MM" (strings compare in time order)."""
    if dt.weekday() not in cfg["days"]:
        return []
    return ["%s %s" % (dt.strftime("%Y-%m-%d"), h) for h in cfg["daily_at"]]


def _prune_plan(entries, sch, now=None):
    """[(entry, reason)] in deletion order. Count and age per (world, kind), then the size cap of the automatic
    ones. Age and size never take the newest of a group: a world left alone for weeks keeps a backup."""
    now = now or time.time()
    plan, gone = [], set()
    for kind in KINDS_KEEP:
        n, days = sch["keep"].get(kind, 0), sch["max_days"].get(kind, 0)
        groups = {}
        for e in entries:
            if e["kind"] == kind and not e["pinned"]:
                groups.setdefault(e.get("world", ""), []).append(e)
        for g in groups.values():
            g.sort(key=lambda e: e["mtime"], reverse=True)
            for i, e in enumerate(g):
                why = "count" if n > 0 and i >= n else "age" if days > 0 and i > 0 and now - e["mtime"] > days * 86400 else None
                if why:
                    plan.append((e, why))
                    gone.add(e["id"])
    if sch["max_gb"] > 0:
        auto = sorted((e for e in entries if e["kind"] in ("startup", "scheduled") and not e["pinned"]
                       and e["id"] not in gone), key=lambda e: e["mtime"])
        newest = {e.get("world", ""): e["id"] for e in auto}   # sorted oldest first: the last one per world wins
        total = sum(e["size"] for e in auto)
        for e in auto:
            if total <= sch["max_gb"] * 1073741824:
                break
            if e["id"] != newest[e.get("world", "")]:
                plan.append((e, "size"))
                total -= e["size"]
    return plan


# ---------------------------------------------------------------- class
class Backups:
    def __init__(self, server_dir, state_file, on_change=None):
        self.server_dir = os.path.abspath(server_dir)
        self.state_file = state_file
        self.on_change = on_change
        self.backups_dir = os.path.join(self.server_dir, "backups")
        self.old_dir = os.path.join(self.server_dir, OLD_DIRS[0])
        self.world_dir = os.path.join(self.server_dir, "world")
        self._op_lock = threading.Lock()
        self._mu = threading.RLock()  # state / busy
        self._busy = None
        self._last_error = None
        self._first_tick = None
        self._sched_thread = None
        self._retry_at = None         # after a failed scheduled backup: not before this moment
        self._size_cache = {}
        self._last_notify = 0.0
        self._state = self._load_state()

    # ------------------------------------------------------------ state
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
        worlds = st.get("world")
        out = {
            "pinned": {x for x in pinned if isinstance(x, str)} if isinstance(pinned, list) else set(),
            "note": {k: v for k, v in notes.items() if isinstance(k, str) and isinstance(v, str)}
            if isinstance(notes, dict) else {},
            "world": {k: v for k, v in worlds.items() if isinstance(k, str) and isinstance(v, str)}
            if isinstance(worlds, dict) else {},
            "schedule": json.loads(json.dumps(DEFAULT_SCHEDULE)),
            "last_scheduled": st.get("last_scheduled") if isinstance(st.get("last_scheduled"), (int, float)) else None,
            "last_daily": st.get("last_daily") if isinstance(st.get("last_daily"), str) else None,
        }
        if out["last_daily"] and len(out["last_daily"]) == 10:
            out["last_daily"] += " 23:59"   # before 1.x only the day was saved: that day counts as done
        sch = st.get("schedule")
        if isinstance(sch, dict):
            for k, v in sch.items():   # an invalid field falls back to its default, the others stay
                try:
                    out["schedule"] = self._normalize_schedule({k: v}, out["schedule"])
                except ValueError:
                    pass
        return out

    def _save_state(self):
        with self._mu:
            data = {
                "pinned": sorted(self._state["pinned"]),
                "note": self._state["note"],
                "world": self._state["world"],
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

    # ------------------------------------------------------------ helpers
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
            raise Busy(tr("Another operation is in progress"))
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

    # ------------------------------------------------------------ list
    def list(self):
        out = []
        with self._mu:
            pinned = set(self._state["pinned"])
            notes = dict(self._state["note"])
            worlds = dict(self._state["world"])
        # shortcut: untagged backups (made before the world was recorded) count as the current world, a renamed world needs a manual tag
        cur = os.path.basename(self.world_dir)
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
                        "pinned": i in pinned, "note": notes.get(i, ""), "world": worlds.get(i, cur)})
        for folder in OLD_DIRS:
            try:
                names = os.listdir(os.path.join(self.server_dir, folder))
            except OSError:
                names = []
            for n in names:
                p = os.path.join(self.server_dir, folder, n)
                if any(n.endswith(sfx) and n[:-len(sfx)] in names for sfx in EXTRA):
                    continue
                try:
                    if not os.path.isdir(p):
                        continue
                    mt = os.stat(p).st_mtime
                except OSError:
                    continue
                i = folder + "/" + n
                out.append({"id": i, "name": n, "folder": folder, "kind": "world",
                            "size": self._folder_size(p, mt), "mtime": mt,
                            "pinned": i in pinned, "note": notes.get(i, ""), "world": worlds.get(i, "")})
        out.sort(key=lambda e: e["mtime"], reverse=True)
        return out

    def check_space(self, need, path=None):
        """A readable error before starting, instead of filling the disk halfway through the copy."""
        free = shutil.disk_usage(path or self.server_dir).free
        if free < need + SPACE_MARGIN:
            raise ValueError(tr("Not enough disk space: about %s needed, %s free.") % (_mb(need + SPACE_MARGIN), _mb(free)))

    def disk(self):
        u = shutil.disk_usage(self.server_dir)
        return {"free": u.free, "total": u.total}

    def _find(self, id, entries=None):
        if not isinstance(id, str) or not id:
            raise ValueError(tr("Invalid backup"))
        parts = id.split("/")
        if (len(parts) != 2 or parts[0] not in ("backups",) + OLD_DIRS
                or parts[1] in ("", ".", "..") or "\\" in id or ":" in id):
            raise ValueError(tr("Invalid backup"))
        for e in entries or self.list():
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

    # ------------------------------------------------------------ public state
    def status(self):
        with self._mu:
            busy = dict(self._busy) if self._busy else None
            cfg = json.loads(json.dumps(self._state["schedule"]))
            return {"busy": busy, "last_error": self._last_error, "schedule": cfg,
                    "last_scheduled": self._state["last_scheduled"],
                    "next_scheduled": self._next_scheduled(cfg),
                    "next_daily": self._next_scheduled(dict(cfg, every_minutes=0))}   # the interval needs the server on

    def _next_scheduled(self, cfg):
        cands = []
        base = self._state["last_scheduled"] or self._first_tick
        if cfg["every_minutes"] > 0 and base:
            t = base + cfg["every_minutes"] * 60
            dt, w = datetime.fromtimestamp(t), cfg["window"]
            if not _in_window(w, dt):
                dt = dt.replace(hour=int(w["from"][:2]), minute=int(w["from"][3:]), second=0, microsecond=0)
                if dt.timestamp() < t:
                    dt += timedelta(days=1)
                t = dt.timestamp()
            cands.append(t)
        if cfg["daily_at"]:
            now = datetime.now()
            for d in range(8):
                for slot in _slots(cfg, now + timedelta(days=d)):
                    t = datetime.strptime(slot, SLOT_FMT)
                    if d == 0 and slot <= (self._state["last_daily"] or ""):
                        continue
                    cands.append(max(t, now).timestamp() if d == 0 else t.timestamp())
        return min(cands) if cands else None

    # ------------------------------------------------------------ pin / note
    def pin(self, id, pinned):
        self.pin_many([id], pinned)

    def pin_many(self, ids, pinned):
        found = self._many(ids)
        with self._mu:
            for e in found:
                if pinned:
                    self._state["pinned"].add(e["id"])
                else:
                    self._state["pinned"].discard(e["id"])
            self._save_state()
        self._notify()

    def set_note(self, id, note):
        e = self._find(id)
        note = "" if note is None else str(note).strip()[:80]
        with self._mu:
            if note:
                self._state["note"][e["id"]] = note
            else:
                self._state["note"].pop(e["id"], None)
            self._save_state()
        self._notify()

    def _many(self, ids):
        if not isinstance(ids, list) or not ids:
            raise ValueError(tr("Invalid backup"))
        if len(ids) > 500:
            raise ValueError(tr("Too many backups at once (max %d)") % 500)
        if not all(isinstance(i, str) for i in ids):
            raise ValueError(tr("Invalid backup"))
        entries = self.list()
        return [self._find(i, entries) for i in dict.fromkeys(ids)]

    # ------------------------------------------------------------ schedule
    @staticmethod
    def _int_in(v, lo, hi, what):
        if isinstance(v, bool) or not isinstance(v, (int, float, str)):
            raise ValueError(tr("Invalid %s") % what)
        try:
            n = int(float(v))
        except (ValueError, OverflowError):   # "1e999" is inf
            raise ValueError(tr("Invalid %s") % what)
        if not lo <= n <= hi:
            raise ValueError(tr("%s must be between %d and %d") % (what, lo, hi))
        return n

    @staticmethod
    def _hhmm(v, msg):
        try:
            return datetime.strptime(v.strip(), "%H:%M").strftime("%H:%M")
        except (ValueError, AttributeError):
            raise ValueError(msg)

    def _normalize_schedule(self, cfg, base):
        if not isinstance(cfg, dict):
            raise ValueError(tr("Invalid configuration"))
        out = json.loads(json.dumps(base))
        if "every_minutes" in cfg:
            out["every_minutes"] = self._int_in(cfg["every_minutes"], 0, 2880, tr("Minutes between backups"))
            if 0 < out["every_minutes"] < 10:
                raise ValueError(tr("%s must be between %d and %d") % (tr("Minutes between backups"), 10, 2880))
        elif "every_hours" in cfg:   # state files and clients from before the minutes
            out["every_minutes"] = self._int_in(cfg["every_hours"], 0, 48, tr("Hours between backups")) * 60
        if "only_when_online" in cfg:
            out["only_when_online"] = bool(cfg["only_when_online"])
        if "window" in cfg:
            w = cfg["window"]
            if not w:
                out["window"] = None
            elif isinstance(w, dict):
                f, t = (self._hhmm(w.get(k), tr("Invalid time window (use HH:MM)")) for k in ("from", "to"))
                if f == t:
                    raise ValueError(tr("The time window must start and end at different times"))
                out["window"] = {"from": f, "to": t}
            else:
                raise ValueError(tr("Invalid time window (use HH:MM)"))
        if "daily_at" in cfg:
            d = cfg["daily_at"]
            if d is None:
                d = []
            elif isinstance(d, str):
                d = [d] if d.strip() else []
            if not isinstance(d, list):
                raise ValueError(tr("Invalid daily time"))
            d = sorted({self._hhmm(x, tr("Invalid daily time (use HH:MM)")) for x in d})
            if len(d) > MAX_DAILY:
                raise ValueError(tr("At most %d daily times") % MAX_DAILY)
            out["daily_at"] = d
        if "days" in cfg:
            d = cfg["days"]
            if not isinstance(d, list):
                raise ValueError(tr("Invalid days"))
            out["days"] = sorted({self._int_in(x, 0, 6, tr("Day")) for x in d})
        if out["daily_at"] and not out["days"]:
            raise ValueError(tr("Pick at least one day"))
        if "keep" in cfg:
            k = cfg["keep"]
            if not isinstance(k, dict):
                raise ValueError(tr("Invalid retention"))
            k = {OLD_KINDS.get(kind, kind): v for kind, v in k.items()}
            for kind in KINDS_KEEP:
                if kind in k:
                    out["keep"][kind] = self._int_in(k[kind], 0, 200, tr("How many to keep (%s)") % kind)
        if "max_days" in cfg:
            k = cfg["max_days"]
            if not isinstance(k, dict):
                raise ValueError(tr("Invalid retention"))
            for kind in KINDS_KEEP:
                if kind in k:
                    out["max_days"][kind] = self._int_in(k[kind], 0, 3650, tr("Maximum age in days (%s)") % kind)
        if "max_gb" in cfg:
            g = cfg["max_gb"]
            try:
                if isinstance(g, bool) or not isinstance(g, (int, float, str)):
                    raise ValueError
                g = round(float(g), 1)
            except ValueError:
                raise ValueError(tr("Invalid %s") % tr("size limit"))
            if not 0 <= g <= 100000:
                raise ValueError(tr("%s must be between %d and %d") % (tr("Size limit (GB)"), 0, 100000))
            out["max_gb"] = int(g) if g == int(g) else g
        return out

    def preview(self, cfg):
        """What the next cleanup would remove with these rules (nothing is saved)."""
        with self._mu:
            sch = self._normalize_schedule(cfg, self._state["schedule"])
        plan = _prune_plan(self.list(), sch)
        return {"ids": [e["id"] for e, _w in plan], "bytes": sum(e["size"] for e, _w in plan)}

    def set_schedule(self, cfg):
        with self._mu:
            new = self._normalize_schedule(cfg, self._state["schedule"])
            self._state["schedule"] = new
            self._save_state()
            res = json.loads(json.dumps(new))
        self._notify()
        return res

    # ------------------------------------------------------------ create
    def create(self, kind="manual", server_on=False, rcon=None, note=""):
        prefix = {"manual": "manual", "scheduled": "auto", "auto": "auto", "startup": "world"}.get(kind)
        if prefix is None:
            raise ValueError(tr("Invalid backup type"))
        if server_on and rcon is None:
            raise ValueError(tr("RCON is needed to back up while the server is on"))
        self._begin("backup", {"auto": tr("Scheduled backup"), "world": tr("Backup before the start")}.get(prefix, tr("Manual backup")))
        with self._mu:
            self._last_error = None   # the old warning does not apply to this attempt
        tmpdir = part = None
        try:
            os.makedirs(self.backups_dir, exist_ok=True)
            self._clean_stale()
            if not os.path.isdir(self.world_dir):
                raise ValueError(tr("World folder not found"))
            ts = datetime.now().strftime(TS_FMT)
            final = self._unique(os.path.join(self.backups_dir, "%s_%s.zip" % (prefix, ts)))
            part = final + ".part"
            srcs = [("world", self.world_dir)] + [("world" + sfx, p) for sfx, p in _extras(self.world_dir)]
            total = sum(self._tree_bytes(p) for _a, p in srcs)
            # the zip is at most as big as the world; with the server on there is the temporary copy first
            self.check_space(total * (2 if server_on else 1), self.backups_dir)
            if server_on:
                tmpdir = os.path.join(self.backups_dir, ".tmp-" + ts)
                _rmtree(tmpdir)
                save_off = False
                try:
                    rcon("save-off")
                    save_off = True
                    rcon("save-all flush")
                    time.sleep(1.0)
                    done = 0
                    for arc, p in srcs:
                        done = self._copy_world(p, os.path.join(tmpdir, arc), total, done)
                finally:
                    # always, even if save-off seems to have failed (the answer may have been lost)
                    err = None
                    for attempt in range(3):
                        try:
                            rcon("save-on")
                            err = None
                            break
                        except Exception as ex:  # noqa: BLE001
                            err = ex
                            time.sleep(2)
                    if err is not None:   # also when save-off's answer was lost but the command went through
                        self._set_error(tr("WARNING: save-on failed (%s): type save-on in the Console") % err)
                srcs = [(arc, os.path.join(tmpdir, arc)) for arc, _p in srcs]
                base, span = 0.3, 0.7
            else:
                base, span = 0.0, 1.0
            self._zip_world(srcs, part, base, span)
            os.replace(part, final)
            part = None
            if tmpdir:
                _rmtree(tmpdir)
                tmpdir = None
            eid = "backups/" + os.path.basename(final)
            with self._mu:
                self._state["world"][eid] = os.path.basename(self.world_dir)
                if note:
                    self._state["note"][eid] = str(note)
                try:
                    self._save_state()
                except OSError:   # the zip is there: without the tag it counts as the current world
                    pass
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

    def _copy_world(self, src, dst, total, done=0):
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
                    _rmfile(d)   # a backup with a missing piece must not pass for a good one
                    raise OSError(tr("unreadable file: %s (%s)") % (os.path.relpath(s, src), ex))
                self._progress(0.3 * done / total)
        return done

    def _zip_world(self, srcs, part, base, span):
        """srcs: [(name in the zip, folder)]: world, and for Paper world_nether and world_the_end."""
        total = sum(self._tree_bytes(p) for _a, p in srcs)
        done = 0
        with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True,
                             strict_timestamps=False) as zf:
            for arc, src in srcs:
                done = self._zip_dir(zf, arc, src, done, total, base, span)

    def _zip_dir(self, zf, arc, src, done, total, base, span):
        for r, dirs, files in os.walk(src):
            rel = os.path.relpath(r, src)
            arc_dir = arc if rel == "." else arc + "/" + rel.replace(os.sep, "/")
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
        return done

    # ------------------------------------------------------------ delete
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

    def delete_many(self, ids):
        found = self._many(ids)
        todo = [e for e in found if not e["pinned"]]
        res = {"deleted": [], "skipped": len(found) - len(todo), "errors": []}
        if not todo:
            return res
        self._begin("delete", tr("Deleting %d backups") % len(todo))
        try:
            for i, e in enumerate(todo):
                try:
                    self._recycle(e)
                    res["deleted"].append(e["id"])
                except Exception as ex:  # noqa: BLE001
                    res["errors"].append("%s: %s" % (e["name"], ex))
                self._progress((i + 1) / len(todo))
            if res["errors"]:
                self._set_error(tr("Delete failed: %s") % res["errors"][0])
        finally:
            self._end()
        return res

    def _recycle(self, e, auto=False):
        paths = [self._abs(e)] + ([p for _s, p in _extras(self._abs(e))] if e["folder"] != "backups" else [])
        for p in paths:
            if os.name == "nt":
                try:
                    _to_recycle_bin(p, e.get("size") if p == paths[0] else None)
                except _BinRefused:
                    # automatic backups must free space even when the bin cannot take them
                    if not (auto and e["folder"] == "backups"):
                        raise
                    os.remove(p)
            elif auto and e["folder"] == "backups":
                os.remove(p)   # pruned zips would fill the trash for 7 days
            else:
                _to_trash_dir(p, os.path.join(self.server_dir, "trash"))
        with self._mu:
            self._state["pinned"].discard(e["id"])
            self._state["note"].pop(e["id"], None)
            self._state["world"].pop(e["id"], None)
            try:
                self._save_state()
            except OSError:   # the file is already gone: do not report a failure
                pass

    # ------------------------------------------------------------ retention
    def prune(self):
        if not self._op_lock.acquire(blocking=False):
            return []
        removed = []
        try:
            with self._mu:
                self._busy = {"op": "delete", "label": tr("Cleaning up old backups"),
                              "progress": 0.0, "started": time.time()}
            with self._mu:
                sch = json.loads(json.dumps(self._state["schedule"]))
            for e, _why in _prune_plan(self.list(), sch):
                try:
                    self._recycle(e, auto=e["kind"] in ("startup", "scheduled"))
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
                dt = datetime.fromtimestamp(now)
                if (cfg["every_minutes"] > 0 and server_on
                        and (players_online > 0 or not cfg["only_when_online"])
                        and now - (last or self._first_tick) >= cfg["every_minutes"] * 60
                        and _in_window(cfg["window"], dt)):
                    start = "interval"
                elif any(last_daily is None or last_daily < x for x in _slots(cfg, dt) if x <= dt.strftime(SLOT_FMT)):
                    start = "daily"
                if start:
                    th = threading.Thread(target=self._run_scheduled,
                                          args=(start, now, server_on, rcon), daemon=True)
                    self._sched_thread = th
                    th.start()
                    return  # cleanup happens when the backup is done
            if not self._op_lock.locked():
                self.prune()
        except Exception as ex:
            self._set_error(tr("Scheduling error: %s") % ex)

    def _run_scheduled(self, why, now, server_on, rcon):
        try:
            self.create("scheduled", server_on=server_on, rcon=rcon if server_on else None)
        except Exception as ex:  # noqa: BLE001
            # last_error is already saved by create; it does not count as done: retried in 15 minutes
            if not isinstance(ex, Busy):
                with self._mu:
                    self._retry_at = now + 900
            return
        with self._mu:
            self._retry_at = None
            self._state["last_scheduled"] = now
            dt = datetime.fromtimestamp(now)
            # an interval backup made after a daily time also covers the slots up to now
            passed = [x for x in _slots(self._state["schedule"], dt) if x <= dt.strftime(SLOT_FMT)]
            if passed:
                self._state["last_daily"] = max(passed)
            try:
                self._save_state()
            except OSError:
                pass
        self._notify()
        try:
            self.prune()
        except Exception as ex:
            self._set_error(tr("Cleanup error: %s") % ex)

    # ------------------------------------------------------------ restore
    def restore(self, id, server_on):
        if server_on:
            raise ValueError(tr("Stop the server first"))
        e = self._find(id)
        self._begin("restore", tr("Restoring %s") % e["name"])
        tmp = os.path.join(self.server_dir, "world.restore-tmp")
        tmps = [tmp + sfx for sfx in EXTRA]
        try:
            for p in [tmp] + tmps:
                _rmtree(p)
            src = self._abs(e)
            if e["folder"] == "backups":
                with zipfile.ZipFile(src) as zf:
                    self.check_space(sum(i.file_size for i in zf.infolist()))
                    tops = {i.filename.replace("\\", "/").split("/")[0] for i in zf.infolist()}
                self._extract_zip(src, tmp)
                for sfx in EXTRA:
                    if "world" + sfx in tops:
                        self._extract_zip(src, tmp + sfx, top="world" + sfx)
            else:
                self.check_space(sum(self._tree_bytes(p) for p in [src] + [p for _s, p in _extras(src)]))
                self._copy_folder(src, tmp)
                for sfx, p in _extras(src):
                    self._copy_folder(p, tmp + sfx)
            if not os.path.isfile(os.path.join(tmp, "level.dat")):
                raise ValueError(tr("The backup does not contain level.dat"))
            os.makedirs(self.old_dir, exist_ok=True)
            old = None
            done = []   # (from, to) of every swap, undone in reverse if one fails: all or nothing

            def move(a, b):
                os.replace(a, b)
                done.append((a, b))
            try:
                if os.path.isdir(self.world_dir):
                    old = self._unique(os.path.join(
                        self.old_dir, "world_%s_before-restore" % datetime.now().strftime(TS_FMT)))
                    move(self.world_dir, old)
                move(tmp, self.world_dir)
                # Paper's Nether and End: only those the backup contains are replaced, the old ones go next to the old world
                for sfx in EXTRA:
                    if os.path.isdir(tmp + sfx):
                        if os.path.isdir(self.world_dir + sfx):
                            os.makedirs(self.old_dir, exist_ok=True)
                            move(self.world_dir + sfx, (old or self._unique(os.path.join(self.old_dir, "world"))) + sfx)
                        move(tmp + sfx, self.world_dir + sfx)
            except Exception:
                for a, b in reversed(done):
                    try:
                        os.replace(b, a)
                    except OSError:
                        pass
                raise
            self._notify()
            return {"moved_to": os.path.basename(old) if old else ""}
        except Exception as ex:
            for p in [tmp] + tmps:
                _rmtree(p)
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

    def _extract_zip(self, zpath, dest, top=None):
        with zipfile.ZipFile(zpath) as zf:
            names = {i.filename.replace("\\", "/") for i in zf.infolist()}
            if top:
                prefix = [top]
            elif "world/level.dat" in names:
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
                parts = self._split_name(info.filename)  # validates ALL the members
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
