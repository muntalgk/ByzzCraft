"""Downloads from Mojang the textures and translations the panel shows (they cannot be redistributed).

Usage:  python panel.py assets [VERSION]     (without a version it is read from the server jar)
"""
import hashlib
import glob
import io
import json
import math
import os
import re
import shutil
import struct
import urllib.request
import zipfile
import zlib

from .mapgen import _png

from .paths import ASSETS, DATA, write_json  # noqa: E402
MANIFEST = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
PAPER = "https://fill.papermc.io/v3/projects/paper"
FABRIC = "https://meta.fabricmc.net/v2/versions"
SERVERS = ("vanilla", "paper", "fabric")   # what the setup can download; Spigot is built with BuildTools, not downloaded
TEX = "assets/minecraft/textures/"
MODELS = "assets/minecraft/models/"
# folder in the jar -> folder in assets/ (subfolders become a prefix: hud/heart/full.png -> hud/heart_full.png)
DIRS = {TEX + "block/": "block", TEX + "item/": "item", TEX + "gui/sprites/hud/": "hud", TEX + "entity/player/": "skin"}
HUD_PREFIX = ("heart", "food_", "armor_", "experience_bar_")
SLOTS = {"boots", "chestplate", "helmet", "leggings", "shield"}   # icons of the empty armour slots in the inventory


def present():
    return all(os.path.exists(os.path.join(ASSETS, p)) for p in ("block", "item", "render", "hud", "skin", "lang.json", "texcolors.json"))


# known names, in order: the Fabric/Quilt launchers and forks like Paper come with the vanilla server.jar too
JAR_PREFIXES = ("fabric-server-launch", "fabric-server", "quilt-server", "paper", "purpur", "folia", "pufferfish", "spigot", "server")


def find_jar(server_dir, configured=None):
    """Name of the jar to start: the one set in the config, otherwise the first one recognised; None if there is none."""
    if configured:
        return configured if os.path.isfile(os.path.join(server_dir, configured)) else None
    try:
        jars = sorted(n for n in os.listdir(server_dir) if n.lower().endswith(".jar") and "installer" not in n.lower())
    except OSError:
        return None
    for pre in JAR_PREFIXES:
        hit = next((n for n in jars if n.lower().startswith(pre)), None)
        if hit:
            return hit
    return jars[0] if len(jars) == 1 else None


def modloader(server_dir):
    """(loader, version, args file relative to server_dir for this OS) of a NeoForge/Forge server, else None."""
    args = "win_args.txt" if os.name == "nt" else "unix_args.txt"
    for loader, sub in (("neoforge", "net/neoforged/neoforge"), ("forge", "net/minecraftforge/forge")):
        base = os.path.join(server_dir, "libraries", *sub.split("/"))
        try:
            vers = [d for d in os.listdir(base) if os.path.isfile(os.path.join(base, d, args))]
        except OSError:
            continue
        if vers:
            v = max(vers, key=lambda s: [int(x) if x.isdigit() else 0 for x in re.split(r"[.-]", s)])
            return loader, v, "/".join(("libraries", *sub.split("/"), v, args))
    return None


_JAVA = {}   # Minecraft version -> required Java (from Mojang's manifest, once per process)


def jar_info(server_dir, jar=None):
    """(Minecraft version, required Java) from the server jar; None where it cannot be told."""
    ver = java = None
    for name in (jar, "server.jar"):
        if not name:
            continue
        try:
            with zipfile.ZipFile(os.path.join(server_dir, name)) as z:
                names = set(z.namelist())
                if "version.json" in names:   # vanilla, Paper and forks
                    v = json.loads(z.read("version.json"))
                    ver, java = ver or v.get("id"), v.get("java_version")
                    break
                if "install.properties" in names:   # Fabric launcher: downloads the vanilla jar on its first start
                    props = dict(x.split("=", 1) for x in z.read("install.properties").decode("utf-8", "replace").splitlines() if "=" in x)
                    ver = ver or props.get("game-version")
        except (OSError, ValueError, KeyError, zipfile.BadZipFile):
            pass
    if not ver:   # NeoForge/Forge: server.jar is only a starter, the vanilla jar sits in libraries/
        ml0 = modloader(server_dir)
        want = ml0[1].split("-")[0] if ml0 and ml0[0] == "forge" else ""   # NeoForge folder names carry no MC version: highest wins
        vk = lambda p: ([int(x) if x.isdigit() else 0 for x in re.split(r"[.-]", os.path.basename(os.path.dirname(p)))])   # noqa: E731
        for p in sorted(glob.glob(os.path.join(server_dir, "libraries", "net", "minecraft", "server", "*", "server-*.jar")),
                        key=lambda p: (bool(want) and os.path.basename(os.path.dirname(p)).split("-")[0] == want, vk(p)), reverse=True):
            try:
                with zipfile.ZipFile(p) as z:
                    v = json.loads(z.read("version.json"))
                ver, java = v.get("id"), v.get("java_version")
                break
            except (OSError, ValueError, KeyError, zipfile.BadZipFile):
                pass
        ml = None if ver else modloader(server_dir)
        if ml and ml[0] == "forge":   # folder is "<mc>-<forge>"
            ver = ml[1].split("-")[0]
    m = not ver and jar and re.search(r"-(\d+\.\d+(?:\.\d+)?)\.jar$", jar)   # BuildTools names it spigot-1.21.4.jar
    ver = m.group(1) if m else ver
    if ver and not java:
        if ver not in _JAVA:
            try:
                v = next(x for x in json.loads(_get(MANIFEST))["versions"] if x["id"] == ver)
                _JAVA[ver] = json.loads(_get(v["url"], v.get("sha1")))["javaVersion"]["majorVersion"]
            except Exception:  # noqa: BLE001  (offline or unknown version: retried at the next start)
                _JAVA[ver] = None
        java = _JAVA[ver]
    return ver, (int(java) if java else None)


def server_version(server_dir, jar=None):
    ver = jar_info(server_dir, jar or find_jar(server_dir))[0]
    if not ver:
        raise ValueError("cannot tell the Minecraft version of the server: pass it, e.g.  panel.py assets 26.3")
    return ver


def _get(url, sha1=None, sha256=None):
    # PaperMC asks every client to say who it is
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "ByzzCraft (github.com/muntalgk/ByzzCraft)"}), timeout=60) as r:
        data = r.read()
    if sha1 and hashlib.sha1(data).hexdigest() != sha1 or sha256 and hashlib.sha256(data).hexdigest() != sha256:
        raise OSError("Corrupted download: %s" % url)
    return data


def mojang_jar(version, kind="client"):
    """Official client.jar or server.jar of a version (None = latest release), SHA1 checked."""
    manifest = json.loads(_get(MANIFEST))
    version = version or manifest["latest"]["release"]
    v = next((x for x in manifest["versions"] if x["id"] == version), None)
    if v is None:
        raise ValueError("Mojang does not know version %s" % version)
    c = json.loads(_get(v["url"], v.get("sha1")))["downloads"][kind]
    return version, _get(c["url"], c["sha1"])


def paper_jar():
    """(version, file name, jar) of the newest Paper version whose latest build is stable, SHA256 checked."""
    for group in json.loads(_get(PAPER))["versions"].values():
        for v in group:
            try:
                b = json.loads(_get("%s/versions/%s/builds/latest" % (PAPER, v)))
            except OSError:   # a version without builds yet
                continue
            if b.get("channel") in ("STABLE", "RECOMMENDED"):
                d = b["downloads"]["server:default"]
                return v, d["name"], _get(d["url"], sha256=d["checksums"]["sha256"])
    raise ValueError("Paper has no stable build")


def fabric_jar():
    """(version, file name, jar) of Fabric's server launcher for the latest stable game, loader and installer:
    a single jar that downloads the vanilla server and the libraries at its first start."""
    game, loader, inst = (next(x["version"] for x in json.loads(_get("%s/%s" % (FABRIC, k))) if x.get("stable"))
                          for k in ("game", "loader", "installer"))
    return (game, "fabric-server-mc.%s-loader.%s-launcher.%s.jar" % (game, loader, inst),
            _get("%s/loader/%s/%s/%s/server/jar" % (FABRIC, game, loader, inst)))


def download_server(server_dir, version=None, log=print, kind="vanilla"):
    """New server: vanilla (Mojang's server.jar, version None = latest), Paper or Fabric (latest stable) into the
    folder, created if missing. Returns the version."""
    log("Downloading the Minecraft server (%s%s)..." % (kind, " " + version if version else ""))
    if kind == "paper":
        version, name, jar = paper_jar()
    elif kind == "fabric":
        version, name, jar = fabric_jar()
    else:
        (version, jar), name = mojang_jar(version, "server"), "server.jar"
    os.makedirs(server_dir, exist_ok=True)
    tmp = os.path.join(server_dir, name + ".part")
    with open(tmp, "wb") as f:
        f.write(jar)
    os.replace(tmp, os.path.join(server_dir, name))
    log("Minecraft server %s saved in %s" % (name, server_dir))
    return version


# grey in the jar, colored by the biome in the game: the inventory shows them with the default color
GRASS, FOLIAGE = (145, 189, 89), (72, 181, 24)
TINT = {"grass_block": GRASS, "short_grass": GRASS, "tall_grass": GRASS, "fern": GRASS, "large_fern": GRASS, "bush": GRASS,
        "oak_leaves": FOLIAGE, "jungle_leaves": FOLIAGE, "acacia_leaves": FOLIAGE, "dark_oak_leaves": FOLIAGE, "vine": FOLIAGE,
        "mangrove_leaves": (146, 198, 72), "birch_leaves": (128, 167, 85), "spruce_leaves": (97, 153, 97), "lily_pad": (32, 128, 48)}
_TINTED = {}


def icon_png(iid, path):
    """The icon's PNG; grey grass, leaves and vines get their inventory color."""
    with open(path, "rb") as f:
        data = f.read()
    c = TINT.get(iid)
    if c is None or os.path.basename(os.path.dirname(path)) == "render":   # rendered icons are tinted already
        return data
    if iid not in _TINTED:
        try:
            w, h, px = png_rgba(data)
        except Exception:
            return data
        op = [p for p in px if p[3]]
        if not op or sum(max(p[:3]) - min(p[:3]) < 16 for p in op) < 0.9 * len(op):   # already colored: a resource pack
            _TINTED[iid] = data
        else:
            _TINTED[iid] = _png(bytes(v for r, g, b, a in px for v in (r * c[0] // 255, g * c[1] // 255, b * c[2] // 255, a)), w, h)
    return _TINTED[iid]


def png_rgba(data):
    """Non-interlaced PNG with 1-8 bit depth -> (width, height, [(r, g, b, a), ...])."""
    pos, idat, plte, trns = 8, b"", b"", b""
    while pos < len(data):
        n, typ = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + n]
        pos += 12 + n
        if typ == b"IHDR":
            w, h, bd, ct, _, _, il = struct.unpack(">IIBBBBB", body)
        elif typ == b"PLTE":
            plte = body
        elif typ == b"tRNS":
            trns = body
        elif typ == b"IDAT":
            idat += body
    if bd > 8 or il:
        raise ValueError("unsupported PNG")
    ch = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[ct]
    bpp = max(1, ch * bd // 8)
    stride = (w * ch * bd + 7) // 8
    d = zlib.decompressobj()   # bounded: a zip bomb must not exhaust the memory
    raw = d.decompress(idat, (stride + 1) * h + 1024)
    if d.unconsumed_tail:
        raise ValueError("PNG data larger than its header says")
    px, prev, i = [], bytearray(stride), 0
    key = struct.unpack(">H", trns[:2])[0] if ct == 0 and trns else struct.unpack(">3H", trns[:6]) if ct == 2 and trns else None
    for _ in range(h):
        f, line = raw[i], bytearray(raw[i + 1:i + 1 + stride])
        i += 1 + stride
        for x in range(stride):
            a = line[x - bpp] if x >= bpp else 0
            b = prev[x]
            c = prev[x - bpp] if x >= bpp else 0
            if f == 1:
                line[x] = (line[x] + a) & 255
            elif f == 2:
                line[x] = (line[x] + b) & 255
            elif f == 3:
                line[x] = (line[x] + (a + b) // 2) & 255
            elif f == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[x] = (line[x] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        prev = line
        per = 8 // bd if bd < 8 else 1
        vals = line if bd == 8 else [(line[k // per] >> (8 - bd - (k % per) * bd)) & ((1 << bd) - 1) for k in range(w * ch)]
        for x in range(w):
            v = vals[x * ch:(x + 1) * ch]
            if ct == 3:
                j = v[0]
                px.append((plte[3 * j], plte[3 * j + 1], plte[3 * j + 2], trns[j] if j < len(trns) else 255))
            elif ct == 0:
                g = v[0] * 255 // ((1 << bd) - 1)
                px.append((g, g, g, 0 if v[0] == key else 255))
            elif ct == 4:
                px.append((v[0], v[0], v[0], v[1]))
            elif ct == 2:
                px.append((v[0], v[1], v[2], 0 if tuple(v) == key else 255))
            else:
                px.append(tuple(v))
    return w, h, px


def tex_color(data):
    """[r, g, b, mean alpha] of the first frame (animated ones are vertical strips); None if fully transparent."""
    w, h, px = png_rgba(data)
    px = px[:w * min(w, h)]
    ws = sum(p[3] for p in px)
    if not ws:
        return None
    a = round(ws / 255 / len(px), 2)
    return [round(sum(p[i] * p[3] for p in px) / ws) for i in range(3)] + [int(a) if a == int(a) else a]


# ---------------------------------------------------------------- 3D inventory icons of block models
# face corners in the order of the game's vertex list (v0 = top-left of the uv rectangle) and normal; coordinates: 0 = from, 1 = to
_FACES = {"down": ((0, 0, 1), (0, 0, 0), (1, 0, 0), (1, 0, 1)), "up": ((0, 1, 0), (0, 1, 1), (1, 1, 1), (1, 1, 0)),
          "north": ((1, 1, 0), (1, 0, 0), (0, 0, 0), (0, 1, 0)), "south": ((0, 1, 1), (0, 0, 1), (1, 0, 1), (1, 1, 1)),
          "west": ((0, 1, 0), (0, 0, 0), (0, 0, 1), (0, 1, 1)), "east": ((1, 1, 1), (1, 0, 1), (1, 0, 0), (1, 1, 0))}
_NORMAL = {"down": (0, -1, 0), "up": (0, 1, 0), "north": (0, 0, -1), "south": (0, 0, 1), "west": (-1, 0, 0), "east": (1, 0, 0)}
GUI_BLOCK = {"rotation": [30, 225, 0], "scale": [0.625] * 3}   # the game's default for blocks
ICON = 64
MAX_ELEMENTS = 256   # a bigger model is too slow to draw: the flat icon stays


def _default_uv(name, f, t):
    return {"down": (f[0], 16 - t[2], t[0], 16 - f[2]), "up": (f[0], f[2], t[0], t[2]), "north": (16 - t[0], 16 - t[1], 16 - f[0], 16 - f[1]),
            "south": (f[0], 16 - t[1], t[0], 16 - f[1]), "west": (f[2], 16 - t[1], t[2], 16 - f[1]), "east": (16 - t[2], 16 - t[1], 16 - f[2], 16 - f[1])}[name]


def _rot(v, axis, deg):
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    x, y, z = v
    return {"x": (x, c * y - s * z, s * y + c * z), "y": (c * x + s * z, y, -s * x + c * z), "z": (c * x - s * y, s * x + c * y, z)}[axis]


def resolve_model(read, ref):
    """{textures, elements, gui} of a model with its parent chain merged; read(ref) -> the model's JSON dict."""
    chain = []
    while isinstance(ref, str) and len(chain) < 9:
        chain.append(read(ref))
        ref = chain[-1].get("parent")
    tex = {}
    for m in reversed(chain):
        tex.update({k: v.get("sprite") if isinstance(v, dict) else v for k, v in (m.get("textures") if isinstance(m.get("textures"), dict) else {}).items() if isinstance(v, (str, dict))})   # 26.x: {"sprite": ...}
    els = next((m["elements"] for m in chain if isinstance(m.get("elements"), list)), None)
    gui = next((m["display"]["gui"] for m in chain if isinstance(m.get("display"), dict) and isinstance(m["display"].get("gui"), dict)), None)
    return {"textures": tex, "elements": els, "gui": gui}


def texloader(get):
    """load(ref) -> (w, h, pixels) of the first frame, cached; get(ref) -> PNG bytes or None."""
    cache = {}

    def load(ref):
        if ref not in cache:
            try:
                w, h, px = png_rgba(get(ref))
                cache[ref] = (w, w, px[:w * w]) if h > w and h % w == 0 else (w, h, px)
            except Exception:
                cache[ref] = None
        return cache[ref]
    return load


def render_model(model, load, tint=None):
    """64x64 PNG of a block model as in the inventory (orthographic, game gui transform, painter's algorithm); None when it has no elements or is flat (layer0)."""
    els, tex = model["elements"], model["textures"]
    if not els or "layer0" in tex or len(els) > MAX_ELEMENTS:
        return None
    g = model["gui"] or GUI_BLOCK
    rx, ry, rz = (g.get("rotation") or GUI_BLOCK["rotation"])
    sc = g.get("scale") or GUI_BLOCK["scale"]
    tr = g.get("translation") or (0, 0, 0)

    def cam(p, normal=False):   # model units -> centered cube, gui rotation, scale; the game's GUI mirrors x, so the north face ends up on the left
        v = p if normal else tuple(c / 16 - 0.5 for c in p)
        for ax, a in (("z", rz), ("y", ry), ("x", rx)):
            v = _rot(v, ax, a)
        return v if normal else (-(v[0] * sc[0] + tr[0] / 16), v[1] * sc[1] + tr[1] / 16, v[2] * sc[2] + tr[2] / 16)

    quads = []
    for el in els:
        f, t = el["from"], el["to"]
        r = el.get("rotation") or {}
        lit = el.get("shade") is not False
        for name, face in (el.get("faces") or {}).items():
            if name not in _FACES:
                continue
            ref = face.get("texture")
            for _ in range(8):   # "#ref" chains
                if not (isinstance(ref, str) and ref.startswith("#")):
                    break
                ref = tex.get(ref[1:])
            img = load(ref) if isinstance(ref, str) and not ref.startswith("#") else None
            if not img:
                continue
            pts = [tuple((t if k else f)[i] for i, k in enumerate(c)) for c in _FACES[name]]
            n = _NORMAL[name]
            if r.get("angle") and r.get("axis") in ("x", "y", "z"):
                o = r.get("origin") or (8, 8, 8)
                pts = [tuple(a + b for a, b in zip(_rot(tuple(p[i] - o[i] for i in range(3)), r["axis"], r["angle"]), o)) for p in pts]
                n = _rot(n, r["axis"], r["angle"])
            if cam(n, True)[2] <= 1e-6:   # faces away from the camera
                continue
            sh = (0.8 * n[2] ** 2 + 0.6 * n[0] ** 2 + (1.0 if n[1] > 0 else 0.5) * n[1] ** 2) if lit else 1.0
            u1, v1, u2, v2 = face.get("uv") or _default_uv(name, f, t)
            uv = [(u1, v1), (u1, v2), (u2, v2), (u2, v1)]
            k = (face.get("rotation") or 0) // 90 % 4
            uv = [uv[(i + k) % 4] for i in range(4)]
            s = [cam(p) for p in pts]
            tc = tint if tint and face.get("tintindex", -1) >= 0 else None
            quads.append((sum(p[2] for p in s), s, uv, img, sh, tc))
    quads.sort(key=lambda q: q[0])
    out = bytearray(ICON * ICON * 4)
    for _, s, uv, (tw, th, px), sh, tc in quads:
        pt = [(ICON / 2 + p[0] * ICON, ICON / 2 - p[1] * ICON) for p in s]
        (ax, ay), (bx, by), (dx, dy) = pt[0], pt[1], pt[3]
        e1x, e1y, e2x, e2y = dx - ax, dy - ay, bx - ax, by - ay   # a runs v0 -> v3, b runs v0 -> v1
        det = e1x * e2y - e1y * e2x
        if abs(det) < 1e-6:
            continue
        du, dv = (uv[3][0] - uv[0][0], uv[3][1] - uv[0][1]), (uv[1][0] - uv[0][0], uv[1][1] - uv[0][1])
        mul = [sh * (c / 255 if tc else 1) for c in tc] if tc else [sh] * 3
        for y in range(max(0, int(min(p[1] for p in pt))), min(ICON, int(max(p[1] for p in pt)) + 1)):
            for x in range(max(0, int(min(p[0] for p in pt))), min(ICON, int(max(p[0] for p in pt)) + 1)):
                px_, py_ = x + 0.5 - ax, y + 0.5 - ay
                a, b = (px_ * e2y - py_ * e2x) / det, (e1x * py_ - e1y * px_) / det
                if not (-1e-9 <= a <= 1 + 1e-9 and -1e-9 <= b <= 1 + 1e-9):
                    continue
                tx = min(tw - 1, max(0, int((uv[0][0] + a * du[0] + b * dv[0]) / 16 * tw)))
                ty = min(th - 1, max(0, int((uv[0][1] + a * du[1] + b * dv[1]) / 16 * th)))
                r_, g_, b_, a_ = px[ty * tw + tx]
                if not a_:
                    continue
                c = (int(r_ * mul[0]), int(g_ * mul[1]), int(b_ * mul[2]))
                i = (y * ICON + x) * 4
                oa = out[i + 3]
                if a_ < 255 and oa:
                    fa = a_ / 255
                    c = tuple(int(n_ * fa + out[i + j] * (1 - fa)) for j, n_ in enumerate(c))
                    a_ = int(a_ + oa * (1 - fa))
                out[i:i + 4] = bytes((*c, a_))
    return _png(out, ICON, ICON)


def render_vanilla(z, dest):
    """dest/render/<id>.png for the vanilla block items drawn from a model with elements."""
    def read(ref):
        ns, _, p = ref.rpartition(":")
        try:
            v = json.loads(z.read("assets/%s/models/%s.json" % (ns or "minecraft", p)))
            return v if isinstance(v, dict) else {}
        except Exception:
            return {}

    def get(ref):
        ns, _, p = ref.rpartition(":")
        return z.read("assets/%s/textures/%s.png" % (ns or "minecraft", p)) if ns in ("", "minecraft") else None
    load = texloader(get)
    pre = "assets/minecraft/items/"
    for n in z.namelist():
        if not (n.startswith(pre) and n.endswith(".json")) or "/" in n[len(pre):]:
            continue
        iid = n[len(pre):-5]
        try:
            m = _first_model(json.loads(z.read(n)))
            png = m and render_model(resolve_model(read, m), load, TINT.get(iid))   # an error here leaves the flat icon
            if png:
                os.makedirs(os.path.join(dest, "render"), exist_ok=True)
                with open(os.path.join(dest, "render", iid + ".png"), "wb") as f:
                    f.write(png)
        except Exception:
            pass


# items the game draws as a 3D model, with no item texture: the icon is cut out of the entity texture
CHESTS = {"chest": "normal", "trapped_chest": "trapped", "ender_chest": "ender", "copper_chest": "copper",
          "exposed_copper_chest": "copper_exposed", "weathered_copper_chest": "copper_weathered",
          "oxidized_copper_chest": "copper_oxidized"}


def _blit(out, src, w, sx, sy, dx, dy, cw, ch):
    for y in range(ch):
        for x in range(cw):
            out[(dy + y) * 16 + dx + x] = src[(sy + y) * w + sx + x]


def entity_icons(z):
    """{name: 16x16 png} for chests and the shield; skips those this version does not have (e.g. copper before 1.21.9)."""
    out = {}
    for name, src in CHESTS.items():
        try:
            w, _, e = png_rgba(z.read(TEX + "entity/chest/%s.png" % src))
        except KeyError:
            continue
        px = [(0, 0, 0, 0)] * 256
        _blit(px, e, w, 14, 14, 1, 1, 14, 5)    # lid
        _blit(px, e, w, 14, 33, 1, 6, 14, 10)   # body
        _blit(px, e, w, 1, 1, 7, 4, 2, 4)       # latch
        out[name] = _png(bytearray(c for p in px for c in p), 16, 16)
    try:
        w, _, e = png_rgba(z.read(TEX + "entity/shield/shield_base_nopattern.png"))
    except KeyError:
        return out
    px = [(0, 0, 0, 0)] * 256
    for i in range(16):   # 10x20 front face scaled to 16 rows
        _blit(px, e, w, 2, 2 + int((i + 0.5) * 20 / 16), 3, i, 10, 1)
    out["shield"] = _png(bytearray(c for p in px for c in p), 16, 16)
    return out


def extract(jar, dest):
    """Extracts from the client jar into dest/ (a temporary folder, renamed by the caller)."""
    colors = {}
    with zipfile.ZipFile(io.BytesIO(jar)) as z:
        for n in z.namelist():
            if n.startswith(MODELS) and n.endswith(".json") and n[len(MODELS):].count("/") == 1 and n[len(MODELS):].startswith(("block/", "item/")):
                os.makedirs(os.path.join(dest, "models", n[len(MODELS):].split("/")[0]), exist_ok=True)   # parents of the mods' models
                with open(os.path.join(dest, "models", n[len(MODELS):]), "wb") as f:
                    f.write(z.read(n))
            if not n.endswith(".png"):
                if n == "assets/minecraft/lang/en_us.json":
                    with open(os.path.join(dest, "lang.json"), "wb") as f:
                        f.write(z.read(n))
                continue
            if n.startswith(TEX + "gui/sprites/container/slot/") and os.path.basename(n)[:-4] in SLOTS:
                out = os.path.join("item", "empty_armor_slot_" + os.path.basename(n))
            else:
                src = next((s for s in DIRS if n.startswith(s)), None)
                if src is None:
                    continue
                rel = n[len(src):]
                if DIRS[src] in ("block", "item") and "/" in rel:
                    continue
                if DIRS[src] == "hud" and not rel.startswith(HUD_PREFIX):
                    continue
                if DIRS[src] == "skin" and not rel.endswith(("/steve.png", "/alex.png")):
                    continue
                out = os.path.join(DIRS[src], rel.replace("/", "_"))
            data = z.read(n)
            os.makedirs(os.path.join(dest, os.path.dirname(out)), exist_ok=True)
            with open(os.path.join(dest, out), "wb") as f:
                f.write(data)
            if out.startswith("block"):
                c = tex_color(data)
                if c:
                    colors[os.path.basename(out)[:-4]] = c
        os.makedirs(os.path.join(dest, "item"), exist_ok=True)
        os.makedirs(os.path.join(dest, "render"), exist_ok=True)
        render_vanilla(z, dest)
        for name, data in entity_icons(z).items():
            with open(os.path.join(dest, "item", name + ".png"), "wb") as f:
                f.write(data)
    with open(os.path.join(dest, "texcolors.json"), "w", encoding="utf-8") as f:
        json.dump(colors, f, separators=(",", ":"))


LOCAL_LANG = os.path.join(ASSETS, "lang-local.json")   # lang.json stays English: the server log's death phrases are matched with it


def mc_lang(lang):
    """Minecraft's code for a panel language: it -> it_it, en -> en_us."""
    return {"en": "en_us"}.get(lang, "%s_%s" % (lang, lang))


def local_lang_ok(lang):
    """True if lang-local.json already holds this language (or none is needed)."""
    if mc_lang(lang) == "en_us":
        return not os.path.exists(LOCAL_LANG)
    try:
        with open(LOCAL_LANG, encoding="utf-8") as f:
            return json.load(f).get("_code") == mc_lang(lang)
    except (OSError, ValueError):
        return False


def install_lang(version, lang):
    """Vanilla names in the panel's language into lang-local.json: not in the client jar, only in Mojang's asset index."""
    code = mc_lang(lang)
    if code == "en_us":
        if os.path.exists(LOCAL_LANG):
            os.remove(LOCAL_LANG)
        return
    v = next((x for x in json.loads(_get(MANIFEST))["versions"] if x["id"] == version), None)
    if v is None:
        raise ValueError("Mojang does not know version %s" % version)
    idx = json.loads(_get(v["url"], v.get("sha1")))["assetIndex"]
    obj = json.loads(_get(idx["url"], idx.get("sha1")))["objects"].get("minecraft/lang/%s.json" % code)
    if obj is None:
        raise ValueError("Mojang has no %s translation" % code)
    h = obj["hash"]
    data = json.loads(_get("https://resources.download.minecraft.net/%s/%s" % (h[:2], h), h))
    data["_code"] = code
    write_json(LOCAL_LANG, data, separators=(",", ":"), ensure_ascii=False)


def install(server_dir, version=None, log=print, jar=None, lang=None):
    version = version or server_version(server_dir, jar)
    log("Downloading the Minecraft %s client from Mojang..." % version)
    _, jar = mojang_jar(version, "client")
    tmp = os.path.join(DATA, "assets.download-tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    extract(jar, tmp)
    os.makedirs(ASSETS, exist_ok=True)
    # overwrites file by file, deleting nothing: icons added by hand and the logo stay
    shutil.copytree(tmp, ASSETS, dirs_exist_ok=True)
    shutil.rmtree(tmp, ignore_errors=True)
    if lang:
        try:
            install_lang(version, lang)
        except Exception as e:  # noqa: BLE001  (names stay English: icons and maps still work)
            log("Minecraft names in %s not downloaded: %s" % (lang, e))
    log("Minecraft %s assets installed in %s" % (version, ASSETS))


# ---------------------------------------------------------------- icons, names and map colors of the mods
# no segment starts with a dot: ".." can never reach a path
_NS_OK, _ID_OK = re.compile(r"^[a-z0-9_-][a-z0-9_.-]*$"), re.compile(r"^[a-z0-9_-][a-z0-9_.-]*(/[a-z0-9_-][a-z0-9_.-]*)*$")
MAX_ENTRY, MAX_PIXELS = 1 << 20, 512 * 512   # a bigger entry or texture is skipped: a zip bomb must not exhaust the memory
FACES = ("layer0", "top", "all", "end", "side", "front", "particle", "texture")
LANG_PREFIX = ("item.", "block.", "entity.")


RENDER_V = 2   # bump when the 3D renderer changes: the mods build is redone


def _assets_marker():
    """Changes when the vanilla models/textures the mod builds read are (re)installed."""
    try:
        return int(os.stat(os.path.join(ASSETS, "models", "block", "cube.json")).st_mtime)
    except OSError:
        return 0


def mods_key(server_dir, lang="en"):
    md = os.path.join(server_dir, "mods")
    try:
        return [[lang, RENDER_V, _assets_marker()]] + [[n, st.st_size, int(st.st_mtime)] for n in sorted(os.listdir(md)) if n.endswith(".jar") for st in [os.stat(os.path.join(md, n))]]
    except OSError:
        return None


def _first_model(o):
    """First plain model of an item model definition or a blockstate (select/condition/range_dispatch, variants, multipart...)."""
    if isinstance(o, dict):
        for k in ("model", "base"):
            if isinstance(o.get(k), str):
                return o[k]
        o = list(o.values())
    for v in o if isinstance(o, list) else ():
        if isinstance(v, (dict, list)):
            m = _first_model(v)
            if m:
                return m
    return None


def _small(d):
    """The PNG, or None when its header says it is too big to decode."""
    w, h = struct.unpack(">II", d[16:24])
    return d if w * h <= MAX_PIXELS else None


class _Mods:
    """All the files of the enabled mod jars: PNG and JSON under assets/, found by path whatever jar holds them."""
    def __init__(self, jars):
        self.zips, self.idx, self.cache = [], {}, {}
        for j in jars:
            try:
                z = zipfile.ZipFile(j)
                self.zips.append(z)
                for n in z.namelist():
                    if n.startswith("assets/") and n.endswith((".png", ".json")):
                        self.idx.setdefault(n, z)
            except Exception:
                pass

    def read(self, path):
        z = self.idx.get(path)
        try:
            if not z or z.getinfo(path).file_size > MAX_ENTRY:
                return None
            return _small(z.read(path)) if path.endswith(".png") else z.read(path)
        except Exception:
            return None

    def json(self, path):
        try:
            v = json.loads(self.read(path))
            return v if isinstance(v, dict) else {}
        except Exception:
            return {}

    def png(self, ref):
        """PNG bytes of a texture reference ('ns:block/x' or 'block/x' = minecraft); the vanilla ones come from the installed assets."""
        ns, _, p = ref.rpartition(":")
        ns = ns or "minecraft"
        d = self.read("assets/%s/textures/%s.png" % (ns, p))
        if d is None and ns == "minecraft" and re.match(r"^(block|item)/[a-z0-9_-][a-z0-9_.-]*$", p):
            try:
                with open(os.path.join(ASSETS, p + ".png"), "rb") as f:
                    d = _small(f.read())
            except Exception:
                pass
        return d

    def model(self, ref):
        if ref not in self.cache:
            ns, _, p = ref.rpartition(":")
            d = self.json("assets/%s/models/%s.json" % (ns or "minecraft", p))
            if not d and ns in ("", "minecraft") and re.match(r"^(block|item)/[a-z0-9_-][a-z0-9_.-]*$", p):   # vanilla: the copy in the assets
                try:
                    with open(os.path.join(ASSETS, "models", p + ".json"), encoding="utf-8") as f:
                        d = json.load(f)
                except (OSError, ValueError):
                    pass
            self.cache[ref] = d if isinstance(d, dict) else {}
        return self.cache[ref]

    def face_ref(self, model, faces=FACES):
        """Texture reference of the first usable face of a model (parent chain, "#ref" chains, 26.x sprites), None if none."""
        t = resolve_model(self.model, model)["textures"] if model else {}
        for f in faces:
            v = t.get(f)
            for _ in range(8):   # "#ref" chains
                if not (isinstance(v, str) and v.startswith("#")):
                    break
                v = t.get(v[1:])
            if isinstance(v, str) and v and not v.startswith("#") and self.png(v):
                return v
        return None

    def face(self, model, faces=FACES):
        """PNG of the first usable face of a model."""
        v = self.face_ref(model, faces)
        return self.png(v) if v else None


def _first_frame(d):
    """Animated textures are vertical strips: the icon is the first frame."""
    try:
        w, h = struct.unpack(">II", d[16:24])
        if h > w and h % w == 0:
            w, h, px = png_rgba(d)
            return _png(bytes(v for p in px[:w * w] for v in p), w, w)
    except Exception:
        pass
    return d


class Cancelled(Exception):
    """The build was asked to stop (a mod is being changed: on Windows the open jars could not be renamed)."""


def extract_mods(server_dir, dest, lang="en", stop=None):
    """Builds dest/ (icons <ns>/<id>.png, lang.json, texcolors.json) from the enabled jars in mods/, swapped in whole.
    0 when the mods are the same as in the last build, 1 when rebuilt, 2 when rebuilt and the block colors differ from the previous build.
    stop: an Event; once set, the jars are closed and Cancelled is raised, nothing is swapped in."""
    key = mods_key(server_dir, lang)
    if key is None:
        return 0
    try:
        with open(os.path.join(dest, "key.json"), encoding="utf-8") as f:
            if json.load(f) == key:
                return 0
    except (OSError, ValueError):
        pass
    md = os.path.join(server_dir, "mods")
    M = _Mods(os.path.join(md, k[0]) for k in key[1:])
    tmp = dest + ".tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    names, colors, ids = {}, {}, {}
    load, renders, tex_colors = texloader(M.png), {}, {}
    real_tmp = os.path.realpath(tmp) + os.sep

    def check():
        if stop is not None and stop.is_set():
            raise Cancelled()
    try:
        for path in M.idx:
            check()
            a = path.split("/", 3)
            if len(a) < 4 or not _NS_OK.match(a[1]):
                continue
            ns, rest = a[1], a[3]
            if a[2] == "items" and rest.endswith(".json"):
                ids[ns, rest[:-5]] = 1
            elif a[2] in ("textures", "models") and rest.startswith("item/") and rest.endswith((".png", ".json")) and rest.count("/") == 1:
                ids.setdefault((ns, rest[5:rest.rindex(".")]), 1)
            elif a[2] == "blockstates" and rest.endswith(".json"):
                try:
                    v = M.face_ref(_first_model(M.json(path)) or "", ("top", "all", "end", "side", "particle", "texture", "front"))
                    if v not in tex_colors:   # many blocks share a texture: decode it once
                        d = M.png(v) if v else None
                        tex_colors[v] = tex_color(d) if d else None
                    c = tex_colors[v]
                    if c:
                        colors[ns + ":" + rest[:-5]] = c
                except Exception:
                    pass
        for ns, iid in ids:
            check()
            if not _ID_OK.match(iid):
                continue
            try:
                d = None
                m = _first_model(M.json("assets/%s/items/%s.json" % (ns, iid)))
                ref = m or "%s:item/%s" % (ns, iid)
                if ref not in renders:   # many items share a model
                    try:
                        renders[ref] = render_model(resolve_model(M.model, ref), load)   # 3D block icon; None for flat or code-drawn items
                    except Exception:
                        renders[ref] = None   # the flat icon is still made
                d = renders[ref]
                if not d:
                    d = M.face(m) if m else None
                    d = d or M.face("%s:item/%s" % (ns, iid)) or M.png("%s:item/%s" % (ns, iid)) or M.png("%s:block/%s" % (ns, iid))
                    d = d and _first_frame(d)
                if d:
                    out = os.path.join(tmp, ns, iid.replace("/", "__") + ".png")
                    if not os.path.realpath(out).startswith(real_tmp):
                        continue
                    os.makedirs(os.path.dirname(out), exist_ok=True)
                    with open(out, "wb") as f:
                        f.write(d)
            except Exception:
                pass
        for code in ("en_us", None):   # English first, then the panel language over it
            for path in M.idx:
                b = os.path.basename(path)
                if "/lang/" in path and b.endswith(".json") and (b == "en_us.json" if code else lang != "en" and b.startswith(lang + "_")):
                    names.update({k: v for k, v in M.json(path).items() if isinstance(v, str) and k.startswith(LANG_PREFIX)})
    except Cancelled:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    finally:
        for z in M.zips:
            z.close()
    try:
        with open(os.path.join(dest, "texcolors.json"), encoding="utf-8") as f:
            same = json.load(f) == colors
    except (OSError, ValueError):
        same = False
    for n, v in (("lang.json", names), ("texcolors.json", colors), ("key.json", key)):
        with open(os.path.join(tmp, n), "w", encoding="utf-8") as f:
            json.dump(v, f, separators=(",", ":"))
    old = dest + ".old"
    shutil.rmtree(old, ignore_errors=True)
    if os.path.exists(dest):
        os.replace(dest, old)
    try:
        os.replace(tmp, dest)
    except OSError:
        if os.path.exists(old):
            os.replace(old, dest)   # keep the previous build
        raise
    shutil.rmtree(old, ignore_errors=True)
    return 1 if same else 2
