import os, shutil, struct, sys, tempfile
root = tempfile.mkdtemp(prefix="mapgen_test_")
os.environ["BYZZ_DATA"] = os.path.join(root, "data")  # before importing mapgen
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))  # panel folder
from byzzcraft import mapgen as M
from byzzcraft import paths; assert paths.DATA == os.path.abspath(os.environ["BYZZ_DATA"]), "run each test file in its own process: real data at risk"

OK = FAIL = 0
def check(name, cond, extra=""):
    global OK, FAIL
    OK, FAIL = (OK + 1, FAIL) if cond else (OK, FAIL + 1)
    if not cond:
        print("FAIL:", name, extra)


def block(body, dlen, meth=0x20):   # one lz4-java LZ4BlockOutputStream block (checksum left 0: not checked)
    return b"LZ4Block" + bytes([meth | 6]) + struct.pack("<iii", len(body), dlen, 0) + body


END = block(b"", 0, 0x10)

try:
    # LZ4 sequences written by hand: token (literals << 4 | match - 4), literals, offset (LE), extra lengths
    check("literals only", M.lz4_java(block(b"\x50hello", 5) + END) == b"hello")
    check("overlapping match repeats", M.lz4_java(block(b"\x24ab\x02\x00\x10z", 11) + END) == b"ababababab" + b"z")
    check("long literal (15 + extra byte)", M.lz4_java(block(b"\xf0\x05" + bytes(range(20)), 20) + END) == bytes(range(20)))
    check("long match (19 + extra byte), no literals at the end",
          M.lz4_java(block(b"\x1fx\x01\x00\x03", 23) + END) == b"x" * 23)
    check("match 255 + more", M.lz4_java(block(b"\x1fx\x01\x00\xff\x01", 1 + 19 + 256) + END) == b"x" * 276)
    check("raw block, then lz4 block", M.lz4_java(block(b"abc", 3, 0x10) + block(b"\x30def\x03\x00", 7) + END) == b"abcdefdefd")
    for name, bad in (("offset before the start", block(b"\x10a\x05\x00", 5)), ("wrong length", block(b"\x50hello", 6))):
        try:
            M.lz4_java(bad + END); check(name + " raises", False)
        except ValueError:
            check(name + " raises", True)

    # a region file with one chunk saved with compression 4 is read like the others
    nbt = b"\x0a\x00\x00" + b"\x01\x00\x01a\x07" + b"\x00"   # {"a": 7}
    data = block(bytes([len(nbt) << 4]) + nbt, len(nbt)) + END
    mca = os.path.join(root, "r.0.0.mca")
    with open(mca, "wb") as f:
        f.write(bytes([0, 0, 2, 1]) + bytes(8188) + struct.pack(">iB", len(data) + 1, 4) + data)
        f.write(bytes(3 * 4096 - f.tell()))
    chunks = list(M._read_chunks(mca))
    check("region: lz4 chunk read", len(chunks) == 1 and chunks[0][0] == 0 and chunks[0][1].get("a") == 7, chunks)
finally:
    shutil.rmtree(root, ignore_errors=True)

print("PASS %d  FAIL %d" % (OK, FAIL))
sys.exit(1 if FAIL else 0)
