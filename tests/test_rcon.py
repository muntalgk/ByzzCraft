"""Rcon against a fake RCON server on loopback: multi-packet reassembly (command_long), reply id (command)."""
import json, os, shutil, socket, struct, sys, tempfile, threading

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))  # panel folder
OK = FAIL = 0
def check(name, cond, extra=""):
    global OK, FAIL
    OK, FAIL = (OK + 1, FAIL) if cond else (OK, FAIL + 1)
    if not cond:
        print("FAIL:", name, extra)

PW = "pw-prova"
ANSWERS = {"short": b"hello", "split": b"a" * 4096 + b"b" * 4096 + b"c" * 10, "exact": b"x" * 8192}

def recv_all(c, n):
    data = b""
    while len(data) < n:
        chunk = c.recv(n - len(data))
        if not chunk:
            raise ConnectionError
        data += chunk
    return data

def send(c, rid, typ, body):
    payload = struct.pack("<ii", rid, typ) + body + b"\x00\x00"
    c.sendall(struct.pack("<i", len(payload)) + payload)

def serve(ls):
    c, _ = ls.accept()
    try:
        while True:
            (ln,) = struct.unpack("<i", recv_all(c, 4))
            data = recv_all(c, ln)
            rid, typ = struct.unpack("<ii", data[:8])
            body = data[8:-2]
            if typ == 3:
                send(c, rid if body == PW.encode() else -1, 2, b"")
            elif body == b"stale":
                send(c, rid - 100, 0, b"old piece")     # leftover of an earlier answer: command() must skip it
                send(c, rid, 0, b"fresh")
            else:
                ans = ANSWERS[body.decode()]
                for i in range(0, len(ans), 4096):      # like Minecraft: 4096-byte pieces, no end marker
                    send(c, rid, 0, ans[i:i + 4096])
    except (ConnectionError, OSError):
        pass
    finally:
        c.close()

root = tempfile.mkdtemp(prefix="rcon_test_")
ls = socket.socket()
try:
    ls.bind(("127.0.0.1", 0)); ls.listen(1)
    j = os.path.join
    os.makedirs(j(root, "data")); os.makedirs(j(root, "srv"))
    os.environ["BYZZ_DATA"] = j(root, "data")
    os.environ.pop("PANEL_CONFIG", None)
    json.dump({"server_dir": j(root, "srv")}, open(j(root, "data", "panel-config.json"), "w"))
    open(j(root, "srv", "server.properties"), "w").write(
        "enable-rcon=true\nrcon.password=%s\nrcon.port=%d\n" % (PW, ls.getsockname()[1]))
    import panel
    from byzzcraft import paths; assert paths.DATA == os.path.abspath(os.environ["BYZZ_DATA"]), "run each test file in its own process: real data at risk"
    threading.Thread(target=serve, args=(ls,), daemon=True).start()

    r = panel.Rcon()
    got = r.command_long("split")
    check("long answer 4096+4096+10 reassembled exactly", got.encode() == ANSWERS["split"], len(got))
    got = r.command_long("exact")
    check("answer of exactly 8192 bytes (ends by timeout)", got.encode() == ANSWERS["exact"], len(got))
    check("short answer", r.command_long("short") == "hello")
    check("command returns the reply to its own id, skipping stale pieces", r.command("stale") == "fresh")
    check("command: plain answer", r.command("short") == "hello")
    r.reset()
finally:
    ls.close()
    shutil.rmtree(root, ignore_errors=True)
print("PASS %d  FAIL %d" % (OK, FAIL))
sys.exit(1 if FAIL else 0)
