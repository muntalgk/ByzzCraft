import os, shutil, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))  # cartella pannello
import players as P

OK = FAIL = 0
def check(name, cond, extra=""):
    global OK, FAIL
    OK, FAIL = (OK + 1, FAIL) if cond else (OK, FAIL + 1)
    if not cond:
        print("FAIL:", name, extra)

root = tempfile.mkdtemp(prefix="layout_test_")
try:
    j = os.path.join
    # 26.x: dimensions/minecraft/<dim>, players/<kind>
    w = j(root, "nuovo", "world")
    for d in ("overworld", "the_nether"):
        os.makedirs(j(w, "dimensions", "minecraft", d, "region"))
    os.makedirs(j(w, "players", "data"))
    check("26.x overworld", P.dim_dir(w, "overworld") == j(w, "dimensions", "minecraft", "overworld"))
    check("26.x end non ancora creato", P.dim_dir(w, "the_end") == j(w, "dimensions", "minecraft", "the_end"))
    check("26.x giocatori", P.player_dir(w, "data") == j(w, "players", "data"))
    # fino alla 1.21: region nel mondo, DIM-1, DIM1, playerdata
    w = j(root, "vecchio", "world")
    for d in ("region", "DIM-1", "DIM1", "playerdata", "stats"):
        os.makedirs(j(w, d))
    check("1.21 overworld", P.dim_dir(w, "overworld") == w, P.dim_dir(w, "overworld"))
    check("1.21 nether", P.dim_dir(w, "the_nether") == j(w, "DIM-1"))
    check("1.21 end", P.dim_dir(w, "the_end") == j(w, "DIM1"))
    check("1.21 giocatori", P.player_dir(w, "data") == j(w, "playerdata") and P.player_dir(w, "stats") == j(w, "stats"))
    # Paper fino alla 1.21: world_nether/DIM-1, world_the_end/DIM1
    w = j(root, "paper", "world")
    os.makedirs(j(w, "region")); os.makedirs(j(w + "_nether", "DIM-1")); os.makedirs(j(w + "_the_end", "DIM1"))
    check("paper nether", P.dim_dir(w, "the_nether") == j(w + "_nether", "DIM-1"))
    check("paper end", P.dim_dir(w, "the_end") == j(w + "_the_end", "DIM1"))
finally:
    shutil.rmtree(root, ignore_errors=True)
print("PASS %d  FAIL %d" % (OK, FAIL))
