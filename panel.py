"""Pannello web del server Minecraft (solo libreria standard di Python, Windows).

Avvio:            pythonw panel.py            (ascolta su 127.0.0.1, pubblicato in tailnet con `tailscale serve`)
Utenti:           python panel.py adduser NOME [--admin]
                  python panel.py passwd NOME
                  python panel.py deluser NOME
                  python panel.py users
"""
import base64, ctypes, getpass, hashlib, hmac, html, json, os, queue, re, secrets, shutil, socket, struct, subprocess, sys, threading, time
from collections import deque
from ctypes import wintypes
from datetime import datetime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import players as PL  # noqa: E402
import mapgen  # noqa: E402
import timeline as TL  # noqa: E402
import backups as BK  # noqa: E402
import accessi as AC  # noqa: E402
import registro as RG  # noqa: E402
CONFIG_PATH = os.environ.get("PANEL_CONFIG", os.path.join(HERE, "panel-config.json"))
DEFAULTS = {
    "server_dir": os.path.dirname(HERE),
    "start_script": "avvia-server.bat",
    "listen": "127.0.0.1",
    "port": 8765,
    "auto_stop_minutes": 30,      # spegne il server se resta vuoto per N minuti (0 = mai)
    "hide_console": True,         # avvio dal pannello senza finestre: log e comandi stanno nella dashboard
    "start_timeout_seconds": 240,
    "title": "Server Minecraft Hardcore",
    "users_file": "panel-users.json",
    "log_file": "panel.log",
    "mc_address": "",
    "user_mc": {},                # utente del pannello -> nick Minecraft (faccia nella barra laterale)
}
SESSION_HOURS = 12
PBKDF2_ROUNDS = 240_000
MAX_FAILS, FAIL_WINDOW, LOCK_SECONDS = 5, 600, 900
SAMPLE_SECONDS = 2
HISTORY_POINTS = 300          # 10 minuti a 2 s
LOCKED_KEYS = {"server-port", "enable-rcon", "rcon.port", "rcon.password", "management-server-secret",
               "management-server-tls-keystore-password"}
SECRET_KEYS = {"rcon.password", "management-server-secret", "management-server-tls-keystore-password"}


def load_config():
    cfg = dict(DEFAULTS)
    if os.path.exists(CONFIG_PATH):
        cfg.update(json.load(open(CONFIG_PATH, encoding="utf-8")))
    for k in ("users_file", "log_file"):
        if not os.path.isabs(cfg[k]):
            cfg[k] = os.path.join(os.path.dirname(CONFIG_PATH), cfg[k])
    return cfg


CFG = load_config()
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


def log(msg):
    msg = re.sub(r"[\r\n\x00-\x1f]+", " ", str(msg))   # niente righe false nel registro
    line = "%s  %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    with LOCK:
        with open(CFG["log_file"], "a", encoding="utf-8") as f:
            f.write(line + "\n")
        AUDIT.append(line)
    HUB.publish("audit", {"line": line, "entry": RG.parse(line)}, admin_only=True)


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
    tmp = CFG["users_file"] + ".tmp"
    json.dump(users, open(tmp, "w", encoding="utf-8"), indent=2)
    os.replace(tmp, CFG["users_file"])


def hash_pw(pw, salt=None, rounds=PBKDF2_ROUNDS):
    salt = salt or secrets.token_bytes(16)
    return salt.hex(), hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, rounds).hex(), rounds


def check_pw(user, pw):
    u = load_users().get(user)
    if not u:
        hash_pw(pw)  # stesso tempo di risposta anche per utenti inesistenti
        return None
    _, h, _ = hash_pw(pw, bytes.fromhex(u["salt"]), u["rounds"])
    return u if hmac.compare_digest(h, u["hash"]) else None


# ---------------------------------------------------------------- server.properties
def props_path():
    return os.path.join(CFG["server_dir"], "server.properties")


def server_props():
    props = {}
    try:
        with open(props_path(), encoding="utf-8") as f:
            for line in f:
                if "=" in line and not line.lstrip().startswith("#"):
                    k, v = line.rstrip("\r\n").split("=", 1)
                    props[k.strip()] = v
    except FileNotFoundError:
        pass
    return props


def write_props(changes, who):
    path = props_path()
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()
    bdir = os.path.join(HERE, "backup-properties")
    os.makedirs(bdir, exist_ok=True)
    with open(os.path.join(bdir, "server.properties.%s" % datetime.now().strftime("%Y%m%d-%H%M%S")), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    for old in sorted(os.listdir(bdir))[:-15]:
        os.remove(os.path.join(bdir, old))
    out, done = [], []
    for line in lines:
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            k = k.strip()
            if k in changes and changes[k] != v:
                done.append((k, v, changes[k]))
                line = "%s=%s" % (k, changes[k])
        out.append(line)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(out) + "\n")
    os.replace(tmp, path)
    for k, old, new in done:
        shown_old = "***" if k in SECRET_KEYS else old
        shown_new = "***" if k in SECRET_KEYS else new
        log("%s ha cambiato %s: %s -> %s" % (who, k, shown_old, shown_new))
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
            raise ConnectionError("chiuso")
        n |= (b[0] & 0x7F) << shift
        if not b[0] & 0x80:
            return n
        shift += 7


def ping(port):
    """Server List Ping: None se spento, altrimenti (online, max, [nomi])."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=3) as s:
            host = b"127.0.0.1"
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
    """Una sola connessione RCON riusata (evita righe 'RCON Client started' a ogni comando)."""

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
                    raise ConnectionError("RCON chiuso")
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
        """Come command(), ma per risposte oltre 4096 byte, che il server spezza in piu' pacchetti
        da 4096 byte mandati di seguito: si continua a leggere finche' un pezzo e' pieno.
        (Niente pacchetto segnaposto: il server legge un solo pacchetto per volta e lo perderebbe.)"""
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
                            if parts:      # risposta lunga esattamente un multiplo di 4096: finita
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
            raise RuntimeError("RCON non attivo in server.properties")
        self.sock = socket.create_connection(("127.0.0.1", int(p.get("rcon.port", 25575))), timeout=5)
        self._send(1, 3, p["rcon.password"])
        rid, _ = self._recv()
        if rid == -1:
            self._close()
            raise RuntimeError("password RCON rifiutata")

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
                        _, body = self._recv()
                    finally:
                        if self.sock:
                            self.sock.settimeout(5)
                    return body
                except socket.timeout:
                    self._close()   # niente secondo invio: il comando potrebbe essere ancora in esecuzione
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


# ---------------------------------------------------------------- Windows: processo, CPU, RAM
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
iphl = ctypes.WinDLL("iphlpapi")


class FILETIME(ctypes.Structure):
    _fields_ = [("lo", wintypes.DWORD), ("hi", wintypes.DWORD)]

    def val(self):
        return (self.hi << 32) | self.lo


class PMC(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [(n, ctypes.c_size_t) for n in (
        "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
        "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage", "PrivateUsage")]


class MEMSTAT(ctypes.Structure):
    _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD)] + [(n, ctypes.c_ulonglong) for n in (
        "ullTotalPhys", "ullAvailPhys", "ullTotalPageFile", "ullAvailPageFile", "ullTotalVirtual", "ullAvailVirtual", "ullAvailExtendedVirtual")]


k32.OpenProcess.restype = wintypes.HANDLE
k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
k32.CloseHandle.argtypes = [wintypes.HANDLE]
k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(FILETIME)] * 4
k32.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]
NCPU = os.cpu_count() or 1


def listening_pid(port):
    """PID del processo in ascolto su una porta TCP (IPv4 o IPv6)."""
    for af, rowsize in ((2, 24), (23, 56)):
        size = wintypes.DWORD(0)
        iphl.GetExtendedTcpTable(None, ctypes.byref(size), False, af, 3, 0)
        buf = ctypes.create_string_buffer(size.value + 512)
        size = wintypes.DWORD(len(buf))
        if iphl.GetExtendedTcpTable(buf, ctypes.byref(size), False, af, 3, 0) != 0:
            continue
        raw = buf.raw
        (n,) = struct.unpack_from("<I", raw, 0)
        for i in range(n):
            off = 4 + i * rowsize
            if af == 2:
                _state, _la, lport, _ra, _rp, pid = struct.unpack_from("<6I", raw, off)
            else:
                lport, = struct.unpack_from("<I", raw, off + 20)
                pid, = struct.unpack_from("<I", raw, off + 52)
            if socket.ntohs(lport & 0xFFFF) == port:
                return pid
    return None


class ProcSampler:
    def __init__(self):
        self.pid = None; self.last = None

    def sample(self, pid):
        """(cpu % del PC, working set MB, privata MB, avviato_epoch) oppure None."""
        if pid is None:
            self.pid = None; self.last = None
            return None
        h = k32.OpenProcess(0x1000 | 0x0010, False, pid)  # QUERY_LIMITED_INFORMATION | VM_READ
        if not h:
            return None
        try:
            c, e, kt, ut = FILETIME(), FILETIME(), FILETIME(), FILETIME()
            k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(kt), ctypes.byref(ut))
            pmc = PMC(); pmc.cb = ctypes.sizeof(PMC)
            k32.K32GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb)
        finally:
            k32.CloseHandle(h)
        busy, now = kt.val() + ut.val(), time.time()
        cpu = None
        if self.pid == pid and self.last:
            dt = now - self.last[1]
            if dt > 0:
                cpu = max(0.0, (busy - self.last[0]) / 1e7 / dt / NCPU * 100)
        self.pid, self.last = pid, (busy, now)
        started = c.val() / 1e7 - 11644473600
        return cpu, pmc.WorkingSetSize / 1048576, pmc.PrivateUsage / 1048576, started


def pid_alive(pid):
    if not pid:
        return False
    h = k32.OpenProcess(0x1000, False, pid)
    if not h:
        return False
    try:
        code = wintypes.DWORD()
        k32.GetExitCodeProcess(h, ctypes.byref(code))
        return code.value == 259  # STILL_ACTIVE
    finally:
        k32.CloseHandle(h)


def system_times():
    """(inattivo, totale) in unita' da 100 ns di tutte le CPU: la CPU del PC e' 1 - d(inattivo)/d(totale)."""
    idle, kern, user = FILETIME(), FILETIME(), FILETIME()
    k32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kern), ctypes.byref(user))
    return idle.val(), kern.val() + user.val()   # il tempo kernel comprende gia' quello inattivo


def system_memory():
    m = MEMSTAT(); m.dwLength = ctypes.sizeof(MEMSTAT)
    k32.GlobalMemoryStatusEx(ctypes.byref(m))
    return m.ullAvailPhys / 1048576, m.ullTotalPhys / 1048576


# ---------------------------------------------------------------- tail di latest.log e gc.log
class Tailer:
    def __init__(self, path, initial_lines=0):
        self.path = path; self.pos = None; self.rest = b""; self.initial = initial_lines

    def read(self):
        try:
            st = os.stat(self.path)
        except FileNotFoundError:
            if self.pos is not None:
                self.pos = 0  # file ruotato: il prossimo si legge dall'inizio
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


# ---------------------------------------------------------------- controllo del server
class ServerControl:
    def __init__(self):
        self.state = "off"          # off | starting | on | stopping
        self.started_at = 0
        self.proc = None
        self.empty_since = None
        self.info = None
        self.restart_pending = False
        self.proc_started = None
        self.last_pid = None        # PID di java: finche' e' vivo il mondo e' ancora in uso
        self.props_mtime_on = None  # data di server.properties quando il server e' risultato acceso

    def port(self):
        return int(server_props().get("server-port", 25565))

    def snapshot(self):
        info = self.info
        empty = int((time.time() - self.empty_since) / 60) if self.empty_since else None
        pending = False
        if self.state == "on" and self.props_mtime_on:
            try:
                # Minecraft riscrive server.properties all'avvio: conta solo cio' che cambia dopo che e' acceso
                pending = os.path.getmtime(props_path()) > self.props_mtime_on + 0.5
            except OSError:
                pass
        return {"state": self.state, "online": info[0] if info else 0, "max": info[1] if info else 0,
                "players": info[2] if info else [], "since": self.proc_started, "empty_minutes": empty,
                "auto_stop_minutes": CFG["auto_stop_minutes"], "restart_pending": self.restart_pending,
                "props_changed": pending}

    def update(self, info):
        with LOCK:
            prev = self.state
            self.info = info
            if info is not None:
                if self.state != "stopping":
                    self.state = "on"
                if info[0] > 0:
                    self.empty_since = None
                elif self.empty_since is None:
                    self.empty_since = time.time()
            else:
                self.empty_since = None
                if self.state == "starting":
                    gone = self.proc is not None and self.proc.poll() is not None
                    if gone or time.time() - self.started_at > CFG["start_timeout_seconds"]:
                        self.state = "off"
                elif self.state in ("on", "stopping"):
                    # la porta si chiude prima che java finisca di salvare: resta "stopping" finche' il processo vive
                    self.state = "stopping" if pid_alive(self.last_pid) else "off"
            changed = prev != self.state
            if changed and self.state == "on":
                try:
                    self.props_mtime_on = os.path.getmtime(props_path())
                except OSError:
                    self.props_mtime_on = None
        if changed and self.state == "off":
            RCON.reset()
        if self.state == "off" and self.restart_pending:
            self.restart_pending = False
            ok, _ = self.start("pannello (riavvio)")
            if not ok and backup_busy():
                self.restart_pending = True   # riprova a fine backup

        return changed

    def start(self, who):
        with LOCK:
            if self.state in ("starting", "on", "stopping"):
                return False, "Il server e' gia' acceso o si sta avviando."
            if pid_alive(self.last_pid):
                return False, "Il server si sta ancora chiudendo: riprova tra qualche secondo."
            busy = backup_busy()
            if busy or OP_PENDING.is_set():
                return False, "Aspetta che finisca: %s in corso." % (busy or "operazione sui backup").lower()
            env = dict(os.environ, NOPAUSE="1")
            script = os.path.join(CFG["server_dir"], CFG["start_script"])
            flags = subprocess.CREATE_NO_WINDOW if CFG.get("hide_console") else subprocess.CREATE_NEW_CONSOLE
            self.proc = subprocess.Popen(["cmd.exe", "/c", script], cwd=CFG["server_dir"], env=env,
                                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                         creationflags=flags)
            self.state, self.started_at = "starting", time.time()
        log("%s ha acceso il server" % who)
        HUB.publish("status", self.snapshot())
        return True, "Avvio in corso: backup del mondo e caricamento."

    def stop(self, who, reason=None, restart=False):
        with LOCK:
            if self.state != "on":
                return False, "Il server non e' acceso."
            busy = BACKUPS.status()["busy"]
            if busy and busy["op"] == "backup":   # lo spegnimento a meta' copia darebbe un backup incoerente
                return False, "Aspetta qualche secondo: backup in corso."
            self.state = "stopping"
            self.restart_pending = restart
        try:
            RCON.command("stop")
        except Exception as e:  # noqa: BLE001
            with LOCK:
                self.state = "on"; self.restart_pending = False
            log("%s: spegnimento fallito (%s)" % (who, e))
            return False, "Spegnimento non riuscito: %s" % e
        log("%s ha %s il server%s" % (who, "riavviato" if restart else "spento", " (%s)" % reason if reason else ""))
        HUB.publish("status", self.snapshot())
        return True, "Riavvio in corso." if restart else "Spegnimento in corso."


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
    while True:
        t0 = time.time()
        try:
            port = SRV.port()
            info = ping(port)
            changed = SRV.update(info)
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
            if mins and info is not None and SRV.state == "on" and SRV.empty_since and time.time() - SRV.empty_since >= mins * 60:
                SRV.stop("pannello", "vuoto da %d minuti" % mins)
        except Exception as e:  # noqa: BLE001
            log("errore campionamento: %s" % e)
        time.sleep(max(0.2, SAMPLE_SECONDS - (time.time() - t0)))


TLINE = TL.Timeline(CFG["server_dir"], os.path.join(HERE, "cache"))
MAP_SEM = threading.BoundedSemaphore(2)     # massimo 2 tile generate insieme: non rubare CPU al server
MARKERS_FILE = os.path.join(HERE, "markers.json")
WORLD_BUSY = threading.Event()              # ripristino in corso: niente letture del mondo (mappa)
OP_PENDING = threading.Event()              # backup/ripristino chiesto dal web, thread non ancora partito: niente avvii
BACKUPS = BK.Backups(CFG["server_dir"], os.path.join(HERE, "cache", "backups-state.json"),
                     on_change=lambda: HUB.publish("backups", BACKUPS.status(), admin_only=True))


WORLD = {}


def world_watcher():
    """Ora del giorno, fase lunare e meteo, via RCON e solo a server acceso (comandi della 26.x:
    le ore sono la timeline minecraft:day, la luna la timeline minecraft:moon)."""
    global WORLD
    ticks = lambda out: int(re.search(r"is at (\d+) tick", out).group(1))  # noqa: E731
    while True:
        try:
            if SRV.state == "on":
                day = ticks(RCON.command("time query day"))
                moon = ticks(RCON.command("time query moon"))
                rain = "passed" in RCON.command('execute if predicate {type:"minecraft:weather_check",raining:true}')
                thunder = rain and "passed" in RCON.command('execute if predicate {type:"minecraft:weather_check",thundering:true}')
                info = SRV.info
                # col server vuoto il mondo e' in pausa (pause-when-empty-seconds): l'ora non avanza
                WORLD = {"day": day % 24000, "moon": (moon // 24000) % 8, "rain": rain, "thunder": thunder,
                         "t": int(time.time() * 1000), "running": bool(info and info[0] > 0)}
                HUB.publish("world", WORLD)
            elif WORLD:
                WORLD = {}
                HUB.publish("world", WORLD)
        except Exception:  # noqa: BLE001  (RCON non pronto, risposta inattesa: si riprova al giro dopo)
            pass
        time.sleep(15)


def access_action(who, action, name, reason):
    """Whitelist / op / ban / kick. Ritorna (ok, messaggio)."""
    st = SRV.state
    if st in ("starting", "stopping") or (st == "off" and pid_alive(SRV.last_pid)):
        return False, "Il server si sta accendendo o spegnendo: riprova tra poco."
    if st == "on":
        try:
            out = re.sub(r"§.", "", RCON.command(AC.command(action, name, reason))).strip()
        except Exception as e:  # noqa: BLE001
            return False, "RCON non disponibile: %s" % e
        # risposte del server in inglese: "Added X to the whitelist", "Made X a server operator", ...
        if not re.match(r"^(Added|Removed|Made|Banned|Unbanned|Kicked)\b", out):
            for rx, it in ((r"isn't banned|not banned", "%s non è bannato."), (r"not an operator", "%s non è operatore."),
                           (r"already an operator", "%s è già operatore."), (r"already whitelisted", "%s è già in whitelist."),
                           (r"not whitelisted", "%s non è in whitelist."), (r"already banned", "%s è già bannato."),
                           (r"No player was found", "%s non è online."), (r"does not exist|Unknown player", "Il giocatore %s non esiste.")):
                if re.search(rx, out, re.I):
                    return False, it % name
            return False, out or "Il server non ha risposto."
        msg = "Fatto: %s %s." % (name, AC.VERBS[action])
    else:
        try:
            prof = AC.resolve(CFG["server_dir"], action, name, known={p["uuid"]: p["name"] for p in roster()})
            with LOCK:   # start() prende lo stesso LOCK: il server non puo' partire mentre scriviamo i file
                if SRV.state != "off" or pid_alive(SRV.last_pid):
                    return False, "Il server si è appena acceso: riprova, ora la modifica passa dal server."
                msg = AC.apply_offline(CFG["server_dir"], action, name, reason, who, profile=prof) + "."
        except (ValueError, OSError) as e:
            return False, str(e)
    reason = re.sub(r"[\r\n\x00-\x1f()]+", " ", reason).strip()[:100]
    log("%s ha %s %s%s" % (who, AC.VERBS[action], name, " (%s)" % reason if reason and action in ("ban", "kick") else ""))
    HUB.publish("access", {}, admin_only=True)
    return True, msg


def backup_busy():
    b = BACKUPS.status()["busy"]
    return b["label"] if b else None


def backup_rcon(cmd):
    # save-all flush scrive tutto il mondo su disco: puo' metterci ben piu' dei 5 s normali
    return RCON.command(cmd, timeout=180 if cmd.startswith("save-all") else 15)


def backup_scheduler():
    """Backup programmati e pulizia dei vecchi (le regole stanno in cache/backups-state.json)."""
    time.sleep(30)
    while True:
        st = SRV.state
        if st in ("on", "off") and not (st == "off" and pid_alive(SRV.last_pid)) and not OP_PENDING.is_set():
            info = SRV.info
            BACKUPS.world_dir = world_dir()
            BACKUPS.tick(time.time(), st == "on", info[0] if (st == "on" and info) else 0, backup_rcon)
        time.sleep(60)


def backup_job(who, what, fn, done_msg):
    """Le operazioni lunghe girano in un thread: l'esito arriva alla pagina come evento.
    Chi chiama ha gia' impostato OP_PENDING (sotto LOCK, insieme ai controlli sullo stato del server)."""
    def run():
        try:
            r = fn()
        except Exception as e:  # noqa: BLE001
            log("%s: %s non riuscito (%s)" % (who, what, e))
            HUB.publish("backup_done", {"ok": False, "message": "%s non riuscito: %s" % (what.capitalize(), e)}, admin_only=True)
        else:
            log("%s: %s completato" % (who, what))
            HUB.publish("backup_done", {"ok": True, "message": done_msg(r)}, admin_only=True)
        finally:
            OP_PENDING.clear()
    threading.Thread(target=run, daemon=True).start()


def do_restore(bid):
    WORLD_BUSY.set()
    try:
        with MAP_SEM, MAP_SEM:   # aspetta che nessuna tile stia leggendo il mondo
            pass
        r = BACKUPS.restore(bid, server_on=SRV.state != "off" or pid_alive(SRV.last_pid))
        shutil.rmtree(os.path.join(HERE, "cache", "map"), ignore_errors=True)   # tile del mondo vecchio
        return r
    finally:
        WORLD_BUSY.clear()


def load_markers():
    try:
        return json.load(open(MARKERS_FILE, encoding="utf-8"))
    except (OSError, ValueError):
        return []


def save_markers(m):
    tmp = MARKERS_FILE + ".tmp"
    json.dump(m, open(tmp, "w", encoding="utf-8"), indent=1)
    os.replace(tmp, MARKERS_FILE)


def map_warmer():
    """Tiene aggiornata la cache delle tile, una alla volta e con calma (la mappa resta veloce da aprire)."""
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
                try:   # un errore su una tile (es. PNG aperto da una richiesta) non ferma le altre
                    with MAP_SEM:
                        if not WORLD_BUSY.is_set():
                            mapgen.get_tile(world_dir(), os.path.join(HERE, "cache", "map"), dim, rx, rz)
                except Exception:  # noqa: BLE001
                    errors += 1
                time.sleep(0.3)
        if errors:
            log("mappa: %d tile non aggiornate in questo giro, riprovo tra 5 minuti" % errors)
        time.sleep(300)


def publish_timeline(evs):
    for e in evs:
        HUB.publish("timeline", e, admin_only=e.get("type") == "panel")


def tailers():
    logs = Tailer(os.path.join(CFG["server_dir"], "logs", "latest.log"), initial_lines=200)
    gc = Tailer(os.path.join(CFG["server_dir"], "logs", "gc.log"), initial_lines=400)
    tl_ready = False   # la prima refresh() restituisce tutto lo storico: non va spedito come "nuovo"
    last_tl = 0
    while True:
        try:
            lines = logs.read()
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
            log("errore lettura log: %s" % e)
        time.sleep(0.5)


# ---------------------------------------------------------------- giocatori
LIVE = {}            # uuid -> {"t": epoch, "data": dati normalizzati letti via RCON}
ROSTER_SENT = None
DATA_SENT = {}
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def world_dir():
    return os.path.join(CFG["server_dir"], server_props().get("level-name", "world") or "world")


def known_players():
    """uuid -> nome, da usercache.json + file dei giocatori nel mondo."""
    names = {}
    try:
        for e in json.load(open(os.path.join(CFG["server_dir"], "usercache.json"), encoding="utf-8")):
            names[e["uuid"]] = e["name"]
    except (OSError, ValueError):
        pass
    pdir = os.path.join(world_dir(), "players", "data")
    on_disk = {f[:-4] for f in os.listdir(pdir) if f.endswith(".dat")} if os.path.isdir(pdir) else set()
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
    """Dati live se online (aggiornati via RCON), altrimenti l'ultimo salvataggio su disco."""
    live = LIVE.get(uuid)
    if name in online_names() and live and time.time() - live["t"] < 15:
        return {"live": True, "saved": live["t"], **live["data"]}
    path = os.path.join(world_dir(), "players", "data", uuid + ".dat")
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
                    key = json.dumps({k: v for k, v in data.items() if k not in ("pos", "yaw")}, sort_keys=True)  # la posizione viaggia in "positions"
                    if DATA_SENT.get(uuid) != key:
                        DATA_SENT[uuid] = key
                        HUB.publish("player", payload)
                except Exception:  # noqa: BLE001 - un errore su un giocatore non ferma gli altri
                    pass
            for name in was_online - on:  # appena uscito: il server salva il file, lo rileggo
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
            log("errore giocatori: %s" % e)
        time.sleep(3)


# ---------------------------------------------------------------- sessioni / limiti tentativi
SESSIONS = {}        # sha256(token) -> sessione; su disco solo l'hash, mai il token
SESS_FILE = os.path.join(HERE, "cache", "sessions.json")


def tok_key(tok):
    return hashlib.sha256(tok.encode()).hexdigest()


SESS_LOCK = threading.Lock()


def save_sessions():
    try:
        with SESS_LOCK:
            os.makedirs(os.path.dirname(SESS_FILE), exist_ok=True)
            live = {k: dict(v) for k, v in list(SESSIONS.items()) if v["exp"] > time.time()}
            tmp = SESS_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(live, f)
            os.replace(tmp, SESS_FILE)
    except (OSError, RuntimeError):
        pass


def load_sessions():
    try:
        SESSIONS.update({k: v for k, v in json.load(open(SESS_FILE, encoding="utf-8")).items() if v["exp"] > time.time()})
    except (OSError, ValueError):
        pass
FAILS = {}
LOCKED = {}


def client_id(handler):
    return handler.headers.get("Tailscale-User-Login") or handler.headers.get("X-Forwarded-For") or handler.client_address[0]


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


# ---------------------------------------------------------------- pagine
LOGIN_CSS = """
:root{--bg:#0f1115;--card:#171a20;--line:#272b33;--text:#f0efec;--mute:#9a9ea6;--accent:#3fb950}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:radial-gradient(1200px 600px at 50% -10%,#1d2a1f 0,var(--bg) 60%);color:var(--text);font:16px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{width:100%;max-width:400px;padding:16px}.logo{width:52px;height:52px;border-radius:14px;display:grid;place-items:center;margin-bottom:18px;background:radial-gradient(circle at 50% 30%,#2d4d2f,#15201a 62%,#101418);border:1px solid #2c3a2e;box-shadow:inset 0 1px 0 #ffffff14,0 8px 22px -6px #3fb95066}
.cube{--s:22px;width:var(--s);height:var(--s);position:relative;transform-style:preserve-3d;transform:rotateX(-35.26deg) rotateY(45deg)}
.cube i{position:absolute;inset:0;background-size:100% 100%;image-rendering:pixelated}
.cube .t{background-color:#79c05a;background-blend-mode:multiply;transform:rotateX(90deg) translateZ(calc(var(--s)/2))}
.cube .l{transform:rotateY(-90deg) translateZ(calc(var(--s)/2));filter:brightness(.8)}.cube .r{transform:translateZ(calc(var(--s)/2));filter:brightness(.6)}
h1{font-size:22px;margin:0 0 4px}p{color:var(--mute);margin:0 0 20px;font-size:14px}
form{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:22px}
label{display:block;font-size:13px;color:var(--mute);margin:12px 0 6px}
input{width:100%;padding:12px 14px;border-radius:10px;border:1px solid var(--line);background:#0c0e12;color:var(--text);font-size:16px}
input:focus{outline:2px solid #3fb95055;border-color:var(--accent)}
button{width:100%;padding:13px;border:0;border-radius:10px;font-size:16px;font-weight:650;cursor:pointer;margin-top:18px;background:var(--accent);color:#06210c}
.err{color:#ff8a80;font-size:14px;margin:12px 0 0}
"""


def _logo_html():
    def uri(name):
        try:
            with open(os.path.join(HERE, "assets", "block", name + ".png"), "rb") as f:
                return "url(data:image/png;base64,%s)" % base64.b64encode(f.read()).decode()
        except OSError:
            return "none"
    side = uri("grass_block_side")
    return ("<div class=cube><i class=t style='background-image:%s'></i><i class=l style='background-image:%s'></i>"
            "<i class=r style='background-image:%s'></i></div>" % (uri("grass_block_top"), side, side))


LOGO_HTML = _logo_html()


def login_page(error=""):
    err = '<p class="err">%s</p>' % html.escape(error) if error else ""
    body = """<main><div class=logo>%s</div><h1>%s</h1><p>Accedi per gestire il server.</p>
<form method=post action=/login autocomplete=on>
<label for=u>Utente</label><input id=u name=user autocomplete=username required autofocus>
<label for=p>Password</label><input id=p name=password type=password autocomplete=current-password required>
<button type=submit>Entra</button>%s</form></main>""" % (LOGO_HTML, html.escape(CFG["title"]), err)
    return ("<!doctype html><html lang=it><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            "<title>%s</title><style>%s</style></head><body>%s</body></html>" % (html.escape(CFG["title"]), LOGIN_CSS, body)).encode()


def app_page(sess):
    with open(os.path.join(HERE, "app.html"), encoding="utf-8") as f:
        tpl = f.read()
    boot = {"user": sess["user"], "admin": sess["admin"], "csrf": sess["csrf"], "title": CFG["title"],
            "address": CFG.get("mc_address") or "", "autostop": CFG["auto_stop_minutes"], "cores": NCPU,
            "mc": (CFG.get("user_mc") or {}).get(sess["user"], "")}
    return tpl.replace("{{TITLE}}", html.escape(CFG["title"])).replace("{{BOOT}}", json.dumps(boot).replace("</", "<\\/")).encode()


class Handler(BaseHTTPRequestHandler):
    server_version = "pannello"
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
                         "connect-src 'self'; img-src 'self' data:; form-action 'self'; frame-ancestors 'none'")

    def send_file(self, path, name):
        size = os.path.getsize(path)
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", 'attachment; filename="%s"' % name)
        self.send_header("Cache-Control", "no-store")
        self.security_headers()
        self.end_headers()
        try:
            with open(path, "rb") as f:
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
            if u:  # utente ancora esistente: aggiorna anche il ruolo se e' cambiato
                s["admin"] = bool(u.get("admin"))
                return key, s
        if key and SESSIONS.pop(key, None):
            save_sessions()
        return None, None

    def body(self):
        try:
            n = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            n = 0
        n = max(0, min(n, 65536))   # mai negativo: read(-1) leggerebbe fino alla chiusura
        return self.rfile.read(n).decode("utf-8", "replace") if n else ""

    def do_GET(self):
        self.safe(self._do_GET)

    def do_POST(self):
        self.safe(self._do_POST)

    def safe(self, fn):
        """Una richiesta malformata riceve un 400 invece di chiudere la connessione senza risposta."""
        try:
            fn()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as e:  # noqa: BLE001
            try:
                self.json(400, {"ok": False, "message": "Richiesta non valida."})
            except OSError:
                pass
            log("richiesta non valida %s: %s" % (self.path[:80], type(e).__name__))

    def cookie(self, value, max_age):
        secure = "" if self.headers.get("Host", "").split(":")[0] in ("127.0.0.1", "localhost") else "; Secure"
        return "sess=%s; Path=/; HttpOnly; SameSite=Strict; Max-Age=%d%s" % (value, max_age, secure)

    # ------------------------------------------------ GET
    def _do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            _, s = self.session()
            return self.send(200, app_page(s) if s else login_page())
        _, s = self.session()
        if not s:
            return self.json(401, {"error": "login"})
        if path == "/api/events":
            return self.events(_, s)
        img_cache = {"Cache-Control": "private, max-age=86400"}
        if path == "/api/players":
            return self.json(200, {"players": roster(), "hardcore": hardcore_world()})
        if path == "/api/player":
            uuid = (parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}).get("uuid", [""])[0]
            names = known_players()
            if not UUID_RE.match(uuid) or uuid not in names:
                return self.json(404, {"error": "giocatore sconosciuto"})
            d = player_data(uuid, names[uuid]) or {}
            return self.json(200, {"uuid": uuid, "name": names[uuid], "online": names[uuid] in online_names(),
                                   "slim": PL.skin(uuid)[1], "hardcore": hardcore_world(), "stats": PL.stats(world_dir(), uuid), **d})
        m = re.match(r"^/skin/([0-9a-f-]{36})\.png$", path)
        if m and UUID_RE.match(m.group(1)):
            png, slim = PL.skin(m.group(1))
            return self.send(200, png, "image/png", {"Cache-Control": "private, max-age=3600", "X-Skin-Slim": "1" if slim else "0"})
        m = re.match(r"^/icon/([a-z0-9_]{1,64})\.png$", path)
        if m:
            p = PL.icon_path(m.group(1))
            return self.send(200, open(p, "rb").read(), "image/png", img_cache) if p else self.send(404, b"", "image/png")
        m = re.match(r"^/hud/([a-z0-9_]{1,64})\.png$", path)
        if m:
            p = os.path.join(HERE, "assets", "hud", m.group(1) + ".png")
            return self.send(200, open(p, "rb").read(), "image/png", img_cache) if os.path.exists(p) else self.send(404, b"", "image/png")
        q = parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
        if path == "/api/map":
            dim = q.get("dim", ["overworld"])[0]
            if dim not in mapgen.DIMS:
                return self.json(404, {"error": "dimensione"})
            spawn = None
            try:
                sp = (PL.read_nbt_file(os.path.join(world_dir(), "level.dat"))["Data"].get("spawn") or {})
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
            cache = os.path.join(HERE, "cache", "map")
            tile = os.path.join(cache, dim, "r.%d.%d.png" % (rx, rz))
            mca = os.path.join(world_dir(), "dimensions", "minecraft", dim, "region", "r.%d.%d.mca" % (rx, rz))
            try:
                fresh = os.path.getmtime(tile) >= os.path.getmtime(mca)
            except OSError:
                fresh = False
            try:
                if fresh:          # gia' pronta: niente attesa dietro le tile in generazione
                    p = tile
                else:
                    with MAP_SEM:
                        if WORLD_BUSY.is_set():   # ripristino partito mentre aspettavamo
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
            return self.json(200, {"sessions": TLINE.sessions(limit=qint(q, "limit", 30, 1, 200))})
        if path in ("/api/backups", "/api/backups/download", "/api/access", "/api/audit") and not s["admin"]:
            return self.json(403, {"error": "solo admin"})
        if path == "/api/access":
            p = server_props()
            return self.json(200, {**AC.snapshot(CFG["server_dir"]), "online": sorted(online_names()), "state": SRV.state,
                                   "white_list": p.get("white-list") == "true", "enforce": p.get("enforce-whitelist") == "true",
                                   "known": [{"uuid": r["uuid"], "name": r["name"]} for r in roster()]})
        if path == "/api/audit":
            entries = RG.read(CFG["log_file"])
            items, total, counts = RG.query(entries, cat=q.get("cat", [""])[0] or None,
                                            user=q.get("user", [""])[0] or None, q=q.get("q", [""])[0][:80],
                                            offset=qint(q, "offset", 0, 0, 10 ** 7), limit=qint(q, "limit", 100, 1, 500))
            return self.json(200, {"entries": items, "total": total, "counts": counts,   # utenti dal log: anche i cancellati
                                   "users": sorted({e["user"] for e in entries if e["user"]}), "mc": CFG.get("user_mc") or {}})
        if path == "/api/backups":
            return self.json(200, {"items": BACKUPS.list(), "disk": BACKUPS.disk(), "status": BACKUPS.status()})
        if path == "/api/backups/download":
            try:
                p = BACKUPS.path(q.get("id", [""])[0])
                return self.send_file(p, re.sub(r"[^\w.\-]", "_", os.path.basename(p), flags=re.ASCII))
            except (ValueError, OSError) as e:
                return self.json(404, {"error": str(e)})
        if path == "/api/properties":
            if not s["admin"]:
                return self.json(403, {"error": "solo admin"})
            items = [{"key": k, "value": "" if k in SECRET_KEYS else v, "secret": k in SECRET_KEYS, "locked": k in LOCKED_KEYS}
                     for k, v in server_props().items()]
            return self.json(200, {"items": items})
        self.json(404, {"error": "non trovato"})

    def events(self, tok, s):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.security_headers()
        self.end_headers()
        q = HUB.subscribe(s["admin"])
        try:
            recent = [e for e in TLINE.events(limit=40) if s["admin"] or e.get("type") != "panel"][:25]
            admin0 = s["admin"]
            init = {"status": SRV.snapshot(), "metrics": list(METRICS), "heap": list(HEAP), "audit": list(AUDIT) if admin0 else [],
                    "console": list(CONSOLE) if admin0 else [], "timeline": recent,
                    # age: quanto e' vecchia la lettura, cosi' il browser non deve confrontare il suo orologio col nostro
                    "world": (lambda w: dict(w, age=int(time.time() * 1000) - w["t"]) if w else {})(WORLD)}
            self.wfile.write(("retry: 3000\nevent: init\ndata: %s\n\n" % json.dumps(init, separators=(",", ":"))).encode())
            self.wfile.flush()
            # se l'utente viene cancellato o cambia ruolo si chiude: il browser si ricollega con i permessi nuovi
            while s["exp"] > time.time() and tok in SESSIONS and bool((load_users().get(s["user"]) or {"admin": None}).get("admin")) == admin0 \
                    and s["user"] in load_users():
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
        if self.path == "/login":
            f = {k: v[0] for k, v in parse_qs(self.body()).items()}
            user = f.get("user", "").strip().lower(); keys = ["u:" + user, "c:" + client_id(self)]
            if locked(keys):
                return self.send(429, login_page("Troppi tentativi: riprova tra 15 minuti."))
            u = check_pw(user, f.get("password", ""))
            if not u:
                note_fail(keys)
                log("accesso fallito per '%s' da %s" % (user[:32], client_id(self)))
                return self.send(401, login_page("Utente o password sbagliati."))
            tok = secrets.token_urlsafe(32)
            SESSIONS[tok_key(tok)] = {"user": user, "admin": bool(u.get("admin")), "csrf": secrets.token_urlsafe(24), "exp": time.time() + SESSION_HOURS * 3600}
            save_sessions()
            log("%s ha fatto l'accesso da %s" % (user, client_id(self)))
            return self.redirect("/", self.cookie(tok, SESSION_HOURS * 3600))
        tok, s = self.session()
        if self.path == "/logout":
            f = {k: v[0] for k, v in parse_qs(self.body()).items()}
            if s and hmac.compare_digest(f.get("csrf", "") or self.headers.get("X-CSRF", ""), s["csrf"]):
                SESSIONS.pop(tok, None)
                save_sessions()
            return self.redirect("/", self.cookie("", 0))
        if not s:
            return self.json(401, {"ok": False, "message": "Sessione scaduta, rientra."})
        if not hmac.compare_digest(self.headers.get("X-CSRF", ""), s["csrf"]):
            return self.json(403, {"ok": False, "message": "Richiesta non valida."})
        try:
            data = json.loads(self.body() or "{}")
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        if self.path == "/api/markers":
            try:
                name = str(data.get("name", "")).strip()[:40]
                dim = str(data.get("dim", "overworld"))
                x, z = int(float(data["x"])), int(float(data["z"]))
                icon = str(data.get("icon", "pin"))
                if icon not in ("pin", "home", "portal", "star", "danger"):
                    icon = "pin"
            except (KeyError, ValueError, TypeError, OverflowError):
                return self.json(200, {"ok": False, "message": "Segnaposto non valido."})
            if not name or dim not in mapgen.DIMS or abs(x) > 30_000_000 or abs(z) > 30_000_000:
                return self.json(200, {"ok": False, "message": "Segnaposto non valido."})
            with LOCK:
                ms = load_markers()
                if len(ms) >= 300:
                    return self.json(200, {"ok": False, "message": "Troppi segnaposti."})
                mk = {"id": secrets.token_hex(6), "name": name, "dim": dim, "x": x, "z": z, "icon": icon, "by": s["user"], "t": int(time.time())}
                ms.append(mk); save_markers(ms)
            log("%s ha aggiunto il segnaposto '%s' (%d, %d)" % (s["user"], name, x, z))
            HUB.publish("markers", {"dim": dim})
            return self.json(200, {"ok": True, "message": "Segnaposto salvato.", "marker": mk})
        if self.path == "/api/markers/delete":
            mid = str(data.get("id", ""))
            with LOCK:
                ms = load_markers(); keep = [m for m in ms if m.get("id") != mid]
                if len(keep) == len(ms):
                    return self.json(200, {"ok": False, "message": "Segnaposto non trovato."})
                save_markers(keep)
            gone = next(m for m in ms if m.get("id") == mid)
            log("%s ha tolto il segnaposto '%s'" % (s["user"], gone.get("name")))
            HUB.publish("markers", {"dim": gone.get("dim")})
            return self.json(200, {"ok": True, "message": "Segnaposto tolto."})
        if self.path == "/api/start":
            ok, msg = SRV.start(s["user"])
            return self.json(200, {"ok": ok, "message": msg})
        if self.path in ("/api/stop", "/api/restart"):
            info = SRV.info
            if not s["admin"] and info and info[0] > 0:
                return self.json(200, {"ok": False, "message": "Ci sono giocatori online: solo un admin puo' farlo adesso."})
            ok, msg = SRV.stop(s["user"], restart=self.path == "/api/restart")
            return self.json(200, {"ok": ok, "message": msg})
        if not s["admin"]:
            return self.json(403, {"ok": False, "message": "Solo per admin."})
        if self.path == "/api/command":
            cmd = str(data.get("command", "")).strip().lstrip("/")
            if not cmd or len(cmd) > 400 or "\n" in cmd:
                return self.json(200, {"ok": False, "message": "Comando non valido."})
            if SRV.state != "on":
                return self.json(200, {"ok": False, "message": "Il server e' spento."})
            console_push(["> " + cmd], "cmd")
            try:
                out = RCON.command(cmd)
            except Exception as e:  # noqa: BLE001
                console_push(["Errore RCON: %s" % e], "err")
                return self.json(200, {"ok": False, "message": "RCON non disponibile: %s" % e})
            if out:
                console_push(re.sub(r"§.", "", out).splitlines() or [out], "resp")
            log("%s ha eseguito: %s" % (s["user"], cmd[:120]))
            return self.json(200, {"ok": True, "message": ""})
        if self.path == "/api/access":
            action, name = str(data.get("action", "")), str(data.get("name", "")).strip()
            if action not in AC.ACTIONS or not AC.NAME_RE.match(name):
                return self.json(200, {"ok": False, "message": "Nome non valido: 3-16 caratteri, lettere, numeri e _."})
            ok, msg = access_action(s["user"], action, name, str(data.get("reason", "")))
            return self.json(200, {"ok": ok, "message": msg})
        if self.path.startswith("/api/backups/"):
            return self.backup_post(self.path[len("/api/backups/"):], data, s["user"])
        if self.path == "/api/properties":
            changes = data.get("changes") or {}
            current = server_props()
            bad = [k for k in changes if k not in current or k in LOCKED_KEYS]
            if bad:
                return self.json(200, {"ok": False, "message": "Non modificabili: " + ", ".join(bad)})
            clean = {}
            for k, v in changes.items():
                v = str(v)
                if "\n" in v or "\r" in v or len(v) > 512:
                    return self.json(200, {"ok": False, "message": "Valore non valido per %s" % k})
                clean[k] = v
            done = write_props(clean, s["user"])
            if data.get("restart") and SRV.state == "on" and done:
                SRV.stop(s["user"], "nuove impostazioni", restart=True)
            HUB.publish("status", SRV.snapshot())
            n = len(done)
            return self.json(200, {"ok": True, "message": ("%d impostazioni salvate" % n) if n else "Nessuna modifica"})
        self.json(404, {"ok": False, "message": "non trovato"})


    def backup_post(self, op, data, who):
        bid = str(data.get("id", ""))
        busy = backup_busy()
        try:
            if op == "create":
                note = str(data.get("note", "")).strip()[:80]
                with LOCK:   # stato del server e prenotazione insieme: start() non puo' infilarsi in mezzo
                    if busy or OP_PENDING.is_set():
                        return self.json(200, {"ok": False, "message": "Aspetta: %s in corso." % (busy or "operazione").lower()})
                    st = SRV.state
                    if st in ("starting", "stopping") or (st == "off" and pid_alive(SRV.last_pid)):
                        return self.json(200, {"ok": False, "message": "Il server si sta accendendo o spegnendo: riprova tra poco."})
                    OP_PENDING.set()
                BACKUPS.world_dir = world_dir()
                backup_job(who, "backup manuale", lambda: BACKUPS.create("manuale", st == "on", backup_rcon, note),
                           lambda e: "Backup pronto: %s (%s MB)." % (e["name"], round(e["size"] / 1048576)))
                return self.json(200, {"ok": True, "message": "Backup avviato" + (": il server resta acceso." if st == "on" else ".")})
            if op == "restore":
                if data.get("confirm") != "RIPRISTINA":
                    return self.json(200, {"ok": False, "message": "Conferma mancante."})
                name = next((e["name"] for e in BACKUPS.list() if e["id"] == bid), None)
                if not name:
                    return self.json(200, {"ok": False, "message": "Backup non trovato."})
                with LOCK:
                    if busy or OP_PENDING.is_set():
                        return self.json(200, {"ok": False, "message": "Aspetta: %s in corso." % (busy or "operazione").lower()})
                    if SRV.state != "off" or pid_alive(SRV.last_pid):
                        return self.json(200, {"ok": False, "message": "Spegni prima il server."})
                    OP_PENDING.set()
                BACKUPS.world_dir = world_dir()
                log("%s ha avviato il ripristino di %s" % (who, name))
                backup_job(who, "ripristino di " + name, lambda: do_restore(bid),
                           lambda r: "Mondo ripristinato da %s. Quello di prima è in mondi-vecchi\\%s." % (name, r["moved_to"]))
                return self.json(200, {"ok": True, "message": "Ripristino avviato."})
            if op == "delete":
                name = next((e["name"] for e in BACKUPS.list() if e["id"] == bid), None)
                BACKUPS.delete(bid)
                log("%s ha spostato nel Cestino il backup %s" % (who, name))
                return self.json(200, {"ok": True, "message": "Spostato nel Cestino."})
            if op == "pin":
                BACKUPS.pin(bid, bool(data.get("pinned")))
                return self.json(200, {"ok": True, "message": "Fissato: non verrà cancellato in automatico." if data.get("pinned") else "Fissaggio tolto."})
            if op == "schedule":
                cfg = BACKUPS.set_schedule(data.get("schedule") if isinstance(data.get("schedule"), dict) else {})
                log("%s ha cambiato la programmazione dei backup" % who)
                return self.json(200, {"ok": True, "message": "Programmazione salvata.", "schedule": cfg})
        except ValueError as e:
            return self.json(200, {"ok": False, "message": str(e)})
        except OSError as e:
            return self.json(200, {"ok": False, "message": "Errore sul disco: %s" % e})
        return self.json(404, {"ok": False, "message": "non trovato"})


class Server(ThreadingHTTPServer):
    daemon_threads = True


# ---------------------------------------------------------------- cli
def cli(args):
    users = load_users()
    cmd = args[0]
    if cmd == "users":
        for n, u in sorted(users.items()):
            print("%s%s" % (n, "  (admin)" if u.get("admin") else ""))
        return
    if cmd in ("adduser", "passwd"):
        name = args[1].strip().lower()
        if cmd == "passwd" and name not in users:
            sys.exit("utente '%s' inesistente" % name)
        pw = os.environ.get("PANEL_PASSWORD")
        if not pw:
            pw = getpass.getpass("Password per %s: " % name)
            if pw != getpass.getpass("Ripeti la password: "):
                sys.exit("le password non coincidono")
        if len(pw) < 10:
            sys.exit("password troppo corta: almeno 10 caratteri")
        salt, h, rounds = hash_pw(pw)
        admin = ("--admin" in args) if cmd == "adduser" else users[name].get("admin", False)
        users[name] = {"salt": salt, "hash": h, "rounds": rounds, "admin": admin}
        save_users(users)
        print("utente %s salvato%s" % (name, " come admin" if admin else ""))
        return
    if cmd == "deluser":
        users.pop(args[1].strip().lower(), None); save_users(users); print("rimosso")
        return
    sys.exit(__doc__)


def main():
    if len(sys.argv) > 1:
        return cli(sys.argv[1:])
    if not load_users():
        sys.exit("Nessun utente: crea prima un utente con  python panel.py adduser NOME --admin")
    load_audit()
    load_sessions()
    for target in (sampler, tailers, player_watcher, map_warmer, backup_scheduler, world_watcher):
        threading.Thread(target=target, daemon=True).start()
    srv = Server((CFG["listen"], CFG["port"]), Handler)
    log("pannello avviato su http://%s:%d" % (CFG["listen"], CFG["port"]))
    srv.serve_forever()


if __name__ == "__main__":
    main()
