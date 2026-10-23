import os, struct, sys, zlib
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))  # cartella pannello
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

# RGBA 2x2: un pixel trasparente non pesa sul colore
d = png(2, 2, 8, 6, [bytes([255, 0, 0, 255, 0, 0, 0, 0]), bytes([255, 0, 0, 255, 0, 0, 255, 255])])
check("rgba", A.tex_color(d) == [170, 0, 85, 0.75], A.tex_color(d))
# palette a 1 bit con tRNS
d = png(8, 1, 1, 3, [bytes([0b10101010])], plte=bytes([10, 20, 30, 200, 100, 0]), trns=bytes([0]))
check("palette 1 bit", A.tex_color(d) == [200, 100, 0, 0.5], A.tex_color(d))
# grigio con colore trasparente (tRNS a 2 byte)
d = png(2, 1, 8, 0, [bytes([0, 128])], trns=struct.pack(">H", 0))
check("grigio + tRNS", A.tex_color(d) == [128, 128, 128, 0.5], A.tex_color(d))
# RGB con filtro Sub: stesso risultato del non filtrato
d = png(2, 1, 8, 2, [bytes([10, 20, 30, 0, 0, 0])], filt=1)
check("filtro sub", A.png_rgba(d)[2] == [(10, 20, 30, 255), (10, 20, 30, 255)], A.png_rgba(d))
# animata 1x3 (striscia verticale): conta solo il primo frame
d = png(1, 3, 8, 2, [bytes([9, 9, 9]), bytes([200, 200, 200]), bytes([200, 200, 200])])
check("primo frame", A.tex_color(d) == [9, 9, 9, 1], A.tex_color(d))
# tutta trasparente: nessun colore
d = png(1, 1, 8, 6, [bytes([1, 2, 3, 0])])
check("trasparente", A.tex_color(d) is None)
print("PASS %d  FAIL %d" % (OK, FAIL))
