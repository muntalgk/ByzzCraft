"""Timeline del server Minecraft: eventi + sessioni di gioco ricostruiti dai log.

Solo libreria standard (Python 3.11). I log vengono solo letti.
Uso CLI di prova:  python -I timeline.py <server_dir> <cache_dir>
"""
import datetime
import gzip
import json
import os
import re
import sys
import threading
import time

import registro as RG
import i18n
from i18n import t as tr

CACHE_NAME = "timeline-cache-%s.json"   # %s = lingua: i testi degli eventi sono gia' tradotti
CACHE_VERSION = 1
TYPES = ("server_start", "server_ready", "server_stop", "join", "leave",
         "death", "advancement", "chat", "panel")

_LINE = re.compile(r"^\[(\d\d):(\d\d):(\d\d)\] \[[^\]]*\]: (.*)$")
_GZ_NAME = re.compile(r"^(\d{4})-(\d\d)-(\d\d)-(\d+)\.log\.gz$")
_SYS = "System chat: "
_CHAT = re.compile(r"^(?:\[Not Secure\] )?<([^>]+)> ?(.*)$")
_RCON_CHAT = re.compile(r"^\[Not Secure\] \[Rcon\] (.*)$")
_JOIN = re.compile(r"^(\w{1,16}) joined the game$")
_LEAVE = re.compile(r"^(\w{1,16}) left the game$")
_ADV = re.compile(r"^(\w{1,16}) has (made the advancement|completed the challenge|reached the goal) \[(.+)\]$")
_START = re.compile(r"^Starting minecraft server version (\S+)")
_DONE = re.compile(r"^Done \(([\d.,]+)s\)!")
_PLACEHOLDER = re.compile(r"%(?:(\d)\$)?s")


def _epoch_ms(dt):
    return int(dt.timestamp() * 1000)


def _mk(t, typ, player, text, detail=None):
    return {"t": t, "type": typ, "player": player, "text": text, "detail": detail or {}}


def _key(e):
    return (e["t"], e["type"], e["player"], e["text"])


class _DeathMatcher:
    """Modelli death.* di lang.json convertiti in regex."""

    def __init__(self, lang_path):
        pats = []
        try:
            with open(lang_path, "r", encoding="utf-8-sig") as f:
                lang = json.load(f)
        except (OSError, ValueError):
            lang = {}
        for key, val in lang.items():
            if not key.startswith("death.") or not isinstance(val, str):
                continue
            parts = []
            literal = 0
            pos = 0
            seen = set()
            for m in _PLACEHOLDER.finditer(val):
                lit = val[pos:m.start()]
                parts.append(re.escape(lit))
                literal += len(lit)
                idx = m.group(1) or str(len(seen) + 1)
                if idx in seen:
                    parts.append("(?P=p%s)" % idx)
                else:
                    seen.add(idx)
                    parts.append("(?P<p%s>.+?)" % idx)
                pos = m.end()
            lit = val[pos:]
            parts.append(re.escape(lit))
            literal += len(lit)
            if "1" not in seen:
                continue
            try:
                pats.append((literal, key, re.compile("^" + "".join(parts) + "$"), val))
            except re.error:
                continue
        pats.sort(key=lambda x: -x[0])  # i modelli piu' specifici prima
        self.patterns = pats

    def match(self, text, known):
        for _lit, key, rx, _val in self.patterns:
            m = rx.match(text)
            if not m:
                continue
            gd = m.groupdict()
            if gd.get("p1") in known:
                return key, gd
        return None


class Timeline:
    def __init__(self, server_dir, cache_dir, panel_log=None):
        self.server_dir = server_dir
        self.cache_dir = cache_dir
        self.logs_dir = os.path.join(server_dir, "logs")
        self.panel_log = panel_log or os.path.join(os.path.dirname(os.path.abspath(__file__)), "panel.log")
        self.cache_path = os.path.join(cache_dir, CACHE_NAME % i18n.LANG)
        self._lock = threading.RLock()
        self._deaths = _DeathMatcher(os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "lang.json"))
        self._gz = {}       # nome -> {"size": int, "events": [...]}
        self._latest = {"pos": 0, "head": "", "cur_day": None, "last_secs": None, "events": []}
        self._panel = {"size": -1, "events": []}
        self._players = set()
        self._all = []
        self._keys = set()
        self._sessions = None
        self._load_cache()
        self._load_usercache()
        self._rebuild()   # eventi gia' in cache: sono "noti" solo dopo il primo refresh
        self._keys = set()

    # ------------------------------------------------------------ cache
    def _load_cache(self):
        try:
            with open(self.cache_path, "r", encoding="utf-8") as f:
                d = json.load(f)
            if d.get("v") != CACHE_VERSION:
                return
            self._gz = d.get("gz", {})
            self._latest = d.get("latest", self._latest)
            self._panel = d.get("panel", self._panel)
            self._players = set(d.get("players", []))
        except (OSError, ValueError, AttributeError):
            pass

    def _save_cache(self):
        try:
            os.makedirs(self.cache_dir, exist_ok=True)
            tmp = self.cache_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"v": CACHE_VERSION, "gz": self._gz, "latest": self._latest,
                           "panel": self._panel, "players": sorted(self._players)},
                          f, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp, self.cache_path)
        except OSError:
            pass

    def _load_usercache(self):
        try:
            with open(os.path.join(self.server_dir, "usercache.json"), "r", encoding="utf-8-sig") as f:
                for u in json.load(f):
                    if isinstance(u, dict) and u.get("name"):
                        self._players.add(u["name"])
        except (OSError, ValueError):
            pass

    # ------------------------------------------------------------ parsing
    def _parse_text(self, t, text, out):
        """Trasforma il testo di una riga del server in eventi (appende a out)."""
        raw = text
        if text.startswith(_SYS):
            text = text[len(_SYS):]
        # chat
        m = _CHAT.match(raw)
        if m:
            out.append(_mk(t, "chat", m.group(1), "%s: %s" % (m.group(1), m.group(2)),
                           {"message": m.group(2)}))
            return
        m = _RCON_CHAT.match(raw)
        if m:
            out.append(_mk(t, "chat", None, "[Rcon] " + m.group(1),
                           {"message": m.group(1), "rcon": True}))
            return
        c = text[:1]
        if c == "S":
            m = _START.match(text)
            if m:
                out.append(_mk(t, "server_start", None, tr("Server started (version %s)") % m.group(1),
                               {"version": m.group(1)}))
                return
            if text == "Stopping server":
                out.append(_mk(t, "server_stop", None, tr("Server stopped")))
                return
        elif c == "D":
            m = _DONE.match(text)
            if m:
                out.append(_mk(t, "server_ready", None, tr("Server ready (%s s)") % m.group(1),
                               {"seconds": float(m.group(1).replace(",", "."))}))
                return
        m = _JOIN.match(text)
        if m:
            self._players.add(m.group(1))
            out.append(_mk(t, "join", m.group(1), tr("%s joined") % m.group(1)))
            return
        m = _LEAVE.match(text)
        if m:
            out.append(_mk(t, "leave", m.group(1), tr("%s left") % m.group(1)))
            return
        m = _ADV.match(text)
        if m:
            kind = {"made the advancement": "advancement", "completed the challenge": "challenge",
                    "reached the goal": "goal"}[m.group(2)]
            out.append(_mk(t, "advancement", m.group(1),
                           tr("%s made the advancement [%s]") % (m.group(1), m.group(3)),
                           {"name": m.group(3), "kind": kind}))
            return
        # morti: la frase inizia col nome di un giocatore noto
        first = text.split(" ", 1)[0]
        if first in self._players and raw.startswith(_SYS):
            r = self._deaths.match(text, self._players)
            if r:
                key, gd = r
                victim = gd["p1"]
                killer = gd.get("p2")
                rest = text[len(victim) + 1:] if text.startswith(victim + " ") else text
                out.append(_mk(t, "death", victim, tr("%s died: %s") % (victim, rest),
                               {"cause": key, "killer": killer, "raw": text,
                                "item": gd.get("p3")}))

    def _parse_lines(self, lines, day0, last_secs=None):
        """lines: iterabile di str. Gestisce i salti di mezzanotte.
        Ritorna (eventi, giorno_corrente, ultimi_secondi)."""
        out = []
        day = day0
        last = last_secs
        base = None
        for line in lines:
            m = _LINE.match(line)
            if not m:
                continue
            secs = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
            if last is not None and secs < last:
                day += datetime.timedelta(days=1)
            last = secs
            if base is None or base[0] != day:
                base = (day, datetime.datetime(day.year, day.month, day.day))
            t = _epoch_ms(base[1] + datetime.timedelta(seconds=secs))
            self._parse_text(t, m.group(4), out)
        return out, day, last

    @staticmethod
    def _decode_lines(data):
        return [b.decode("utf-8", "replace").rstrip("\r") for b in data.split(b"\n")]

    # ------------------------------------------------------------ fonti
    def _read_gz(self):
        changed = False
        try:
            names = os.listdir(self.logs_dir)
        except OSError:
            return False
        found = []
        for n in names:
            m = _GZ_NAME.match(n)
            if m:
                found.append((int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)), n))
        found.sort()
        for y, mo, d, _i, n in found:
            p = os.path.join(self.logs_dir, n)
            try:
                size = os.path.getsize(p)
            except OSError:
                continue
            ent = self._gz.get(n)
            if ent and ent.get("size") == size:
                continue
            try:
                with gzip.open(p, "rb") as f:
                    data = f.read()
            except (OSError, EOFError):
                continue  # gz incompleto/in scrittura: riprova al prossimo giro
            evs, _day, _last = self._parse_lines(self._decode_lines(data), datetime.date(y, mo, d))
            self._gz[n] = {"size": size, "events": evs}
            changed = True
        return changed

    def _read_latest(self):
        p = os.path.join(self.logs_dir, "latest.log")
        try:
            st = os.stat(p)
            with open(p, "rb") as f:
                head = f.read(96).decode("latin-1")
                st_pos = self._latest.get("pos", 0)
                reset = (st.st_size < st_pos or self._latest.get("head") != head[:len(self._latest.get("head", ""))]
                         or not self._latest.get("head"))
                if not reset and st.st_size == st_pos:
                    return False
                start = 0 if reset else st_pos
                f.seek(start)
                data = f.read()
        except OSError:
            return False
        cut = data.rfind(b"\n")
        if cut < 0:
            return False  # nessuna riga completa
        chunk = data[:cut + 1]
        lines = self._decode_lines(chunk)
        if reset:
            # data iniziale dedotta da mtime e dai salti di mezzanotte
            times = []
            for ln in lines:
                m = _LINE.match(ln)
                if m:
                    times.append(int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3)))
            if not times:
                self._latest = {"pos": start + len(chunk), "head": head, "cur_day": None,
                                "last_secs": None, "events": []}
                return True
            rolls = sum(1 for a, b in zip(times, times[1:]) if b < a)
            mt = datetime.datetime.fromtimestamp(st.st_mtime)
            last_dt = datetime.datetime(mt.year, mt.month, mt.day) + datetime.timedelta(seconds=times[-1])
            if last_dt > mt + datetime.timedelta(minutes=2):
                last_dt -= datetime.timedelta(days=1)
            day0 = last_dt.date() - datetime.timedelta(days=rolls)
            evs, day, last = self._parse_lines(lines, day0)
            self._latest = {"pos": start + len(chunk), "head": head, "cur_day": day.isoformat(),
                            "last_secs": last, "events": evs}
        else:
            cd = self._latest.get("cur_day")
            if not cd:
                return False
            day0 = datetime.date.fromisoformat(cd)
            evs, day, last = self._parse_lines(lines, day0, self._latest.get("last_secs"))
            self._latest["events"].extend(evs)
            self._latest["cur_day"] = day.isoformat()
            if last is not None:
                self._latest["last_secs"] = last
            self._latest["pos"] = start + len(chunk)
        return True

    def _read_panel(self):
        try:
            size = os.path.getsize(self.panel_log)
            if size == self._panel.get("size"):
                return False
            with open(self.panel_log, "rb") as f:
                data = f.read()
        except OSError:
            return False
        cut = data.rfind(b"\n")
        if cut < 0:
            return False
        evs = []
        for ln in self._decode_lines(data[:cut + 1]):
            e = RG.parse(ln)
            if not e or (e["cat"] == "login" and e["level"] == "warn") or "passw" in e["raw"].lower():
                continue
            who = tr("panel") if e["user"] == "panel" else e["user"]   # utente di sistema
            txt = (who + " " if who else "") + e["text"]
            evs.append(_mk(e["t"], "panel", None, txt, {"cat": e["cat"]}))
        self._panel = {"size": size, "events": evs}
        return True

    # ------------------------------------------------------------ API
    def _rebuild(self):
        allv = []
        for n in sorted(self._gz, key=lambda n: tuple(int(x) for x in _GZ_NAME.match(n).groups())
                        if _GZ_NAME.match(n) else (0,)):
            allv.extend(self._gz[n]["events"])
        allv.extend(self._latest.get("events", []))
        allv.extend(self._panel.get("events", []))
        allv.sort(key=lambda e: e["t"])  # stabile
        self._all = allv
        self._sessions = None

    def refresh(self):
        """Legge solo il nuovo; ritorna gli eventi nuovi (la prima volta: tutti quelli noti)."""
        with self._lock:
            ch = self._read_gz()
            ch = self._read_latest() or ch
            ch = self._read_panel() or ch
            if ch or not self._all and not self._keys:
                self._rebuild()
                if ch:
                    self._save_cache()
            new = []
            for e in self._all:
                k = _key(e)
                if k not in self._keys:
                    self._keys.add(k)
                    new.append(e)
            return new

    def events(self, since_ms=None, types=None, player=None, limit=500):
        with self._lock:
            tset = set(types) if types else None
            pl = player.lower() if player else None
            out = []
            for e in reversed(self._all):
                if since_ms is not None and e["t"] < since_ms:
                    break
                if tset and e["type"] not in tset:
                    continue
                if pl and (e["player"] or "").lower() != pl:
                    continue
                out.append(e)
                if limit and len(out) >= limit:
                    break
            return out

    def _build_sessions(self):
        sessions = []
        cur = None
        online = {}   # nome -> entry
        prev_t = None

        def close(end):
            nonlocal cur, online
            for en in online.values():
                en["left"] = end
                en["online_s"] = max(0, (end - en["joined"]) // 1000)
            cur["end"] = end
            cur["duration_s"] = max(0, (end - cur["start"]) // 1000)
            online = {}
            cur = None

        for e in self._all:
            ty = e["type"]
            t = e["t"]
            if ty == "panel":
                continue
            if ty == "server_start" and cur is not None:
                close(prev_t if prev_t is not None else t)   # crash/spegnimento senza log
            if ty == "server_stop" and cur is not None:
                close(t)
            elif ty == "join":
                if cur is None:
                    cur = {"id": t, "start": t, "end": None, "duration_s": 0, "players": [],
                           "deaths": [], "advancements": [], "events": 0}
                    sessions.append(cur)
                name = e["player"]
                if name in online:   # doppio join: chiudi il precedente
                    old = online.pop(name)
                    old["left"] = t
                    old["online_s"] = max(0, (t - old["joined"]) // 1000)
                en = {"name": name, "joined": t, "left": None, "online_s": 0}
                cur["players"].append(en)
                online[name] = en
            elif ty == "leave":
                en = online.pop(e["player"], None)
                if en is not None:
                    en["left"] = t
                    en["online_s"] = max(0, (t - en["joined"]) // 1000)
                    if not online:
                        close(t)
            elif ty in ("death", "advancement"):
                tgt = cur
                if tgt is None and sessions and sessions[-1]["end"] is not None \
                        and t - sessions[-1]["end"] <= 60000:
                    tgt = sessions[-1]
                if tgt is not None:
                    tgt["deaths" if ty == "death" else "advancements"].append(e)
            if cur is not None:
                cur["events"] += 1
            elif ty != "join" and sessions and sessions[-1]["end"] is not None \
                    and t - sessions[-1]["end"] <= 0:
                sessions[-1]["events"] += 1
            prev_t = t
        if cur is not None:   # in corso
            now_ms = int(time.time() * 1000)
            for en in online.values():
                en["online_s"] = max(0, (now_ms - en["joined"]) // 1000)
            cur["duration_s"] = max(0, (now_ms - cur["start"]) // 1000)
        return sessions

    def sessions(self, limit=50):
        with self._lock:
            if self._sessions is None:
                self._sessions = self._build_sessions()
            res = self._sessions[::-1]
            if limit:
                res = res[:limit]
            return json.loads(json.dumps(res))   # copia: il chiamante non altera lo stato


# ---------------------------------------------------------------- CLI
def _fmt_ts(ms):
    return datetime.datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S") if ms else "-"


def _fmt_dur(s):
    return "%dh%02dm%02ds" % (s // 3600, s % 3600 // 60, s % 60)


def main(argv):
    if len(argv) < 3:
        print("uso: python -I timeline.py <server_dir> <cache_dir>")
        return 2
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    had_cache = os.path.exists(os.path.join(argv[2], CACHE_NAME % i18n.LANG))
    t0 = time.perf_counter()
    tl = Timeline(argv[1], argv[2])
    new = tl.refresh()
    t1 = time.perf_counter()
    cnt = {}
    for e in tl.events(limit=0):
        cnt[e["type"]] = cnt.get(e["type"], 0) + 1
    print("eventi per tipo:", ", ".join("%s=%d" % (k, cnt[k]) for k in sorted(cnt)), "(totale %d, nuovi %d)"
          % (sum(cnt.values()), len(new)))
    print("ultime 5 sessioni:")
    for s in tl.sessions(5):
        pl = ", ".join("%s(%s)" % (p["name"], _fmt_dur(p["online_s"])) for p in s["players"])
        print("  %s -> %s  %s  [%s]  morti=%d progressi=%d eventi=%d" % (
            _fmt_ts(s["start"]), _fmt_ts(s["end"]), _fmt_dur(s["duration_s"]), pl,
            len(s["deaths"]), len(s["advancements"]), s["events"]))
        for d in s["deaths"]:
            print("      morte %s %s" % (_fmt_ts(d["t"]), d["text"]))
    print("morti totali:")
    for d in reversed(tl.events(types=["death"], limit=0)):
        print("  %s %s" % (_fmt_ts(d["t"]), d["text"]))
    t2 = time.perf_counter()
    tl.refresh()
    t3 = time.perf_counter()
    print("tempo costruzione (cache %s): %.3fs; refresh successivo: %.3fs" % (
        "usata" if had_cache else "assente", t1 - t0, t3 - t2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
