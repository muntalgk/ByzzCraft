"""Scarica dal sito di Mojang le texture e le traduzioni mostrate dal pannello (non si possono ridistribuire).

Uso:  python panel.py assets [VERSIONE]     (senza versione la legge da server.jar)
"""
import hashlib
import io
import json
import os
import shutil
import struct
import urllib.request
import zipfile
import zlib

from mapgen import _png

HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(HERE, "assets")
MANIFEST = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
TEX = "assets/minecraft/textures/"
# cartella nel jar -> cartella in assets/ (le sottocartelle diventano prefisso: hud/heart/full.png -> hud/heart_full.png)
DIRS = {TEX + "block/": "block", TEX + "item/": "item", TEX + "gui/sprites/hud/": "hud", TEX + "entity/player/": "skin"}
HUD_PREFIX = ("heart", "food_", "armor_", "experience_bar_")
SLOTS = {"boots", "chestplate", "helmet", "leggings", "shield"}   # icone degli slot armatura vuoti nell'inventario


def present():
    return all(os.path.exists(os.path.join(ASSETS, p)) for p in ("block", "item", "hud", "skin", "lang.json", "texcolors.json"))


def server_version(server_dir):
    with zipfile.ZipFile(os.path.join(server_dir, "server.jar")) as z:
        return json.loads(z.read("version.json"))["id"]


def _get(url, sha1=None):
    with urllib.request.urlopen(url, timeout=60) as r:
        data = r.read()
    if sha1 and hashlib.sha1(data).hexdigest() != sha1:
        raise OSError("Corrupted download: %s" % url)
    return data


def client_jar(version):
    versions = json.loads(_get(MANIFEST))["versions"]
    v = next((x for x in versions if x["id"] == version), None)
    if v is None:
        raise ValueError("Mojang does not know version %s" % version)
    c = json.loads(_get(v["url"], v.get("sha1")))["downloads"]["client"]
    return _get(c["url"], c["sha1"])


def png_rgba(data):
    """PNG non interlacciato con profondita' 1-8 bit -> (larghezza, altezza, [(r, g, b, a), ...])."""
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
    """[r, g, b, alfa media] del primo frame (le animate sono strisce verticali); None se tutta trasparente."""
    w, h, px = png_rgba(data)
    px = px[:w * min(w, h)]
    ws = sum(p[3] for p in px)
    if not ws:
        return None
    a = round(ws / 255 / len(px), 2)
    return [round(sum(p[i] * p[3] for p in px) / ws) for i in range(3)] + [int(a) if a == int(a) else a]


# oggetti che il gioco disegna come modello 3D, senza texture d'oggetto: icona ritagliata dalla texture dell'entita'
CHESTS = {"chest": "normal", "trapped_chest": "trapped", "ender_chest": "ender", "copper_chest": "copper",
          "exposed_copper_chest": "copper_exposed", "weathered_copper_chest": "copper_weathered",
          "oxidized_copper_chest": "copper_oxidized"}


def _blit(out, src, w, sx, sy, dx, dy, cw, ch):
    for y in range(ch):
        for x in range(cw):
            out[(dy + y) * 16 + dx + x] = src[(sy + y) * w + sx + x]


def entity_icons(z):
    """{nome: png 16x16} per forzieri e scudo; salta quelli che questa versione non ha (es. rame prima della 1.21.9)."""
    out = {}
    for name, src in CHESTS.items():
        try:
            w, _, e = png_rgba(z.read(TEX + "entity/chest/%s.png" % src))
        except KeyError:
            continue
        px = [(0, 0, 0, 0)] * 256
        _blit(px, e, w, 14, 14, 1, 1, 14, 5)    # coperchio
        _blit(px, e, w, 14, 33, 1, 6, 14, 10)   # corpo
        _blit(px, e, w, 1, 1, 7, 4, 2, 4)       # serratura
        out[name] = _png(bytearray(c for p in px for c in p), 16, 16)
    try:
        w, _, e = png_rgba(z.read(TEX + "entity/shield/shield_base_nopattern.png"))
    except KeyError:
        return out
    px = [(0, 0, 0, 0)] * 256
    for i in range(16):   # faccia frontale 10x20 ridotta a 16 righe
        _blit(px, e, w, 2, 2 + int((i + 0.5) * 20 / 16), 3, i, 10, 1)
    out["shield"] = _png(bytearray(c for p in px for c in p), 16, 16)
    return out


def extract(jar, dest):
    """Estrae dal client jar in dest/ (cartella temporanea, poi rinominata dal chiamante)."""
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


def install(server_dir, version=None, log=print):
    version = version or server_version(server_dir)
    log("Downloading the Minecraft %s client from Mojang..." % version)
    jar = client_jar(version)
    tmp = os.path.join(HERE, "assets.download-tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    extract(jar, tmp)
    os.makedirs(ASSETS, exist_ok=True)
    # sovrascrive file per file, senza cancellare: icone aggiunte a mano e logo restano
    shutil.copytree(tmp, ASSETS, dirs_exist_ok=True)
    shutil.rmtree(tmp, ignore_errors=True)
    log("Minecraft %s assets installed in %s" % (version, ASSETS))
