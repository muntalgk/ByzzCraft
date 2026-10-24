import os, struct, sys, zlib
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))  # panel folder
import assets as A

OK = FAIL = 0
def check(name, cond, extra=""):
    global OK, FAIL
    OK, FAIL = (OK + 1, FAIL) if cond else (OK, FAIL + 1)
    if not cond:
        print("FAIL:", name, extra)

def png(w, h, bd, ct, rows, plte=b"", trns=b"", filt=0):
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    raw = b"".join(bytes([filt]) + r for r in rows)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, bd, ct, 0, 0, 0)) +
            (chunk(b"PLTE", plte) if plte else b"") + (chunk(b"tRNS", trns) if trns else b"") +
            chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))

# RGBA 2x2: a transparent pixel does not count towards the colour
d = png(2, 2, 8, 6, [bytes([255, 0, 0, 255, 0, 0, 0, 0]), bytes([255, 0, 0, 255, 0, 0, 255, 255])])
check("rgba", A.tex_color(d) == [170, 0, 85, 0.75], A.tex_color(d))
# 1-bit palette with tRNS
d = png(8, 1, 1, 3, [bytes([0b10101010])], plte=bytes([10, 20, 30, 200, 100, 0]), trns=bytes([0]))
check("1-bit palette", A.tex_color(d) == [200, 100, 0, 0.5], A.tex_color(d))
# grey with a transparent colour (2-byte tRNS)
d = png(2, 1, 8, 0, [bytes([0, 128])], trns=struct.pack(">H", 0))
check("grey + tRNS", A.tex_color(d) == [128, 128, 128, 0.5], A.tex_color(d))
# RGB with the Sub filter: same result as unfiltered
d = png(2, 1, 8, 2, [bytes([10, 20, 30, 0, 0, 0])], filt=1)
check("sub filter", A.png_rgba(d)[2] == [(10, 20, 30, 255), (10, 20, 30, 255)], A.png_rgba(d))
# animated 1x3 (vertical strip): only the first frame counts
d = png(1, 3, 8, 2, [bytes([9, 9, 9]), bytes([200, 200, 200]), bytes([200, 200, 200])])
check("first frame", A.tex_color(d) == [9, 9, 9, 1], A.tex_color(d))
# fully transparent: no colour
d = png(1, 1, 8, 6, [bytes([1, 2, 3, 0])])
check("transparent", A.tex_color(d) is None)
# chest and shield from the entity textures: every pixel of the fake texture is (x, y)
import io, zipfile
tex = png(64, 64, 8, 6, [bytes(c for x in range(64) for c in (x, y, 0, 255)) for y in range(64)])
buf = io.BytesIO()
with zipfile.ZipFile(buf, "w") as z:
    z.writestr(A.TEX + "entity/chest/normal.png", tex)
    z.writestr(A.TEX + "entity/shield/shield_base_nopattern.png", tex)
with zipfile.ZipFile(buf) as z:
    ic = A.entity_icons(z)
check("only the textures present", sorted(ic) == ["chest", "shield"], sorted(ic))
px = A.png_rgba(ic["chest"])[2]
check("lid", px[1 * 16 + 1][:2] == (14, 14), px[17])
check("body", px[15 * 16 + 14][:2] == (27, 42), px[254])
check("latch", px[4 * 16 + 7][:2] == (1, 1) and px[7 * 16 + 8][:2] == (2, 4))
check("transparent border", px[0][3] == 0 and px[15][3] == 0)
px = A.png_rgba(ic["shield"])[2]
check("shield: skipped rows", [px[i * 16 + 3][1] for i in (0, 2, 15)] == [2, 5, 21] and px[3 * 16 + 12][0] == 11)
# which jar to start and which Java it needs: vanilla, Paper (version.json), Fabric launcher (install.properties)
import json, shutil, tempfile
d = tempfile.mkdtemp(prefix="jar_test_")
try:
    def jar(name, files):
        with zipfile.ZipFile(os.path.join(d, name), "w") as z:
            for n, data in files.items():
                z.writestr(n, data)
    check("empty folder", A.find_jar(d) is None)
    jar("server.jar", {"version.json": json.dumps({"id": "26.3", "java_version": 25})})
    check("vanilla", A.find_jar(d) == "server.jar" and A.jar_info(d, "server.jar") == ("26.3", 25))
    jar("paper-26.3-218.jar", {"version.json": json.dumps({"id": "26.3", "java_version": 25})})
    check("Paper before vanilla", A.find_jar(d) == "paper-26.3-218.jar")
    jar("fabric-server-launch.jar", {"install.properties": "fabric-loader-version=0.20.1\ngame-version=1.21.4"})
    A._JAVA["1.21.4"] = 21   # no network in the test: this is Mojang's manifest answer
    os.remove(os.path.join(d, "server.jar"))
    check("Fabric before everything", A.find_jar(d) == "fabric-server-launch.jar")
    check("Fabric: version from the launcher, Java from the manifest", A.jar_info(d, "fabric-server-launch.jar") == ("1.21.4", 21))
    check("jar from config", A.find_jar(d, "paper-26.3-218.jar") == "paper-26.3-218.jar" and A.find_jar(d, "manca.jar") is None)
    jar("forge-installer.jar", {})
    check("installer ignored", A.find_jar(d) == "fabric-server-launch.jar")
finally:
    shutil.rmtree(d, ignore_errors=True)
print("PASS %d  FAIL %d" % (OK, FAIL))
sys.exit(1 if FAIL else 0)
