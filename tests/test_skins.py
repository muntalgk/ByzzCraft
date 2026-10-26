import json, os, shutil, sys, tempfile, urllib.request
root = tempfile.mkdtemp(prefix="skins_test_")
os.environ["BYZZ_DATA"] = os.path.join(root, "data")  # before importing players
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))  # panel folder
from byzzcraft import players as P
from byzzcraft import paths; assert paths.DATA == os.path.abspath(os.environ["BYZZ_DATA"]), "run each test file in its own process: real data at risk"

OK = FAIL = 0
def check(name, cond, extra=""):
    global OK, FAIL
    OK, FAIL = (OK + 1, FAIL) if cond else (OK, FAIL + 1)
    if not cond:
        print("FAIL:", name, extra)

calls = []
def no_network(*a, **k):   # Mojang unreachable: every call is counted
    calls.append(a)
    raise OSError("offline")
urllib.request.urlopen = no_network

try:
    os.makedirs(os.path.join(P.ASSETS, "skin"))
    for n in ("slim_alex.png", "wide_steve.png"):
        with open(os.path.join(P.ASSETS, "skin", n), "wb") as f:
            f.write(n.encode())
    offline = "b50ad385-829d-3141-a216-7e7d7539ba7f"   # version 3: offline-mode player
    png, _ = P.skin(offline)
    check("offline UUID: default skin, Mojang not asked", png in (b"slim_alex.png", b"wide_steve.png") and not calls, calls)
    online = "069a79f4-44e9-4726-a5be-fca90e38aaf5"
    first = P.skin(online)
    check("Mojang down: default skin", first[0] in (b"slim_alex.png", b"wide_steve.png"))
    n = len(calls)
    check("Mojang down: second request served from the cache", P.skin(online) == first and len(calls) == n, (n, len(calls)))
    shutil.rmtree(os.path.join(P.ASSETS, "skin"))   # assets downloaded by an old version
    check("no skin/ in the assets: empty skin, no exception", P.skin("11111111-1111-4111-8111-111111111111") == (b"", False))
    check("missing icon not cached", P.icon_path("nope_item") is None and "nope_item" not in P._ICON_CACHE)
    pot = P.item({"id": "minecraft:potion", "count": 1, "Slot": 5, "components": {"minecraft:potion_contents": {"potion": "minecraft:long_fire_resistance"}}})
    check("potion: liquid colored by its effect", pot["tint"] == "#ff9900", pot)
    check("potion: custom color wins", P.item({"id": "potion", "components": {"minecraft:potion_contents": {"custom_color": -16711936}}})["tint"] == "#00ff00")
    check("potion: id-only component, water color by default", P.item({"id": "splash_potion", "components": {"minecraft:potion_contents": "minecraft:water"}})["tint"] == "#385dc6")
    check("no tint on other items", "tint" not in P.item({"id": "glass_bottle"}))
    try:
        P.snbt("[:]"); check("snbt: empty token raises", False)
    except ValueError:
        check("snbt: empty token raises", True)
finally:
    shutil.rmtree(root, ignore_errors=True)
print("PASS %d  FAIL %d" % (OK, FAIL))
sys.exit(1 if FAIL else 0)
