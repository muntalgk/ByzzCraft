import json, os, shutil, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))  # panel folder
from byzzcraft import access as A

d = tempfile.mkdtemp(prefix="acc_test_")
for f in A.FILES.values():
    with open(os.path.join(d, f), "w", encoding="utf-8") as fh:
        json.dump([{"uuid": "a1b2c3d4-0000-4000-8000-000000000001", "name": "Alice"},
                   {"uuid": "cd8c8cc0-0000-4000-8000-000000000002", "name": "Bob"}] if f == "whitelist.json" else [], fh)
ok = fail = 0
A.lookup = lambda name: ("069a79f4-44e9-4726-a5be-fca90e38aaf5", "Notch") if name.lower() == "notch" else None  # no network


def check(name, cond, extra=""):
    global ok, fail
    ok, fail = (ok + 1, fail) if cond else (ok, fail + 1)
    print(("PASS " if cond else "FAIL ") + name, extra if not cond else "")


def err(name, fn, *a, **k):
    try:
        fn(*a, **k)
    except ValueError as e:
        check(name, True)
        return str(e)
    check(name, False, "no error")


print(A.apply_offline(d, "wl_add", "notch", who="test"))
wl = A.snapshot(d)["whitelist"]
n = next(e for e in wl if e["name"].lower() == "notch")
check("notch added with Mojang uuid and exact name", n["uuid"] == "069a79f4-44e9-4726-a5be-fca90e38aaf5" and n["name"] == "Notch", n)
print(err("già in whitelist", A.apply_offline, d, "wl_add", "NOTCH"))
print(A.apply_offline(d, "op", "Notch"))
check("op level 4", any(e["name"] == "Notch" and e["level"] == 4 for e in A.snapshot(d)["ops"]))
print(A.apply_offline(d, "ban", "Notch", reason="test\nriga", who="admin"))
b = A.snapshot(d)["bans"][0]
check("ban with clean reason and source", b["reason"] == "test riga" and b["source"] == "ByzzCraft (admin)" and b["expires"] == "forever", b)
print(A.apply_offline(d, "pardon", "notch"))
print(A.apply_offline(d, "deop", "notch"))
print(A.apply_offline(d, "wl_remove", "notch"))
s = A.snapshot(d)
check("all removed", not any(e["name"] == "Notch" for k in ("whitelist", "ops", "bans") for e in s[k]))
check("Alice and Bob untouched", {e["name"] for e in s["whitelist"]} == {"Alice", "Bob"})
print(err("nome inesistente", A.apply_offline, d, "wl_add", "zq9xx_nonesiste7"))
print(err("kick a server spento", A.apply_offline, d, "kick", "Alice"))
print(err("deop di chi non è op", A.apply_offline, d, "deop", "Bob"))
print(A.apply_offline(d, "op", "Bob"))
check("uuid taken from the whitelist without Mojang", any(e["name"] == "Bob" and e["uuid"].startswith("cd8c8cc0") for e in A.snapshot(d)["ops"]))
check("valid JSON files", all(isinstance(json.load(open(os.path.join(d, f))), list) for f in A.FILES.values()))
check("rcon ban command with reason", A.command("ban", "Notch", "x\ny") == "ban Notch x y")
check("reason with % does not break", A.command("kick", "Notch", "100% sicuro %s") == "kick Notch 100% sicuro %s")
check("reason ignored for op", A.command("op", "Notch", "boh") == "op Notch")
# whitelist with a BOM (Notepad): read, and a change keeps the entries
wlp = os.path.join(d, "whitelist.json")
raw = open(wlp, encoding="utf-8").read()
open(wlp, "w", encoding="utf-8-sig").write(raw)
print(A.apply_offline(d, "wl_add", "Notch", profile=("069a79f4-44e9-4726-a5be-fca90e38aaf5", "Notch")))
check("BOM: entries kept", {e["name"] for e in A.snapshot(d)["whitelist"]} == {"Alice", "Bob", "Notch"})
# damaged file: not overwritten
open(wlp, "w", encoding="utf-8").write('[{"uuid": "x", "name": "Alice"},')
print(err("file rovinato rifiutato", A.apply_offline, d, "wl_add", "Dinnerbone", profile=("61699b2e-d327-4a01-9f1e-0ea8c3f06bc6", "Dinnerbone")))
check("damaged file untouched", open(wlp, encoding="utf-8").read() == '[{"uuid": "x", "name": "Alice"},')
os.remove(wlp)
print(A.apply_offline(d, "wl_add", "Dinnerbone", profile=("61699b2e-d327-4a01-9f1e-0ea8c3f06bc6", "Dinnerbone")))
check("missing file = new list", [e["name"] for e in A.snapshot(d)["whitelist"]] == ["Dinnerbone"])
off = A.resolve(d, "wl_add", "Notch", offline=True)
check("offline uuid v3 of Notch", off == ("b50ad385-829d-3141-a216-7e7d7539ba7f", "Notch") and off[0][14] == "3", off)
shutil.rmtree(d)
print("PASS %d  FAIL %d" % (ok, fail))
sys.exit(1 if fail else 0)
