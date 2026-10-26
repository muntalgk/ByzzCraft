"""The real panel, in a separate process: login, cookie, CSRF, roles, login lock.
No network and no Minecraft: fake jar, fake assets, setup from the shell without Java."""
import http.client, json, os, re, shutil, socket, subprocess, sys, tempfile, time, zipfile

PANEL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
OK = FAIL = 0
def check(name, cond, extra=""):
    global OK, FAIL
    OK, FAIL = (OK + 1, FAIL) if cond else (OK, FAIL + 1)
    if not cond:
        print("FAIL:", name, extra)

root = tempfile.mkdtemp(prefix="http_test_")
data, srv = os.path.join(root, "data"), os.path.join(root, "srv")
os.makedirs(srv)
with zipfile.ZipFile(os.path.join(srv, "server.jar"), "w") as z:
    z.writestr("version.json", json.dumps({"id": "26.3", "java_version": 25}))
for d in ("block", "item", "hud", "skin"):            # assets "already downloaded": no download from Mojang
    os.makedirs(os.path.join(data, "assets", d))
for f in ("lang.json", "texcolors.json"):
    open(os.path.join(data, "assets", f), "w").write("{}")
PW = "password-di-prova"
env = dict(os.environ, BYZZ_DATA=data, PANEL_PASSWORD=PW, PYTHONIOENCODING="utf-8")
env.pop("PANEL_CONFIG", None)
run = lambda *a: subprocess.run([sys.executable, os.path.join(PANEL, "panel.py")] + list(a), env=env, capture_output=True, text=True, timeout=120)
proc = None
try:
    r = run("setup", srv, "--admin", "admin", "--no-java")
    check("setup from shell", r.returncode == 0, r.stderr[-500:])
    r = run("adduser", "bob")
    check("adduser", r.returncode == 0, r.stderr[-300:])
    check("adduser does not overwrite", run("adduser", "bob").returncode != 0)
    check("deluser keeps the last admin", run("deluser", "admin").returncode != 0)
    with socket.socket() as so:
        so.bind(("127.0.0.1", 0)); port = so.getsockname()[1]
    cfg_path = os.path.join(data, "panel-config.json")
    cfg = json.load(open(cfg_path)); cfg["port"] = port; json.dump(cfg, open(cfg_path, "w"))
    proc = subprocess.Popen([sys.executable, os.path.join(PANEL, "panel.py")], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def req(method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        c.request(method, path, body, headers or {})
        r = c.getresponse()
        out = (r.status, r.read().decode("utf-8", "replace"), {k.lower(): v for k, v in r.getheaders()})
        c.close()
        return out

    for _ in range(100):
        try:
            if req("GET", "/")[0] == 200:
                break
        except OSError:
            time.sleep(0.2)
    st, body, _ = req("GET", "/")
    check("login page", st == 200 and "password" in body)
    form = {"Content-Type": "application/x-www-form-urlencoded"}

    def login(user, pw):
        return req("POST", "/login", "user=%s&password=%s" % (user, pw), form)
    check("wrong password", login("admin", "sbagliata")[0] == 401)
    st, _, h = login("admin", PW)
    ck = h.get("set-cookie", "")
    check("login", st == 303 and ck.startswith("sess="), (st, ck))
    check("cookie HttpOnly and SameSite, no Secure on http", "HttpOnly" in ck and "SameSite=Strict" in ck and "Secure" not in ck, ck)
    cookie = {"Cookie": ck.split(";")[0]}
    st, body, h = req("GET", "/", headers=cookie)
    csrf = re.search(r'"csrf": "([^"]+)"', body)
    check("app page", st == 200 and csrf and "Max-Age=2592000" in h.get("set-cookie", ""), h.get("set-cookie"))
    csrf = csrf.group(1) if csrf else ""
    nonce = re.search(r"script-src 'nonce-([\w-]+)'", h.get("content-security-policy", ""))
    check("CSP: scripts only with this page's nonce", nonce and body.count('<script nonce="%s">' % nonce.group(1)) == 2
          and "<script>" not in body and "unsafe-inline'; connect" not in h["content-security-policy"], h.get("content-security-policy"))
    check("CSP: a new nonce per page", nonce and nonce.group(1) not in req("GET", "/", headers=cookie)[2]["content-security-policy"])
    check("CSP: no scripts on the login page", "script-src 'none'" in req("GET", "/")[2]["content-security-policy"])
    check("no HSTS on plain http", "strict-transport-security" not in h)
    check("HSTS behind an HTTPS proxy", "strict-transport-security" in req("GET", "/", headers={"X-Forwarded-Proto": "https"})[2])
    js = dict(cookie, **{"Content-Type": "application/json"})
    check("POST without CSRF", req("POST", "/api/command", "{}", js)[0] == 403)
    st, body, _ = req("POST", "/api/command", '{"command": "list"}', dict(js, **{"X-CSRF": csrf}))
    check("POST with CSRF", st == 200 and json.loads(body)["ok"] is False, body)   # server off: an answer, not an error
    st, body, _ = req("GET", "/api/health", headers=cookie)
    check("health (admin)", st == 200 and isinstance(json.loads(body)["checks"], list), body[:200])
    check("API without login", req("GET", "/api/health")[0] == 401)
    check("public manifest", req("GET", "/manifest.webmanifest")[0] == 200)
    st, _, h = login("bob", PW)
    bob = {"Cookie": h.get("set-cookie", "").split(";")[0]}
    check("player: no health", req("GET", "/api/health", headers=bob)[0] == 403)
    bcsrf = re.search(r'"csrf": "([^"]+)"', req("GET", "/", headers=bob)[1]).group(1)
    st, body, _ = req("POST", "/api/users/save", '{"name": "eve", "password": "0123456789", "new": true, "admin": true}',
                      dict(bob, **{"Content-Type": "application/json", "X-CSRF": bcsrf}))
    check("player: cannot create admin", st == 403, body)
    # password change: the other session of the same user dies, the one that changed it lives
    bob2 = {"Cookie": login("bob", PW)[2].get("set-cookie", "").split(";")[0]}
    check("bob second session valid", req("GET", "/api/health", headers=bob2)[0] == 403)
    st, body, _ = req("POST", "/api/account/password", json.dumps({"current": PW, "new": "nuova-password-1"}),
                      dict(bob, **{"Content-Type": "application/json", "X-CSRF": bcsrf}))
    check("password change", st == 200 and json.loads(body)["ok"], body)
    check("other session of the user: 401", req("GET", "/api/health", headers=bob2)[0] == 401)
    check("session that changed it: still valid", req("GET", "/api/health", headers=bob)[0] == 403)
    # last: it also locks the address 127.0.0.1
    codes = [login("nessuno", "x")[0] for _ in range(6)]
    check("lock after 5 attempts", codes == [401] * 4 + [429] * 2, codes)   # the first wrong one is admin's, above
finally:
    if proc:
        proc.kill(); proc.wait()
    shutil.rmtree(root, ignore_errors=True)
print("PASS %d  FAIL %d" % (OK, FAIL))
sys.exit(1 if FAIL else 0)
