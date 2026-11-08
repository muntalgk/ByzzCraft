import hashlib, io, json, os, shutil, sys, tempfile, urllib.request, zipfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))  # panel folder

OK = FAIL = 0
def check(name, cond, extra=""):
    global OK, FAIL
    OK, FAIL = (OK + 1, FAIL) if cond else (OK, FAIL + 1)
    if not cond:
        print("FAIL:", name, extra)

root = tempfile.mkdtemp(prefix="mods_test_")
try:
    os.environ["BYZZ_DATA"] = os.path.join(root, "data")
    from byzzcraft import mods as M, paths
    assert paths.DATA == os.path.abspath(os.environ["BYZZ_DATA"]), "run each test file in its own process: real data at risk"
    sd = os.path.join(root, "srv"); md = os.path.join(sd, "mods")
    os.makedirs(md)

    def jar(files=()):
        b = io.BytesIO()
        with zipfile.ZipFile(b, "w") as z:
            for n, c in dict(files).items():
                z.writestr(n, c)
        return b.getvalue()
    def put(name, data):
        open(os.path.join(md, name), "wb").write(data)

    TOML = '# c\nmodLoader="javafml"\n\n[[mods]] #mandatory\nmodId="alpha" #mandatory\nversion="${file.jarVersion}"\ndisplayName=\'Alpha Mod\'\n\n[[dependencies.alpha]]\nmodId="neoforge"\n'
    put("alpha.jar", jar({"META-INF/neoforge.mods.toml": TOML, "META-INF/MANIFEST.MF": "Manifest-Version: 1.0\r\nImplementation-Version: 2.5\r\n"}))
    put("beta.jar", jar({"META-INF/mods.toml": '[[mods]]\nmodId = "beta"\nversion = "1.0"\ndisplayName = "Beta"\n'}))
    put("gamma.jar", jar({"fabric.mod.json": json.dumps({"id": "gamma", "name": "Gamma", "version": "3.0"})}))
    put("delta.jar", jar({"quilt.mod.json": json.dumps({"quilt_loader": {"id": "delta", "version": "4", "metadata": {"name": "Delta"}}})}))
    put("junk.jar", b"not a zip")
    put("[26.3] Odd name.jar", jar())
    put("off.jar.disabled", jar({"META-INF/mods.toml": '[[mods]]\nmodId="off"\nversion="9"\n'}))
    open(os.path.join(md, "notes.txt"), "w").write("x")

    inst = {m["file"]: m for m in M.installed(sd)}
    check("listing skips non-jars", "notes.txt" not in inst and len(inst) == 7, sorted(inst))
    a = inst["alpha.jar"]
    check("neoforge.mods.toml + jarVersion from manifest", (a["id"], a["name"], a["version"]) == ("alpha", "Alpha Mod", "2.5"), a)
    b = inst["beta.jar"]
    check("mods.toml", (b["id"], b["name"], b["version"]) == ("beta", "Beta", "1.0"), b)
    g = inst["gamma.jar"]
    check("fabric.mod.json", (g["id"], g["name"], g["version"], g["kind"]) == ("gamma", "Gamma", "3.0", "fabric"), g)
    d = inst["delta.jar"]
    check("quilt.mod.json", (d["id"], d["name"], d["kind"]) == ("delta", "Delta", "quilt"), d)
    check("broken jar falls back to the file name", inst["junk.jar"]["name"] == "junk" and inst["junk.jar"]["version"] == "")
    check("disabled flag", inst["off.jar.disabled"]["enabled"] is False and inst["off.jar.disabled"]["id"] == "off" and inst["alpha.jar"]["enabled"])
    check("loader from mods / jar name", M.loader(sd, None)[0] == "fabric" and M.loader(sd, "quilt-server-launch.jar")[0] == "quilt")

    # enable / disable
    r = M.toggle(sd, "beta.jar")
    check("disable renames", r["ok"] and os.path.exists(os.path.join(md, "beta.jar.disabled")) and not os.path.exists(os.path.join(md, "beta.jar")), r)
    r = M.toggle(sd, "beta.jar.disabled")
    check("enable renames back", r["ok"] and os.path.exists(os.path.join(md, "beta.jar")), r)
    for bad in ("../x.jar", "..\\x.jar", "sub/alpha.jar", "nope.jar", "", "notes.txt", os.path.join(md, "alpha.jar")):
        check("rejected %r" % bad, not M.toggle(sd, bad)["ok"] and not M.remove(sd, bad)["ok"])
    check("files untouched", sorted(os.listdir(md)) == sorted(set(inst) | {"notes.txt"}), os.listdir(md))

    # remove -> trash
    r = M.remove(sd, "junk.jar")
    check("remove to trash", r["ok"] and not os.path.exists(os.path.join(md, "junk.jar")) and any(n.endswith("junk.jar") for n in os.listdir(os.path.join(sd, "trash"))), r)

    # fake Modrinth
    ROUTES, GOT = {}, []
    class R:
        def __init__(self, b): self.b = b
        def read(self, n=-1): return self.b if n < 0 else self.b[:n]
        def __enter__(self): return self
        def __exit__(self, *a): return False
    def fake(req, timeout=None):
        u = req.full_url; GOT.append((u, req.data, req.get_header("User-agent")))
        for k, v in ROUTES.items():
            if k in u:
                if isinstance(v, Exception): raise v
                return R(v if isinstance(v, bytes) else json.dumps(v).encode())
        raise OSError("no route " + u)
    urllib.request.urlopen = fake
    M.UA = "ByzzCraft/1.0"

    ROUTES["/search"] = {"hits": [{"title": "Foo", "slug": "foo", "author": "me", "description": "d", "downloads": 3, "icon_url": "https://cdn.modrinth.com/a.png"},
                                  {"title": "Bar", "slug": "bar", "downloads": 1, "icon_url": "https://evil.example/a.png"}], "offset": 0, "total_hits": 30}
    r = M.search("fo", 0, "neoforge", "26.3")
    q = GOT[-1][0]
    check("search", r["ok"] and [x["slug"] for x in r["results"]] == ["foo", "bar"] and r["more"] and r["results"][0]["avatar"] and not r["results"][1]["avatar"], r)
    check("search facets", "categories%3Aneoforge" in q and "versions%3A26.3" in q and "project_type%3Amod" in q, q)
    ROUTES["/search"] = OSError("offline")
    check("search offline is a message", not M.search("", 0, "neoforge", "26.3")["ok"])

    # install
    data = jar({"META-INF/mods.toml": '[[mods]]\nmodId="foo"\nversion="1"\n'})
    def ver(num="1.0", name="foo-1.0.jar", d=data, url="https://cdn.modrinth.com/data/x/foo-1.0.jar", h=None, deps=()):
        return {"version_number": num, "version_type": "release", "dependencies": [{"project_id": p, "dependency_type": "required"} for p in deps],
                "files": [{"primary": True, "filename": name, "url": url, "hashes": h if h is not None else {"sha512": hashlib.sha512(d).hexdigest(), "sha1": hashlib.sha1(d).hexdigest()}}]}
    ROUTES["/project/foo/version"] = [ver(deps=["P1", "P2"])]
    ROUTES["/v2/projects?ids"] = [{"id": "P1", "slug": "alpha", "title": "Alpha"}, {"id": "P2", "slug": "other-lib", "title": "Other Lib"}]
    ROUTES["cdn.modrinth.com/data"] = data
    r = M.install(sd, "foo", "neoforge", "26.3")
    check("install ok", r["ok"] and open(os.path.join(md, "foo-1.0.jar"), "rb").read() == data and "Restart" in r["message"], r)
    check("deps: installed one not reported", r["deps"] == ["Other Lib"], r)
    check("generic user agent, no email", all((g[2] or "").startswith("ByzzCraft") and "@" not in g[2] for g in GOT), GOT[-1][2])
    check("no tmp left", not any(n.endswith(".panel-tmp") for n in os.listdir(md)))
    os.rename(os.path.join(md, "foo-1.0.jar"), os.path.join(md, "foo-1.0.hold"))   # an installed foo is refused earlier: set it aside
    before = sorted(os.listdir(md))
    ROUTES["/project/foo/version"] = [ver(h={"sha512": "0" * 128})]
    check("sha512 mismatch refused", not M.install(sd, "foo", "neoforge", "26.3")["ok"] and sorted(os.listdir(md)) == before)
    ROUTES["/project/foo/version"] = [ver(h={"sha1": "0" * 40})]
    check("sha1 mismatch refused", not M.install(sd, "foo", "neoforge", "26.3")["ok"] and sorted(os.listdir(md)) == before)
    ROUTES["/project/foo/version"] = [ver(h={})]
    check("no hash refused", not M.install(sd, "foo", "neoforge", "26.3")["ok"])
    for u in ("https://evil.example/x.jar", "http://cdn.modrinth.com/x.jar", "https://cdn.modrinth.com.evil.example/x.jar"):
        ROUTES["/project/foo/version"] = [ver(url=u)]
        check("host refused %s" % u, not M.install(sd, "foo", "neoforge", "26.3")["ok"])
    for bad in ("../x.jar", "a/b.jar", "x.exe", ".hidden.jar", ""):
        ROUTES["/project/foo/version"] = [ver(name=bad)]
        check("bad file name %r" % bad, not M.install(sd, "foo", "neoforge", "26.3")["ok"])
    check("bad slug refused", not M.install(sd, "../x", "neoforge", "26.3")["ok"])
    ROUTES["/project/foo/version"] = []
    check("no compatible version", not M.install(sd, "foo", "neoforge", "26.3")["ok"])
    check("nothing changed", sorted(os.listdir(md)) == before, os.listdir(md))
    os.rename(os.path.join(md, "foo-1.0.hold"), os.path.join(md, "foo-1.0.jar"))
    n = len(GOT)
    ROUTES["/project/foo/version"] = [ver()]
    r = M.install(sd, "foo", "neoforge", "26.3")
    check("install refused: same mod id already installed (no network)", not r["ok"] and "already" in r["message"] and len(GOT) == n, r)
    ROUTES["/project/off/version"] = [ver(name="off-1.jar")]
    check("install refused: disabled copy counts", not M.install(sd, "off", "neoforge", "26.3")["ok"] and len(GOT) == n)
    ROUTES["/project/bar/version"] = [ver(name="foo-1.0.jar")]
    check("install refused: same file name already installed", "already" in M.install(sd, "bar", "neoforge", "26.3")["message"])
    check("install refused: empty Minecraft version", not M.install(sd, "bar", "neoforge", "")["ok"])
    put("cc.jar", jar({"META-INF/mods.toml": '[[mods]]\nmodId="cloth_config"\nversion="1"\n'}))
    ROUTES["/project/cloth-config/version"] = [ver(name="cloth-config-9.jar")]
    check("install refused: mod id cloth_config = slug cloth-config", "already" in M.install(sd, "cloth-config", "neoforge", "26.3")["message"])
    os.remove(os.path.join(md, "cc.jar"))
    ROUTES["/project/betamod/version"] = [dict(ver(name="betamod-0.1.jar"), version_type="beta")]
    r = M.install(sd, "betamod", "neoforge", "26.3")
    check("beta install says it gets no automatic updates", r["ok"] and "beta" in r["message"] and "automatic" in r["message"], r)
    os.remove(os.path.join(md, "betamod-0.1.jar"))

    # updates: the old jar goes to the trash only after the new one is verified
    d2 = jar({"META-INF/mods.toml": '[[mods]]\nmodId="foo"\nversion="2"\n'})
    h1 = hashlib.sha1(data).hexdigest()
    ROUTES["version_files/update"] = {h1: ver("2.0", "foo-2.0.jar", d2)}
    ROUTES["cdn.modrinth.com/data"] = d2
    check("check_updates", M.check_updates(sd, "neoforge", "26.3") and {m["file"]: m for m in M.installed(sd)}["foo-1.0.jar"]["update"] == "2.0")
    r = M.update(sd, "foo-1.0.jar", "neoforge", "26.3")
    check("update replaces", r["ok"] and "foo-2.0.jar" in os.listdir(md) and "foo-1.0.jar" not in os.listdir(md), (r, os.listdir(md)))
    ROUTES["version_files/update"] = {hashlib.sha1(d2).hexdigest(): ver("3.0", "foo-3.0.jar", data)}   # file is not what the API promised
    r = M.update(sd, "foo-2.0.jar", "neoforge", "26.3")
    check("hash mismatch on update keeps the old jar", not r["ok"] and "foo-2.0.jar" in os.listdir(md) and "foo-3.0.jar" not in os.listdir(md), (r, os.listdir(md)))
    # a disabled mod stays disabled when updated
    os.rename(os.path.join(md, "foo-2.0.jar"), os.path.join(md, "foo-2.0.jar.disabled"))
    ROUTES["version_files/update"] = {hashlib.sha1(d2).hexdigest(): ver("3.0", "foo-3.0.jar", data)}
    ROUTES["cdn.modrinth.com/data"] = data
    r = M.update(sd, "foo-2.0.jar.disabled", "neoforge", "26.3")
    check("update of a disabled mod keeps it disabled", r["ok"] and "foo-3.0.jar.disabled" in os.listdir(md) and "foo-3.0.jar" not in os.listdir(md)
          and "foo-2.0.jar.disabled" not in os.listdir(md), (r, os.listdir(md)))
    os.remove(os.path.join(md, "foo-3.0.jar.disabled"))
    open(os.path.join(md, "foo-2.0.jar"), "wb").write(d2)
    ROUTES["cdn.modrinth.com/data"] = d2

    # update: beta is not an update; a failing file operation keeps the old jar and says so
    ROUTES["version_files/update"] = {hashlib.sha1(d2).hexdigest(): dict(ver("3.0", "foo-3.0.jar", data), version_type="beta")}
    r = M.update(sd, "foo-2.0.jar", "neoforge", "26.3")
    check("beta is not offered by update()", not r["ok"] and "newer" in r["message"] and "foo-3.0.jar" not in os.listdir(md), r)
    M._upd.clear()
    M.check_updates(sd, "neoforge", "26.3")
    check("beta is not shown by check_updates", {m["file"]: m for m in M.installed(sd)}["foo-2.0.jar"]["update"] == "")
    ROUTES["version_files/update"] = {hashlib.sha1(d2).hexdigest(): ver("3.0", "foo-3.0.jar", data)}
    ROUTES["cdn.modrinth.com/data"] = data
    real = M.BK._to_trash_dir
    def locked(*a): raise PermissionError("jar locked")
    M.BK._to_trash_dir = locked
    r = M.update(sd, "foo-2.0.jar", "neoforge", "26.3")
    M.BK._to_trash_dir = real
    check("locked jar: old kept, new not written, no tmp, in-use message (not Modrinth)",
          not r["ok"] and "in use" in r["message"] and "Modrinth" not in r["message"] and "foo-2.0.jar" in os.listdir(md)
          and "foo-3.0.jar" not in os.listdir(md) and not any(n.endswith(".panel-tmp") for n in os.listdir(md)), (r, os.listdir(md)))
    real_rename = os.rename
    def locked_rename(*a): raise PermissionError("locked")
    os.rename = locked_rename
    r = M.toggle(sd, "foo-2.0.jar")
    os.rename = real_rename
    check("toggle on a locked jar: in-use message", not r["ok"] and "in use" in r["message"], r)

    # client pack
    for n in list(os.listdir(md)):
        if n not in ("alpha.jar", "beta.jar", "off.jar.disabled", "foo-2.0.jar"):
            os.remove(os.path.join(md, n))
    ha, hb = (hashlib.sha1(open(os.path.join(md, n), "rb").read()).hexdigest() for n in ("alpha.jar", "beta.jar"))
    ROUTES.clear()
    ROUTES["/v2/version_files"] = OSError("offline")
    p, skipped = M.pack(sd, "neoforge", "26.3.0.101", "26.3")
    names = zipfile.ZipFile(p).namelist()
    check("Modrinth down: everything enabled is included", skipped == [] and sorted(names) == ["README.txt", "mods/alpha.jar", "mods/beta.jar", "mods/foo-2.0.jar"], names)
    check("README has loader and version", "NeoForge 26.3.0.101, Minecraft 26.3" in zipfile.ZipFile(p).read("README.txt").decode())
    check("nothing cached when offline", not os.path.exists(M.SIDES))
    ROUTES["/v2/version_files"] = {ha: {"project_id": "PA"}, hb: {"project_id": "PB"}}
    ROUTES["/v2/projects?ids"] = [{"id": "PA", "client_side": "unsupported"}, {"id": "PB", "client_side": "required"}]
    p2, skipped = M.pack(sd, "neoforge", "26.3.0.101", "26.3")
    names = zipfile.ZipFile(p2).namelist()
    check("unsupported on client excluded", skipped == ["Alpha Mod"] and "mods/alpha.jar" not in names and "mods/beta.jar" in names and "mods/foo-2.0.jar" in names, (skipped, names))
    check("zip stored, old zip replaced", all(i.compress_type == zipfile.ZIP_STORED for i in zipfile.ZipFile(p2).infolist()) and not os.path.exists(p), os.listdir(paths.CACHE))
    n = len(GOT)
    p3, _ = M.pack(sd, "neoforge", "26.3.0.101", "26.3")
    check("same mods: same zip, no new network", p3 == p2 and len(GOT) == n)
    check("server_only flag in the listing", {m["file"]: m for m in M.installed(sd)}["alpha.jar"]["server_only"])
    os.utime(os.path.join(md, "beta.jar"), (0, 0))   # mtime before 1980: zip stores no such date
    try:
        p4, _ = M.pack(sd, "neoforge", "26.3.0.101", "26.3")
        check("pack with a 1970 jar", "mods/beta.jar" in zipfile.ZipFile(p4).namelist())
    except Exception as e:  # noqa: BLE001
        check("pack with a 1970 jar", False, e)

    # icons in the list: Modrinth's (cdn only), else the logo inside the jar
    M._sha.clear()
    os.remove(M.ICONS)   # filled by the pack above, when Modrinth sent no icon
    ROUTES["/v2/projects?ids"] = [{"id": "PA", "client_side": "unsupported", "icon_url": "https://cdn.modrinth.com/data/PA/icon.png"},
                                  {"id": "PB", "client_side": "required", "icon_url": "https://evil.example/x.png"}]
    M.fetch_sides([M._sha1(os.path.join(md, n)) for n in ("alpha.jar", "beta.jar")])
    by = {m["file"]: m for m in M.installed(sd)}
    check("Modrinth icon in the listing", by["alpha.jar"]["icon"] == "https://cdn.modrinth.com/data/PA/icon.png", by["alpha.jar"])
    check("icon from another host dropped", by["beta.jar"]["icon"] == "", by["beta.jar"])
    png = bytes([0x89]) + b"PNG" + bytes([13, 10, 26, 10]) + b"rest"
    put("gamma.jar", jar({"META-INF/neoforge.mods.toml": '[[mods]]\nmodId="gamma"\nversion="1"\nlogoFile="gamma_logo.png"\n', "gamma_logo.png": png}))
    put("delta.jar", jar({"fabric.mod.json": '{"id": "delta", "version": "1", "icon": "assets/delta/icon.png"}', "assets/delta/icon.png": b"not a png"}))
    put("huge.jar", jar({"META-INF/mods.toml": '[[mods]]\nmodId="huge"\nlogoFile="h.png"\n', "h.png": png + b"x" * (M.LOGO_MAX + 1)}))
    by = {m["file"]: m for m in M.installed(sd)}
    check("jar logo flagged", by["gamma.jar"]["logo"] and by["delta.jar"]["logo"] and not by["beta.jar"]["logo"])
    check("jar logo served", M.logo(sd, "gamma.jar") == png)
    check("not a PNG: no logo", M.logo(sd, "delta.jar") is None)
    check("logo too big: none", M.logo(sd, "huge.jar") is None)
    check("logo: name not in the listing", M.logo(sd, "../gamma.jar") is None and M.logo(sd, "nope.jar") is None)
finally:
    shutil.rmtree(root, ignore_errors=True)

print("PASS %d  FAIL %d" % (OK, FAIL))
sys.exit(1 if FAIL else 0)
