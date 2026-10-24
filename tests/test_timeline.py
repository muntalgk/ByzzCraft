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

    # panel.log is read incrementally: appended lines once, a rewritten file from the start
    plog = os.path.join(root, "panel.log")
    panel = lambda: [e["text"] for e in tl._all if e["type"] == "panel"]
    with open(plog, "w", encoding="utf-8") as f:
        f.write("2026-10-24 12:00:00  alex started the server\n2026-10-24 12:30:00  alex stopped the server\n")
    tl.refresh()
    check("panel.log: two entries", len(panel()) == 2, panel())
    with open(plog, "a", encoding="utf-8") as f:
        f.write("2026-10-24 13:00:00  alex started the server\n2026-10-24 13:00:01  alex sta")   # last line still being written
    tl.refresh()
    check("panel.log: appended line once, partial line waits", len(panel()) == 3, panel())
    with open(plog, "a", encoding="utf-8") as f:
        f.write("rted the server\n")
    tl.refresh()
    check("panel.log: partial line completed", len(panel()) == 4, panel())
    with open(plog, "w", encoding="utf-8") as f:
        f.write("2026-10-25 09:00:00  alex started the server\n")
    tl.refresh()
    check("panel.log: shorter file read from the start", len(panel()) == 1, panel())
finally:
    shutil.rmtree(root, ignore_errors=True)
print("PASS %d  FAIL %d" % (OK, FAIL))
sys.exit(1 if FAIL else 0)
