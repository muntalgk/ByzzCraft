"""Player data for the panel: NBT from the files, SNBT from RCON, statistics, icons and skins."""
import base64, gzip, json, os, re, shutil, struct, threading, time, urllib.request

from .paths import ASSETS, CACHE  # noqa: E402
UA = "byzzcraft (+https://github.com/muntalgk/byzzcraft)"


# ---------------------------------------------------------------- binary NBT (.dat files)
def nbt_parse(buf):
    pos = [0]

    def rd(fmt):
        v = struct.unpack_from(fmt, buf, pos[0]); pos[0] += struct.calcsize(fmt); return v[0]

    def rstr():
        n = rd(">H"); s = buf[pos[0]:pos[0] + n].decode("utf-8", "replace"); pos[0] += n; return s

    def payload(t):
        if t == 1: return rd(">b")
        if t == 2: return rd(">h")
        if t == 3: return rd(">i")
        if t == 4: return rd(">q")
        if t == 5: return rd(">f")
        if t == 6: return rd(">d")
        if t == 7:
            n = rd(">i"); b = bytes(buf[pos[0]:pos[0] + n]); pos[0] += n; return list(b)
        if t == 8: return rstr()
        if t == 9:
            st = rd(">B"); n = rd(">i"); return [payload(st) for _ in range(n)]
        if t == 10:
            d = {}
            while True:
                tt = rd(">B")
                if tt == 0: return d
                k = rstr(); d[k] = payload(tt)
        if t == 11:
            n = rd(">i"); v = list(struct.unpack_from(">%di" % n, buf, pos[0])); pos[0] += 4 * n; return v
        if t == 12:
            n = rd(">i"); v = list(struct.unpack_from(">%dq" % n, buf, pos[0])); pos[0] += 8 * n; return v
        raise ValueError("tag %d" % t)

    t = rd(">B"); rstr(); return payload(t)


def read_nbt_file(path):
    raw = open(path, "rb").read()
    return nbt_parse(gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw)


# ---------------------------------------------------------------- SNBT (output of /data get)
_NUM = re.compile(r"^[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?([us]?[bslfdBSLFD])?$")


class SNBT:
    def __init__(self, text):
        self.t, self.i = text, 0

    def ws(self):
        while self.i < len(self.t) and self.t[self.i] in " \t\r\n":
            self.i += 1

    def peek(self):
        self.ws(); return self.t[self.i] if self.i < len(self.t) else ""

    def quoted(self):
        q = self.t[self.i]; self.i += 1; out = []
        while self.i < len(self.t):
            c = self.t[self.i]
            if c == "\\":
                nxt = self.t[self.i + 1]
                if nxt == "n": out.append("\n")
                elif nxt == "u": out.append(chr(int(self.t[self.i + 2:self.i + 6], 16))); self.i += 4
                else: out.append(nxt)
                self.i += 2; continue
            if c == q:
                self.i += 1; return "".join(out)
            out.append(c); self.i += 1
        raise ValueError("stringa non chiusa")

    def token(self):
        start = self.i
        while self.i < len(self.t) and self.t[self.i] not in ",}]:; \t\r\n":
            self.i += 1
        return self.t[start:self.i]

    def value(self):
        c = self.peek()
        if c == "{":
            self.i += 1; d = {}
            while True:
                if self.peek() == "}":
                    self.i += 1; return d
                k = self.quoted() if self.peek() in "\"'" else self.token()
                self.ws()
                if self.t[self.i] != ":": raise ValueError("':' atteso a %d" % self.i)
                self.i += 1
                d[k] = self.value()
                if self.peek() == ",": self.i += 1
        if c == "[":
            self.i += 1
            if self.t[self.i:self.i + 2] in ("B;", "I;", "L;"):
                self.i += 2
            out = []
            while True:
                if self.peek() == "]":
                    self.i += 1; return out
                out.append(self.value())
                if self.peek() == ",": self.i += 1
        if c in "\"'":
            return self.quoted()
        tok = self.token()
        if not tok: raise ValueError("token vuoto a %d" % self.i)   # e.g. "[:]": would never advance
        if tok in ("true", "false"):
            return 1 if tok == "true" else 0
        m = _NUM.match(tok)
        if m:
            core = tok.rstrip("bslfdBSLFD")
            if core and core[-1] in "usUS": core = core[:-1]
            suf = tok[len(core):].lower()
            if suf.endswith(("f", "d")) or "." in core or "e" in core.lower():
                return float(core)
            return int(core)
        return tok


def snbt(text):
    return SNBT(text).value()


# ---------------------------------------------------------------- names and tables
_LANG = None


def lang():
    global _LANG
    if _LANG is None:
        try:
            _LANG = json.load(open(os.path.join(ASSETS, "lang.json"), encoding="utf-8"))
        except (OSError, ValueError):
            _LANG = {}
    return _LANG


# ---------------------------------------------------------------- where the world files are
_OLD_DIM = {"overworld": "", "the_nether": "DIM-1", "the_end": "DIM1"}


def dim_dir(world, dim):
    """Folder of a dimension: 26.x <world>/dimensions/minecraft/<dim>; up to 1.21 <world>, <world>/DIM-1, <world>/DIM1;
    Paper and Spigot keep the Nether and the End in <world>_nether/DIM-1 and <world>_the_end/DIM1."""
    new = os.path.join(world, "dimensions", "minecraft", dim)
    if os.path.isdir(new) or os.path.isdir(os.path.join(world, "dimensions")):
        return new
    old = os.path.join(world, _OLD_DIM[dim])
    if dim == "overworld" or os.path.isdir(old):
        return old.rstrip("\\/")
    return os.path.join(world + "_" + dim.replace("the_nether", "nether"), _OLD_DIM[dim])


def player_dir(world, kind):
    """kind: data, stats, advancements. 26.x: <world>/players/<kind>; before, <world>/playerdata, stats, advancements."""
    new = os.path.join(world, "players", kind)
    if os.path.isdir(new):
        return new
    return os.path.join(world, "playerdata" if kind == "data" else kind)


def strip_ns(s):
    return str(s).split(":", 1)[1] if ":" in str(s) else str(s)


def item_name(iid):
    L = lang()
    return L.get("item.minecraft." + iid) or L.get("block.minecraft." + iid) or iid.replace("_", " ").title()


ROMAN = ["", "I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X"]
MAX_DMG = {}
for mat, vals in {"leather": (55, 80, 75, 65), "golden": (77, 112, 105, 91), "chainmail": (165, 240, 225, 195), "iron": (165, 240, 225, 195),
                  "copper": (121, 176, 165, 143), "diamond": (363, 528, 495, 429), "netherite": (407, 592, 555, 481)}.items():
    for part, v in zip(("helmet", "chestplate", "leggings", "boots"), vals):
        MAX_DMG["%s_%s" % (mat, part)] = v
for mat, v in {"wooden": 59, "stone": 131, "copper": 190, "iron": 250, "golden": 32, "diamond": 1561, "netherite": 2031}.items():
    for tool in ("sword", "pickaxe", "axe", "shovel", "hoe"):
        MAX_DMG["%s_%s" % (mat, tool)] = v
MAX_DMG.update({"turtle_helmet": 275, "shield": 336, "bow": 384, "crossbow": 465, "trident": 250, "elytra": 432, "fishing_rod": 64,
                "shears": 238, "flint_and_steel": 64, "carrot_on_a_stick": 25, "warped_fungus_on_a_stick": 100, "mace": 500, "brush": 64})
ARMOR_PTS = {"leather": (1, 3, 2, 1), "golden": (2, 5, 3, 1), "chainmail": (2, 5, 4, 1), "iron": (2, 6, 5, 2), "copper": (2, 4, 3, 1),
             "diamond": (3, 8, 6, 3), "netherite": (3, 8, 6, 3)}


def text_of(v):
    if isinstance(v, str):
        try:
            return text_of(json.loads(v)) if v.startswith(("{", "[", '"')) else v
        except ValueError:
            return v
    if isinstance(v, dict):
        return (v.get("text") or "") + "".join(text_of(x) for x in v.get("extra", []))
    if isinstance(v, list):
        return "".join(text_of(x) for x in v)
    return ""


def enchants(comp, key):
    e = comp.get(key) or {}
    if isinstance(e, dict) and "levels" in e:
        e = e["levels"]
    L = lang()
    return [[L.get("enchantment.minecraft." + strip_ns(k), strip_ns(k).replace("_", " ").title()), ROMAN[v] if 0 < v < len(ROMAN) else str(v)]
            for k, v in (e.items() if isinstance(e, dict) else [])]


# the liquid's color in the game, by potion (long_/strong_ stripped); water and the others: 0x385DC6
# shortcut: the color of the potion's own effect, not the blend of a custom potion's effects; upgrade if those show up
POTION_COLOR = {"swiftness": 0x33EBFF, "slowness": 0x8BAFE0, "strength": 0xFFC700, "healing": 0xF82423, "harming": 0xA9656A,
                "leaping": 0xFDFF84, "regeneration": 0xCD5CAB, "fire_resistance": 0xFF9900, "water_breathing": 0x98DAC0,
                "invisibility": 0xF6F6F6, "night_vision": 0xC2FF66, "weakness": 0x484D48, "poison": 0x87A363, "luck": 0x59C106,
                "slow_falling": 0xF3CFB9, "turtle_master": 0x8E7EA4, "wind_charged": 0xBDC9FF, "weaving": 0x78695A,
                "oozing": 0x99FFA3, "infested": 0x8C9B8C}


def item(it):
    if not isinstance(it, dict) or not it.get("id"):
        return None
    iid = strip_ns(it["id"])
    comp = it.get("components") or it.get("tag") or {}
    name = text_of(comp.get("minecraft:custom_name")) or item_name(iid)
    ench = enchants(comp, "minecraft:enchantments")
    stored = enchants(comp, "minecraft:stored_enchantments")
    out = {"id": iid, "count": it.get("count", it.get("Count", 1)), "slot": it.get("Slot"), "name": name,
           "ench": ench + stored, "glint": bool(ench or stored), "dmg": comp.get("minecraft:damage", 0) or 0,
           "max": comp.get("minecraft:max_damage") or MAX_DMG.get(iid)}
    if iid in ("potion", "splash_potion", "lingering_potion"):   # the icon is an empty bottle: the page adds the liquid in this color
        pc = comp.get("minecraft:potion_contents") or {}
        pc = pc if isinstance(pc, dict) else {"potion": pc}       # the component can be just the potion's id
        base = re.sub(r"^(long|strong)_", "", strip_ns(pc.get("potion", "")))
        color = pc.get("custom_color")
        out["tint"] = "#%06x" % ((color if isinstance(color, int) else POTION_COLOR.get(base, 0x385DC6)) & 0xFFFFFF)
        if base and not comp.get("minecraft:custom_name"):
            out["name"] = lang().get("item.minecraft.%s.effect.%s" % (iid, base), name)
    return out


def normalize(d):
    """Player data (from the file or from RCON) in a simple shape for the page."""
    inv = [x for x in (item(i) for i in d.get("Inventory", [])) if x]
    eq = {}
    raw_eq = d.get("equipment") or {}
    for slot in ("head", "chest", "legs", "feet", "offhand"):
        eq[slot] = item(raw_eq.get(slot)) if raw_eq.get(slot) else None
    for x in list(inv):  # old format: armour in slots 100-103 and the off hand in -106
        legacy = {100: "feet", 101: "legs", 102: "chest", 103: "head", -106: "offhand"}.get(x["slot"])
        if legacy:
            eq[legacy] = x; inv.remove(x)
    armor = 0
    for i, slot in enumerate(("head", "chest", "legs", "feet")):
        it = eq.get(slot)
        if it:
            mat = it["id"].split("_")[0]
            if mat in ARMOR_PTS and it["id"].endswith(("helmet", "chestplate", "leggings", "boots")):
                armor += ARMOR_PTS[mat][i]
            elif it["id"] == "turtle_helmet":
                armor += 2
    L = lang()
    effects = [{"id": strip_ns(e.get("id", "")), "name": L.get("effect.minecraft." + strip_ns(e.get("id", "")), strip_ns(e.get("id", ""))),
                "amp": e.get("amplifier", 0), "dur": e.get("duration", 0)} for e in d.get("active_effects", []) or []]
    pos = d.get("Pos") or [0, 0, 0]
    return {"health": round(float(d.get("Health", 0)), 1), "absorption": round(float(d.get("AbsorptionAmount", 0)), 1),
            "food": d.get("foodLevel", 0), "saturation": round(float(d.get("foodSaturationLevel", 0)), 1),
            "xp_level": d.get("XpLevel", 0), "xp_progress": round(float(d.get("XpP", 0)), 3), "xp_total": d.get("XpTotal", 0),
            "score": d.get("Score", 0), "pos": [round(float(p), 1) for p in pos], "yaw": round(float((d.get("Rotation") or [0])[0]), 1), "dim": strip_ns(d.get("Dimension", "overworld")),
            "gamemode": d.get("playerGameType", 0), "selected": d.get("SelectedItemSlot", 0), "inventory": inv, "equipment": eq,
            "ender": [x for x in (item(i) for i in d.get("EnderItems", [])) if x], "effects": effects, "armor": armor,
            "fire": d.get("Fire", -20) > 0, "air": d.get("Air", 300), "dead": float(d.get("Health", 1)) <= 0,
            "last_death": (d.get("LastDeathLocation") or {}).get("pos"),
            "last_death_dim": strip_ns((d.get("LastDeathLocation") or {}).get("dimension", "overworld"))}


# ---------------------------------------------------------------- statistics and advancements
def stats(world, uuid):
    out = {}
    try:
        s = json.load(open(os.path.join(player_dir(world, "stats"), uuid + ".json"), encoding="utf-8")).get("stats", {})
    except (OSError, ValueError):
        s = {}
    c = {strip_ns(k): v for k, v in s.get("minecraft:custom", {}).items()}
    dist = sum(c.get(k, 0) for k in ("walk_one_cm", "sprint_one_cm", "crouch_one_cm", "swim_one_cm", "walk_on_water_one_cm", "walk_under_water_one_cm",
                                     "climb_one_cm", "fall_one_cm", "fly_one_cm", "aviate_one_cm", "boat_one_cm", "horse_one_cm", "minecart_one_cm", "pig_one_cm", "strider_one_cm"))
    out["summary"] = {"play_time": c.get("play_time", 0) // 20, "deaths": c.get("deaths", 0), "mob_kills": c.get("mob_kills", 0),
                      "player_kills": c.get("player_kills", 0), "damage_dealt": c.get("damage_dealt", 0) / 10, "damage_taken": c.get("damage_taken", 0) / 10,
                      "distance_km": round(dist / 100000, 2), "jumps": c.get("jump", 0), "since_death": c.get("time_since_death", 0) // 20,
                      "sleep": c.get("sleep_in_bed", 0), "fish": c.get("fish_caught", 0), "traded": c.get("traded_with_villager", 0)}

    def top(cat, n=6):
        d = s.get(cat, {})
        return [[item_name(strip_ns(k)) if cat != "minecraft:killed" and cat != "minecraft:killed_by" else
                 lang().get("entity.minecraft." + strip_ns(k), strip_ns(k)), v, strip_ns(k)] for k, v in sorted(d.items(), key=lambda kv: -kv[1])[:n]]
    out["mined"], out["killed"], out["killed_by"], out["crafted"] = top("minecraft:mined"), top("minecraft:killed"), top("minecraft:killed_by"), top("minecraft:crafted")
    try:
        adv = json.load(open(os.path.join(player_dir(world, "advancements"), uuid + ".json"), encoding="utf-8"))
        done = [k for k, v in adv.items() if isinstance(v, dict) and v.get("done") and "recipes/" not in k]
        out["advancements"] = len(done)
    except (OSError, ValueError):
        out["advancements"] = 0
    return out


# ---------------------------------------------------------------- icons
_ICON_CACHE = {}
_SUFFIX = re.compile(r"_(stairs|slab|wall|fence_gate|fence|pressure_plate|button|door|trapdoor|wall_sign|sign|hanging_sign|carpet|pane)$")


_COLORS = ("white", "orange", "magenta", "light_blue", "yellow", "lime", "pink", "gray", "light_gray", "cyan", "purple", "blue", "brown",
           "green", "red", "black")
_ALIAS = {"crossbow": "crossbow_standby", "snow_block": "snow", "magma_block": "magma", "sticky_piston": "piston_top_sticky",
          "dried_kelp_block": "dried_kelp_top", "enchanted_golden_apple": "golden_apple", "lodestone_compass": "compass_00",
          "tipped_arrow": "arrow", "scute": "turtle_scute", "petrified_oak_slab": "oak_planks", "sign": "oak_sign",
          "heavy_weighted_pressure_plate": "iron_block", "light_weighted_pressure_plate": "gold_block", "trial_spawner": "trial_spawner_side_inactive",
          "quartz_slab": "quartz_block_top", "quartz_stairs": "quartz_block_top", "smooth_quartz": "quartz_block_bottom",
          "smooth_quartz_slab": "quartz_block_bottom", "smooth_quartz_stairs": "quartz_block_bottom", "smooth_sandstone": "sandstone_top",
          "smooth_sandstone_slab": "sandstone_top", "smooth_sandstone_stairs": "sandstone_top", "smooth_red_sandstone": "red_sandstone_top",
          "smooth_red_sandstone_slab": "red_sandstone_top", "smooth_red_sandstone_stairs": "red_sandstone_top", "suspicious_sand": "suspicious_sand_0",
          "suspicious_gravel": "suspicious_gravel_0", "smithing_template": "netherite_upgrade_smithing_template", "harness": "white_harness",
          "candle_cake": "cake_top", "potatoes": "potatoes_stage3", "carrots": "carrots_stage3", "beetroots": "beetroots_stage3",
          "wall_torch": "torch", "soul_wall_torch": "soul_torch", "redstone_wall_torch": "redstone_torch", "copper_wall_torch": "copper_torch",
          "water": "water_bucket", "lava": "lava_bucket", "fire": "fire_0", "soul_fire": "soul_fire_0", "redstone_wire": "redstone"}


def _alias(iid):
    if iid in _ALIAS:
        return _ALIAS[iid]
    if iid.endswith("_map"):
        return "filled_map"
    if iid.endswith("_pottery_shard"):
        return iid.replace("_shard", "_sherd")
    if iid.startswith("pottery_shard_"):
        return iid[len("pottery_shard_"):] + "_pottery_sherd"
    if iid.endswith("_hyphae"):
        return iid.replace("_hyphae", "_stem")
    if iid.endswith("_wood"):
        return iid[:-5] + "_log"
    for c in _COLORS:
        if iid in (c + "_bed", c + "_banner", c + "_wall_banner"):
            return c + "_wool"
        if iid == c + "_candle_cake":
            return "cake_top"
    if iid == "ominous_banner":
        return "white_wool"
    if "_wall_hanging_sign" in iid:
        return iid.replace("_wall_hanging_sign", "_hanging_sign")
    for p in ("waxed_", "infested_", "potted_"):
        if iid.startswith(p):
            return iid[len(p):]
    if iid.endswith("_wall_fan"):
        return iid.replace("_wall_fan", "_fan")
    return None


def icon_path(iid):
    if iid in _ICON_CACHE:
        return _ICON_CACHE[iid]
    orig = iid = re.sub(r"[^a-z0-9_]", "", iid)
    p = _icon_lookup(iid)
    seen = {iid}
    while p is None:
        iid = _alias(iid)
        if not iid or iid in seen:
            break
        seen.add(iid)
        p = _icon_lookup(iid)
    if p:   # only hits: names from the URL are arbitrary, and the icons may arrive later
        _ICON_CACHE[orig] = p
    return p


def _icon_lookup(iid):
    base = _SUFFIX.sub("", iid)
    cands = [("item", iid), ("block", iid), ("block", iid + "_front"), ("block", iid + "_top"), ("block", iid + "_side"),
             ("item", iid + "_00"), ("block", base + "_planks"), ("block", base + "s"), ("block", base), ("block", base.replace("_wool", "") + "_wool"),
             ("block", base + "_block"), ("block", iid.replace("waxed_", "")), ("block", base + "_log")]
    for kind, name in cands:
        p = os.path.join(ASSETS, kind, name + ".png")
        if os.path.exists(p):
            return p
    return None


# ---------------------------------------------------------------- skins (Mojang, cached for 6 hours)
_SKIN_LOCKS, _SKIN_GUARD = {}, threading.Lock()   # one lock per uuid: slow Mojang calls do not block other skins


def skin(uuid):
    """(png_bytes, slim) - downloaded from Mojang when needed, otherwise Steve/Alex."""
    os.makedirs(os.path.join(CACHE, "skins"), exist_ok=True)
    png = os.path.join(CACHE, "skins", uuid + ".png"); meta = os.path.join(CACHE, "skins", uuid + ".json")
    with _SKIN_GUARD:
        lock = _SKIN_LOCKS.setdefault(uuid, threading.Lock())
    with lock:
        fresh = os.path.exists(png) and time.time() - os.path.getmtime(png) < 6 * 3600
        if not fresh:
            try:
                if uuid[14] != "4":   # offline-mode UUID (version 3): Mojang has no skin for it
                    raise ValueError("offline UUID")
                req = urllib.request.Request("https://sessionserver.mojang.com/session/minecraft/profile/" + uuid.replace("-", ""),
                                             headers={"User-Agent": UA})
                prof = json.load(urllib.request.urlopen(req, timeout=6))
                tex = json.loads(base64.b64decode(prof["properties"][0]["value"]))["textures"]["SKIN"]
                url = tex["url"].replace("http://", "https://")
                data = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=6).read()
                open(png, "wb").write(data)
                json.dump({"slim": (tex.get("metadata") or {}).get("model") == "slim"}, open(meta, "w"))
            except Exception:  # noqa: BLE001 - offline or a profile without a skin: the default one is used
                # cached like a real skin (or the old one kept), so Mojang is not asked again for 6 hours
                if os.path.exists(png):
                    os.utime(png)
                else:
                    slim = int(uuid.replace("-", "")[-1], 16) % 2 == 1
                    default = os.path.join(ASSETS, "skin", "slim_alex.png" if slim else "wide_steve.png")
                    try:
                        shutil.copyfile(default, png)
                        json.dump({"slim": slim}, open(meta, "w"))
                    except OSError:   # assets from an old version have no skin/ folder: "no skin"
                        return b"", False
        slim = False
        try:
            slim = json.load(open(meta)).get("slim", False)
        except (OSError, ValueError):
            pass
        return open(png, "rb").read(), slim
