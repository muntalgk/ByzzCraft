"""Top-down PNG tiles of a Minecraft Java world (1.18 and newer), read from the region files (.mca).

Standard library only. API used by the web panel:
    DIMS, list_regions, render_region, get_tile
CLI:
    python -m byzzcraft.mapgen <world_dir> <dim> <rx> <rz> <out.png>
    python -m byzzcraft.mapgen <world_dir> <dim> --all <cache_dir>
"""
import contextlib, json, marshal, math, os, re, struct, sys, threading, time, zlib

from .players import ASSETS, dim_dir, nbt_parse  # noqa: E402

DIMS = ("overworld", "the_nether", "the_end")
SIZE = 512

# ------------------------------------------------------------------ block colours
_TEX = None
_GID_LOCK = threading.Lock()
KIND, COL, TINT = [], [], []   # per gid: 0 skip, 1 solid, 2 water, 3 lava | rgb | 0 none, 1 grass, 2 leaves
_GID = {}
NEUTRAL = (125, 125, 125)

_SKIP_EXACT = {"air", "cave_air", "void_air", "light", "barrier", "structure_void", "moving_piston", "end_portal", "end_gateway",
               "brown_mushroom", "red_mushroom", "chain", "lily_pad", "bamboo_sapling", "fire", "soul_fire", "tripwire", "string",
               "nether_wart", "lantern", "soul_lantern", "end_rod", "kelp", "kelp_plant", "seagrass", "tall_seagrass", "scaffolding_never"}
_SKIP_RE = re.compile(r"(short_grass|tall_grass|^fern$|large_fern|flower|dandelion|poppy|tulip|bluet|daisy|cornflower|allium|orchid|lily_of|petals|"
                      r"sapling|dead_bush|sunflower|lilac|rose_bush|peony|sugar_cane|wildflowers|leaf_litter|bush|dry_grass|potatoes|wheat|carrots|"
                      r"beetroots|button|pressure_plate|sign|banner|ladder|rail|lever|redstone_wire|tripwire|carpet|vine|lichen|cobweb|cushion|"
                      r"frame|torch|candle|glass(_pane)?$|iron_bars|_bars$|dripleaf|roots|fungus|sprouts|spore_blossom|pitcher|torchflower|"
                      r"hanging_moss|sculk_vein|coral_fan|coral$|^coral_|sea_pickle|pointed_dripstone|amethyst_cluster|_bud$|pale_moss_carpet|"
                      r"resin_clump|redstone_torch|^skull|_head$)")
_FIXED = {"water": (63, 118, 228), "bubble_column": (63, 118, 228), "lava": (230, 110, 20), "nether_portal": (130, 50, 220),
          "birch_leaves": (128, 167, 85), "spruce_leaves": (97, 153, 97), "snow": None, "snow_block": None}
_TINT_GRASS = {"grass_block"}
_TINT_FOLIAGE = {"oak_leaves", "acacia_leaves", "jungle_leaves", "dark_oak_leaves", "mangrove_leaves"}
_ALIAS = {"snow": "snow", "snow_block": "snow", "magma_block": "magma", "bamboo": "bamboo_stalk", "mycelium": "mycelium_top",
          "farmland": "farmland", "dirt_path": "dirt_path_top", "grass_path": "dirt_path_top", "powder_snow": "snow"}
_SUFFIX = re.compile(r"_(stairs|slab|wall|fence_gate|fence|pressure_plate|button|door|trapdoor|wall_sign|wall_hanging_sign|hanging_sign|sign)$")


def _tex():
    global _TEX
    if _TEX is None:
        try:
            with open(os.path.join(ASSETS, "texcolors.json"), encoding="utf-8") as f:
                _TEX = json.load(f)
        except Exception:
            _TEX = {}
    return _TEX


def _texcol(name):
    tex = _tex()
    base = _SUFFIX.sub("", name)
    cands = [_ALIAS.get(name, name), name + "_top", name, name + "_side", name + "_front", base + "_planks", base + "s", base,
             base + "_block_top", base + "_block", base.replace("_wool", "") + "_wool", base + "_top", base.replace("wall_", ""),
             name[:-6] if name.endswith("_block") else name]
    for c in cands:
        if c in tex:
            return tuple(tex[c][:3])
    return None


def _classify(name):
    """name without the minecraft: prefix -> (kind, rgb, tint)"""
    if name in _SKIP_EXACT or _SKIP_RE.search(name):
        return 0, NEUTRAL, 0
    if name in ("water", "bubble_column"):
        return 2, _FIXED["water"], 0
    if name == "lava":
        return 3, _FIXED["lava"], 0
    if name in _TINT_GRASS:
        return 1, (145, 189, 89), 1
    if name in _TINT_FOLIAGE:
        return 1, (119, 171, 47), 2
    if _FIXED.get(name):
        return 1, _FIXED[name], 0
    c = _texcol(name)
    if c is None:
        c = NEUTRAL
    return 1, c, 0


def _gid(name):
    g = _GID.get(name)
    if g is None:
        with _GID_LOCK:
            g = _GID.get(name)
            if g is None:
                k, c, t = _classify(name)
                KIND.append(k); COL.append(c); TINT.append(t)
                g = len(KIND) - 1
                _GID[name] = g
    return g


def _palette_gids(pal):
    out = []
    for e in pal:
        if isinstance(e, str):
            n = e
        else:
            n = e.get("id") or e.get("Name") or e.get("") or "air"
        out.append(_gid(n[10:] if n.startswith("minecraft:") else n))
    return out


# ------------------------------------------------------------------ biomes
_W = (63, 118, 228)
_BT = {
    "plains": ((145, 189, 89), (119, 171, 47), _W), "forest": ((121, 192, 90), (89, 174, 48), _W),
    "birch": ((136, 187, 103), (107, 169, 65), _W), "dark_forest": ((80, 122, 50), (89, 174, 48), _W),
    "taiga": ((134, 183, 131), (104, 164, 100), _W), "snowy": ((128, 180, 151), (96, 161, 123), (61, 87, 214)),
    "savanna": ((191, 183, 85), (174, 164, 42), _W), "badlands": ((144, 129, 77), (158, 129, 77), _W),
    "jungle": ((89, 201, 60), (48, 187, 11), _W), "sparse_jungle": ((100, 199, 63), (62, 184, 15), _W),
    "swamp": ((106, 112, 57), (106, 112, 57), (97, 123, 100)), "mangrove": ((141, 177, 39), (141, 177, 39), (58, 122, 106)),
    "meadow": ((131, 187, 109), (99, 169, 72), _W), "cherry": ((182, 219, 97), (182, 219, 97), (93, 183, 239)),
    "windswept": ((138, 182, 137), (109, 163, 107), _W), "mushroom": ((85, 201, 63), (43, 187, 15), _W),
    "pale": ((119, 130, 114), (135, 141, 118), (118, 136, 157)),
}
_BT_LIST = []
_BT_IDX = {}


def _biome_idx(name):
    i = _BT_IDX.get(name)
    if i is not None:
        return i
    n = name[10:] if name.startswith("minecraft:") else name
    if "cherry" in n: k = "cherry"
    elif "mangrove" in n: k = "mangrove"
    elif "swamp" in n: k = "swamp"
    elif "pale_garden" in n: k = "pale"
    elif "sparse_jungle" in n: k = "sparse_jungle"
    elif "jungle" in n: k = "jungle"
    elif "badlands" in n or "eroded" in n: k = "badlands"
    elif "savanna" in n and "windswept" not in n: k = "savanna"
    elif "desert" in n: k = "savanna"
    elif "dark_forest" in n: k = "dark_forest"
    elif "birch" in n: k = "birch"
    elif "forest" in n: k = "forest"
    elif "snowy_taiga" in n: k = "snowy"
    elif "taiga" in n: k = "taiga"
    elif "snowy" in n or "frozen" in n or "ice_spikes" in n or n in ("grove", "jagged_peaks"): k = "snowy"
    elif "windswept" in n or n in ("stony_peaks", "stony_shore"): k = "windswept"
    elif "meadow" in n: k = "meadow"
    elif "mushroom" in n: k = "mushroom"
    else: k = "plains"
    g, f, w = _BT[k]
    if "frozen_ocean" in n or "frozen_river" in n: w = (57, 56, 201)
    elif "cold_ocean" in n: w = (61, 87, 214)
    elif "lukewarm" in n: w = (69, 173, 242)
    elif "warm_ocean" in n: w = (67, 213, 238)
    elif k == "snowy" and "beach" in n: w = (61, 87, 214)
    with _GID_LOCK:
        i = _BT_IDX.get(name)
        if i is None:
            _BT_LIST.append((g, f, w))
            i = len(_BT_LIST) - 1
            _BT_IDX[name] = i
    return i


# ------------------------------------------------------------------ anvil decoding
_LOW = bytes(x & 15 for x in range(256))
_HIGH = bytes(x >> 4 for x in range(256))


def _unpack(data, bits, n):
    vpl = 64 // bits
    mask = (1 << bits) - 1
    shifts = [bits * k for k in range(vpl)]
    # signed longs are fine: (L >> s) & mask is right for negative values too
    out = [(L >> s) & mask for L in data for s in shifts]
    return out[:n]


def _decode_section(sec):
    """-> (lp_gids, idx) ; idx None if uniform; bytes/list of local indices (order y,z,x)."""
    bs = sec.get("block_states")
    if not bs:
        return ((_gid("air"),), None)
    lp = _palette_gids(bs["palette"])
    data = bs.get("data")
    if len(lp) == 1 or not data:
        return (lp, None)
    bits = max(4, (len(lp) - 1).bit_length())
    if bits == 4:
        raw = struct.pack("<%dq" % len(data), *data)
        idx = bytearray(4096)
        idx[0::2] = raw.translate(_LOW)
        idx[1::2] = raw.translate(_HIGH)
        return (lp, idx)
    vals = _unpack(data, bits, 4096)
    if bits <= 8:
        return (lp, bytes(vals))
    return (lp, vals)


def _biome_cells(sec):
    """-> int (uniforme) o list di 64 indici di tinta."""
    b = sec.get("biomes")
    if not b:
        return _biome_idx("plains")
    pal = [_biome_idx(n if isinstance(n, str) else (n.get("") or n.get("id") or "plains")) for n in b["palette"]]
    data = b.get("data")
    if len(pal) == 1 or not data:
        return pal[0]
    bits = max(1, (len(pal) - 1).bit_length())
    return [pal[v] for v in _unpack(data, bits, 64)]


def _lz4_block(src, out):
    """Appends to out one LZ4 block (sequences of literals + a match copied from what is already written)."""
    i, n = 0, len(src)
    while i < n:
        tok = src[i]; i += 1
        ln = tok >> 4
        if ln == 15:
            while True:
                b = src[i]; i += 1; ln += b
                if b != 255:
                    break
        out += src[i:i + ln]; i += ln
        if i >= n:   # the last sequence has no match
            break
        off = src[i] | src[i + 1] << 8; i += 2
        ml = (tok & 15) + 4
        if ml == 19:
            while True:
                b = src[i]; i += 1; ml += b
                if b != 255:
                    break
        if not 0 < off <= len(out):
            raise ValueError("lz4: bad offset")
        s = len(out) - off
        # a match longer than its offset repeats the last off bytes
        out += out[s:s + ml] if off >= ml else (out[s:] * (ml // off + 1))[:ml]


def lz4_java(data):
    """Region compression 4 (region-file-compression=lz4): the blocks of lz4-java's LZ4BlockOutputStream,
    each "LZ4Block" + method + compressed length + length + checksum (not checked: the length is, and NBT fails on junk)."""
    out, i = bytearray(), 0
    while data[i:i + 8] == b"LZ4Block":
        meth = data[i + 8] & 0xF0
        clen, dlen = struct.unpack_from("<ii", data, i + 9)
        body = data[i + 21:i + 21 + clen]; i += 21 + clen
        if dlen == 0:   # end mark
            break
        before = len(out)
        if meth == 0x10:   # stored as it is
            out += body
        elif meth == 0x20:
            _lz4_block(body, out)
        if len(out) - before != dlen:
            raise ValueError("lz4: bad length")
    return bytes(out)


def _read_chunks(path, only=None, buf=None):
    """Generator of NBT chunks (dict) from a .mca (or its bytes); only: the chunk numbers to read. Tolerates unreadable chunks."""
    if buf is None:
        with open(path, "rb") as f:
            buf = f.read()
    if len(buf) < 8192:
        return
    for n in range(1024) if only is None else only:
        o = 4 * n
        off = (buf[o] << 16) | (buf[o + 1] << 8) | buf[o + 2]
        if off == 0 or buf[o + 3] == 0:
            continue
        try:
            p = off * 4096
            length, comp = struct.unpack_from(">iB", buf, p)
            if comp & 128 or length < 2:
                continue
            data = buf[p + 5:p + 4 + length]
            if comp == 2:
                data = zlib.decompress(data)
            elif comp == 3:
                pass
            elif comp == 4:
                data = lz4_java(data)
            elif comp == 1:
                import gzip
                data = gzip.decompress(data)
            else:
                continue
            yield n, nbt_parse(data)
        except Exception:
            continue


# ------------------------------------------------------------------ render
_SHADE = [1.0 + 0.07 * d for d in range(-3, 4)]
_DEPTH_DK = [1.0 - 0.62 * (1.0 - math.exp(-d / 12.0)) for d in range(80)]
_NOH = -99999


def _chunk_top(ch, base, H, cxl, czl, nether):
    secs_raw = {}
    for s in ch.get("sections", ()):
        secs_raw[s.get("Y", 0)] = s
    if not secs_raw:
        return
    miny = ch.get("yPos", -4) * 16
    dec = {}
    bio = {}
    air = ((_gid("air"),), None)

    def load(sy):
        r = secs_raw.get(sy)
        d = _decode_section(r) if r is not None else air
        dec[sy] = d
        return d

    def at(y, i):
        s = dec.get(y >> 4) or load(y >> 4)
        lp, idx = s
        if idx is None:
            return lp[0]
        return lp[idx[((y & 15) << 8) | i]]

    def tint_at(y, i):
        sy = y >> 4
        b = bio.get(sy)
        if b is None:
            r = secs_raw.get(sy)
            b = _biome_cells(r) if r is not None else _biome_idx("plains")
            bio[sy] = b
        if isinstance(b, int):
            return _BT_LIST[b]
        lx = i & 15; lz = i >> 4
        return _BT_LIST[b[((y & 15) >> 2 << 4) | (lz >> 2 << 2) | (lx >> 2)]]

    ox = cxl * 16
    oz = czl * 16
    kind, col, tint = KIND, COL, TINT

    if nether:
        nsec = 8
        clsb = []
        for sy in range(nsec):
            lp, idx = dec.get(sy) or load(sy)
            cl = [0 if kind[g] == 0 else (2 if kind[g] == 3 else 1) for g in lp]
            if idx is None:
                clsb.append(bytes([cl[0]]) * 4096)
            elif isinstance(idx, (bytes, bytearray)):
                tb = bytes(cl) + bytes(256 - len(cl))
                clsb.append(idx.translate(tb))
            else:
                clsb.append(bytes([cl[v] for v in idx]))
        for i in range(256):
            colm = b"".join([c[i::256] for c in clsb])
            a = colm.rfind(0, 0, 121)
            if a < 0:
                yy = 120
            else:
                yy = len(colm[:a + 1].rstrip(b"\0")) - 1
                if yy < 0:
                    continue
            g = at(yy, i)
            if kind[g] == 0:
                continue
            p = (oz + (i >> 4)) * SIZE + ox + (i & 15)
            base[p] = col[g]
            if kind[g] == 1:
                H[p] = yy
        return

    hms = ch.get("Heightmaps") or {}
    hm = hms.get("WORLD_SURFACE")
    if not hm:
        return
    hm = _unpack(hm, 9, 256)
    for i in range(256):
        hv = hm[i]
        if hv == 0:
            continue
        y = hv + miny - 1
        g = 0
        found = False
        while y >= miny:
            sy = y >> 4
            s = dec.get(sy) or load(sy)
            lp, idx = s
            if idx is None:
                g = lp[0]
                if kind[g] == 0:
                    y = (sy << 4) - 1
                    continue
            else:
                g = lp[idx[((y & 15) << 8) | i]]
                if kind[g] == 0:
                    y -= 1
                    continue
            found = True
            break
        if not found:
            continue
        p = (oz + (i >> 4)) * SIZE + ox + (i & 15)
        k = kind[g]
        if k == 2:
            wc = tint_at(y, i)[2]
            d = 1
            y2 = y - 1
            bc = (100, 100, 100)
            while y2 >= miny and d < 79:
                g2 = at(y2, i)
                k2 = kind[g2]
                if k2 == 2 or k2 == 0:
                    d += 1
                else:
                    bc = col[g2]
                    break
                y2 -= 1
            dk = _DEPTH_DK[d]
            wa = min(0.93, 0.50 + 0.04 * d)
            base[p] = (int(bc[0] * (1 - wa) + wc[0] * dk * wa), int(bc[1] * (1 - wa) + wc[1] * dk * wa),
                       int(bc[2] * (1 - wa) + wc[2] * dk * wa))
            continue
        t = tint[g]
        if t == 1:
            base[p] = tint_at(y, i)[0]
        elif t == 2:
            f = tint_at(y, i)[1]
            base[p] = (int(f[0] * 0.85), int(f[1] * 0.85), int(f[2] * 0.85))
        else:
            base[p] = col[g]
        if k == 1:
            H[p] = y


def _png(img, w, h):
    stride = w * 4
    raw = b"".join([b"\0" + bytes(img[r * stride:(r + 1) * stride]) for r in range(h)])

    def chunk(t, d):
        c = struct.pack(">I", len(d)) + t + d
        return c + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(raw, 4)) + chunk(b"IEND", b""))


def region_path(world_dir, dim, rx, rz):
    return os.path.join(dim_dir(world_dir, dim), "region", "r.%d.%d.mca" % (rx, rz))


with open(__file__, "rb") as _f:   # another version of this file may draw differently: its states are not reused
    _STATE_V = zlib.crc32(_f.read())


def _sigs(buf):
    """Per chunk: sector, timestamp (1 s) and data length; a chunk saved again changes at least one of them."""
    out = []
    for n in range(1024):
        o = 4 * n
        off = int.from_bytes(buf[o:o + 3], "big") * 4096
        out.append(buf[o:o + 4] + buf[4096 + o:4100 + o] + (buf[off:off + 4] if off else b""))
    return out


def _load_state(path):
    try:
        with open(path, "rb") as f:
            v, sigs, base, H = marshal.loads(zlib.decompress(f.read()))
        if v == _STATE_V and len(sigs) == 1024 and len(base) == len(H) == SIZE * SIZE:
            return sigs, base, H
    except Exception:  # missing, damaged, written by another Python: a full render
        pass
    return None


def _save_state(path, sigs, base, H):
    tmp = path + ".tmp%d" % threading.get_ident()
    try:
        with open(tmp, "wb") as f:
            f.write(zlib.compress(marshal.dumps((_STATE_V, sigs, base, H)), 1))
        os.replace(tmp, path)
    except OSError:   # disk full, cache emptied meanwhile: the tile is still written, the next render is a full one
        with contextlib.suppress(OSError):
            os.remove(tmp)


def render_region(world_dir, dim, rx, rz, state=None):
    """PNG of a region. state: a file keeping the colours of the last render with the chunk signatures (_sigs) they
    came from; then only the chunks saved since are read again."""
    path = region_path(world_dir, dim, rx, rz)
    try:
        with open(path, "rb") as f:
            buf = f.read()
    except OSError:
        return None
    nether = dim == "the_nether"
    sigs = _sigs(buf) if state and len(buf) >= 8192 else None
    old = _load_state(state) if sigs else None
    if old:
        prev, base, H = old
        todo = [n for n in range(1024) if sigs[n] != prev[n]]
        for n in todo:   # the old pixels go: the chunk may now be gone or unreadable
            o = (n >> 5) * 16 * SIZE + (n & 31) * 16
            for p in range(o, o + 16 * SIZE, SIZE):
                base[p:p + 16] = [None] * 16
                H[p:p + 16] = [_NOH] * 16
    else:
        base = [None] * (SIZE * SIZE)
        H = [_NOH] * (SIZE * SIZE)
        todo = None
    for n, ch in _read_chunks(path, todo, buf):
        try:
            _chunk_top(ch, base, H, n & 31, n >> 5, nether)
        except Exception:
            continue
    if sigs:
        _save_state(state, sigs, base, H)
    img = bytearray(SIZE * SIZE * 4)
    sh = _SHADE
    for p in range(SIZE * SIZE):
        c = base[p]
        if c is None:
            continue
        h = H[p]
        if h != _NOH and p >= SIZE:
            hn = H[p - SIZE]
            if hn != _NOH:
                d = h - hn
                f = sh[3 + (3 if d > 3 else (-3 if d < -3 else d))]
                r = int(c[0] * f); g = int(c[1] * f); b = int(c[2] * f)
                c = (255 if r > 255 else r, 255 if g > 255 else g, 255 if b > 255 else b)
        o = p * 4
        img[o] = c[0]; img[o + 1] = c[1]; img[o + 2] = c[2]; img[o + 3] = 255
    return _png(img, SIZE, SIZE)


# ------------------------------------------------------------------ panel API
def list_regions(world_dir, dim):
    d = os.path.join(dim_dir(world_dir, dim), "region")
    out = []
    try:
        names = os.listdir(d)
    except OSError:
        return out
    for n in names:
        m = re.fullmatch(r"r\.(-?\d+)\.(-?\d+)\.mca", n)
        if not m:
            continue
        try:
            st = os.stat(os.path.join(d, n))
        except OSError:
            continue
        if st.st_size < 8192:
            continue
        out.append((int(m.group(1)), int(m.group(2)), int(st.st_mtime)))
    return sorted(out)


_LOCKS = {}
_LOCKS_G = threading.Lock()


def get_tile(world_dir, cache_dir, dim, rx, rz):
    src = region_path(world_dir, dim, rx, rz)
    try:
        mt = os.stat(src).st_mtime
    except OSError:
        return None
    out = os.path.join(cache_dir, dim, "r.%d.%d.png" % (rx, rz))
    with _LOCKS_G:
        lock = _LOCKS.setdefault(out, threading.Lock())
    with lock:
        try:
            if os.stat(out).st_mtime >= mt:
                return out
        except OSError:
            pass
        # state only for a tile drawn before: a region that changes; the untouched ones would only take disk space
        png = render_region(world_dir, dim, rx, rz, out[:-4] + ".state" if os.path.exists(out) else None)
        if png is None:
            return None
        os.makedirs(os.path.dirname(out), exist_ok=True)
        tmp = out + ".tmp%d" % threading.get_ident()
        with open(tmp, "wb") as f:
            f.write(png)
        os.utime(tmp, (mt, mt))   # the region as it was read: if it is saved again while rendering, the tile is stale
        os.replace(tmp, out)
        return out


def main(argv):
    if len(argv) >= 4 and argv[1] in DIMS and argv[2] == "--all":
        world, dim, cache = argv[0], argv[1], argv[3]
        ts = []
        t00 = time.time()
        for rx, rz, _ in list_regions(world, dim):
            t0 = time.time()
            get_tile(world, cache, dim, rx, rz)
            ts.append(time.time() - t0)
            print("r.%d.%d  %.2fs" % (rx, rz, ts[-1]))
        if ts:
            print("%d region, totale %.1fs, min %.2f medio %.2f max %.2f" % (len(ts), time.time() - t00, min(ts), sum(ts) / len(ts), max(ts)))
        return 0
    if len(argv) == 5 and argv[1] in DIMS:
        world, dim, rx, rz, out = argv[0], argv[1], int(argv[2]), int(argv[3]), argv[4]
        t0 = time.time()
        png = render_region(world, dim, rx, rz)
        if png is None:
            print("region inesistente")
            return 1
        with open(out, "wb") as f:
            f.write(png)
        print("r.%d.%d -> %s in %.2fs" % (rx, rz, out, time.time() - t0))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
