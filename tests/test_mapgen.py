import os, shutil, struct, sys, tempfile, time
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

    # incremental render: with the state file only the chunks saved again are read, and the PNG is the same as a full one
    world = os.path.join(root, "world")
    rpath = M.region_path(world, "overworld", 0, 0)
    os.makedirs(os.path.dirname(rpath))

    def write_region(chunks):   # {chunk number: (colour value, timestamp[, padding])}, uncompressed NBT {"c": value}
        head, ts, body = bytearray(4096), bytearray(4096), bytearray(4096 * 41)
        for n, (k, t, *pad) in sorted(chunks.items()):
            nb = b"\x0a\x00\x00\x03\x00\x01c" + struct.pack(">i", k) + b"\x01\x00\x01p\x00" * (pad[0] if pad else 0) + b"\x00"
            sec = 2 + n   # fixed sectors: Minecraft moves only the chunks it writes
            head[4 * n:4 * n + 4] = struct.pack(">I", sec << 8 | 1); ts[4 * n:4 * n + 4] = struct.pack(">I", t)
            o = (sec - 2) * 4096
            body[o:o + 5 + len(nb)] = struct.pack(">iB", len(nb) + 1, 3) + nb
        with open(rpath, "wb") as f:
            f.write(bytes(head) + bytes(ts) + body)

    read, real_top = [], M._chunk_top
    def fake_top(ch, base, H, cx, cz, nether):   # paints the chunk with its value; the left half higher, for shading
        read.append((cx, cz))
        for z in range(16):
            for x in range(16):
                p = (cz * 16 + z) * M.SIZE + cx * 16 + x
                base[p], H[p] = (ch["c"], ch["c"], ch["c"]), ch["c"] + (5 if x < 8 else 0)
    M._chunk_top = fake_top
    try:
        state = os.path.join(root, "r.0.0.state")
        write_region({0: (10, 100), 1: (20, 100), 33: (30, 100)})
        first = M.render_region(world, "overworld", 0, 0, state)
        check("state: first render is a full one", sorted(read) == [(0, 0), (1, 0), (1, 1)] and os.path.exists(state), read)
        write_region({1: (25, 200), 33: (30, 100), 40: (50, 200)})   # chunk 0 deleted, 1 saved again, 40 new
        del read[:]
        part = M.render_region(world, "overworld", 0, 0, state)
        check("state: only the changed chunks read", sorted(read) == [(1, 0), (8, 1)], read)
        del read[:]
        full = M.render_region(world, "overworld", 0, 0)
        check("state: same PNG as a full render (deleted chunk cleared)", part == full and part != first)
        open(state, "wb").write(b"junk")
        del read[:]
        check("state: damaged file -> full render", M.render_region(world, "overworld", 0, 0, state) == full and len(read) == 3)
        write_region({1: (60, 200, 3), 33: (30, 100), 40: (50, 200)})   # chunk 1 saved again in the same second
        del read[:]
        part = M.render_region(world, "overworld", 0, 0, state)
        check("state: same sector and second, other length -> read", read == [(1, 0)] and part == M.render_region(world, "overworld", 0, 0), read)
        os.remove(state)
        os.makedirs(state)   # cannot be written: the PNG still comes, no .tmp left
        png = M.render_region(world, "overworld", 0, 0, state)
        check("state: not writable -> PNG anyway, no tmp left", png and not [f for f in os.listdir(root) if ".tmp" in f])
        os.rmdir(state)
        # get_tile keeps a state only for a tile drawn before
        cache = os.path.join(root, "tiles")
        M.get_tile(world, cache, "overworld", 0, 0)
        st = os.path.join(cache, "overworld", "r.0.0.state")
        check("get_tile: first tile without state", not os.path.exists(st))
        os.utime(rpath, (time.time() + 10, time.time() + 10))
        M.get_tile(world, cache, "overworld", 0, 0)
        check("get_tile: redrawn tile keeps a state", os.path.exists(st))
    finally:
        M._chunk_top = real_top
finally:
    shutil.rmtree(root, ignore_errors=True)

print("PASS %d  FAIL %d" % (OK, FAIL))
sys.exit(1 if FAIL else 0)
