import json, os, shutil, sys, tempfile
root = tempfile.mkdtemp(prefix="timeline_test_")
os.environ["BYZZ_DATA"] = os.path.join(root, "data")  # before importing timeline
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))  # panel folder
import timeline as T

OK = FAIL = 0
def check(name, cond, extra=""):
    global OK, FAIL
    OK, FAIL = (OK + 1, FAIL) if cond else (OK, FAIL + 1)
    if not cond:
        print("FAIL:", name, extra)

try:
    srv = os.path.join(root, "server")
    cache = os.path.join(root, "cache")
    os.makedirs(os.path.join(srv, "logs"))
    with open(os.path.join(srv, "logs", "latest.log"), "w", encoding="utf-8") as f:
        f.write("[12:00:00] [Server thread/INFO]: System chat: Steve joined the game\n"
                "[12:01:00] [Server thread/INFO]: System chat: Steve was slain by Zombie\n")
    deaths = lambda tl: [e for e in tl._all if e["type"] == "death"]
    tl = T.Timeline(srv, cache, panel_log=os.path.join(root, "panel.log"))
    tl.refresh()
    check("no lang.json: join seen", any(e["type"] == "join" for e in tl._all))
    check("no lang.json: no death", not deaths(tl))
    os.makedirs(T.ASSETS)
    with open(os.path.join(T.ASSETS, "lang.json"), "w", encoding="utf-8") as f:
        json.dump({"death.attack.mob": "%1$s was slain by %2$s"}, f)
    tl = T.Timeline(srv, cache, panel_log=os.path.join(root, "panel.log"))
    tl.refresh()
    d = deaths(tl)
    check("lang.json added: one death for Steve", len(d) == 1 and d[0].get("player") == "Steve", d)
finally:
    shutil.rmtree(root, ignore_errors=True)
print("PASS %d  FAIL %d" % (OK, FAIL))
sys.exit(1 if FAIL else 0)
