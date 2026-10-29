import hashlib, io, json, os, shutil, sys, tempfile, urllib.request, zipfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))  # panel folder

OK = FAIL = 0
def check(name, cond, extra=""):
    global OK, FAIL
    OK, FAIL = (OK + 1, FAIL) if cond else (OK, FAIL + 1)
    if not cond:
        print("FAIL:", name, extra)

root = tempfile.mkdtemp(prefix="plugins_test_")
try:
    os.environ["BYZZ_DATA"] = os.path.join(root, "data")
    from byzzcraft import plugins as P, paths
    assert paths.DATA == os.path.abspath(os.environ["BYZZ_DATA"]), "run each test file in its own process: real data at risk"
    sd = os.path.join(root, "srv"); pd = os.path.join(sd, "plugins")
    os.makedirs(pd)

    def jar(name, ver):
        b = io.BytesIO()
        with zipfile.ZipFile(b, "w") as z:
            z.writestr("plugin.yml", "name: %s\nversion: %s\n" % (name, ver))
        return b.getvalue()

    CDN = "https://hangarcdn.papermc.io/plugins/o/Foo/PAPER/1.0/Foo-1.0.jar"
    def version(ver="1.0", file="Foo-1.0.jar", data=b"x", url=CDN, ext=None, sha=None, deps=()):
        return {"result": [{"name": ver, "channel": {"name": "Release"}, "pluginDependencies": {"PAPER": [{"name": n, "required": r} for n, r in deps]},
                            "downloads": {"PAPER": {"fileInfo": {"name": file, "sizeBytes": len(data), "sha256Hash": sha or hashlib.sha256(data).hexdigest()},
                                                    "externalUrl": ext, "downloadUrl": url}}}]}
    ROUTES, GOT = {}, []
    class R:
        def __init__(self, b): self.b = b
        def read(self): return self.b
        def __enter__(self): return self
        def __exit__(self, *a): return False
    def fake(req, timeout=None):
        u = req.full_url; GOT.append(u)
        for k, v in ROUTES.items():
            if k in u:
                if isinstance(v, Exception): raise v
                return R(v if isinstance(v, bytes) else json.dumps(v).encode())
        raise OSError("no route " + u)
    urllib.request.urlopen = fake

    # search parsing and trimming
    ROUTES["/projects?"] = {"pagination": {"count": 30, "limit": 20, "offset": 0}, "result": [
        {"name": "Foo", "namespace": {"owner": "O", "slug": "Foo"}, "description": "d", "stats": {"downloads": 5, "stars": 2},
         "avatarUrl": "https://hangarcdn.papermc.io/avatars/a.webp"},
        {"name": "Bar", "namespace": {"owner": "P", "slug": "Bar"}, "stats": {"downloads": 1, "stars": 0}, "avatarUrl": "https://evil.example/a.png"}]}
    r = P.search("fo o", 0, "26.3")
    check("search ok", r["ok"] and [x["slug"] for x in r["results"]] == ["Foo", "Bar"] and r["more"], r)
    check("search url", "q=fo+o" in GOT[-1] and "version=26.3" in GOT[-1] and "platform=PAPER" in GOT[-1], GOT[-1])
    check("search card", r["results"][0]["url"] == "https://hangar.papermc.io/O/Foo" and r["results"][0]["downloads"] == 5)
    check("avatar only from the CDN", r["results"][0]["avatar"] and not r["results"][1]["avatar"])
    ROUTES["/projects?"] = OSError("offline")
    r = P.search("", 0, "26.3")
    check("search offline is a message", r["ok"] is False and "offline" in r["message"], r)

    # install: happy path
    data = jar("Foo", "1.0")
    ROUTES["/projects/Foo/versions"] = version(data=data, deps=[("Vault", True), ("Soft", False)])
    ROUTES["hangarcdn"] = data
    r = P.install(sd, "Foo", "26.3")
    check("install ok", r["ok"] and r["file"] == "Foo-1.0.jar" and open(os.path.join(pd, "Foo-1.0.jar"), "rb").read() == data, r)
    check("required missing dep reported, optional not", r["deps"] == ["Vault"], r)
    check("restart message", "Restart" in r["message"])
    check("no tmp left", os.listdir(pd) == ["Foo-1.0.jar"], os.listdir(pd))
    inst = P.installed(sd)
    check("installed list", len(inst) == 1 and inst[0]["slug"] == "Foo" and inst[0]["version"] == "1.0" and inst[0]["name"] == "Foo", inst)

    # update: the old jar of the same slug goes away
    d2 = jar("Foo", "2.0")
    ROUTES["/projects/Foo/versions"] = version("2.0", "Foo-2.0.jar", d2)
    ROUTES["hangarcdn"] = d2
    r = P.install(sd, "Foo", "26.3")
    check("update replaces", r["ok"] and sorted(os.listdir(pd)) == ["Foo-2.0.jar"], (r, os.listdir(pd)))
    check("old jar in trash", any(n.endswith("Foo-1.0.jar") for n in os.listdir(os.path.join(sd, "trash"))))
    check("tracking follows", list(json.load(open(os.path.join(paths.DATA, "plugins.json")))) == ["Foo-2.0.jar"])

    # a dependency that is installed is not reported
    open(os.path.join(pd, "Vault.jar"), "wb").write(jar("Vault", "1"))
    ROUTES["/projects/Baz/versions"] = version("1", "Baz-1.jar", d2, deps=[("vault", True)])
    check("installed dep not reported", P.install(sd, "Baz", "26.3")["deps"] == [])

    # refusals: nothing written
    before = sorted(os.listdir(pd))
    ROUTES["/projects/Foo/versions"] = version(sha="0" * 64, data=data)
    ROUTES["hangarcdn"] = data
    r = P.install(sd, "Foo", "26.3")
    check("bad sha refused", not r["ok"] and sorted(os.listdir(pd)) == before, r)
    ROUTES["/projects/Foo/versions"] = version(url="https://evil.example/Foo-1.0.jar", data=data)
    check("non-hangarcdn refused", not P.install(sd, "Foo", "26.3")["ok"])
    ROUTES["/projects/Foo/versions"] = version(url="http://hangarcdn.papermc.io/x.jar", data=data)
    check("http refused", not P.install(sd, "Foo", "26.3")["ok"])
    ROUTES["/projects/Foo/versions"] = version(url="https://hangarcdn.papermc.io.evil.example/x.jar", data=data)
    check("lookalike host refused", not P.install(sd, "Foo", "26.3")["ok"])
    ROUTES["/projects/Foo/versions"] = version(url=None, ext="https://example.com/dl", data=data)
    n = len(GOT)
    r = P.install(sd, "Foo", "26.3")
    check("externalUrl only refused, no download", not r["ok"] and r["url"] == "https://example.com/dl" and not any("hangarcdn" in u for u in GOT[n:]), r)
    for bad in ("../x.jar", "a/b.jar", "x.exe", "x.jar\..\y", ""):
        ROUTES["/projects/Foo/versions"] = version(file=bad, data=data)
        check("bad file name %r" % bad, not P.install(sd, "Foo", "26.3")["ok"])
    check("bad slug refused", not P.install(sd, "../x", "26.3")["ok"])
    check("nothing changed", sorted(os.listdir(pd)) == before, os.listdir(pd))

    # remove
    check("remove traversal", not P.remove(sd, "../x.jar")["ok"])
    check("remove missing", not P.remove(sd, "nope.jar")["ok"])
    check("remove non-jar", not P.remove(sd, "plugins")["ok"])
    r = P.remove(sd, "Foo-2.0.jar")
    check("remove ok", r["ok"] and not os.path.exists(os.path.join(pd, "Foo-2.0.jar")), r)
    check("untracked on remove", "Foo-2.0.jar" not in json.load(open(os.path.join(paths.DATA, "plugins.json"))))
finally:
    shutil.rmtree(root, ignore_errors=True)

print("PASS %d  FAIL %d" % (OK, FAIL))
sys.exit(1 if FAIL else 0)
