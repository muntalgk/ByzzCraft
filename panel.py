"""ByzzCraft: self-hosted web panel for a Minecraft server (Python standard library only, Windows and Linux).

Start:            python3 panel.py    the first time it prints a link (with a code) to set it up from another PC
Setup from shell: python3 panel.py setup FOLDER --admin NAME [--lang it] [--network] [--accept-eula] [--new-server] [--no-java]
Service:          python3 panel.py service       (systemd unit on Linux, start at login on Windows)
Java:             python3 panel.py java          (downloads the version the server jar asks for)
Users:            python panel.py adduser NAME [--admin]
                  python panel.py passwd NAME
                  python panel.py deluser NAME
                  python panel.py users
Assets:           python panel.py assets [VERSION]   (textures and translations downloaded from Mojang)
"""
import getpass, hashlib, hmac, html, ipaddress, json, logging, logging.handlers, os, queue, re, secrets, shlex, shutil, signal, socket, struct, subprocess, sys, threading, time, urllib.request
from collections import deque
from datetime import datetime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
VERSION = "1.0.0"
REPO = "muntalgk/byzzcraft"
sys.path.insert(0, HERE)
import players as PL  # noqa: E402
import mapgen  # noqa: E402
import timeline as TL  # noqa: E402
import backups as BK  # noqa: E402
import access as AC  # noqa: E402
import activity as ACT  # noqa: E402
import assets as AS  # noqa: E402
import runtime as RT  # noqa: E402
import i18n  # noqa: E402
from i18n import t  # noqa: E402
from sysinfo import NCPU, ProcSampler, listening_pid, pid_alive, system_memory, system_times  # noqa: E402
from paths import CACHE, DATA, LOGS, ASSETS as MOJANG, write_json  # noqa: E402
CONFIG_PATH = os.environ.get("PANEL_CONFIG", os.path.join(DATA, "panel-config.json"))
DEFAULTS = {
    "server_dir": os.path.dirname(HERE),
    "start_script": "",           # empty = the panel starts Java itself; otherwise a script in the server folder
    "server_jar": "",             # jar to start; empty = found by itself (Fabric, Paper, vanilla server.jar)
    "ram": "",                    # server RAM, e.g. "6G"; empty = automatic (half of the machine, 512 MB to 4 GB)
    "java_args": "",              # extra Java flags, after the standard ones
    "backup_on_start": True,      # world backup before each start (only without start_script: a script does its own)
    "auto_restart": True,         # restart the server after a crash (at most 3 times in 10 minutes)
    "listen": "127.0.0.1",
    "port": 8765,
    "auto_stop_minutes": 30,      # stop the server after N minutes with nobody on (0 = never)
    "hide_console": True,         # start from the panel without windows: log and commands are in the dashboard
    "start_timeout_seconds": 240,
    "title": "ByzzCraft",
    "lang": "en",                 # interface language: en, it
    "users_file": "panel-users.json",
    "log_file": "panel.log",
    "mc_address": "",
    "user_mc": {},                # panel user -> Minecraft name (face in the sidebar)
    "update_check": True,         # once a day ask GitHub whether there is a new release
}
SESSION_SECONDS = 30 * 86400   # 30 days from the last use: no daily login on the phone
PBKDF2_ROUNDS = 240_000
MAX_FAILS, FAIL_WINDOW, LOCK_SECONDS = 5, 600, 900
SAMPLE_SECONDS = 2
HISTORY_POINTS = 300          # 10 minutes at 2 s
LOCKED_KEYS = {"server-port", "server-ip", "enable-rcon", "rcon.port", "rcon.password", "management-server-secret",
               "management-server-tls-keystore-password"}
SECRET_KEYS = {"rcon.password", "management-server-secret", "management-server-tls-keystore-password"}


def load_config():
    cfg = dict(DEFAULTS)
    if os.path.exists(CONFIG_PATH):
        cfg.update(json.load(open(CONFIG_PATH, encoding="utf-8")))
    if os.environ.get("BYZZ_LISTEN"):   # Docker: inside the container listen on every interface
        cfg["listen"] = os.environ["BYZZ_LISTEN"]
    for k in ("users_file", "log_file"):
        if not os.path.isabs(cfg[k]):
            cfg[k] = os.path.join(os.path.dirname(CONFIG_PATH), cfg[k])
    return cfg


CFG = load_config()
i18n.load(CFG["lang"])
LOCK = threading.RLock()


# ---------------------------------------------------------------- event hub (Server-Sent Events)
class Hub:
    def __init__(self):
        self.clients = []
        self.lock = threading.Lock()

    def subscribe(self, admin):
        q = queue.Queue(maxsize=2000)
        with self.lock:
            self.clients.append((q, admin))
        return q

    def unsubscribe(self, q):
        with self.lock:
            self.clients = [c for c in self.clients if c[0] is not q]

    def publish(self, event, data, admin_only=False):
        msg = "event: %s\ndata: %s\n\n" % (event, json.dumps(data, separators=(",", ":")))
        with self.lock:
            for q, admin in list(self.clients):
                if admin_only and not admin:
                    continue
                try:
                    q.put_nowait(msg)
                except queue.Full:
                    pass


HUB = Hub()
AUDIT = deque(maxlen=60)


LOG_DIR = LOGS
L = logging.getLogger("byzzcraft")   # technical log (logs/byzzcraft.log + stderr/journalctl); log() is the "who did what" activity log


def setup_logging():
    os.makedirs(LOG_DIR, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s [%(threadName)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    handlers = [logging.handlers.RotatingFileHandler(os.path.join(LOG_DIR, "byzzcraft.log"), maxBytes=1_000_000, backupCount=5, encoding="utf-8")]
    if sys.stderr:   # pythonw has no console
        handlers.append(logging.StreamHandler())
    for h in handlers:
        h.setFormatter(fmt)
        L.addHandler(h)
    L.setLevel(logging.INFO)


def worker(fn):
    """Worker thread: if it dies, the log says so, with the traceback."""
    def run():
        try:
            fn()
        except Exception:  # noqa: BLE001
            L.exception("%s stopped", fn.__name__)
    run.__name__ = fn.__name__
    return run


JOBS = {}   # downloads running or failed: name -> "running" | error message


def job(name, fn):
    """Long background job (Java, assets download); its state shows up in the health checks."""
    if JOBS.get(name) == "running":
        return

    def run():
        JOBS[name] = "running"
        try:
            fn()
            JOBS.pop(name, None)
        except Exception as e:  # noqa: BLE001
            JOBS[name] = str(e)
            L.exception("%s failed", name)
            log("%s error: %s" % (name, e))
        HUB.publish("health", {})
    threading.Thread(target=run, name=name, daemon=True).start()


def tail(path, n=300):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 200_000))
            return f.read().decode("utf-8", "replace").splitlines()[-n:]
    except OSError:
        return []


def log(msg):
    msg = re.sub(r"[\r\n\x00-\x1f]+", " ", str(msg))   # no forged lines in the activity log
    L.info(msg)
    line = "%s  %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    with LOCK:
        with open(CFG["log_file"], "a", encoding="utf-8") as f:
            f.write(line + "\n")
        AUDIT.append(line)
    HUB.publish("audit", {"line": line, "entry": ACT.parse(line)}, admin_only=True)


_LOOP_ERR = {}


def loop_error(where, e):
    """Error of a background loop: in the technical log, once until it changes. Not in the "who did what" activity
    log, where it would repeat every few seconds (e.g. a server folder on an unmounted disk)."""
    msg = "%s: %s" % (type(e).__name__, e)
    if _LOOP_ERR.get(where) != msg:
        _LOOP_ERR[where] = msg
        L.error("%s error", where, exc_info=e)


def qint(q, key, default, lo, hi):
    try:
        return max(lo, min(hi, int(q.get(key, [default])[0] or default)))
    except (ValueError, OverflowError):
        return default


def load_audit():
    try:
        with open(CFG["log_file"], encoding="utf-8") as f:
            AUDIT.extend(l.rstrip("\n") for l in f.readlines()[-60:])
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------- users
def load_users():
    try:
        return json.load(open(CFG["users_file"], encoding="utf-8"))
    except FileNotFoundError:
        return {}


def save_users(users):
    write_json(CFG["users_file"], users, 0o600, indent=2)   # password hashes: only the panel's user reads them


def hash_pw(pw, salt=None, rounds=PBKDF2_ROUNDS):
    salt = salt or secrets.token_bytes(16)
    return salt.hex(), hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, rounds).hex(), rounds


USER_RE = re.compile(r"^[a-z0-9_.-]{1,32}$")   # same characters activity.py recognises in the log
RESERVED = {"panel", "init"}


def mc_names():
    """panel user -> Minecraft name (face in the sidebar): from the users file, then from user_mc in the config."""
    return {**(CFG.get("user_mc") or {}), **{n: u["mc"] for n, u in load_users().items() if u.get("mc")}}


def set_password(users, name, pw):
    salt, h, rounds = hash_pw(pw)
    users[name] = {**users.get(name, {}), "salt": salt, "hash": h, "rounds": rounds}


def check_pw(user, pw):
    u = load_users().get(user)
    if not u:
        hash_pw(pw)  # same response time for users that do not exist
        return None
    _, h, _ = hash_pw(pw, bytes.fromhex(u["salt"]), u["rounds"])
    return u if hmac.compare_digest(h, u["hash"]) else None


# ---------------------------------------------------------------- server.properties
def props_path():
    return os.path.join(CFG["server_dir"], "server.properties")


def read_props_text():
    """UTF-8, or Windows ANSI for a server.properties saved by an old editor (written back as UTF-8, which Minecraft reads)."""
    with open(props_path(), "rb") as f:
        raw = f.read()
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1252", "replace")


def server_props():
    props = {}
    try:
        for line in read_props_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                props[k.strip()] = v
    except FileNotFoundError:
        pass
    return props


def write_props(changes, who):
    path = props_path()
    lines = read_props_text().splitlines()
    bdir = os.path.join(DATA, "backup-properties")
    os.makedirs(bdir, exist_ok=True)
    with open(os.path.join(bdir, "server.properties.%s" % datetime.now().strftime("%Y%m%d-%H%M%S")), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    for old in sorted(os.listdir(bdir))[:-15]:
        os.remove(os.path.join(bdir, old))
    out, done, seen = [], [], set()
    for line in lines:
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            k = k.strip()
            seen.add(k)
            if k in changes and changes[k] != v:
                done.append((k, v, changes[k]))
                line = "%s=%s" % (k, changes[k])
        out.append(line)
    for k in changes:   # missing key (new or incomplete server.properties): Minecraft keeps it
        if k not in seen:
            done.append((k, "", changes[k]))
            out.append("%s=%s" % (k, changes[k]))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(out) + "\n")
    os.replace(tmp, path)
    for k, old, new in done:
        shown_old = "***" if k in SECRET_KEYS else old
        shown_new = "***" if k in SECRET_KEYS else new
        log("%s changed %s: %s -> %s" % (who, k, shown_old, shown_new))
    return done


# ---------------------------------------------------------------- minecraft: status ping + RCON
def _varint(n):
    out = b""
    while True:
        b = n & 0x7F; n >>= 7
        out += bytes([b | (0x80 if n else 0)])
        if not n:
            return out


def _read_varint(sock):
    n = shift = 0
    while True:
        b = sock.recv(1)
        if not b:
            raise ConnectionError("connection closed")
        n |= (b[0] & 0x7F) << shift
        if not b[0] & 0x80:
            return n
        shift += 7


def mc_host():
    """Where Minecraft listens: server-ip, or loopback when it is empty or a wildcard (connecting to 0.0.0.0 fails on Windows)."""
    ip = server_props().get("server-ip", "").strip()
    return "127.0.0.1" if ip in ("", "0.0.0.0", "::") else ip


def ping(port):
    """Server List Ping: None if off, otherwise (online, max, [names])."""
    try:
        with socket.create_connection((mc_host(), port), timeout=3) as s:
            host = mc_host().encode()
            hs = _varint(0) + _varint(777) + _varint(len(host)) + host + struct.pack(">H", port) + _varint(1)
            s.sendall(_varint(len(hs)) + hs + _varint(1) + _varint(0))
            _read_varint(s); _read_varint(s)
            n = _read_varint(s); data = b""
            while len(data) < n:
                chunk = s.recv(n - len(data))
                if not chunk:
                    break
                data += chunk
            st = json.loads(data.decode("utf-8", "replace"))
            pl = st.get("players", {})
            return pl.get("online", 0), pl.get("max", 0), sorted(p.get("name", "?") for p in pl.get("sample", []) or [])
    except (OSError, ValueError, ConnectionError):
        return None


class Rcon:
    """One RCON connection, reused (no 'RCON Client started' line for every command)."""

    def __init__(self):
        self.sock = None
        self.lock = threading.Lock()
        self.rid = 10

    def _close(self):
        try:
            if self.sock:
                self.sock.close()
        finally:
            self.sock = None

    def _recv_raw(self):
        def need(n):
            data = b""
            while len(data) < n:
                c = self.sock.recv(n - len(data))
                if not c:
                    raise ConnectionError(t("RCON connection closed"))
                data += c
            return data
        (ln,) = struct.unpack("<i", need(4))
        data = need(ln)
        rid, _typ = struct.unpack("<ii", data[:8])
        return rid, data[8:-2]

    def _recv(self):
        rid, body = self._recv_raw()
        return rid, body.decode("utf-8", "replace")

    def command_long(self, text):
        """Like command(), but for answers over 4096 bytes, which the server splits into several 4096-byte
        packets sent one after the other: reading goes on while a piece is full.
        (No placeholder packet: the server reads one packet at a time and would lose it.)"""
        with self.lock:
            for attempt in (1, 2):
                try:
                    if self.sock is None:
                        self._connect()
                    self.rid += 1
                    main = self.rid
                    self._send(main, 2, text)
                    parts = []
                    while True:
                        try:
                            rid, body = self._recv_raw()
                        except socket.timeout:
                            if parts:      # answer exactly a multiple of 4096 long: done
                                break
                            raise
                        finally:
                            self.sock.settimeout(5)
                        if rid == main:
                            parts.append(body)
                        if len(body) < 4096:
                            break
                        self.sock.settimeout(0.4)
                    return b"".join(parts).decode("utf-8", "replace")
                except (OSError, ConnectionError):
                    self._close()
                    if attempt == 2:
                        raise
        return ""

    def _send(self, rid, typ, body):
        payload = struct.pack("<ii", rid, typ) + body.encode("utf-8") + b"\x00\x00"
        self.sock.sendall(struct.pack("<i", len(payload)) + payload)

    def _connect(self):
        p = server_props()
        if p.get("enable-rcon") != "true" or not p.get("rcon.password"):
            raise RuntimeError(t("RCON is not enabled in server.properties"))
        self.sock = socket.create_connection((mc_host(), int(p.get("rcon.port", 25575))), timeout=5)
        self._send(1, 3, p["rcon.password"])
        rid, _ = self._recv()
        if rid == -1:
            self._close()
            raise RuntimeError(t("RCON password rejected"))

    def command(self, text, timeout=5):
        with self.lock:
            for attempt in (1, 2):
                try:
                    if self.sock is None:
                        self._connect()
                    self.rid += 1
                    self._send(self.rid, 2, text)
                    if text.strip().split(" ")[0] == "stop":
                        self._close()
                        return ""
                    self.sock.settimeout(timeout)
                    try:
                        while True:
                            rid, body = self._recv()
                            if rid == self.rid:   # leftover pieces of a long answer carry an older id
                                break
                    finally:
                        if self.sock:
                            self.sock.settimeout(5)
                    return body
                except socket.timeout:
                    self._close()   # no second send: the command may still be running
                    raise
                except (OSError, ConnectionError):
                    self._close()
                    if attempt == 2:
                        raise
        return ""

    def reset(self):
        with self.lock:
            self._close()


RCON = Rcon()


# ---------------------------------------------------------------- tail of latest.log and gc.log
class Tailer:
    def __init__(self, path, initial_lines=0):
        self.path = path; self.pos = None; self.rest = b""; self.initial = initial_lines

    def read(self):
        try:
            st = os.stat(self.path)
        except FileNotFoundError:
            if self.pos is not None:
                self.pos = 0  # file rotated: the next one is read from the start
            return []
        if self.pos is None or st.st_size < self.pos:
            first = self.pos is None and self.initial
            self.pos, self.rest = 0, b""
            if first:
                with open(self.path, "rb") as f:
                    data = f.read()
                self.pos = len(data)
                lines = data.decode("utf-8", "replace").splitlines()
                return lines[-self.initial:]
        if st.st_size == self.pos:
            return []
        with open(self.path, "rb") as f:
            f.seek(self.pos)
            data = f.read()
        self.pos += len(data)
        data = self.rest + data
        parts = data.split(b"\n")
        self.rest = parts.pop()
        return [p.decode("utf-8", "replace").rstrip("\r") for p in parts]


GC_RE = re.compile(r"^\[([^\]]+)\].*?GC\(\d+\) Pause (.+?) (\d+)M->(\d+)M\((\d+)M\) ([\d.]+)ms")


def parse_gc(line):
    m = GC_RE.match(line)
    if not m:
        return None
    try:
        t = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S.%f%z").timestamp()
    except ValueError:
        t = time.time()
    return {"t": int(t * 1000), "kind": m.group(2), "before": int(m.group(3)), "after": int(m.group(4)),
            "total": int(m.group(5)), "ms": float(m.group(6))}


# ---------------------------------------------------------------- server control
class ServerControl:
    def __init__(self):
        self.state = "off"          # off | starting | on | stopping
        self.started_at = 0
        self.proc = None
        self.empty_since = None
        self.info = None
        self.restart_pending = False
        self.proc_started = None
        self.last_pid = None        # java PID: while it is alive the world is still in use
        self.props_mtime_on = None  # server.properties mtime when the server was found on
        self.start_error = None     # why the last start failed (shown on the dashboard)
        self.preparing = False      # "starting", but still in the backup before the start
        self.stop_requested = False # wanted stop (panel, /stop, "Stopping the server" in the log): not a crash
        self.on_since = 0
        self.crashes = []           # when it crashed in the last 10 minutes

    def port(self):
        return int(server_props().get("server-port", 25565))

    def snapshot(self):
        info = self.info
        empty = int((time.time() - self.empty_since) / 60) if self.empty_since else None
        pending = False
        if self.state == "on" and self.props_mtime_on:
            try:
                # Minecraft rewrites server.properties when it starts: only changes after it is on count
                pending = os.path.getmtime(props_path()) > self.props_mtime_on + 0.5
            except OSError:
                pass
        return {"state": self.state, "online": info[0] if info else 0, "max": info[1] if info else 0,
                "players": info[2] if info else [], "since": self.proc_started, "empty_minutes": empty,
                "auto_stop_minutes": CFG["auto_stop_minutes"], "restart_pending": self.restart_pending,
                "props_changed": pending, "start_error": self.start_error,
                "backup_progress": round(100 * ((BACKUPS.status()["busy"] or {}).get("progress") or 0)) if self.preparing else None}

    def update(self, info):
        with LOCK:
            prev = self.state
            self.info = info
            if info is not None:
                if self.state != "stopping" or not self.stop_requested:   # a missed ping alone must not keep it "stopping"
                    self.state = "on"
                if info[0] > 0:
                    self.empty_since = None
                elif self.empty_since is None:
                    self.empty_since = time.time()
            else:
                self.empty_since = None
                if self.state == "starting":
                    gone = self.proc is not None and self.proc.poll() is not None
                    if gone or not self.preparing and time.time() - self.started_at > CFG["start_timeout_seconds"]:
                        self.state = "off"
                        self.start_error = start_failure(gone, self.proc.returncode if self.proc else None)
                elif self.state in ("on", "stopping"):
                    # the port closes before java finishes saving: it stays "stopping" while the process lives
                    self.state = "stopping" if pid_alive(self.last_pid) else "off"
            if self.proc is not None:
                self.proc.poll()   # reaps the finished script: no zombie processes
            if self.state == "off" and not pid_alive(self.last_pid):
                self.last_pid = None   # Windows reuses PIDs: a stale one would look "still shutting down" forever
            changed = prev != self.state
            crashed = changed and self.state == "off" and prev in ("on", "stopping") \
                      and (not self.stop_requested or bool(crash_reason(self.on_since)))   # a crash also logs "Stopping server"
            if changed and self.state == "on" and prev != "stopping":   # back from a missed ping: the same run, keep its state
                self.start_error, self.stop_requested, self.on_since = None, False, time.time()
                try:
                    self.props_mtime_on = os.path.getmtime(props_path())
                except OSError:
                    self.props_mtime_on = None
        if changed and self.state == "off":
            RCON.reset()
        if crashed:
            self.on_crash()
        if self.state == "off" and self.restart_pending:
            self.restart_pending = False
            ok, _ = self.start("panel (restart)")
            if not ok and backup_busy():
                self.restart_pending = True   # retried when the backup is done

    def start(self, who):
        jar = server_jar()
        java = RT.java_for(RT.required(CFG["server_dir"], jar))[0]   # slow (java -version, manifests): not under LOCK
        with LOCK:
            if self.state in ("starting", "on", "stopping"):
                return False, t("The server is already on or starting.")
            if pid_alive(self.last_pid) or SHUTTING_DOWN.is_set():
                return False, t("The server is still shutting down: try again in a few seconds.")
            busy = backup_busy()
            if busy or OP_PENDING.is_set():
                return False, t("Wait for it to finish: %s in progress.") % (busy or t("backup operation")).lower()
            script = CFG.get("start_script")
            if script:
                script = os.path.join(CFG["server_dir"], script)
                if not os.path.exists(script):
                    return False, t("Start script %s not found: see the Dashboard checks.") % script
            elif not jar:
                return False, t("No Minecraft server (.jar) in %s.") % CFG["server_dir"]
            self.state, self.started_at, self.start_error, self.proc = "starting", time.time(), None, None
            self.preparing = not script and CFG.get("backup_on_start", True) and os.path.isdir(world_dir())
        log("%s started the server" % who)
        threading.Thread(target=worker(lambda: self._launch(script, jar, java)), name="launch", daemon=True).start()
        HUB.publish("status", self.snapshot())
        return True, t("Starting: world backup and loading.") if self.preparing else t("Starting.")

    def _launch(self, script, jar, java):
        """World backup (with the disk space check), then Java. In a thread: the page sees the backup progress and the
        start timeout counts from the end of the backup."""
        if self.preparing:
            try:
                BACKUPS.world_dir = world_dir()
                BACKUPS.create("startup")
            except Exception as e:  # noqa: BLE001  (start anyway, as the start script did)
                log("panel: backup before start failed (%s)" % e)
                HUB.publish("notice", {"ok": False, "message": t("World backup before the start failed: %s") % e}, admin_only=True)
        with LOCK:
            self.preparing = False
            if self.state != "starting":
                return
            env = dict(os.environ, NOPAUSE="1", PYTHON=sys.executable, SERVER_JAR=jar or "server.jar")
            if java:
                env["JAVA"] = java   # the example scripts use it; a custom script may ignore it
            if script:
                cmd = ["cmd.exe", "/c", script] if os.name == "nt" else ([script] if has_shebang(script) else ["sh", script])
            else:
                cmd = launch_cmd(jar, java)
                os.makedirs(os.path.join(CFG["server_dir"], "logs"), exist_ok=True)
            os.makedirs(LOG_DIR, exist_ok=True)
            out = open(START_LOG, "wb")   # only the last start: that is what tells why it does not start
            out.write(("--- %s: %s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), " ".join(cmd))).encode())
            out.flush()
            io = dict(cwd=CFG["server_dir"], env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT)
            try:
                if os.name == "nt":
                    flags = subprocess.CREATE_NO_WINDOW if CFG.get("hide_console") else subprocess.CREATE_NEW_CONSOLE
                    self.proc = subprocess.Popen(cmd, creationflags=flags, **io)
                else:
                    # own session: the server keeps running when the panel restarts
                    self.proc = subprocess.Popen(cmd, start_new_session=True, **io)
            except OSError as e:   # Java not found
                out.write(str(e).encode())
                out.close()
                self.state = "off"
                self.start_error = start_failure(True)
                HUB.publish("status", self.snapshot())
                return
            out.close()   # the child has its own copy
            self.started_at = time.time()
        HUB.publish("status", self.snapshot())

    def on_crash(self):
        """Off without anybody asking: reason from the crash report, a notice and the automatic restart."""
        why = crash_reason(self.on_since)
        msg = t("The server crashed: %s") % why if why else t("The server stopped without being asked: see the Console.")
        log("panel: the server crashed (%s)" % (why or "no crash report"))
        console_push([msg], "err")
        HUB.publish("notice", {"ok": False, "message": msg})
        now = time.time()
        self.crashes = [c for c in self.crashes if now - c < 600] + [now]
        if CFG.get("auto_restart", True) and len(self.crashes) <= 3:
            self.start("panel (after a crash)")
        else:
            self.start_error = msg + (" " + t("Not restarted: it crashed 3 times in 10 minutes.") if CFG.get("auto_restart", True) else "")

    def kill(self, who):
        """Force stop: for a server that hangs, does not answer RCON or never finishes stopping."""
        with LOCK:
            # last_pid only if on: while starting it is the previous run's, and the PID may now belong to another process
            java = self.last_pid if self.state in ("on", "stopping") and pid_alive(self.last_pid) else None
            child = self.proc.pid if self.proc and self.proc.poll() is None else None   # java, or the script with its java
            if self.state == "off" or not (java or child):
                if self.preparing:   # starting, still in the backup: Java is not there yet
                    return False, t("Wait for it to finish: %s in progress.") % (backup_busy() or t("backup operation")).lower()
                return False, t("The server is not running.")
            self.stop_requested, self.restart_pending = True, False
            if self.state == "on":
                self.state = "stopping"
            elif self.state == "starting":
                self.state = "off"   # stopped on purpose: not a failed start
            for pid, group in ((java, False), (child, True)):
                if not pid:
                    continue
                try:
                    if os.name == "nt":
                        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=20,
                                       creationflags=subprocess.CREATE_NO_WINDOW)
                    elif group:
                        os.killpg(pid, 9)   # own session (start_new_session): script and java together
                    else:
                        os.kill(pid, 9)
                except (OSError, subprocess.SubprocessError) as e:
                    L.warning("force stop of %s: %s", pid, e)
        log("%s force-stopped the server" % who)
        HUB.publish("status", self.snapshot())
        return True, t("Server force-stopped.")

    def stop(self, who, reason=None, restart=False):
        with LOCK:
            if self.state != "on":
                return False, t("The server is not on.")
            busy = BACKUPS.status()["busy"]
            if busy and busy["op"] == "backup":   # stopping halfway through the copy would give an inconsistent backup
                return False, t("Wait a few seconds: backup in progress.")
            self.state = "stopping"
            self.restart_pending, self.stop_requested = restart, True
        try:
            RCON.command("stop")
        except Exception as e:  # noqa: BLE001
            with LOCK:
                self.state = "on"; self.restart_pending = self.stop_requested = False
            log("%s: stop failed (%s)" % (who, e))
            return False, t("Could not stop the server (%s): if RCON was turned on while it was running, type stop once in its own console.") % e
        log("%s %s the server%s" % (who, "restarted" if restart else "stopped", " (%s)" % reason if reason else ""))
        HUB.publish("status", self.snapshot())
        return True, t("Restarting.") if restart else t("Stopping.")


START_LOG = os.path.join(LOG_DIR, "server-start.log")
# Aikar's flags (https://docs.papermc.io/paper/aikars-flags): G1 tuned for Minecraft servers
AIKAR = ("-XX:+UseG1GC -XX:+ParallelRefProcEnabled -XX:MaxGCPauseMillis=200 -XX:+UnlockExperimentalVMOptions -XX:+DisableExplicitGC "
         "-XX:+AlwaysPreTouch -XX:G1NewSizePercent=30 -XX:G1MaxNewSizePercent=40 -XX:G1HeapRegionSize=8M -XX:G1ReservePercent=20 "
         "-XX:G1HeapWastePercent=5 -XX:G1MixedGCCountTarget=4 -XX:InitiatingHeapOccupancyPercent=15 -XX:G1MixedGCLiveThresholdPercent=90 "
         "-XX:G1RSetUpdatingPauseTimePercent=5 -XX:SurvivorRatio=32 -XX:+PerfDisableSharedMem -XX:MaxTenuringThreshold=1 "
         "-Dusing.aikars.flags=https://mcflags.emc.gs -Daikars.new.flags=true --enable-native-access=ALL-UNNAMED").split()
RAM_RE = re.compile(r"^(\d{3,6})M$|^(\d{1,3})G$")


def auto_ram():
    """Half of the machine's RAM, 512 MB to 4 GB, and never above 80% of what is free now: with -Xms = -Xmx and
    AlwaysPreTouch Java takes all of it at once, and if it is not there the system kills the server while starting."""
    free, total = system_memory()
    return "%dM" % max(512, min(4096, int(total / 2), int(free * 0.8)))


def has_shebang(script):
    """Executable and starting with #!: run it directly (a bash script breaks under dash); otherwise with sh."""
    try:
        with open(script, "rb") as f:
            return os.access(script, os.X_OK) and f.read(2) == b"#!"
    except OSError:
        return False


def ram_mb(ram):
    m = RAM_RE.match(ram)
    return int(m.group(1)) if m.group(1) else int(m.group(2)) * 1024


def launch_cmd(jar, java):
    """Java command line when there is no start script. gc.log feeds the heap chart."""
    ram = CFG.get("ram") or auto_ram()
    return ([java or "java", "-Xms" + ram, "-Xmx" + ram] + AIKAR + ["-Xlog:gc:file=logs/gc.log:time,uptime:filecount=3,filesize=10M"]
            + shlex.split(CFG.get("java_args") or "", posix=os.name != "nt") + ["-jar", jar, "nogui"])
START_HINTS = (   # (text in the start output, explanation for the user)
    (r"java: (command )?not found|'java' is not recognized|java.exe.*(not found|cannot find)|No such file or directory.*java|WinError 2\]",
     "Java is not installed: download it from the Dashboard checks."),
    (r"UnsupportedClassVersionError|has been compiled by a more recent version", "Java is too old for this Minecraft version: download the right one from the Dashboard checks."),
    (r"agree to the EULA", "You have not accepted the Minecraft EULA: accept it from the Dashboard checks."),
    (r"FAILED TO BIND|Address already in use|BindException", "The Minecraft port is already in use: is another server running?"),
    (r"Unable to access jarfile|Invalid or corrupt jarfile", "The server jar is missing or damaged: check server_jar in panel-config.json."),
    (r"Could not reserve enough space|Invalid maximum heap size|insufficient memory", "Not enough memory for this RAM: lower it in Settings > Panel (or in the start script)."),
)


def crash_reason(since):
    """'Description:' of the newest crash report, if it was written after the server came on."""
    d = os.path.join(CFG["server_dir"], "crash-reports")
    try:
        f = max((os.path.join(d, n) for n in os.listdir(d) if n.endswith(".txt")), key=os.path.getmtime)
        if os.path.getmtime(f) >= since:
            with open(f, encoding="utf-8", errors="replace") as fh:
                return next((l[13:].strip() for l in fh if l.startswith("Description: ")), None)
    except (OSError, ValueError):
        pass
    return None


def start_failure(exited, code=None):
    """The server did not start: a readable reason from the start output, in the log, the console and a notice."""
    lines = [x for x in tail(START_LOG, 40) if x.strip()]
    text = "\n".join(lines)
    hint = next((h for rx, h in START_HINTS if re.search(rx, text, re.I)), None)
    if not hint and code in (-9, 137):   # SIGKILL without a message: almost always the kernel, out of memory
        hint = "The system killed the server, probably out of memory: lower RAM in Settings > Panel (or in the start script)."
    msg = t(hint) if hint else (t("The server stopped while starting: see logs/server-start.log.") if exited
                                else t("The server did not answer in time: see logs/server-start.log."))
    L.error("start failed (exit code %s): %s\n%s", code, hint or ("exited" if exited else "timeout"), "\n".join(lines[-15:]))
    log("panel: start failed (%s)" % (hint or "see logs/server-start.log"))
    console_push(lines[-15:] + [msg], "err")
    HUB.publish("notice", {"ok": False, "message": msg}, admin_only=True)
    return msg


SRV = ServerControl()
METRICS = deque(maxlen=HISTORY_POINTS)
HEAP = deque(maxlen=400)
CONSOLE = deque(maxlen=500)
TICK_RE = re.compile(r"Average time per tick: ([\d.]+)ms")
P95_RE = re.compile(r"P95: ([\d.]+)ms")


def console_push(lines, kind="log"):
    items = [{"t": int(time.time() * 1000), "text": l, "kind": kind} for l in lines if l is not None]
    if not items:
        return
    CONSOLE.extend(items)
    HUB.publish("log", {"lines": items}, admin_only=True)


def sampler():
    procs = ProcSampler()
    last_tick = 0; mspt = p95 = None
    last_sys = system_times()
    auto_stop_retry = 0
    while True:
        t0 = time.time()
        try:
            port = SRV.port()
            info = ping(port)
            SRV.update(info)
            pid = listening_pid(port) if info is not None else None
            if pid:
                SRV.last_pid = pid
            ps = procs.sample(pid)
            SRV.proc_started = ps[3] if ps else None
            if info is not None and time.time() - last_tick >= 4:
                last_tick = time.time()
                try:
                    out = RCON.command("tick query")
                    m1, m2 = TICK_RE.search(out), P95_RE.search(out)
                    mspt = float(m1.group(1)) if m1 else None
                    p95 = float(m2.group(1)) if m2 else None
                except Exception:  # noqa: BLE001
                    mspt = p95 = None
            if info is None:
                mspt = p95 = None
            free, total = system_memory()
            cur_sys = system_times()
            d_idle, d_all = cur_sys[0] - last_sys[0], cur_sys[1] - last_sys[1]
            last_sys = cur_sys
            disk = shutil.disk_usage(CFG["server_dir"])
            point = {"t": int(time.time() * 1000), "cpu": round(ps[0], 2) if ps and ps[0] is not None else None,
                     "ws": round(ps[1]) if ps else None, "priv": round(ps[2]) if ps else None,
                     "mspt": mspt, "p95": p95, "online": info[0] if info else 0,
                     "sys_free": round(free), "sys_total": round(total),
                     "pc_cpu": round(100 * (1 - d_idle / d_all), 1) if d_all > 0 else None,
                     "disk_free": round(disk.free / 1048576), "disk_total": round(disk.total / 1048576)}
            METRICS.append(point)
            HUB.publish("metrics", point)
            HUB.publish("status", SRV.snapshot())
            mins = CFG["auto_stop_minutes"]
            if mins and info is not None and SRV.state == "on" and SRV.empty_since and time.time() - SRV.empty_since >= mins * 60 \
                    and time.time() >= auto_stop_retry:
                if not SRV.stop("panel", "empty for %d minutes" % mins)[0]:
                    auto_stop_retry = time.time() + 60   # RCON down: do not retry (and log) every 2 s
        except Exception as e:  # noqa: BLE001
            loop_error("sampling", e)
        time.sleep(max(0.2, SAMPLE_SECONDS - (time.time() - t0)))


TLINE = TL.Timeline(CFG["server_dir"], CACHE, CFG["log_file"])
MAP_SEM = threading.BoundedSemaphore(2)     # at most 2 tiles rendered at once: do not steal CPU from the server
MARKERS_FILE = os.path.join(DATA, "markers.json")
WORLD_BUSY = threading.Event()              # restore running: no reads of the world (map)
OP_PENDING = threading.Event()              # backup/restore asked from the web, thread not started yet: no starts
SHUTTING_DOWN = threading.Event()           # the container is stopping: no starts, not even after a "crash"
BACKUPS = BK.Backups(CFG["server_dir"], os.path.join(CACHE, "backups-state.json"),
                     on_change=lambda: HUB.publish("backups", BACKUPS.status(), admin_only=True))


WORLD = {}


def world_watcher():
    """Time of day, moon phase and weather, over RCON and only with the server on. 26.x: the hours are the
    minecraft:day timeline ("is at N ticks"), the moon the minecraft:moon timeline. Up to 1.21: "time query daytime"
    gives the ticks of the day and "time query day" the days passed ("The time is N")."""
    global WORLD
    num = lambda out: int(re.search(r"(?:is at|time is) (\d+)", out).group(1))  # noqa: E731
    while True:
        try:
            if SRV.state == "on":
                out = RCON.command("time query day")
                if "tick" in out:
                    day, moon = num(out), num(RCON.command("time query moon"))
                else:
                    day, moon = num(RCON.command("time query daytime")), num(out) * 24000
                rain = "passed" in RCON.command('execute if predicate {type:"minecraft:weather_check",raining:true}')
                thunder = rain and "passed" in RCON.command('execute if predicate {type:"minecraft:weather_check",thundering:true}')
                info = SRV.info
                # with nobody on the world is paused (pause-when-empty-seconds): time does not move
                WORLD = {"day": day % 24000, "moon": (moon // 24000) % 8, "rain": rain, "thunder": thunder,
                         "t": int(time.time() * 1000), "running": bool(info and info[0] > 0)}
                HUB.publish("world", WORLD)
            elif WORLD:
                WORLD = {}
                HUB.publish("world", WORLD)
        except Exception:  # noqa: BLE001  (RCON not ready, unexpected answer: retried next round)
            pass
        time.sleep(15)


def uuid_forwarded():
    """A BungeeCord/Velocity proxy in front: the server gets the real UUIDs even with online-mode=false."""
    def has(name, rx):
        try:
            with open(os.path.join(CFG["server_dir"], name), encoding="utf-8", errors="replace") as f:
                return re.search(rx, f.read()) is not None
        except OSError:
            return False
    velocity = r"velocity(?:-support)?:\s*\n\s+enabled:\s*true"
    return (has("spigot.yml", r"(?m)^\s*bungeecord:\s*true") or has(os.path.join("config", "paper-global.yml"), velocity)
            or has("paper.yml", velocity))


def access_action(who, action, name, reason):
    """Whitelist / op / ban / kick. Returns (ok, message)."""
    st = SRV.state
    if st in ("starting", "stopping") or (st == "off" and pid_alive(SRV.last_pid)):
        return False, t("The server is starting or stopping: try again shortly.")
    if st == "on":
        try:
            out = re.sub(r"§.", "", RCON.command(AC.command(action, name, reason))).strip()
        except Exception as e:  # noqa: BLE001
            return False, t("RCON unavailable: %s") % e
        # the server answers in English: "Added X to the whitelist", "Made X a server operator", ...
        if not re.match(r"^(Added|Removed|Made|Banned|Unbanned|Kicked)\b", out):
            for rx, it in ((r"isn't banned|not banned", "%s is not banned"), (r"not an operator", "%s is not an operator"),
                           (r"already an operator", "%s is already an operator"), (r"already whitelisted", "%s is already whitelisted"),
                           (r"not whitelisted", "%s is not whitelisted"), (r"already banned", "%s is already banned"),
                           (r"No player was found", "%s is not online"), (r"does not exist|Unknown player", "Player %s does not exist")):
                if re.search(rx, out, re.I):
                    return False, t(it) % name + "."
            return False, out or t("The server did not answer.")
        msg = t(AC.DONE[action]) % name + "."
    else:
        try:
            prof = AC.resolve(CFG["server_dir"], action, name, known={p["uuid"]: p["name"] for p in roster()},
                             offline=server_props().get("online-mode") == "false" and not uuid_forwarded())
            with LOCK:   # start() takes the same LOCK: the server cannot start while we write the files
                if SRV.state != "off" or pid_alive(SRV.last_pid):
                    return False, t("The server has just started: try again, the change now goes through the server.")
                msg = AC.apply_offline(CFG["server_dir"], action, name, reason, who, profile=prof) + "."
        except (ValueError, OSError) as e:
            return False, str(e)
    reason = re.sub(r"[\r\n\x00-\x1f()]+", " ", reason).strip()[:100]
    log("%s %s %s%s" % (who, AC.VERBS[action], name, " (%s)" % reason if reason and action in ("ban", "kick") else ""))
    HUB.publish("access", {}, admin_only=True)
    return True, msg


def backup_busy():
    b = BACKUPS.status()["busy"]
    return b["label"] if b else None


def backup_rcon(cmd):
    # save-all flush writes the whole world to disk: it can take far more than the usual 5 s
    return RCON.command(cmd, timeout=180 if cmd.startswith("save-all") else 15)


def backup_scheduler():
    """Scheduled backups and cleanup of old ones (the rules are in cache/backups-state.json)."""
    time.sleep(30)
    while True:
        st = SRV.state
        if st in ("on", "off") and not (st == "off" and pid_alive(SRV.last_pid)) and not OP_PENDING.is_set():
            info = SRV.info
            BACKUPS.world_dir = world_dir()
            BACKUPS.tick(time.time(), st == "on", info[0] if (st == "on" and info) else 0, backup_rcon)
        time.sleep(60)


def backup_job(who, what, fn, done_msg, fail_msg):
    """Long operations run in a thread: the result reaches the page as an event.
    The caller has already set OP_PENDING (under LOCK, together with the server state checks)."""
    def run():
        try:
            r = fn()
        except Exception as e:  # noqa: BLE001
            log("%s: %s failed (%s)" % (who, what, e))
            HUB.publish("backup_done", {"ok": False, "message": fail_msg % e}, admin_only=True)
        else:
            log("%s: %s completed" % (who, what))
            HUB.publish("backup_done", {"ok": True, "message": done_msg(r)}, admin_only=True)
        finally:
            OP_PENDING.clear()
    threading.Thread(target=run, daemon=True).start()


def do_restore(bid):
    WORLD_BUSY.set()
    try:
        with MAP_SEM, MAP_SEM:   # wait until no tile is reading the world
            pass
        r = BACKUPS.restore(bid, server_on=SRV.state != "off" or pid_alive(SRV.last_pid))
        shutil.rmtree(os.path.join(CACHE, "map"), ignore_errors=True)   # tiles of the old world
        return r
    finally:
        WORLD_BUSY.clear()


def load_markers():
    try:
        return json.load(open(MARKERS_FILE, encoding="utf-8"))
    except (OSError, ValueError):
        return []


def save_markers(m):
    write_json(MARKERS_FILE, m, indent=1)


def map_warmer():
    """Keeps the tile cache up to date, one at a time and slowly (so the map opens fast)."""
    time.sleep(20)
    while True:
        errors = 0
        for dim in mapgen.DIMS:
            try:
                regions = mapgen.list_regions(world_dir(), dim)
            except Exception:  # noqa: BLE001
                continue
            for rx, rz, _mt in regions:
                if WORLD_BUSY.is_set():
                    break
                try:   # an error on one tile (e.g. PNG open by a request) does not stop the others
                    with MAP_SEM:
                        if not WORLD_BUSY.is_set():
                            mapgen.get_tile(world_dir(), os.path.join(CACHE, "map"), dim, rx, rz)
                except Exception:  # noqa: BLE001
                    errors += 1
                time.sleep(0.3)
        if errors:
            L.warning("map: %d tiles not updated this round, retrying in 5 minutes", errors)
        time.sleep(300)


def publish_timeline(evs):
    for e in evs:
        HUB.publish("timeline", e, admin_only=e.get("type") == "panel")


# system line only: chat has <name> after "]: " and must not stop the server
STOPPING_RE = re.compile(r"^\[[^\]]*\](?: \[[^\]]*\])*(?:: | \([^)]*\) )Stopping (?:the )?server\s*$")   # Fabric: "] (Minecraft) Stopping"


def tailers():
    logs = Tailer(os.path.join(CFG["server_dir"], "logs", "latest.log"), initial_lines=200)
    gc = Tailer(os.path.join(CFG["server_dir"], "logs", "gc.log"), initial_lines=400)
    tl_ready = False   # the first refresh() returns the whole history: it must not be sent as "new"
    last_tl = 0
    while True:
        try:
            lines = logs.read()
            if tl_ready and any(STOPPING_RE.match(l) for l in lines):   # not the old lines read at start-up
                SRV.stop_requested = True   # /stop from the game or the console: not a crash
            console_push(lines)
            if lines or time.time() - last_tl > 5:
                last_tl = time.time()
                new = TLINE.refresh()
                if tl_ready:
                    publish_timeline(new)
                tl_ready = True
            for line in gc.read():
                ev = parse_gc(line)
                if ev:
                    HEAP.append(ev)
                    HUB.publish("heap", ev)
        except Exception as e:  # noqa: BLE001
            loop_error("log read", e)
        time.sleep(0.5)


# ---------------------------------------------------------------- players
LIVE = {}            # uuid -> {"t": epoch, "data": normalised data read over RCON}
ROSTER_SENT = None
DATA_SENT = {}
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def level_name_ok(v):
    """level-name goes into a path: no separators, no .., not absolute, not empty."""
    return bool(v.strip()) and v != "." and not v.endswith((" ", ".")) and not re.search(r"[/\\]|\.\.|^[A-Za-z]:", v) and not os.path.isabs(v)


def world_dir():
    return os.path.join(CFG["server_dir"], server_props().get("level-name", "world") or "world")


def known_players():
    """uuid -> name, from usercache.json + the player files in the world."""
    names = {}
    try:
        for e in json.load(open(os.path.join(CFG["server_dir"], "usercache.json"), encoding="utf-8")):
            if isinstance(e, dict) and UUID_RE.match(str(e.get("uuid", ""))) and e.get("name"):
                names[e["uuid"]] = e["name"]
    except (OSError, ValueError):
        pass
    pdir = PL.player_dir(world_dir(), "data")
    on_disk = {f[:-4] for f in os.listdir(pdir) if f.endswith(".dat") and UUID_RE.match(f[:-4])} if os.path.isdir(pdir) else set()
    return {u: names.get(u, u[:8]) for u in set(names) | on_disk if u in on_disk or names.get(u)}


def online_names():
    info = SRV.info
    return set(info[2]) if info else set()


def hardcore_world():
    try:
        d = PL.read_nbt_file(os.path.join(world_dir(), "level.dat"))["Data"]
        return bool((d.get("difficulty_settings") or {}).get("hardcore", d.get("hardcore", 0)))
    except (OSError, KeyError, ValueError, struct.error):
        return False


def player_data(uuid, name):
    """Live data if online (refreshed over RCON), otherwise the last save on disk."""
    live = LIVE.get(uuid)
    if name in online_names() and live and time.time() - live["t"] < 15:
        return {"live": True, "saved": live["t"], **live["data"]}
    path = os.path.join(PL.player_dir(world_dir(), "data"), uuid + ".dat")
    try:
        d = PL.normalize(PL.read_nbt_file(path))
        return {"live": False, "saved": os.path.getmtime(path), **d}
    except (OSError, ValueError, KeyError, struct.error, EOFError):
        return None


def roster():
    on = online_names()
    out = []
    for uuid, name in sorted(known_players().items(), key=lambda kv: kv[1].lower()):
        d = player_data(uuid, name)
        out.append({"uuid": uuid, "name": name, "online": name in on, "health": d["health"] if d else None,
                    "dead": d["dead"] if d else False, "saved": d["saved"] if d else None,
                    "pos": d["pos"] if d else None, "dim": d["dim"] if d else None,
                    "last_death": d.get("last_death") if d else None, "last_death_dim": d.get("last_death_dim") if d else None})
    return out


def player_watcher():
    global ROSTER_SENT
    was_online = set()
    while True:
        try:
            names = known_players()
            by_name = {v.lower(): k for k, v in names.items()}
            on = online_names() if SRV.state == "on" else set()
            for name in on:
                uuid = by_name.get(name.lower())
                if not uuid or not re.match(r"^\w{1,16}$", name):
                    continue
                try:
                    out = RCON.command_long("data get entity %s" % name)
                    if "entity data:" not in out:
                        continue
                    data = PL.normalize(PL.snbt(out.split("entity data:", 1)[1].strip()))
                    LIVE[uuid] = {"t": time.time(), "data": data}
                    payload = {"uuid": uuid, "name": name, "online": True, "live": True, "saved": time.time(), **data}
                    key = json.dumps({k: v for k, v in data.items() if k not in ("pos", "yaw")}, sort_keys=True)  # the position travels in "positions"
                    if DATA_SENT.get(uuid) != key:
                        DATA_SENT[uuid] = key
                        HUB.publish("player", payload)
                except Exception:  # noqa: BLE001 - an error on one player does not stop the others
                    pass
            for name in was_online - on:  # just left: the server saves the file, read it again
                uuid = by_name.get(name.lower())
                if uuid:
                    LIVE.pop(uuid, None); time.sleep(1.5)
                    d = player_data(uuid, name)
                    if d:
                        HUB.publish("player", {"uuid": uuid, "name": name, "online": False, **d})
            was_online = on
            pos = [{"uuid": u, "name": names.get(u, "?"), "pos": v["data"]["pos"], "dim": v["data"]["dim"], "yaw": v["data"].get("yaw", 0),
                    "health": v["data"]["health"]} for u, v in LIVE.items() if names.get(u) in on and time.time() - v["t"] < 15]
            HUB.publish("positions", {"t": int(time.time() * 1000), "players": pos})
            r = roster()
            key = json.dumps([[p["uuid"], p["online"], p["health"], p["dead"]] for p in r])
            if key != ROSTER_SENT:
                ROSTER_SENT = key
                HUB.publish("roster", r)
        except Exception as e:  # noqa: BLE001
            loop_error("players", e)
        time.sleep(3)


# ---------------------------------------------------------------- sessions / login limits
SESSIONS = {}        # sha256(token) -> session; only the hash on disk, never the token
SESS_FILE = os.path.join(CACHE, "sessions.json")


def tok_key(tok):
    return hashlib.sha256(tok.encode()).hexdigest()


SESS_LOCK = threading.Lock()


def save_sessions():
    try:
        with SESS_LOCK:
            write_json(SESS_FILE, {k: dict(v) for k, v in list(SESSIONS.items()) if v["exp"] > time.time()}, 0o600)
    except (OSError, RuntimeError):
        pass


def pw_version(u):
    """Changes with the password (new salt) or when the user is deleted and created again: read from the users file, so `panel.py passwd` counts too."""
    return (u.get("hash") or "")[:16]


def drop_sessions(user, keep=None):
    """Closes a user's sessions (password changed, user deleted), except keep."""
    for k in [k for k, v in list(SESSIONS.items()) if v["user"] == user and k != keep]:
        SESSIONS.pop(k, None)
    save_sessions()


def load_sessions():
    try:
        SESSIONS.update({k: v for k, v in json.load(open(SESS_FILE, encoding="utf-8")).items() if v["exp"] > time.time()})
    except (OSError, ValueError):
        pass
FAILS = {}
LOCKED = {}


def from_local_proxy(handler):
    """Peer is this machine: loopback, or its own LAN address (a proxy reaching us through it)."""
    ip = handler.client_address[0]
    return ip in LOOPBACK or ip == handler.connection.getsockname()[0]


def client_id(handler):
    ip = handler.client_address[0]
    if not from_local_proxy(handler):
        return ip   # proxy headers count only from a local proxy (tailscale serve, nginx): otherwise they could be forged
    # the last X-Forwarded-For hop is the one our proxy appended; no Tailscale-User-Login fallback: a client can forge it, dodging the login lock
    xff = (handler.headers.get("X-Forwarded-For") or "").split(",")[-1].strip()
    return xff or ip


LOOPBACK = ("127.0.0.1", "::1", "::ffff:127.0.0.1")
PROXY_HEADERS = ("X-Forwarded-For", "X-Real-IP", "Forwarded", "X-Forwarded-Host", "Tailscale-User-Login")


def is_local(handler):
    """Browser open on this machine. Tailscale Serve and nginx also come from 127.0.0.1, but with their headers.
    Same machine through its LAN address (the link printed at start-up): source and destination address match."""
    return from_local_proxy(handler) and not any(handler.headers.get(h) for h in PROXY_HEADERS)


def locked(keys):
    now = time.time()
    return any(LOCKED.get(k, 0) > now for k in keys)


def note_fail(keys):
    now = time.time()
    for k in keys:
        lst = [t for t in FAILS.get(k, []) if now - t < FAIL_WINDOW] + [now]
        FAILS[k] = lst
        if len(lst) >= MAX_FAILS:
            LOCKED[k] = now + LOCK_SECONDS; FAILS[k] = []


# ---------------------------------------------------------------- pages
LOGIN_CSS = """
:root{--bg:#0f1115;--card:#171a20;--line:#272b33;--text:#f0efec;--mute:#9a9ea6;--accent:#3fb950}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:radial-gradient(1200px 600px at 50% -10%,#1d2a1f 0,var(--bg) 60%);color:var(--text);font:16px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{width:100%;max-width:400px;padding:16px}.logo{width:64px;height:64px;margin-bottom:18px;filter:drop-shadow(0 8px 16px #3fb95040)}
.logo svg{width:100%;height:100%;display:block}
h1{font-size:22px;margin:0 0 4px}p{color:var(--mute);margin:0 0 20px;font-size:14px}
form{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:22px}
label{display:block;font-size:13px;color:var(--mute);margin:12px 0 6px}
select{width:100%;padding:12px 14px;border-radius:10px;border:1px solid var(--line);background:#0c0e12;color:var(--text);font-size:16px}
label.chk{display:flex;gap:8px;align-items:center;margin-top:16px}label.chk input{width:auto}a{color:var(--accent)}
input{width:100%;padding:12px 14px;border-radius:10px;border:1px solid var(--line);background:#0c0e12;color:var(--text);font-size:16px}
input:focus{outline:2px solid #3fb95055;border-color:var(--accent)}
button{width:100%;padding:13px;border:0;border-radius:10px;font-size:16px;font-weight:650;cursor:pointer;margin-top:18px;background:var(--accent);color:#06210c}
.err{color:#ff8a80;font-size:14px;margin:12px 0 0}
"""


with open(os.path.join(HERE, "assets", "logo.svg"), encoding="utf-8") as _f:
    LOGO_SVG = _f.read().strip()
# PNG, not SVG: Safari (also on the iPhone Home screen) does not use SVG icons
# manifest and meta: from the phone's Home screen it opens like an app, without the browser bar
ICONS = {"/favicon.ico": "icon-32.png", "/apple-touch-icon.png": "icon-180.png"}
# ?v= changes with the files: the icons are cached for a day, a new logo must not wait for it
_v = hashlib.sha256()
for _f in ICONS.values():
    with open(os.path.join(HERE, "assets", _f), "rb") as _fh:
        _v.update(_fh.read())
ICON_V = _v.hexdigest()[:8]
LOGO_ICON = ("<link rel=icon type=image/png href=/favicon.ico?v=%s><link rel=apple-touch-icon href=/apple-touch-icon.png?v=%s>"
             "<link rel=manifest href=/manifest.webmanifest><meta name=theme-color content='#0d0f13'>"
             "<meta name=apple-mobile-web-app-capable content=yes><meta name=mobile-web-app-capable content=yes>") % (ICON_V, ICON_V)


def login_page(error=""):
    err = '<p class="err">%s</p>' % html.escape(error) if error else ""
    body = """<main><div class=logo>%s</div><h1>%s</h1><p>%s</p>
<form method=post action=/login autocomplete=on>
<label for=u>%s</label><input id=u name=user autocomplete=username required autofocus>
<label for=p>%s</label><input id=p name=password type=password autocomplete=current-password required>
<button type=submit>%s</button>%s</form></main>""" % (LOGO_SVG, html.escape(CFG["title"]), html.escape(t("Log in to manage the server.")),
                                                    html.escape(t("User")), html.escape(t("Password")), html.escape(t("Log in")), err)
    return _page(body)


def _page(body):
    return ("<!doctype html><html lang=" + i18n.LANG + "><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            "<title>%s</title>%s<style>%s</style></head><body>%s</body></html>" % (html.escape(CFG["title"]), LOGO_ICON, LOGIN_CSS, body)).encode()


def guess_server_dir():
    for d in (CFG["server_dir"], os.path.dirname(HERE), os.getcwd()):
        if AS.find_jar(d) or os.path.isfile(os.path.join(d, "server.properties")):
            return d
    return ""


SETUP_TOKEN_FILE = os.path.join(CACHE, "setup-token")


def setup_token():
    """Code to set up from another PC: the same across restarts, deleted once the setup is done."""
    try:
        with open(SETUP_TOKEN_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        os.makedirs(os.path.dirname(SETUP_TOKEN_FILE), exist_ok=True)
        tok = secrets.token_urlsafe(12)
        with os.fdopen(os.open(SETUP_TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
            f.write(tok)
        return tok


def host_is_literal(handler):
    """Host header is an IP or localhost: a DNS-rebinding page cannot reach the code-less setup with its own name."""
    h = (handler.headers.get("Host") or "").strip()
    h = h[1:].split("]")[0] if h.startswith("[") else h.split(":")[0] if h.count(":") == 1 else h
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        return h.lower() == "localhost"


def same_origin(handler):
    """No Origin (plain navigation, curl) or the same host as the page: blocks cross-site POSTs."""
    o = handler.headers.get("Origin")
    return not o or o.split("://", 1)[-1] == handler.headers.get("Host")


def setup_allowed(handler, token):
    """Setup from the browser of this machine, or remotely with the code printed at start-up."""
    if is_local(handler) and host_is_literal(handler):
        return True
    keys = ["c:" + client_id(handler)]
    if not token or locked(keys):
        return False
    if hmac.compare_digest(token, setup_token()):
        return True
    note_fail(keys)
    return False


def lan_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as so:
            so.connect(("8.8.8.8", 80))   # UDP, nothing is sent: only to learn which interface goes out
            return so.getsockname()[0]
    except OSError:
        return "SERVER-IP"


def setup_page(error="", form=None, token="", remote=False):
    f = form or {}
    e = html.escape
    err = '<p class="err">%s</p>' % e(error) if error else ""
    lang = f.get("lang", i18n.LANG)
    opts = "".join("<option value=%s%s>%s</option>" % (k, " selected" if k == lang else "", LANG_NAMES.get(k, k)) for k in languages())
    sd = f.get("server_dir", guess_server_dir() or os.environ.get("BYZZ_SERVER_DIR") or os.path.join(os.path.expanduser("~"), "minecraft"))
    network = f.get("network", "1" if remote else "") == "1"
    has_jar = bool(sd) and bool(AS.find_jar(sd))
    chk = lambda on: " checked" if on else ""
    body = """<main><div class=logo>%s</div><h1>%s</h1><p>%s</p>
<form method=post action=/setup><input type=hidden name=token value="%s">
<label for=d>%s</label><input id=d name=server_dir value="%s" required>
<label class=chk><input type=checkbox name=new_server value=1%s> %s</label>
<label for=l>%s</label><select id=l name=lang>%s</select>
<label for=u>%s</label><input id=u name=user autocomplete=username required pattern="[a-z0-9_.\\-]{1,32}" value="%s">
<label for=p>%s</label><input id=p name=password type=password autocomplete=new-password required minlength=10>
<label for=p2>%s</label><input id=p2 name=password2 type=password autocomplete=new-password required minlength=10>
<label>%s</label>
<label class=chk><input type=radio name=network value=""%s> %s</label>
<label class=chk><input type=radio name=network value=1%s> %s</label>
<label class=chk><input type=checkbox name=java value=1%s> %s</label>
<label class=chk><input type=checkbox name=eula value=1> %s <a href="https://aka.ms/MinecraftEULA" target=_blank rel=noopener>EULA</a></label>
<button type=submit>%s</button><p class=note>%s</p>%s</form></main>""" % (
        LOGO_SVG, e(t("Welcome to ByzzCraft")), e(t("First setup of the panel.")), e(token),
        e(t("Minecraft server folder")), e(sd), chk(f.get("new_server") == "1" or not has_jar and not os.path.exists(os.path.join(sd, "server.properties"))),
        e(t("No server yet: download the latest Minecraft server into this folder")),
        e(t("Language")), opts, e(t("Admin user")), e(f.get("user", "")), e(t("Password (at least 10 characters)")), e(t("Repeat the password")),
        e(t("Who can open the panel")), chk(not network), e(t("Only this machine (then Tailscale, nginx or an SSH tunnel)"))
        + (" " + e(t("- from this device the panel will not open any more after the setup")) if remote else ""),
        chk(network), e(t("My home network: http://%s:%d (no HTTPS: fine at home, not on the internet)") % (lan_ip(), CFG["port"])),
        chk(f.get("java", "1") == "1"), e(t("Download Java automatically if it is missing")),
        e(t("I accept the Minecraft")), e(t("Set up the panel")), e(t("Downloads can take a minute.")), err)
    return _page(body)


def setup_remote_page():
    return _page("<main><div class=logo>%s</div><h1>%s</h1><p>%s</p><p><code>%s</code></p></main>" % (
        LOGO_SVG, html.escape(t("Panel not set up yet")),
        html.escape(t("Open the link with the setup code that the panel printed when it started, or open http://127.0.0.1:%d on the server. To see it again:") % CFG["port"]),
        html.escape("grep setup= %s" % os.path.join(LOG_DIR, "byzzcraft.log"))))


def save_config(changes):
    """Updates panel-config.json (keys it does not touch stay) and the config in memory."""
    cfg = {}
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            cfg = json.load(fh)
    cfg.update(changes)
    write_json(CONFIG_PATH, cfg, indent=2, ensure_ascii=False)
    CFG.update(changes)
    return cfg


def panel_settings():
    need = RT.required(CFG["server_dir"], server_jar())
    java = RT.java_for(need)[0]
    return {**{k: CFG.get(k, DEFAULTS.get(k)) for k in PANEL_KEYS}, "ram_auto": auto_ram(), "ram_total": round(system_memory()[1]),
            "jar": server_jar(), "java": java, "java_need": need, "start_script": CFG.get("start_script") or "",
            "java_bundled": bool(java and java.startswith(RT.RUNTIME)), "langs": [[c, LANG_NAMES.get(c, c)] for c in languages()]}


PANEL_KEYS = ("ram", "java_args", "backup_on_start", "auto_stop_minutes", "mc_address", "auto_restart", "lang")
LANG_NAMES = {"en": "English", "it": "Italiano"}


def languages():
    """English plus one language per i18n/<code>.json."""
    return ["en"] + sorted(f[:-5] for f in os.listdir(os.path.join(HERE, "i18n")) if f.endswith(".json"))


# allowlist: -XX:OnError, -javaagent, @argfile, -cp... would run commands or code on the host
JAVA_ARG_RE = re.compile(r"^(?:-XX:(?![+-]?(?:OnError|OnOutOfMemoryError|Flags|VMOptionsFile)\b)\S+|-D(?!com\.sun\.management|log4j|java\.security)[\w.-]+=\S*|-Xss\S+|-Xmn\S+|-Xlog:\S*"
                         r"|--enable-native-access=\S+|--add-(?:opens|exports|modules)=\S+)$")
JAVA_ARGS_MSG = "Java flags: one line, only -XX:, -D, -Xss, -Xmn, -Xlog:, --add-opens, --add-exports, --add-modules and --enable-native-access (RAM has its own field)."


def save_panel_settings(data, who):
    """Panel settings from the Settings page. ValueError with the message to show."""
    ram = str(data.get("ram", "")).strip().upper()
    if ram and not RAM_RE.match(ram):
        raise ValueError(t("RAM: write it like 4G or 3072M, or leave it empty for automatic."))
    if ram and ram_mb(ram) > system_memory()[1] - 256:
        raise ValueError(t("RAM: this machine has %s MB in all, leave some to the system.") % round(system_memory()[1]))
    args = str(data.get("java_args", "")).strip()
    if len(args) > 500 or "\n" in args or "\r" in args:
        raise ValueError(t(JAVA_ARGS_MSG))
    try:
        toks = shlex.split(args)   # on Windows too: here it only has to find unclosed quotes
    except ValueError:
        raise ValueError(t("Java flags: a quote is not closed."))
    if args != (CFG.get("java_args") or "").strip() and not all(JAVA_ARG_RE.match(x) for x in toks):   # flags already in the config file: do not block saving the RAM
        raise ValueError(t(JAVA_ARGS_MSG))
    try:
        mins = int(data.get("auto_stop_minutes", 0))
    except (TypeError, ValueError):
        mins = -1
    if not 0 <= mins <= 1440:
        raise ValueError(t("Auto stop: minutes from 0 (never) to 1440."))
    addr = str(data.get("mc_address", "")).strip()
    if len(addr) > 100 or re.search(r"[\s<>\"']", addr):
        raise ValueError(t("Invalid server address."))
    lang = str(data.get("lang") or CFG["lang"])
    if lang not in languages():
        raise ValueError(t("Invalid request."))
    if lang != CFG["lang"] and (SRV.state in ("starting", "stopping") or backup_busy() or OP_PENDING.is_set()):
        raise ValueError(t("Change the language when the server is not starting or stopping and no backup is running."))
    new = {"ram": ram, "java_args": args, "backup_on_start": bool(data.get("backup_on_start")), "auto_stop_minutes": mins, "mc_address": addr,
           "auto_restart": bool(data.get("auto_restart")), "lang": lang}
    changed = [k for k in PANEL_KEYS if CFG.get(k, DEFAULTS.get(k)) != new[k]]
    if changed:
        save_config(new)
        log("%s changed the panel settings: %s" % (who, ", ".join(changed)))
    return changed


def do_setup(f):
    """First setup, from the web page or `panel.py setup`. Returns the error to show, or None."""
    sd = os.path.abspath(os.path.expanduser(f.get("server_dir", "").strip().strip('"')))
    name, pw = f.get("user", "").strip().lower(), f.get("password", "")
    if not USER_RE.match(name) or name in RESERVED:
        return t("Invalid user name: lowercase letters, digits, . _ -")
    if len(pw) < 10:
        return t("Password too short: at least 10 characters.")
    if not AS.find_jar(sd):
        if f.get("new_server") != "1":
            return t("No Minecraft server (.jar) in %s.") % sd
        if os.path.isdir(sd) and any(os.path.isfile(os.path.join(sd, d, "level.dat")) for d in os.listdir(sd)):
            return t("%s already has a world but no server jar the panel recognises: set server_jar in panel-config.json.") % sd
        try:
            AS.download_server(sd, log=L.info)
        except Exception as e:  # noqa: BLE001
            L.exception("server download failed")
            return t("Could not download the Minecraft server: %s") % e
    cfg = save_config({"server_dir": sd, "port": CFG["port"], "lang": f.get("lang") if f.get("lang") in languages() else "en",
                       "listen": "0.0.0.0" if f.get("network") == "1" else "127.0.0.1", "java_auto": f.get("java") == "1"})
    i18n.load(cfg["lang"])   # this process still serves the first page, before the restart
    prepare_server(sd, f.get("eula") == "1")
    users = {}
    set_password(users, name, pw)
    users[name]["admin"] = True
    save_users(users)
    try:
        os.remove(SETUP_TOKEN_FILE)
    except OSError:
        pass
    log("%s completed the first setup" % name)
    L.info("setup done: server %s, listen %s, language %s", sd, cfg["listen"], cfg["lang"])
    return None


def port_free(port):
    """Nobody answers on 127.0.0.1 and the port can be taken on every interface. A bind alone is not enough on
    Windows: a program listening on 127.0.0.1 without SO_EXCLUSIVEADDRUSE does not block a bind on 0.0.0.0."""
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
        return False
    except OSError:
        pass
    with socket.socket() as so:
        try:
            so.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


def free_port(start):
    return next((pt for pt in range(start, start + 100) if port_free(pt)), start)


def prepare_server(sd, accept_eula):
    """Server folder ready for the panel: RCON on, EULA if accepted, example start script if configured."""
    props = os.path.join(sd, "server.properties")
    if not os.path.exists(props):
        open(props, "w").close()   # server never started: Minecraft fills in the file at its first start
    p = server_props()
    ch = {}
    if "server-port" not in p and not port_free(25565):   # new server on a machine that already runs one
        ch["server-port"] = str(free_port(25566))
    if p.get("enable-rcon") != "true" or not p.get("rcon.password"):
        ch.update({"enable-rcon": "true", "rcon.port": p.get("rcon.port") or str(free_port(25575)),
                   "rcon.password": p.get("rcon.password") or secrets.token_urlsafe(24)})
    if ch:
        write_props(ch, "setup")
    eula = os.path.join(sd, "eula.txt")
    if accept_eula and "eula=true" not in (open(eula, encoding="utf-8").read() if os.path.exists(eula) else ""):
        with open(eula, "w", encoding="utf-8") as f:
            f.write("# accepted in the ByzzCraft setup (https://aka.ms/MinecraftEULA)\neula=true\n")
    if CFG.get("start_script"):   # the panel starts Java itself: the script is needed only if configured
        script = os.path.join(sd, CFG["start_script"])
        example = os.path.join(HERE, "examples", CFG["start_script"])
        if not os.path.exists(script) and os.path.exists(example):
            shutil.copy(example, script)


def restart_soon():
    """After the first setup, start again with the new config (server folder, language, worker threads)."""
    time.sleep(1)
    os.environ["BYZZ_RESTART"] = "1"
    if os.name == "nt":
        subprocess.Popen([sys.executable] + sys.argv, cwd=os.getcwd())   # the new process retries the port until this one exits
        os._exit(0)
    os.execv(sys.executable, [sys.executable] + sys.argv)   # same PID: systemd does not notice


def download_assets():
    def run():
        AS.install(CFG["server_dir"], log=L.info, jar=server_jar())
        log("panel downloaded the Minecraft assets")
        shutil.rmtree(os.path.join(CACHE, "map"), ignore_errors=True)
        while SRV.state in ("starting", "stopping") or backup_busy() or OP_PENDING.is_set() or JOBS.get("java") == "running":
            time.sleep(5)   # a restart now would lose a start, a backup or a restore in progress
        restart_soon()   # colours, item names and death phrases are read once per process: the stale ones must go
    job("assets", run)


def server_jar():
    return AS.find_jar(CFG["server_dir"], CFG.get("server_jar"))


def download_java():
    need = RT.required(CFG["server_dir"], server_jar())

    def run():
        RT.install(need, log=L.info)
        log("panel downloaded Java %d" % need)
    if need:
        job("java", run)


STARTUP_CMD = os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs", "Startup", "ByzzCraft.cmd")
SERVICE_HINT = ('"%s" "%s" service' % (sys.executable, os.path.join(HERE, "panel.py")) if os.name == "nt" else
                "cd %s && %s panel.py service | sudo tee /etc/systemd/system/byzzcraft.service"
                " && sudo systemctl daemon-reload && sudo systemctl enable --now byzzcraft" % (shlex.quote(HERE), sys.executable))


def service():
    """Linux: prints the systemd unit (to pipe into sudo tee). Windows: start at login, no admin needed."""
    panel = os.path.join(HERE, "panel.py")
    if os.name == "nt":
        pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        with open(STARTUP_CMD, "w", encoding="utf-8") as f:
            f.write('@start "" "%s" "%s"\r\n' % (pyw if os.path.exists(pyw) else sys.executable, panel))
        print("ByzzCraft will start at every login: %s" % STARTUP_CMD)
        return
    if os.geteuid() == 0:
        sys.exit("run `panel.py service` as the user that will run the panel, not as root (the unit runs as that user)")
    import pwd
    sys.stdout.write("""[Unit]
Description=ByzzCraft Minecraft panel
After=network-online.target
Wants=network-online.target

[Service]
User=%s
WorkingDirectory=%s
ExecStart="%s" "%s"
Restart=on-failure
# Minecraft runs in its own session: it keeps running when the panel restarts
KillMode=process

[Install]
WantedBy=multi-user.target
""" % (pwd.getpwuid(os.getuid()).pw_name, HERE, sys.executable, panel))
    print("# install it with:\n#   " + SERVICE_HINT, file=sys.stderr)


def health():
    """What is missing to start the server, with the fix (a `fix` button or a command)."""
    sd = CFG["server_dir"]
    if not os.path.isdir(sd):   # unmounted disk, moved folder: the other checks would make no sense
        return [{"id": "server_dir", "text": t("Server folder %s not found: is the disk mounted? Otherwise change server_dir in panel-config.json.") % sd,
                 "fix": None, "cmd": None}]
    p = server_props()
    jar = server_jar()
    need = RT.required(sd, jar)
    java, found = RT.java_for(need)
    eula_path = os.path.join(sd, "eula.txt")
    eula = os.path.exists(eula_path) and "eula=true" in open(eula_path, encoding="utf-8", errors="replace").read()
    script = CFG.get("start_script") and os.path.join(sd, CFG["start_script"])
    checks = [
        ("server", bool(jar), t("No Minecraft server (.jar) in %s.") % sd, None),
        # a custom script picks its own Java (e.g. the launcher's): checked only for the built-in start and the examples
        ("java", bool(java) or not need or CFG.get("start_script") not in ("", "start.sh", "start.bat"),
         t("Java %s is not installed (found: %s).") % (need, found or t("none")), "java"),
        ("root", os.name == "nt" or os.geteuid() != 0, t("The panel runs as root: the server files will belong to root. Run it as a normal user."), None),
        ("eula", eula, t("The Minecraft EULA is not accepted yet: the server will not start."), "eula"),
        ("rcon", p.get("enable-rcon") == "true" and bool(p.get("rcon.password")), t("RCON is off: the panel cannot talk to the server."), "rcon"),
        ("script", not script or os.path.exists(script), t("Start script %s not found.") % script,
         "script" if script and os.path.exists(os.path.join(HERE, "examples", CFG["start_script"])) else None),
        ("assets", AS.present(), t("Map textures and item icons are not downloaded yet."), "assets"),
        # systemd sets INVOCATION_ID; on Windows pythonw (no console) means started at login, not from a terminal
        ("service", bool(os.environ.get("INVOCATION_ID")) or os.path.exists("/.dockerenv") or os.name == "nt" and (os.path.exists(STARTUP_CMD) or not sys.stdout),
         t("The panel does not start by itself after a reboot."), None),
    ]
    sizes = [e["size"] for e in BACKUPS.list() if e.get("folder") == "backups"]
    free = shutil.disk_usage(sd).free
    if sizes and free < max(1073741824, 3 * max(sizes)):   # less than three backups as big as the biggest one
        checks.append(("disk", False, t("Little disk space left: %s free, the biggest backup is %s. Delete old backups or lower how many to keep.")
                       % (BK.mb(free), BK.mb(max(sizes))), None))
    if UPDATE.get("newer"):
        checks.append(("update", False, t("ByzzCraft %s is available (you have %s).") % (UPDATE["latest"], VERSION), None))
    out = []
    for cid, ok, text, fix in checks:
        if cid in JOBS:
            what = {"java": "Java", "assets": t("textures and names from Mojang")}.get(cid, cid)
            ok, text = False, (t("Downloading %s...") % what if JOBS[cid] == "running" else t("Download of %s failed: %s") % (what, JOBS[cid]))
        if not ok:
            out.append({"id": cid, "text": text, "fix": None if JOBS.get(cid) == "running" else fix,
                        "cmd": SERVICE_HINT if cid == "service" else UPDATE_HINT if cid == "update" else None})
    return out


UPDATE = {}   # latest release on GitHub: {"latest": "1.1.0", "newer": True}
UPDATE_HINT = ("git pull && docker compose build --pull && docker compose up -d   (in the folder with docker-compose.yml)" if os.path.exists("/.dockerenv") else
               "https://github.com/%s/releases/latest" % REPO if not os.path.isdir(os.path.join(HERE, ".git")) else
               'git -C "%s" pull, then restart the panel' % HERE if os.name == "nt" else
               "cd %s && git pull && " % shlex.quote(HERE) + ("sudo systemctl restart byzzcraft" if os.environ.get("INVOCATION_ID") else "restart the panel"))


def _vtuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


def update_checker():
    """Once a day: is there a newer release on GitHub? One GET request, no data sent."""
    time.sleep(60)
    while True:
        if CFG.get("update_check", True):
            try:
                req = urllib.request.Request("https://api.github.com/repos/%s/releases/latest" % REPO,
                                             headers={"User-Agent": "byzzcraft/" + VERSION, "Accept": "application/vnd.github+json"})
                with urllib.request.urlopen(req, timeout=15) as r:
                    tag = json.loads(r.read())["tag_name"].lstrip("v")
                UPDATE.update(latest=tag, newer=_vtuple(tag) > _vtuple(VERSION))
                if UPDATE["newer"]:
                    L.info("ByzzCraft %s is available (running %s)", tag, VERSION)
                    HUB.publish("health", {})
            except Exception as e:  # noqa: BLE001  (offline, no release yet: retried tomorrow)
                L.info("update check: %s", e)
        time.sleep(86400)


def fix_health(cid, who):
    sd = CFG["server_dir"]
    if cid == "java":
        download_java()
    elif cid == "assets":
        download_assets()
    elif cid == "eula":
        with open(os.path.join(sd, "eula.txt"), "w", encoding="utf-8") as f:
            f.write("# accepted in ByzzCraft by %s (https://aka.ms/MinecraftEULA)\neula=true\n" % who)
        log("%s accepted the Minecraft EULA" % who)
    elif cid in ("rcon", "script"):
        prepare_server(sd, False)
    else:
        return False
    return True


def app_page(sess):
    with open(os.path.join(HERE, "app.html"), encoding="utf-8") as f:
        tpl = f.read()
    boot = {"user": sess["user"], "admin": sess["admin"], "csrf": sess["csrf"], "title": CFG["title"],
            "address": CFG.get("mc_address") or "", "autostop": CFG["auto_stop_minutes"], "cores": NCPU,
            "mc": mc_names().get(sess["user"], ""), "lang": i18n.LANG, "i18n": i18n.TABLE,
            "version": VERSION, "latest": UPDATE.get("latest") if UPDATE.get("newer") else None, "repo": REPO}
    tpl = tpl.replace("{{TITLE}}", html.escape(CFG["title"])).replace("{{LANG}}", i18n.LANG)
    tpl = tpl.replace("{{LOGO}}", LOGO_SVG).replace("{{ICON}}", LOGO_ICON)
    # {{text}} in the static HTML: translated here; JavaScript uses _t("text")
    tpl = re.sub(r"\{\{([^{}]+)\}\}", lambda m: m.group(0) if m.group(1) == "BOOT" else html.escape(t(m.group(1))), tpl)
    return tpl.replace("{{BOOT}}", json.dumps(boot).replace("</", "<\\/")).encode()


class Handler(BaseHTTPRequestHandler):
    server_version = "byzzcraft"
    timeout = 120   # a stuck connection does not hold a thread forever (the event stream pings every 15 s)
    sys_version = ""

    def log_message(self, fmt, *args):
        pass

    def send(self, code, body, ctype="text/html; charset=utf-8", headers=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        headers = dict(headers or {})
        self.send_header("Cache-Control", headers.pop("Cache-Control", "no-store"))
        self.security_headers()
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def security_headers(self):
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                         "connect-src 'self'; img-src 'self' data:; manifest-src 'self'; form-action 'self'; frame-ancestors 'none'")

    def send_file(self, path, name):
        try:
            f = open(path, "rb")   # before the headers: a zip pruned meanwhile is a clean 404
        except OSError:
            return self.json(404, {"error": "not found"})
        with f:
            self.send_file_open(f, name)

    def send_file_open(self, f, name):
        size = os.fstat(f.fileno()).st_size
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", 'attachment; filename="%s"' % name)
        self.send_header("Cache-Control", "no-store")
        self.security_headers()
        self.end_headers()
        try:
            shutil.copyfileobj(f, self.wfile, 1 << 20)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def json(self, code, obj):
        self.send(code, json.dumps(obj).encode(), "application/json")

    def redirect(self, to, cookie=None):
        h = {"Location": to}
        if cookie:
            h["Set-Cookie"] = cookie
        self.send(303, b"", headers=h)

    def session(self):
        c = SimpleCookie(self.headers.get("Cookie", ""))
        tok = c["sess"].value if "sess" in c else None
        key = tok_key(tok) if tok else None
        s = SESSIONS.get(key) if key else None
        if s and s["exp"] > time.time():
            u = load_users().get(s["user"])
            if u and s.setdefault("pv", pw_version(u)) == pw_version(u):   # user still exists, password unchanged: also refresh the role
                s["admin"] = bool(u.get("admin"))
                if s["exp"] < time.time() + SESSION_SECONDS - 86400:
                    s["exp"] = time.time() + SESSION_SECONDS
                    save_sessions()
                return key, s
            if u:   # password changed: refuse, but do not drop it (a change from this very session updates pv a moment later)
                return None, None
        if key and SESSIONS.pop(key, None):
            save_sessions()
        return None, None

    def body(self):
        try:
            n = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            n = 0
        n = max(0, min(n, 65536))   # never negative: read(-1) would read until the connection closes
        return self.rfile.read(n).decode("utf-8", "replace") if n else ""

    def form(self):   # an HTML form post: one value per field
        return {k: v[0] for k, v in parse_qs(self.body()).items()}

    def do_GET(self):
        self.safe(self._do_GET)

    def do_POST(self):
        self.safe(self._do_POST)

    def safe(self, fn):
        """A malformed request gets a 400 instead of a connection closed without an answer."""
        try:
            fn()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as e:  # noqa: BLE001
            try:
                self.json(400, {"ok": False, "message": t("Invalid request.")})
            except OSError:
                pass
            log("invalid request %s: %s" % (self.path[:80], type(e).__name__))
            L.exception("request %s %s failed", self.command, self.path[:200])

    def cookie(self, value, max_age):
        # Secure only if the browser speaks HTTPS to a local proxy: on http://home-IP the cookie would be dropped
        h = self.headers
        https = from_local_proxy(self) and (h.get("X-Forwarded-Proto", "").lower() == "https" or bool(h.get("Tailscale-User-Login")))
        secure = "; Secure" if https else ""
        return "sess=%s; Path=/; HttpOnly; SameSite=Strict; Max-Age=%d%s" % (value, max_age, secure)

    # ------------------------------------------------ GET
    def _do_GET(self):
        path, _, qs = self.path.partition("?")
        q = parse_qs(qs)
        if path in ICONS:
            with open(os.path.join(HERE, "assets", ICONS[path]), "rb") as f:
                return self.send(200, f.read(), "image/png", {"Cache-Control": "public, max-age=86400"})
        if path == "/" and not load_users():
            token = q.get("setup", [""])[0]
            if not setup_allowed(self, token):
                return self.send(200, setup_remote_page())
            return self.send(200, setup_page(token=token, remote=not is_local(self)))
        if path == "/":
            _, s = self.session()
            if not s:
                return self.send(200, login_page())
            tok = SimpleCookie(self.headers.get("Cookie", ""))["sess"].value   # the cookie starts again from 30 days too
            return self.send(200, app_page(s), headers={"Set-Cookie": self.cookie(tok, SESSION_SECONDS)})
        if path == "/manifest.webmanifest":
            return self.send(200, json.dumps({"name": CFG["title"], "short_name": CFG["title"], "start_url": "/", "display": "standalone",
                                              "background_color": "#0d0f13", "theme_color": "#0d0f13",
                                              "icons": [{"src": "/apple-touch-icon.png?v=" + ICON_V, "sizes": "180x180", "type": "image/png"}]}).encode(),
                             "application/manifest+json")
        _, s = self.session()
        if not s:
            return self.json(401, {"error": "login"})
        if path == "/api/events":
            return self.events(_, s)
        img_cache = {"Cache-Control": "private, max-age=86400"}
        if path == "/api/players":
            return self.json(200, {"players": roster(), "hardcore": hardcore_world()})
        if path == "/api/player":
            uuid = q.get("uuid", [""])[0]
            names = known_players()
            if not UUID_RE.match(uuid) or uuid not in names:
                return self.json(404, {"error": "unknown player"})
            d = player_data(uuid, names[uuid]) or {}
            return self.json(200, {"uuid": uuid, "name": names[uuid], "online": names[uuid] in online_names(),
                                   "slim": PL.skin(uuid)[1], "hardcore": hardcore_world(), "stats": PL.stats(world_dir(), uuid), **d})
        m = re.match(r"^/skin/([0-9a-f-]{36})\.png$", path)
        if m and UUID_RE.match(m.group(1)):
            if m.group(1) not in known_players():   # else any logged-in user could make us query Mojang for any uuid
                return self.send(404, b"", "image/png")
            png, slim = PL.skin(m.group(1))
            if not png:   # no skin from Mojang and no default one in the assets
                return self.send(404, b"", "image/png")
            return self.send(200, png, "image/png", {"Cache-Control": "private, max-age=3600", "X-Skin-Slim": "1" if slim else "0"})
        m = re.match(r"^/icon/([a-z0-9_]{1,64})\.png$", path)
        if m:
            p = PL.icon_path(m.group(1))
            return self.send(200, AS.icon_png(m.group(1), p), "image/png", img_cache) if p else self.send(404, b"", "image/png")
        m = re.match(r"^/hud/([a-z0-9_]{1,64})\.png$", path)
        if m:
            p = os.path.join(MOJANG, "hud", m.group(1) + ".png")
            return self.send(200, open(p, "rb").read(), "image/png", img_cache) if os.path.exists(p) else self.send(404, b"", "image/png")
        if path == "/api/map":
            dim = q.get("dim", ["overworld"])[0]
            if dim not in mapgen.DIMS:
                return self.json(404, {"error": "unknown dimension"})
            spawn = None
            try:
                d = PL.read_nbt_file(os.path.join(world_dir(), "level.dat"))["Data"]
                sp = d.get("spawn") or ({"pos": [d["SpawnX"], d["SpawnY"], d["SpawnZ"]]} if "SpawnX" in d else {})   # up to 1.21
                if sp.get("pos") and PL.strip_ns(sp.get("dimension", "overworld")) == dim:
                    spawn = list(sp["pos"])
            except (OSError, KeyError, ValueError, struct.error, EOFError):
                pass
            return self.json(200, {"dim": dim, "regions": [[rx, rz, int(mt)] for rx, rz, mt in mapgen.list_regions(world_dir(), dim)],
                                   "spawn": spawn, "markers": [m for m in load_markers() if m.get("dim") == dim]})
        m = re.match(r"^/map/(overworld|the_nether|the_end)/(-?\d{1,4})\.(-?\d{1,4})\.png$", path)
        if m:
            dim, rx, rz = m.group(1), int(m.group(2)), int(m.group(3))
            if WORLD_BUSY.is_set():
                return self.send(503, b"", "image/png")
            cache = os.path.join(CACHE, "map")
            tile = os.path.join(cache, dim, "r.%d.%d.png" % (rx, rz))
            mca = mapgen.region_path(world_dir(), dim, rx, rz)
            try:
                fresh = os.path.getmtime(tile) >= os.path.getmtime(mca)
            except OSError:
                fresh = False
            try:
                if fresh:          # already rendered: no waiting behind the tiles being rendered
                    p = tile
                else:
                    with MAP_SEM:
                        if WORLD_BUSY.is_set():   # a restore started while we were waiting
                            return self.send(503, b"", "image/png", {"Retry-After": "5"})
                        p = mapgen.get_tile(world_dir(), cache, dim, rx, rz)
                data = open(p, "rb").read() if p else None
            except OSError:
                return self.send(503, b"", "image/png", {"Retry-After": "2"})
            return self.send(200, data, "image/png", img_cache) if data else self.send(404, b"", "image/png")
        if path == "/api/timeline":
            types = [t for t in q.get("type", [""])[0].split(",") if t] or None
            if not s["admin"]:
                types = [t for t in (types or ["server_start", "server_ready", "server_stop", "join", "leave", "death", "advancement", "chat"]) if t != "panel"]
                if not types:
                    return self.json(200, {"events": []})
            before = qint(q, "before", 0, 0, 2 ** 62)
            evs = TLINE.events(types=types, player=(q.get("player", [""])[0] or None), limit=10 ** 7)
            if before:
                evs = [e for e in evs if e["t"] <= before]
            return self.json(200, {"events": evs[:qint(q, "limit", 200, 1, 500)]})
        if path == "/api/sessions":
            return self.json(200, {"sessions": TLINE.sessions(limit=qint(q, "limit", 30, 1, 200), running=SRV.state != "off")})
        if path in ("/api/backups", "/api/backups/download", "/api/access", "/api/audit", "/api/users", "/api/health", "/api/syslog") and not s["admin"]:
            return self.json(403, {"error": "admins only"})
        if path == "/api/access":
            p = server_props()
            return self.json(200, {**AC.snapshot(CFG["server_dir"]), "online": sorted(online_names()), "state": SRV.state,
                                   "white_list": p.get("white-list") == "true", "enforce": p.get("enforce-whitelist") == "true",
                                   "known": [{"uuid": r["uuid"], "name": r["name"]} for r in roster()]})
        if path == "/api/health":
            return self.json(200, {"checks": health()})
        if path == "/api/syslog":
            which = q.get("file", ["panel"])[0]
            return self.json(200, {"lines": tail(START_LOG if which == "start" else os.path.join(LOG_DIR, "byzzcraft.log"))})
        if path == "/api/users":
            return self.json(200, {"users": [{"name": n, "admin": bool(u.get("admin")), "mc": u.get("mc") or (CFG.get("user_mc") or {}).get(n, ""),
                                              "me": n == s["user"]} for n, u in sorted(load_users().items())]})
        if path == "/api/audit":
            entries = ACT.read(CFG["log_file"])
            items, total, counts = ACT.query(entries, cat=q.get("cat", [""])[0] or None,
                                            user=q.get("user", [""])[0] or None, q=q.get("q", [""])[0][:80],
                                            offset=qint(q, "offset", 0, 0, 10 ** 7), limit=qint(q, "limit", 100, 1, 500))
            return self.json(200, {"entries": items, "total": total, "counts": counts,   # users from the log: deleted ones too
                                   "users": sorted({e["user"] for e in entries if e["user"]}), "mc": mc_names()})
        if path == "/api/backups":
            BACKUPS.world_dir = world_dir()   # untagged backups count as the current world
            return self.json(200, {"items": BACKUPS.list(), "disk": BACKUPS.disk(), "status": BACKUPS.status(),
                                   "doomed": BACKUPS.preview({})["ids"], "world": os.path.basename(world_dir())})
        if path == "/api/backups/download":
            try:
                p = BACKUPS.path(q.get("id", [""])[0])
                return self.send_file(p, re.sub(r"[^\w.\-]", "_", os.path.basename(p), flags=re.ASCII))
            except (ValueError, OSError) as e:
                return self.json(404, {"error": str(e)})
        if path == "/api/panel-settings":
            if not s["admin"]:
                return self.json(403, {"error": "admins only"})
            return self.json(200, panel_settings())
        if path == "/api/properties":
            if not s["admin"]:
                return self.json(403, {"error": "admins only"})
            items = [{"key": k, "value": "" if k in SECRET_KEYS else v, "secret": k in SECRET_KEYS, "locked": k in LOCKED_KEYS}
                     for k, v in server_props().items()]
            return self.json(200, {"items": items})
        self.json(404, {"error": "not found"})

    def events(self, tok, s):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.security_headers()
        self.end_headers()
        q = HUB.subscribe(s["admin"])
        try:
            # what the dashboard feed shows, filtered before the cut: 25 console commands must not hide the server starts
            recent = [e for e in TLINE.events(limit=0) if e["type"] != "chat" and (e["type"] != "panel" or s["admin"] and e["detail"].get("cat") == "server")][:25]
            admin0 = s["admin"]
            init = {"status": SRV.snapshot(), "metrics": list(METRICS), "heap": list(HEAP), "audit": list(AUDIT) if admin0 else [],
                    "console": list(CONSOLE) if admin0 else [], "timeline": recent,
                    # age: how old the reading is, so the browser does not compare its clock with ours
                    "world": (lambda w: dict(w, age=int(time.time() * 1000) - w["t"]) if w else {})(WORLD)}
            self.wfile.write(("retry: 3000\nevent: init\ndata: %s\n\n" % json.dumps(init, separators=(",", ":"))).encode())
            self.wfile.flush()
            # if the user is deleted or changes role it closes: the browser reconnects with the new permissions
            while s["exp"] > time.time() and tok in SESSIONS and (u := load_users().get(s["user"])) and bool(u.get("admin")) == admin0 \
                    and pw_version(u) == s["pv"]:
                try:
                    msg = q.get(timeout=15)
                except queue.Empty:
                    msg = ": ping\n\n"
                self.wfile.write(msg.encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        finally:
            HUB.unsubscribe(q)

    # ------------------------------------------------ POST
    def _do_POST(self):
        if self.path == "/setup":
            return self.first_setup()
        if self.path == "/login":
            f = self.form()
            user = f.get("user", "").strip().lower(); keys = ["c:" + client_id(self)]   # not by name: anyone could lock out "admin"
            if locked(keys):
                return self.send(429, login_page(t("Too many attempts: try again in 15 minutes.")))
            u = check_pw(user, f.get("password", ""))
            if not u:
                note_fail(keys)
                log("login failed for '%s' from %s" % (user[:32], client_id(self)))
                return self.send(401, login_page(t("Wrong user or password.")))
            tok = secrets.token_urlsafe(32)
            SESSIONS[tok_key(tok)] = {"user": user, "admin": bool(u.get("admin")), "pv": pw_version(u), "csrf": secrets.token_urlsafe(24), "exp": time.time() + SESSION_SECONDS}
            save_sessions()
            log("%s logged in from %s" % (user, client_id(self)))
            return self.redirect("/", self.cookie(tok, SESSION_SECONDS))
        tok, s = self.session()
        if self.path == "/logout":
            f = self.form()
            if s and hmac.compare_digest(f.get("csrf", "") or self.headers.get("X-CSRF", ""), s["csrf"]):
                SESSIONS.pop(tok, None)
                save_sessions()
            return self.redirect("/", self.cookie("", 0))
        if not s:
            return self.json(401, {"ok": False, "message": t("Session expired, log in again.")})
        if not hmac.compare_digest(self.headers.get("X-CSRF", ""), s["csrf"]):
            return self.json(403, {"ok": False, "message": t("Invalid request.")})
        try:
            data = json.loads(self.body() or "{}")
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        if self.path == "/api/account/password":
            keys = ["u:" + s["user"]]
            if locked(keys):
                return self.json(200, {"ok": False, "message": t("Too many attempts: try again in 15 minutes.")})
            if not check_pw(s["user"], str(data.get("current", ""))):
                note_fail(keys)
                return self.json(200, {"ok": False, "message": t("The current password is wrong.")})
            pw = str(data.get("new", ""))
            if len(pw) < 10:
                return self.json(200, {"ok": False, "message": t("Password too short: at least 10 characters.")})
            with LOCK:
                users = load_users(); set_password(users, s["user"], pw); save_users(users)
            s["pv"] = pw_version(load_users()[s["user"]])   # this session stays valid
            drop_sessions(s["user"], keep=tok)
            log("%s changed their password" % s["user"])
            return self.json(200, {"ok": True, "message": t("Password changed.")})
        if self.path == "/api/markers":
            try:
                name = str(data.get("name", "")).strip()[:40]
                dim = str(data.get("dim", "overworld"))
                x, z = int(float(data["x"])), int(float(data["z"]))
                icon = str(data.get("icon", "pin"))
                if icon not in ("pin", "home", "portal", "star", "danger"):
                    icon = "pin"
            except (KeyError, ValueError, TypeError, OverflowError):
                return self.json(200, {"ok": False, "message": t("Invalid marker.")})
            if not name or dim not in mapgen.DIMS or abs(x) > 30_000_000 or abs(z) > 30_000_000:
                return self.json(200, {"ok": False, "message": t("Invalid marker.")})
            with LOCK:
                ms = load_markers()
                if len(ms) >= 300:
                    return self.json(200, {"ok": False, "message": t("Too many markers.")})
                mk = {"id": secrets.token_hex(6), "name": name, "dim": dim, "x": x, "z": z, "icon": icon, "by": s["user"], "t": int(time.time())}
                ms.append(mk); save_markers(ms)
            log("%s added the marker '%s' (%d, %d)" % (s["user"], name, x, z))
            HUB.publish("markers", {"dim": dim})
            return self.json(200, {"ok": True, "message": t("Marker saved."), "marker": mk})
        if self.path == "/api/markers/delete":
            mid = str(data.get("id", ""))
            with LOCK:
                ms = load_markers(); keep = [m for m in ms if m.get("id") != mid]
                if len(keep) == len(ms):
                    return self.json(200, {"ok": False, "message": t("Marker not found.")})
                save_markers(keep)
            gone = next(m for m in ms if m.get("id") == mid)
            log("%s removed the marker '%s'" % (s["user"], gone.get("name")))
            HUB.publish("markers", {"dim": gone.get("dim")})
            return self.json(200, {"ok": True, "message": t("Marker removed.")})
        if self.path == "/api/start":
            ok, msg = SRV.start(s["user"])
            return self.json(200, {"ok": ok, "message": msg})
        if self.path in ("/api/stop", "/api/restart"):
            info = SRV.info
            if not s["admin"] and info and info[0] > 0:
                return self.json(200, {"ok": False, "message": t("Players are online: only an admin can do that now.")})
            ok, msg = SRV.stop(s["user"], restart=self.path == "/api/restart")
            return self.json(200, {"ok": ok, "message": msg})
        if not s["admin"]:
            return self.json(403, {"ok": False, "message": t("Admins only.")})
        if self.path == "/api/kill":
            ok, msg = SRV.kill(s["user"])
            return self.json(200, {"ok": ok, "message": msg})
        if self.path == "/api/health/fix":
            ok = fix_health(str(data.get("id", "")), s["user"])
            HUB.publish("health", {})
            return self.json(200, {"ok": ok, "message": t("Done.") if ok else t("Invalid request.")})
        if self.path == "/api/panel-settings":
            try:
                changed = save_panel_settings(data, s["user"])
            except ValueError as e:
                return self.json(200, {"ok": False, "message": str(e)})
            HUB.publish("status", SRV.snapshot())
            if "lang" in changed:   # every text, the timeline cache included, is translated once per process
                threading.Thread(target=restart_soon, daemon=True).start()
                return self.json(200, {"ok": True, "message": t("Saved: the panel restarts in the new language."), "restart": True})
            msg = t("Saved: the new RAM and Java flags apply at the next start.") if {"ram", "java_args"} & set(changed) else t("Saved.") if changed else t("No changes")
            return self.json(200, {"ok": True, "message": msg, "settings": panel_settings()})
        if self.path == "/api/users/save":
            return self.save_user(tok, s, data)
        if self.path == "/api/users/delete":
            name = str(data.get("name", ""))
            with LOCK:
                users = load_users()
                if name == s["user"] or name not in users:
                    return self.json(200, {"ok": False, "message": t("You cannot delete this user.")})
                del users[name]; save_users(users)
            drop_sessions(name)
            log("%s deleted user %s" % (s["user"], name))
            return self.json(200, {"ok": True, "message": t("User %s deleted.") % name})
        if self.path == "/api/command":
            cmd = str(data.get("command", "")).strip().lstrip("/")
            if not cmd or len(cmd) > 400 or "\n" in cmd:
                return self.json(200, {"ok": False, "message": t("Invalid command.")})
            if SRV.state != "on":
                return self.json(200, {"ok": False, "message": t("The server is off.")})
            console_push(["> " + cmd], "cmd")
            try:
                out = RCON.command(cmd)
            except Exception as e:  # noqa: BLE001
                console_push([t("RCON error: %s") % e], "err")
                return self.json(200, {"ok": False, "message": t("RCON unavailable: %s") % e})
            if out:
                console_push(re.sub(r"§.", "", out).splitlines() or [out], "resp")
            log("%s ran: %s" % (s["user"], cmd[:120]))
            return self.json(200, {"ok": True, "message": ""})
        if self.path == "/api/access":
            action, name = str(data.get("action", "")), str(data.get("name", "")).strip()
            if action not in AC.ACTIONS or not AC.NAME_RE.match(name):
                return self.json(200, {"ok": False, "message": t("Invalid name: 3-16 characters, letters, digits and _.")})
            ok, msg = access_action(s["user"], action, name, str(data.get("reason", "")))
            return self.json(200, {"ok": ok, "message": msg})
        if self.path.startswith("/api/backups/"):
            return self.backup_post(self.path[len("/api/backups/"):], data, s["user"])
        if self.path == "/api/properties":
            changes = data.get("changes") or {}
            current = server_props()
            bad = [k for k in changes if k not in current or k in LOCKED_KEYS]
            if bad:
                return self.json(200, {"ok": False, "message": t("Not editable: %s") % ", ".join(bad)})
            clean = {}
            for k, v in changes.items():
                v = str(v)
                if re.search("[\x00-\x1f\x7f\x85  ]", v) or len(v) > 512:   # splitlines() would split on these and inject locked keys
                    return self.json(200, {"ok": False, "message": t("Invalid value for %s") % k})
                if k == "level-name" and not level_name_ok(v):
                    return self.json(200, {"ok": False, "message": t("Invalid value for %s") % k})
                clean[k] = v
            done = write_props(clean, s["user"])
            if data.get("restart") and SRV.state == "on" and done:
                SRV.stop(s["user"], "new settings", restart=True)
            HUB.publish("status", SRV.snapshot())
            n = len(done)
            return self.json(200, {"ok": True, "message": (t("%d settings saved") % n) if n else t("No changes")})
        self.json(404, {"ok": False, "message": t("not found")})


    def backup_post(self, op, data, who):
        BACKUPS.world_dir = world_dir()
        bid = str(data.get("id", ""))
        busy = backup_busy()
        try:
            if op == "create":
                note = str(data.get("note", "")).strip()[:80]
                with LOCK:   # server state and booking together: start() cannot slip in between
                    if busy or OP_PENDING.is_set():
                        return self.json(200, {"ok": False, "message": t("Wait: %s in progress.") % (busy or t("operation")).lower()})
                    st = SRV.state
                    if st in ("starting", "stopping") or (st == "off" and pid_alive(SRV.last_pid)):
                        return self.json(200, {"ok": False, "message": t("The server is starting or stopping: try again shortly.")})
                    OP_PENDING.set()
                BACKUPS.world_dir = world_dir()
                backup_job(who, "manual backup", lambda: BACKUPS.create("manual", st == "on", backup_rcon, note),
                           lambda e: t("Backup ready: %s (%s MB).") % (e["name"], round(e["size"] / 1048576)), t("Manual backup failed: %s"))
                return self.json(200, {"ok": True, "message": t("Backup started: the server stays on.") if st == "on" else t("Backup started.")})
            if op == "restore":
                if data.get("confirm") != "RESTORE":
                    return self.json(200, {"ok": False, "message": t("Confirmation missing.")})
                name = next((e["name"] for e in BACKUPS.list() if e["id"] == bid), None)
                if not name:
                    return self.json(200, {"ok": False, "message": t("Backup not found.")})
                with LOCK:
                    if busy or OP_PENDING.is_set():
                        return self.json(200, {"ok": False, "message": t("Wait: %s in progress.") % (busy or t("operation")).lower()})
                    if SRV.state != "off" or pid_alive(SRV.last_pid):
                        return self.json(200, {"ok": False, "message": t("Stop the server first.")})
                    OP_PENDING.set()
                BACKUPS.world_dir = world_dir()
                log("%s started a restore of %s" % (who, name))
                backup_job(who, "restore of " + name, lambda: do_restore(bid),
                           lambda r: t("World restored from %s. The previous one is in %s.") % (name, os.path.join(os.path.basename(BACKUPS.old_dir), r["moved_to"])),
                           t("Restore failed: %s"))
                return self.json(200, {"ok": True, "message": t("Restore started.")})
            if op == "delete":
                name = next((e["name"] for e in BACKUPS.list() if e["id"] == bid), None)
                BACKUPS.delete(bid)
                log("%s moved backup %s to the trash" % (who, name))
                return self.json(200, {"ok": True, "message": t("Moved to the trash.")})
            if op == "delete_many":
                with LOCK:
                    if busy or OP_PENDING.is_set():
                        return self.json(200, {"ok": False, "message": t("Wait: %s in progress.") % (busy or t("operation")).lower()})
                r = BACKUPS.delete_many(data.get("ids"))
                if r["deleted"]:
                    log("%s moved %d backups to the trash" % (who, len(r["deleted"])))
                if r["errors"]:
                    return self.json(200, {"ok": False, "message": r["errors"][0], **r})
                msg = t("Moved %d to the trash.") % len(r["deleted"]) + (" " + t("%d pinned skipped.") % r["skipped"] if r["skipped"] else "")
                return self.json(200, {"ok": True, "message": msg, **r})
            if op == "pin_many":
                BACKUPS.pin_many(data.get("ids"), bool(data.get("pinned")))
                return self.json(200, {"ok": True, "message": t("Pinned: it will never be deleted automatically.") if data.get("pinned") else t("Unpinned.")})
            if op == "note":
                BACKUPS.set_note(bid, data.get("note", ""))
                return self.json(200, {"ok": True, "message": t("Note saved.")})
            if op == "preview":
                return self.json(200, {"ok": True, **BACKUPS.preview(data.get("schedule") if isinstance(data.get("schedule"), dict) else {})})
            if op == "pin":
                BACKUPS.pin(bid, bool(data.get("pinned")))
                return self.json(200, {"ok": True, "message": t("Pinned: it will never be deleted automatically.") if data.get("pinned") else t("Unpinned.")})
            if op == "schedule":
                cfg = BACKUPS.set_schedule(data.get("schedule") if isinstance(data.get("schedule"), dict) else {})
                log("%s changed the backup schedule" % who)
                return self.json(200, {"ok": True, "message": t("Schedule saved."), "schedule": cfg})
        except ValueError as e:
            return self.json(200, {"ok": False, "message": str(e)})
        except OSError as e:
            return self.json(200, {"ok": False, "message": t("Disk error: %s") % e})
        return self.json(404, {"ok": False, "message": t("not found")})


    def save_user(self, tok, s, data):
        """Creates or edits a user (admins only). Empty password when editing = unchanged."""
        name = str(data.get("name", "")).strip().lower()
        pw, mc, new = str(data.get("password", "")), str(data.get("mc", "")).strip(), bool(data.get("new"))
        if not USER_RE.match(name) or name in RESERVED:
            return self.json(200, {"ok": False, "message": t("Invalid user name: lowercase letters, digits, . _ -")})
        if mc and not AC.NAME_RE.match(mc):
            return self.json(200, {"ok": False, "message": t("Invalid name: 3-16 characters, letters, digits and _.")})
        if (new or pw) and len(pw) < 10:
            return self.json(200, {"ok": False, "message": t("Password too short: at least 10 characters.")})
        with LOCK:
            users = load_users()
            if new == (name in users):
                return self.json(200, {"ok": False, "message": t("User %s already exists.") % name if new else t("User %s not found.") % name})
            if name == s["user"] and not data.get("admin"):
                return self.json(200, {"ok": False, "message": t("You cannot remove your own admin role.")})
            if pw:
                set_password(users, name, pw)
            users[name].update({"admin": bool(data.get("admin")), "mc": mc})
            save_users(users)
        if pw and not new:
            if name == s["user"]:
                s["pv"] = pw_version(load_users()[name])
            drop_sessions(name, keep=tok)
        log("%s %s user %s" % (s["user"], "created" if new else "updated", name))
        return self.json(200, {"ok": True, "message": (t("User %s created.") if new else t("User %s saved.")) % name})

    def first_setup(self):
        """First setup: only with no users, from the browser of this machine or with the setup code."""
        f = self.form()
        if load_users() or not same_origin(self) or not setup_allowed(self, f.get("token", "")):
            return self.send(403, setup_remote_page())
        remote = not is_local(self)
        if f.get("password") != f.get("password2"):
            return self.send(200, setup_page(t("The passwords are too short or do not match."), f, f.get("token", ""), remote))
        err = do_setup(f)
        if err:
            return self.send(200, setup_page(err, f, f.get("token", ""), remote))
        name = f["user"].strip().lower()
        tok = secrets.token_urlsafe(32)
        SESSIONS[tok_key(tok)] = {"user": name, "admin": True, "pv": pw_version(load_users().get(name, {})), "csrf": secrets.token_urlsafe(24), "exp": time.time() + SESSION_SECONDS}
        save_sessions()
        self.redirect("/", self.cookie(tok, SESSION_SECONDS))
        threading.Thread(target=restart_soon, daemon=True).start()


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # on Windows SO_REUSEADDR lets two processes share a port: a second panel would steal the first one's requests
    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


# ---------------------------------------------------------------- cli
def cli(args):
    users = load_users()
    cmd = args[0]
    if cmd == "assets":
        return AS.install(CFG["server_dir"], args[1] if len(args) > 1 else None, jar=server_jar())
    if cmd == "java":
        need = int(args[1]) if len(args) > 1 else RT.required(CFG["server_dir"], server_jar())
        if not need:
            sys.exit("cannot tell which Java the server in %s needs: pass it, e.g.  panel.py java 25" % CFG["server_dir"])
        return RT.install(need)
    if cmd == "service":
        return service()
    if cmd == "setup":
        import argparse
        ap = argparse.ArgumentParser(prog="panel.py setup", description="First setup without a browser.")
        ap.add_argument("server_dir", help="Minecraft server folder (created with --new-server)")
        ap.add_argument("--admin", required=True, help="name of the first admin user")
        ap.add_argument("--lang", default="en", choices=languages())
        ap.add_argument("--network", action="store_true", help="open the panel to the home network (http://IP:%d)" % CFG["port"])
        ap.add_argument("--accept-eula", action="store_true", help="accept the Minecraft EULA (https://aka.ms/MinecraftEULA)")
        ap.add_argument("--new-server", action="store_true", help="download the latest Minecraft server if the folder has none")
        ap.add_argument("--no-java", action="store_true", help="do not download Java if it is missing")
        a = ap.parse_args(args[1:])
        if users:
            sys.exit("already set up: use adduser / passwd to manage users")
        pw = os.environ.get("PANEL_PASSWORD") or getpass.getpass("Password for %s: " % a.admin)
        if not os.environ.get("PANEL_PASSWORD") and pw != getpass.getpass("Repeat the password: "):
            sys.exit("passwords do not match")
        setup_logging()
        if not port_free(CFG["port"]):   # like the first web start: port taken by another service
            CFG["port"] = free_port(CFG["port"] + 1)
            L.warning("port in use, the panel will use %d", CFG["port"])
        err = do_setup({"server_dir": a.server_dir, "user": a.admin, "password": pw, "lang": a.lang, "network": "1" if a.network else "",
                        "eula": "1" if a.accept_eula else "", "new_server": "1" if a.new_server else "", "java": "" if a.no_java else "1"})
        if err:
            sys.exit(err)
        need = RT.required(CFG["server_dir"], server_jar())
        if not a.no_java and need and not RT.java_for(need)[0]:
            RT.install(need, log=L.info)
        if not AS.present():
            AS.install(CFG["server_dir"], log=L.info, jar=server_jar())
        L.info("done. Start the panel with:  %s panel.py   or keep it running with:  %s", sys.executable, SERVICE_HINT)
        return
    if cmd == "users":
        for n, u in sorted(users.items()):
            print("%s%s" % (n, "  (admin)" if u.get("admin") else ""))
        return
    if cmd in ("adduser", "passwd"):
        name = args[1].strip().lower()
        if not USER_RE.match(name) or name in RESERVED or name.startswith("-"):   # "adduser --admin" is a typo, not a user
            sys.exit("invalid user name: lowercase letters, digits, . _ -")
        if cmd == "passwd" and name not in users:
            sys.exit("no such user '%s'" % name)
        if cmd == "adduser" and name in users:
            sys.exit("user '%s' already exists: use passwd to change the password" % name)
        pw = os.environ.get("PANEL_PASSWORD")
        if not pw:
            pw = getpass.getpass("Password for %s: " % name)
            if pw != getpass.getpass("Repeat the password: "):
                sys.exit("passwords do not match")
        if len(pw) < 10:
            sys.exit("password too short: at least 10 characters")
        admin = ("--admin" in args) if cmd == "adduser" else users[name].get("admin", False)
        set_password(users, name, pw)
        users[name]["admin"] = admin
        save_users(users)
        print("user %s saved%s" % (name, " as admin" if admin else ""))
        return
    if cmd == "deluser":
        name = args[1].strip().lower()
        if name not in users:
            sys.exit("no such user '%s'" % name)
        if users[name].get("admin") and sum(1 for u in users.values() if u.get("admin")) == 1:
            sys.exit("'%s' is the last admin: add another admin first" % name)
        del users[name]; save_users(users); print("removed")
        return
    sys.exit(__doc__)


def main():
    if len(sys.argv) > 1:
        return cli(sys.argv[1:])
    setup_logging()
    L.info("ByzzCraft %s starting: Python %s on %s, data in %s", VERSION, sys.version.split()[0], sys.platform, DATA)
    if os.name != "nt" and os.geteuid() == 0:
        L.warning("running as root: the server files would belong to root. Run it as a normal user.")
    load_sessions()
    listen, port = CFG["listen"], CFG["port"]
    if load_users():
        load_audit()
        need = RT.required(CFG["server_dir"], server_jar())
        java, found = RT.java_for(need)
        L.info("server folder %s, listen %s:%d, language %s", CFG["server_dir"], listen, port, i18n.LANG)
        L.info("Java: needed %s, using %s (in PATH: %s)", need, java, found)
        for target in (sampler, tailers, player_watcher, map_warmer, backup_scheduler, world_watcher, update_checker):
            threading.Thread(target=worker(target), name=target.__name__, daemon=True).start()
        if not AS.present():
            download_assets()
        if need and not java and CFG.get("java_auto"):
            download_java()
    else:
        listen = "0.0.0.0"   # until there is an admin, setup works from another PC too, but only with the code
    # first run: if the port is taken (another service) a nearby one is used, saved later by the setup
    ports = [port] if load_users() else list(range(port, port + 11))
    srv, err = None, None
    for attempt in range(10 if os.environ.pop("BYZZ_RESTART", None) else 1):   # on restart the old process may hold it a moment longer
        for pt in ports:
            try:
                srv, port = Server((listen, pt), Handler), pt
                break
            except OSError as e:
                err = e
        if srv:
            break
        time.sleep(1)
    if not srv:
        L.error("cannot listen on %s:%d (%s): is the panel already running? Otherwise change \"port\" in %s", listen, port, err, CONFIG_PATH)
        sys.exit(1)
    if port != CFG["port"]:
        L.warning("port %d is in use, using %d", CFG["port"], port)
        CFG["port"] = port
    log("panel started on http://%s:%d" % (listen, port))
    if not load_users():   # only after taking the port: the link must really work
        fw = ("Windows may ask to let Python through the firewall: allow it on private networks." if os.name == "nt" else
              "The firewall may block the port: sudo ufw allow %d/tcp" % port)
        L.info("ByzzCraft is not set up yet.\n  From your computer open:  http://%s:%d/?setup=%s\n  On this machine:          http://127.0.0.1:%d\n"
               "  Page does not open? %s (a Tailscale IP of this machine works the same way)",
               lan_ip(), port, setup_token(), port, fw)
    if os.getpid() == 1:   # Docker: PID 1 ignores SIGTERM without a handler, so docker stop would SIGKILL Java after 10 s
        def on_term(signum, frame):
            SHUTTING_DOWN.set()
            SRV.restart_pending = False
            pid = SRV.last_pid if pid_alive(SRV.last_pid) else SRV.proc.pid if SRV.proc and SRV.proc.poll() is None else None
            if pid:
                end = time.time() + 100
                while backup_busy() and time.time() < end - 40:   # stopping halfway through a backup would spoil it
                    time.sleep(1)
                if not SRV.stop("panel", "container stop")[0]:
                    SRV.stop_requested = True   # wanted: not a crash
                    os.kill(pid, signal.SIGTERM)   # Minecraft's shutdown hook saves the world
                while pid_alive(pid) and time.time() < end:
                    time.sleep(0.5)
            sys.exit(0)
        signal.signal(signal.SIGTERM, on_term)
    srv.serve_forever()


if __name__ == "__main__":
    main()
