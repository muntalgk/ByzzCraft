"""ByzzCraft: pannello web self-hosted per un server Minecraft (solo libreria standard di Python, Windows e Linux).

Avvio:            python panel.py     poi apri http://127.0.0.1:8765 su questa macchina: al primo accesso
                  si sceglie la cartella del server e si crea l'admin (per l'accesso remoto usa un proxy HTTPS)
Utenti:           python panel.py adduser NOME [--admin]
                  python panel.py passwd NOME
                  python panel.py deluser NOME
                  python panel.py users
Asset:            python panel.py assets [VERSIONE]   (texture e traduzioni scaricate da Mojang)
"""
import base64, getpass, hashlib, hmac, html, json, os, queue, re, secrets, shutil, socket, struct, subprocess, sys, threading, time
from collections import deque
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
import assets as AS  # noqa: E402
import i18n  # noqa: E402
from i18n import t  # noqa: E402
from sysinfo import NCPU, ProcSampler, listening_pid, pid_alive, system_memory, system_times  # noqa: E402
CONFIG_PATH = os.environ.get("PANEL_CONFIG", os.path.join(HERE, "panel-config.json"))
DEFAULTS = {
    "server_dir": os.path.dirname(HERE),
    "start_script": "start.bat" if os.name == "nt" else "start.sh",
    "listen": "127.0.0.1",
    "port": 8765,
    "auto_stop_minutes": 30,      # spegne il server se resta vuoto per N minuti (0 = mai)
    "hide_console": True,         # avvio dal pannello senza finestre: log e comandi stanno nella dashboard
    "start_timeout_seconds": 240,
    "title": "ByzzCraft",
    "lang": "en",                 # lingua dell'interfaccia: en, it
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


USER_RE = re.compile(r"^[a-z0-9_.-]{1,32}$")   # stessi caratteri che registro.py riconosce nei log
RESERVED = {"panel", "init"}


def mc_names():
    """utente del pannello -> nick Minecraft (faccia nella barra laterale): dal file utenti, poi da user_mc in config."""
    return {**(CFG.get("user_mc") or {}), **{n: u["mc"] for n, u in load_users().items() if u.get("mc")}}


def set_password(users, name, pw):
    salt, h, rounds = hash_pw(pw)
    users[name] = {**users.get(name, {}), "salt": salt, "hash": h, "rounds": rounds}


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
    for k in changes:   # chiave assente (server.properties nuovo o incompleto): Minecraft la tiene
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
            raise RuntimeError(t("RCON is not enabled in server.properties"))
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
            ok, _ = self.start("panel (restart)")
            if not ok and backup_busy():
                self.restart_pending = True   # riprova a fine backup

        return changed

    def start(self, who):
        with LOCK:
            if self.state in ("starting", "on", "stopping"):
                return False, t("The server is already on or starting.")
            if pid_alive(self.last_pid):
                return False, t("The server is still shutting down: try again in a few seconds.")
            busy = backup_busy()
            if busy or OP_PENDING.is_set():
                return False, t("Wait for it to finish: %s in progress.") % (busy or t("backup operation")).lower()
            env = dict(os.environ, NOPAUSE="1")
            script = os.path.join(CFG["server_dir"], CFG["start_script"])
            io = dict(cwd=CFG["server_dir"], env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if os.name == "nt":
                flags = subprocess.CREATE_NO_WINDOW if CFG.get("hide_console") else subprocess.CREATE_NEW_CONSOLE
                self.proc = subprocess.Popen(["cmd.exe", "/c", script], creationflags=flags, **io)
            else:
                # sessione propria: il server non si ferma se il pannello viene riavviato
                self.proc = subprocess.Popen(["sh", script], start_new_session=True, **io)
            self.state, self.started_at = "starting", time.time()
        log("%s started the server" % who)
        HUB.publish("status", self.snapshot())
        return True, t("Starting: world backup and loading.")

    def stop(self, who, reason=None, restart=False):
        with LOCK:
            if self.state != "on":
                return False, t("The server is not on.")
            busy = BACKUPS.status()["busy"]
            if busy and busy["op"] == "backup":   # lo spegnimento a meta' copia darebbe un backup incoerente
                return False, t("Wait a few seconds: backup in progress.")
            self.state = "stopping"
            self.restart_pending = restart
        try:
            RCON.command("stop")
        except Exception as e:  # noqa: BLE001
            with LOCK:
                self.state = "on"; self.restart_pending = False
            log("%s: stop failed (%s)" % (who, e))
            return False, t("Could not stop the server: %s") % e
        log("%s %s the server%s" % (who, "restarted" if restart else "stopped", " (%s)" % reason if reason else ""))
        HUB.publish("status", self.snapshot())
        return True, t("Restarting.") if restart else t("Stopping.")


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
                SRV.stop("panel", "empty for %d minutes" % mins)
        except Exception as e:  # noqa: BLE001
            log("sampling error: %s" % e)
        time.sleep(max(0.2, SAMPLE_SECONDS - (time.time() - t0)))


TLINE = TL.Timeline(CFG["server_dir"], os.path.join(HERE, "cache"), CFG["log_file"])
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
        return False, t("The server is starting or stopping: try again shortly.")
    if st == "on":
        try:
            out = re.sub(r"§.", "", RCON.command(AC.command(action, name, reason))).strip()
        except Exception as e:  # noqa: BLE001
            return False, t("RCON unavailable: %s") % e
        # risposte del server in inglese: "Added X to the whitelist", "Made X a server operator", ...
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
            prof = AC.resolve(CFG["server_dir"], action, name, known={p["uuid"]: p["name"] for p in roster()})
            with LOCK:   # start() prende lo stesso LOCK: il server non puo' partire mentre scriviamo i file
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


def backup_job(who, what, fn, done_msg, fail_msg):
    """Le operazioni lunghe girano in un thread: l'esito arriva alla pagina come evento.
    Chi chiama ha gia' impostato OP_PENDING (sotto LOCK, insieme ai controlli sullo stato del server)."""
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
            log("map: %d tiles not updated this round, retrying in 5 minutes" % errors)
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
            log("log read error: %s" % e)
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
            log("players error: %s" % e)
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


def drop_sessions(user, keep=None):
    """Chiude le sessioni di un utente (password cambiata, utente eliminato), tranne keep."""
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


def client_id(handler):
    ip = handler.client_address[0]
    if ip not in LOOPBACK:
        return ip   # header di proxy validi solo da un proxy locale (tailscale serve, nginx): altrimenti falsificabili
    return handler.headers.get("Tailscale-User-Login") or handler.headers.get("X-Forwarded-For") or ip


LOOPBACK = ("127.0.0.1", "::1", "::ffff:127.0.0.1")
PROXY_HEADERS = ("X-Forwarded-For", "X-Real-IP", "Forwarded", "X-Forwarded-Host", "Tailscale-User-Login")


def is_local(handler):
    """Browser aperto su questa macchina. Tailscale Serve e nginx arrivano anche loro da 127.0.0.1, ma con i loro header."""
    return handler.client_address[0] in LOOPBACK and not any(handler.headers.get(h) for h in PROXY_HEADERS)


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
.logo svg{width:32px;height:32px}
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
# PNG e non SVG: Safari (anche sulla schermata Home dell'iPhone) non usa le icone SVG
LOGO_ICON = "<link rel=icon type=image/png href=/favicon.ico><link rel=apple-touch-icon href=/apple-touch-icon.png>"
ICONS = {"/favicon.ico": "icon-32.png", "/apple-touch-icon.png": "icon-180.png"}


def login_page(error=""):
    err = '<p class="err">%s</p>' % html.escape(error) if error else ""
    body = """<main><div class=logo>%s</div><h1>%s</h1><p>%s</p>
<form method=post action=/login autocomplete=on>
<label for=u>%s</label><input id=u name=user autocomplete=username required autofocus>
<label for=p>%s</label><input id=p name=password type=password autocomplete=current-password required>
<button type=submit>%s</button>%s</form></main>""" % (LOGO_SVG, html.escape(CFG["title"]), html.escape(t("Log in to manage the server.")),
                                                    html.escape(t("User")), html.escape(t("Password")), html.escape(t("Log in")), err)
    return ("<!doctype html><html lang=" + i18n.LANG + "><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            "<title>%s</title>%s<style>%s</style></head><body>%s</body></html>" % (html.escape(CFG["title"]), LOGO_ICON, LOGIN_CSS, body)).encode()


def _page(body):
    return ("<!doctype html><html lang=" + i18n.LANG + "><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            "<title>%s</title>%s<style>%s</style></head><body>%s</body></html>" % (html.escape(CFG["title"]), LOGO_ICON, LOGIN_CSS, body)).encode()


def guess_server_dir():
    for d in (CFG["server_dir"], os.path.dirname(HERE), os.getcwd()):
        if any(os.path.isfile(os.path.join(d, f)) for f in ("server.jar", "server.properties")):
            return d
    return ""


def setup_page(error="", form=None):
    f = form or {}
    e = html.escape
    err = '<p class="err">%s</p>' % e(error) if error else ""
    lang = f.get("lang", i18n.LANG)
    opts = "".join("<option value=%s%s>%s</option>" % (k, " selected" if k == lang else "", v) for k, v in (("en", "English"), ("it", "Italiano")))
    body = """<main><div class=logo>%s</div><h1>%s</h1><p>%s</p>
<form method=post action=/setup>
<label for=d>%s</label><input id=d name=server_dir value="%s" required placeholder="/home/me/minecraft">
<label for=l>%s</label><select id=l name=lang>%s</select>
<label for=u>%s</label><input id=u name=user autocomplete=username required pattern="[a-z0-9_.\\-]{1,32}" value="%s">
<label for=p>%s</label><input id=p name=password type=password autocomplete=new-password required minlength=10>
<label for=p2>%s</label><input id=p2 name=password2 type=password autocomplete=new-password required minlength=10>
<label class=chk><input type=checkbox name=eula value=1> %s <a href="https://aka.ms/MinecraftEULA" target=_blank rel=noopener>EULA</a></label>
<button type=submit>%s</button>%s</form></main>""" % (
        LOGO_SVG, e(t("Welcome to ByzzCraft")), e(t("First setup: this page only opens on the server machine.")),
        e(t("Minecraft server folder")), e(f.get("server_dir", guess_server_dir())), e(t("Language")), opts,
        e(t("Admin user")), e(f.get("user", "")), e(t("Password (at least 10 characters)")), e(t("Repeat the password")),
        e(t("I accept the Minecraft")), e(t("Set up the panel")), err)
    return _page(body)


def setup_remote_page():
    url = "http://127.0.0.1:%d" % CFG["port"]
    return _page("<main><div class=logo>%s</div><h1>%s</h1><p>%s</p></main>" % (
        LOGO_SVG, html.escape(t("Panel not set up yet")),
        html.escape(t("Open %s in a browser on the server machine to create the admin user.") % url)))


def prepare_server(sd, accept_eula):
    """Cartella del server pronta per il pannello: RCON attivo, EULA se accettata, script di avvio d'esempio."""
    props = os.path.join(sd, "server.properties")
    if not os.path.exists(props):
        open(props, "w").close()   # server mai avviato: Minecraft completa il file al primo avvio
    p = server_props()
    if p.get("enable-rcon") != "true" or not p.get("rcon.password"):
        write_props({"enable-rcon": "true", "rcon.port": p.get("rcon.port") or "25575",
                     "rcon.password": p.get("rcon.password") or secrets.token_urlsafe(24)}, "setup")
    eula = os.path.join(sd, "eula.txt")
    if accept_eula and "eula=true" not in (open(eula, encoding="utf-8").read() if os.path.exists(eula) else ""):
        with open(eula, "w", encoding="utf-8") as f:
            f.write("# accepted in the ByzzCraft setup (https://aka.ms/MinecraftEULA)\neula=true\n")
    script = os.path.join(sd, CFG["start_script"])
    example = os.path.join(HERE, "examples", CFG["start_script"])
    if not os.path.exists(script) and os.path.exists(example):
        shutil.copy(example, script)


def restart_soon():
    """Dopo il primo avvio si riparte con la nuova config (cartella del server, lingua, thread di lavoro)."""
    time.sleep(1)
    if os.name == "nt":
        subprocess.Popen([sys.executable] + sys.argv, cwd=os.getcwd())   # il nuovo processo riprova la porta finche' questo esce
        os._exit(0)
    os.execv(sys.executable, [sys.executable] + sys.argv)   # stesso PID: systemd non se ne accorge


def download_assets():
    try:
        AS.install(CFG["server_dir"], log=log)
    except Exception as e:  # noqa: BLE001
        log("assets error: %s" % e)


def app_page(sess):
    with open(os.path.join(HERE, "app.html"), encoding="utf-8") as f:
        tpl = f.read()
    boot = {"user": sess["user"], "admin": sess["admin"], "csrf": sess["csrf"], "title": CFG["title"],
            "address": CFG.get("mc_address") or "", "autostop": CFG["auto_stop_minutes"], "cores": NCPU,
            "mc": mc_names().get(sess["user"], ""), "lang": i18n.LANG, "i18n": i18n.TABLE}
    tpl = tpl.replace("{{TITLE}}", html.escape(CFG["title"])).replace("{{LANG}}", i18n.LANG)
    tpl = tpl.replace("{{LOGO}}", LOGO_SVG).replace("{{ICON}}", LOGO_ICON)
    # {{testo}} nell'HTML statico: tradotto qui; nel JavaScript si usa t("testo")
    tpl = re.sub(r"\{\{([^{}]+)\}\}", lambda m: m.group(0) if m.group(1) == "BOOT" else html.escape(t(m.group(1))), tpl)
    return tpl.replace("{{BOOT}}", json.dumps(boot).replace("</", "<\\/")).encode()


class Handler(BaseHTTPRequestHandler):
    server_version = "byzzcraft"
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
                self.json(400, {"ok": False, "message": t("Invalid request.")})
            except OSError:
                pass
            log("invalid request %s: %s" % (self.path[:80], type(e).__name__))

    def cookie(self, value, max_age):
        secure = "" if self.headers.get("Host", "").split(":")[0] in ("127.0.0.1", "localhost") else "; Secure"
        return "sess=%s; Path=/; HttpOnly; SameSite=Strict; Max-Age=%d%s" % (value, max_age, secure)

    # ------------------------------------------------ GET
    def _do_GET(self):
        path = self.path.split("?")[0]
        if path in ICONS:
            with open(os.path.join(HERE, "assets", ICONS[path]), "rb") as f:
                return self.send(200, f.read(), "image/png", {"Cache-Control": "public, max-age=86400"})
        if path == "/" and not load_users():
            return self.send(200, setup_page() if is_local(self) else setup_remote_page())
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
        if path in ("/api/backups", "/api/backups/download", "/api/access", "/api/audit", "/api/users") and not s["admin"]:
            return self.json(403, {"error": "solo admin"})
        if path == "/api/access":
            p = server_props()
            return self.json(200, {**AC.snapshot(CFG["server_dir"]), "online": sorted(online_names()), "state": SRV.state,
                                   "white_list": p.get("white-list") == "true", "enforce": p.get("enforce-whitelist") == "true",
                                   "known": [{"uuid": r["uuid"], "name": r["name"]} for r in roster()]})
        if path == "/api/users":
            return self.json(200, {"users": [{"name": n, "admin": bool(u.get("admin")), "mc": u.get("mc") or (CFG.get("user_mc") or {}).get(n, ""),
                                              "me": n == s["user"]} for n, u in sorted(load_users().items())]})
        if path == "/api/audit":
            entries = RG.read(CFG["log_file"])
            items, total, counts = RG.query(entries, cat=q.get("cat", [""])[0] or None,
                                            user=q.get("user", [""])[0] or None, q=q.get("q", [""])[0][:80],
                                            offset=qint(q, "offset", 0, 0, 10 ** 7), limit=qint(q, "limit", 100, 1, 500))
            return self.json(200, {"entries": items, "total": total, "counts": counts,   # utenti dal log: anche i cancellati
                                   "users": sorted({e["user"] for e in entries if e["user"]}), "mc": mc_names()})
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
        if self.path == "/setup":
            return self.first_setup()
        if self.path == "/login":
            f = {k: v[0] for k, v in parse_qs(self.body()).items()}
            user = f.get("user", "").strip().lower(); keys = ["u:" + user, "c:" + client_id(self)]
            if locked(keys):
                return self.send(429, login_page(t("Too many attempts: try again in 15 minutes.")))
            u = check_pw(user, f.get("password", ""))
            if not u:
                note_fail(keys)
                log("login failed for '%s' from %s" % (user[:32], client_id(self)))
                return self.send(401, login_page(t("Wrong user or password.")))
            tok = secrets.token_urlsafe(32)
            SESSIONS[tok_key(tok)] = {"user": user, "admin": bool(u.get("admin")), "csrf": secrets.token_urlsafe(24), "exp": time.time() + SESSION_HOURS * 3600}
            save_sessions()
            log("%s logged in from %s" % (user, client_id(self)))
            return self.redirect("/", self.cookie(tok, SESSION_HOURS * 3600))
        tok, s = self.session()
        if self.path == "/logout":
            f = {k: v[0] for k, v in parse_qs(self.body()).items()}
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
                if "\n" in v or "\r" in v or len(v) > 512:
                    return self.json(200, {"ok": False, "message": t("Invalid value for %s") % k})
                clean[k] = v
            done = write_props(clean, s["user"])
            if data.get("restart") and SRV.state == "on" and done:
                SRV.stop(s["user"], "nuove impostazioni", restart=True)
            HUB.publish("status", SRV.snapshot())
            n = len(done)
            return self.json(200, {"ok": True, "message": (t("%d settings saved") % n) if n else t("No changes")})
        self.json(404, {"ok": False, "message": t("not found")})


    def backup_post(self, op, data, who):
        bid = str(data.get("id", ""))
        busy = backup_busy()
        try:
            if op == "create":
                note = str(data.get("note", "")).strip()[:80]
                with LOCK:   # stato del server e prenotazione insieme: start() non puo' infilarsi in mezzo
                    if busy or OP_PENDING.is_set():
                        return self.json(200, {"ok": False, "message": t("Wait: %s in progress.") % (busy or t("operation")).lower()})
                    st = SRV.state
                    if st in ("starting", "stopping") or (st == "off" and pid_alive(SRV.last_pid)):
                        return self.json(200, {"ok": False, "message": t("The server is starting or stopping: try again shortly.")})
                    OP_PENDING.set()
                BACKUPS.world_dir = world_dir()
                backup_job(who, "manual backup", lambda: BACKUPS.create("manuale", st == "on", backup_rcon, note),
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
                           lambda r: t("World restored from %s. The previous one is in %s.") % (name, os.path.join("mondi-vecchi", r["moved_to"])),
                           t("Restore failed: %s"))
                return self.json(200, {"ok": True, "message": t("Restore started.")})
            if op == "delete":
                name = next((e["name"] for e in BACKUPS.list() if e["id"] == bid), None)
                BACKUPS.delete(bid)
                log("%s moved backup %s to the trash" % (who, name))
                return self.json(200, {"ok": True, "message": t("Moved to the trash.")})
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
        """Crea o modifica un utente (solo admin). Password vuota in modifica = invariata."""
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
            drop_sessions(name, keep=tok)
        log("%s %s user %s" % (s["user"], "created" if new else "updated", name))
        return self.json(200, {"ok": True, "message": (t("User %s created.") if new else t("User %s saved.")) % name})

    def first_setup(self):
        """Primo avvio: solo senza utenti e solo da un browser su questa macchina."""
        if load_users() or not is_local(self):
            return self.send(403, setup_remote_page())
        f = {k: v[0] for k, v in parse_qs(self.body()).items()}
        sd = os.path.abspath(os.path.expanduser(f.get("server_dir", "").strip().strip('"')))
        name, pw = f.get("user", "").strip().lower(), f.get("password", "")
        if not any(os.path.isfile(os.path.join(sd, x)) for x in ("server.jar", "server.properties")):
            return self.send(200, setup_page(t("No server.jar in %s.") % sd, f))
        if not USER_RE.match(name) or name in RESERVED:
            return self.send(200, setup_page(t("Invalid user name: lowercase letters, digits, . _ -"), f))
        if len(pw) < 10 or pw != f.get("password2"):
            return self.send(200, setup_page(t("The passwords are too short or do not match."), f))
        cfg = {}
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, encoding="utf-8") as fh:
                cfg = json.load(fh)
        cfg.update({"server_dir": sd, "lang": f.get("lang") if f.get("lang") in ("en", "it") else "en"})
        with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2, ensure_ascii=False)
        CFG["server_dir"] = sd
        i18n.load(cfg["lang"])   # la prima pagina la serve ancora questo processo, prima del riavvio
        prepare_server(sd, f.get("eula") == "1")
        users = {}
        set_password(users, name, pw)
        users[name]["admin"] = True
        save_users(users)
        tok = secrets.token_urlsafe(32)
        SESSIONS[tok_key(tok)] = {"user": name, "admin": True, "csrf": secrets.token_urlsafe(24), "exp": time.time() + SESSION_HOURS * 3600}
        save_sessions()
        log("%s completed the first setup" % name)
        self.redirect("/", self.cookie(tok, SESSION_HOURS * 3600))
        threading.Thread(target=restart_soon, daemon=True).start()


class Server(ThreadingHTTPServer):
    daemon_threads = True


# ---------------------------------------------------------------- cli
def cli(args):
    users = load_users()
    cmd = args[0]
    if cmd == "assets":
        return AS.install(CFG["server_dir"], args[1] if len(args) > 1 else None)
    if cmd == "users":
        for n, u in sorted(users.items()):
            print("%s%s" % (n, "  (admin)" if u.get("admin") else ""))
        return
    if cmd in ("adduser", "passwd"):
        name = args[1].strip().lower()
        if not USER_RE.match(name) or name in RESERVED:
            sys.exit("invalid user name: lowercase letters, digits, . _ -")
        if cmd == "passwd" and name not in users:
            sys.exit("no such user '%s'" % name)
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
        users.pop(args[1].strip().lower(), None); save_users(users); print("removed")
        return
    sys.exit(__doc__)


def main():
    if len(sys.argv) > 1:
        return cli(sys.argv[1:])
    url = "http://127.0.0.1:%d" % CFG["port"]
    load_sessions()
    if load_users():
        load_audit()
        threads = [sampler, tailers, player_watcher, map_warmer, backup_scheduler, world_watcher]
        if not AS.present():
            threads.append(download_assets)
        for target in threads:
            threading.Thread(target=target, daemon=True).start()
    elif sys.stdout:   # pythonw non ha console
        print("ByzzCraft: first run, open %s on this machine to set it up" % url)
    for attempt in range(10):   # dopo il primo avvio il processo vecchio puo' tenere la porta ancora un attimo
        try:
            srv = Server((CFG["listen"], CFG["port"]), Handler)
            break
        except OSError:
            if attempt == 9:
                raise
            time.sleep(1)
    log("panel started on http://%s:%d" % (CFG["listen"], CFG["port"]))
    if sys.stdout and load_users():
        print("ByzzCraft running on %s" % url)
    srv.serve_forever()


if __name__ == "__main__":
    main()
