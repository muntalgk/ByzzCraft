"""Downloads from Mojang the textures and translations the panel shows (they cannot be redistributed).

Usage:  python panel.py assets [VERSION]     (without a version it is read from the server jar)
"""
import hashlib
import io
import json
import os
import re
import shutil
import struct
import urllib.request
import zipfile
import zlib

from .mapgen import _png

from .paths import ASSETS, DATA  # noqa: E402
MANIFEST = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
PAPER = "https://fill.papermc.io/v3/projects/paper"
FABRIC = "https://meta.fabricmc.net/v2/versions"
SERVERS = ("vanilla", "paper", "fabric")   # what the setup can download; Spigot is built with BuildTools, not downloaded
TEX = "assets/minecraft/textures/"
# folder in the jar -> folder in assets/ (subfolders become a prefix: hud/heart/full.png -> hud/heart_full.png)
DIRS = {TEX + "block/": "block", TEX + "item/": "item", TEX + "gui/sprites/hud/": "hud", TEX + "entity/player/": "skin"}
HUD_PREFIX = ("heart", "food_", "armor_", "experience_bar_")
SLOTS = {"boots", "chestplate", "helmet", "leggings", "shield"}   # icons of the empty armour slots in the inventory


def present():
    return all(os.path.exists(os.path.join(ASSETS, p)) for p in ("block", "item", "hud", "skin", "lang.json", "texcolors.json"))


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
    if c is None:
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
    raw = zlib.decompress(idat)
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
        for name, data in entity_icons(z).items():
            with open(os.path.join(dest, "item", name + ".png"), "wb") as f:
                f.write(data)
    with open(os.path.join(dest, "texcolors.json"), "w", encoding="utf-8") as f:
        json.dump(colors, f, separators=(",", ":"))


def install(server_dir, version=None, log=print, jar=None):
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
    log("Minecraft %s assets installed in %s" % (version, ASSETS))
