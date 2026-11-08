import json, os, shutil, struct, sys, tempfile, zipfile, zlib
root = tempfile.mkdtemp(prefix="modassets_test_")
os.environ["BYZZ_DATA"] = os.path.join(root, "data")  # before importing players
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))  # panel folder
from byzzcraft import assets as A, players as P, mapgen as M
from byzzcraft import paths; assert paths.DATA == os.path.abspath(os.environ["BYZZ_DATA"]), "run each test file in its own process: real data at risk"

OK = FAIL = 0
def check(name, cond, extra=""):
    global OK, FAIL
    OK, FAIL = (OK + 1, FAIL) if cond else (OK, FAIL + 1)
    if not cond:
        print("FAIL:", name, extra)

def png(w, h, rows):   # RGB rows of (r, g, b)
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    raw = b"".join(b"\0" + bytes(c for px in r for c in px) for r in rows)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
solid = lambda c, w=2, h=2: png(w, h, [[c] * w] * h)
J = json.dumps

try:
    sd = os.path.join(root, "srv"); md = os.path.join(sd, "mods"); os.makedirs(md)
    def jar(name, files):
        with zipfile.ZipFile(os.path.join(md, name), "w") as z:
            for k, v in files.items():
                z.writestr(k, v)
    jar("a.jar", {
        "assets/alpha/items/tool.json": J({"model": {"type": "minecraft:select", "cases": [{"model": {"type": "minecraft:model", "model": "alpha:item/tool"}}]}}),
        "assets/alpha/models/item/tool.json": J({"parent": "alpha:item/base"}),
        "assets/alpha/models/item/base.json": J({"parent": "minecraft:item/generated", "textures": {"layer0": "beta:item/shared"}}),
        "assets/alpha/items/rock.json": J({"model": {"type": "minecraft:model", "model": "alpha:block/rock"}}),
        "assets/alpha/models/block/rock.json": J({"parent": "minecraft:block/cube", "textures": {"side": "alpha:block/rock_side", "top": "#t", "t": "alpha:block/rock_top"}}),
        "assets/alpha/textures/block/rock_side.png": solid((0, 0, 255)),
        "assets/alpha/textures/block/rock_top.png": solid((200, 100, 0)),
        "assets/alpha/blockstates/rock.json": J({"variants": {"": {"model": "alpha:block/rock"}}}),
        "assets/alpha/blockstates/multi.json": J({"multipart": [{"apply": [{"model": "alpha:block/rock"}]}]}),
        "assets/alpha/textures/item/plain.png": solid((1, 2, 3)),
        "assets/alpha/textures/item/anim.png": png(2, 4, [[(9, 9, 9)] * 2, [(9, 9, 9)] * 2, [(200, 0, 0)] * 2, [(200, 0, 0)] * 2]),
        "assets/alpha/items/anim.json": J({"model": {"type": "minecraft:model", "model": "alpha:item/anim"}}),
        "assets/alpha/models/item/anim.json": J({"textures": {"layer0": "alpha:item/anim"}}),
        "assets/alpha/items/sub/deep.json": J({"model": {"type": "minecraft:model", "model": "alpha:item/plain"}}),
        "assets/alpha/models/item/plain.json": J({"textures": {"layer0": "alpha:item/plain"}}),
        "assets/alpha/lang/en_us.json": J({"item.alpha.tool": "Tool", "block.alpha.rock": "Rock", "advancement.x": "no"}),
        "assets/alpha/lang/it_it.json": J({"item.alpha.tool": "Attrezzo"}),
    })
    jar("b.jar", {"assets/beta/textures/item/shared.png": solid((10, 200, 30)), "assets/beta/lang/en_us.json": "not json"})
    jar("broken.jar", {})
    open(os.path.join(md, "broken2.jar"), "wb").write(b"not a zip")
    jar("off.jar.disabled", {"assets/gamma/textures/item/x.png": solid((1, 1, 1))})
    dest = os.path.join(P.ASSETS, "mods")
    check("built, colors new", A.extract_mods(sd, dest, "it") == 2)
    ic = lambda ns, n: open(os.path.join(dest, ns, n + ".png"), "rb").read() if os.path.exists(os.path.join(dest, ns, n + ".png")) else None
    px = lambda d: A.png_rgba(d)[2][0][:3]
    check("model -> parent -> layer0 in another namespace", ic("alpha", "tool") and px(ic("alpha", "tool")) == (10, 200, 30))
    check("block item: top via #ref", ic("alpha", "rock") and px(ic("alpha", "rock")) == (200, 100, 0))
    check("fallback textures/item", ic("alpha", "plain") and px(ic("alpha", "plain")) == (1, 2, 3))
    an = ic("alpha", "anim")
    check("animated strip cropped to the first frame", an and A.png_rgba(an)[:2] == (2, 2) and px(an) == (9, 9, 9), an and A.png_rgba(an)[:2])
    check("id with / stored with __", ic("alpha", "sub__deep") is not None)
    check("disabled jar ignored", not os.path.exists(os.path.join(dest, "gamma")))
    L = json.load(open(os.path.join(dest, "lang.json")))
    check("lang: it over en, en kept, only item/block", L == {"item.alpha.tool": "Attrezzo", "block.alpha.rock": "Rock"}, L)
    C = json.load(open(os.path.join(dest, "texcolors.json")))
    check("block color from blockstates (variants and multipart)", C.get("alpha:rock") == [200, 100, 0, 1] and C.get("alpha:multi", [0])[:3] == [200, 100, 0], C)
    check("same mods: no rebuild", A.extract_mods(sd, dest, "it") == 0)
    check("other language: rebuilt, same colors", A.extract_mods(sd, dest, "en") == 1 and json.load(open(os.path.join(dest, "lang.json")))["item.alpha.tool"] == "Tool")
    check("no leftovers", not os.path.exists(dest + ".tmp") and not os.path.exists(dest + ".old"))

    # a change to mods/ stops the build: jars closed (Windows can rename them), nothing swapped in
    import threading
    jar("c.jar", {"assets/gamma/textures/item/x.png": solid((1, 1, 1))})
    stop = threading.Event(); stop.set()
    try:
        A.extract_mods(sd, dest, "en", stop)
        check("stopped build raises Cancelled", False)
    except A.Cancelled:
        check("stopped build raises Cancelled", True)
    os.rename(os.path.join(md, "c.jar"), os.path.join(md, "c.jar.disabled"))   # fails on Windows if a jar stayed open
    check("stopped build: jars closed, old build kept, no tmp", not os.path.exists(dest + ".tmp") and json.load(open(os.path.join(dest, "lang.json")))["item.alpha.tool"] == "Tool"
          and not os.path.exists(os.path.join(dest, "gamma")))
    os.remove(os.path.join(md, "c.jar.disabled"))

    # 26.x sprite textures reach the flat icon and the map color
    jar("d.jar", {"assets/delta/models/block/stone.json": J({"parent": "minecraft:block/cube_all", "textures": {"all": {"sprite": "delta:block/stone"}}}),
                  "assets/delta/textures/block/stone.png": solid((50, 60, 70)),
                  "assets/delta/blockstates/stone.json": J({"variants": {"": {"model": "delta:block/stone"}}})})
    A.extract_mods(sd, dest, "en")
    C = json.load(open(os.path.join(dest, "texcolors.json")))
    check("sprite texture gives the block color", C.get("delta:stone", [0])[:3] == [50, 60, 70], C.get("delta:stone"))
    os.remove(os.path.join(md, "d.jar"))

    # hostile jars: zip-slip names, huge entries, huge PNG headers, older layout
    big = png(2, 2, [[(5, 5, 5)] * 2] * 2)
    jar("evil.jar", {"assets/../items/x.json": J({"model": "evil:item/x"}), "assets/../textures/item/x.png": solid((6, 6, 6)),
                     "assets/evil/textures/item/huge.png": big[:16] + struct.pack(">II", 5000, 5000) + big[24:],
                     "assets/evil/textures/item/fat.png": big + b"\0" * (2 << 20),
                     "assets/evil/textures/item/../../../../up.png": solid((7, 7, 7)),
                     "assets/old/models/item/gem.json": J({"textures": {"layer0": "old:item/gem_tex"}}),
                     "assets/old/textures/item/gem_tex.png": solid((8, 9, 10)),
                     "assets/old/textures/item/bad.png": b"\x89PNG short"})
    check("hostile jar: rebuilt", A.extract_mods(sd, dest, "en") in (1, 2))
    check("zip-slip: nothing outside the build", not os.path.exists(os.path.join(P.ASSETS, "x.png")) and not os.path.exists(os.path.join(dest, "x.png")) and not os.path.exists(os.path.join(os.path.dirname(dest), "x.png")))
    check("huge header and big entry skipped", ic("evil", "huge") is None and ic("evil", "fat") is None)
    check("older layout: models/item/<id>.json", ic("old", "gem") and px(ic("old", "gem")) == (8, 9, 10))
    check("truncated png does not crash", True)
    open(os.path.join(md, "a.jar"), "ab").close()   # jars closed: renaming works on Windows
    os.replace(os.path.join(md, "a.jar"), os.path.join(md, "a2.jar")); os.replace(os.path.join(md, "a2.jar"), os.path.join(md, "a.jar"))
    check("vanilla texture ref cannot escape", A._Mods([]).png("minecraft:block/..\\..\\x") is None)
    check("namespace of dots refused", not A._NS_OK.match("..") and not A._NS_OK.match(".") and A._NS_OK.match("my_mod-2"))
    check("id of dots refused", not A._ID_OK.match("../x") and not A._ID_OK.match("a/../b") and A._ID_OK.match("a/b.c"))
    check("player icon of ns '..' refused", P.icon_path("..:tool") is None)
    check("entity name of a mod", P.entity_name("alpha:wild_beast") == "Wild Beast" and P.entity_name("minecraft:zombie") == "zombie")

    # players
    P.mods_reset()
    it = P.item({"id": "alpha:tool", "count": 1, "Slot": 0})
    check("item keeps ns:id", it["id"] == "alpha:tool", it)
    check("item name from the mods lang", it["name"] == "Tool", it)
    check("vanilla id stays bare", P.item({"id": "minecraft:stone"})["id"] == "stone")
    check("icon resolved", P.icon_path("alpha:tool") == os.path.join(dest, "alpha", "tool.png"))
    check("icon with /", P.icon_path("alpha:sub/deep") == os.path.join(dest, "alpha", "sub__deep.png"))
    check("no vanilla fallback for a mod's id", P.icon_path("alpha:stone") is None)
    check("traversal refused", P.icon_path("alpha:../../key") is None and P.icon_path("../x:y") is None)
    check("unknown name prettified", P.item({"id": "alpha:wild_thing"})["name"] == "Wild Thing")
    P.mods_reset()
    check("reset clears caches", not P._ICON_CACHE and P._MODS_LANG is None)

    # map
    M._GID.clear(); del M.KIND[:], M.COL[:], M.TINT[:]
    g = M._palette_gids([{"Name": "alpha:rock"}, {"Name": "zeta:other"}])
    check("modded block colored", M.COL[g[0]] == (200, 100, 0) and M.KIND[g[0]] == 1, M.COL[g[0]])
    check("unknown modded block neutral", M.COL[g[1]] == M.NEUTRAL)
    shutil.rmtree(dest); M._MODS_TEX = None
    M._GID.clear(); del M.KIND[:], M.COL[:], M.TINT[:]
    g2 = M._palette_gids([{"Name": "alpha:rock"}])
    check("before the build: neutral", M.COL[g2[0]] == M.NEUTRAL)
    A.extract_mods(sd, dest, "en")
    check("reload recolors in place", M.reload_mod_colors() == 1 and M.COL[g2[0]] == (200, 100, 0))

    # 3D icons
    def cube(h=16, **face):
        return {"textures": {"all": "t"}, "gui": None, "elements": [{"from": [0, 0, 0], "to": [16, h, 16], "faces": {f: dict({"texture": "#all"}, **face) for f in ("up", "down", "north", "south", "east", "west")}}]}
    ld = lambda ref: (2, 2, [(200, 200, 200, 255)] * 4)
    def px(model, tint=None):
        b = A.render_model(model, ld, tint)
        return b and A.png_rgba(b)[2]
    at = lambda p, x, y: p[y * 64 + x]
    full, slab = px(cube()), px(cube(8))
    check("cube: 64x64 opaque body", full and at(full, 32, 32)[3] == 255 and at(full, 0, 0)[3] == 0)
    check("cube: top lighter than sides", at(full, 32, 12)[0] > at(full, 20, 40)[0] > at(full, 44, 40)[0], (at(full, 32, 12), at(full, 20, 40), at(full, 44, 40)))
    check("slab: top quarter empty, cube has pixels", at(full, 32, 12)[3] == 255 and at(slab, 32, 12)[3] == 0)
    check("no elements: None", A.render_model({"textures": {}, "gui": None, "elements": None}, ld) is None)
    check("layer0: stays flat", A.render_model(dict(cube(), textures={"all": "t", "layer0": "t"}), ld) is None)
    tp = px(cube(), (100, 200, 50))
    check("untinted face ignores tint", at(tp, 32, 12) == at(full, 32, 12))
    tc = cube(); [f.update(tintindex=0) for f in tc["elements"][0]["faces"].values()]
    tp = px(tc, (100, 200, 50))
    check("tintindex face tinted", at(tp, 32, 12)[:3] == (100 * 200 // 255, 200 * 200 // 255, 50 * 200 // 255), at(tp, 32, 12))
    rd = A.resolve_model(lambda r: {"m": {"parent": "p", "textures": {"a": {"sprite": "x"}}}, "p": {"elements": [], "display": {"gui": {"scale": [1, 1, 1]}}, "textures": {"a": "y", "b": "z"}}}.get(r, {}), "m")
    check("parent chain merged", rd["textures"] == {"a": "x", "b": "z"} and rd["gui"] == {"scale": [1, 1, 1]} and rd["elements"] == [], rd)
    # rebuild keys, caps, malformed models, PNG bound
    A.extract_mods(sd, dest, "en")
    check("same key: no rebuild", A.extract_mods(sd, dest, "en") == 0)
    A.RENDER_V += 1
    check("renderer version: rebuild", A.extract_mods(sd, dest, "en") != 0)
    mk = os.path.join(P.ASSETS, "models", "block"); os.makedirs(mk, exist_ok=True)
    open(os.path.join(mk, "cube.json"), "w").write("{}"); os.utime(os.path.join(mk, "cube.json"), (1000, 1000))
    check("vanilla assets installed: rebuild", A.extract_mods(sd, dest, "en") != 0 and A.extract_mods(sd, dest, "en") == 0)
    big = cube(); big["elements"] = big["elements"] * (A.MAX_ELEMENTS + 1)
    check("too many elements: None", A.render_model(big, ld) is None)
    bad = cube(); bad["elements"][0]["faces"]["up"].update(uv=[0, 0, 16], tintindex=None)
    jar("bad.jar", {"assets/bad/items/x.json": J({"model": {"type": "minecraft:model", "model": "bad:block/x"}}),
                    "assets/bad/models/block/x.json": J({"textures": ["a"], "elements": [{"from": [0, 0, 0], "to": [16, 16, 16], "faces": {"up": {"texture": "#a", "uv": [0, 0, 16]}}}]}),
                    "assets/bad/textures/block/x.png": solid((5, 6, 7)), "assets/bad/textures/item/x.png": solid((5, 6, 7))})
    A.extract_mods(sd, dest, "en")
    check("render error: flat icon stays", os.path.isfile(os.path.join(dest, "bad", "x.png")))
    try:
        A.render_model(bad, ld); ok = False
    except Exception:
        ok = True
    check("malformed face raises (callers catch)", ok)
    bomb = M._png(bytes(4 * 600 * 600), 600, 600)
    hdr = bomb[:16] + struct.pack(">II", 2, 2) + bomb[24:]   # header lies: 2x2
    try:
        A.png_rgba(hdr); ok = False
    except Exception:
        ok = True
    check("png data larger than header refused", ok)
finally:
    shutil.rmtree(root, ignore_errors=True)
print("PASS %d  FAIL %d" % (OK, FAIL))
sys.exit(1 if FAIL else 0)
