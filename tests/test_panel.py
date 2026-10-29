import contextlib, io, json, os, shutil, sys, tempfile, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))  # panel folder

OK = FAIL = 0
def check(name, cond, extra=""):
    global OK, FAIL
    OK, FAIL = (OK + 1, FAIL) if cond else (OK, FAIL + 1)
    if not cond:
        print("FAIL:", name, extra)

root = tempfile.mkdtemp(prefix="panel_test_")
try:
    j = os.path.join
    srv_dir = j(root, "srv")
    os.makedirs(j(root, "data")); os.makedirs(srv_dir)
    os.environ["BYZZ_DATA"] = j(root, "data")
    os.environ.pop("PANEL_CONFIG", None)
    with open(j(root, "data", "panel-config.json"), "w") as f:
        json.dump({"server_dir": srv_dir}, f)
    import panel
    from byzzcraft import paths; assert paths.DATA == os.path.abspath(os.environ["BYZZ_DATA"]), "run each test file in its own process: real data at risk"

    # crash detection: "Stopping server" is also logged after an exception, so stop_requested alone is not a stop
    def run_update(report):
        c = panel.ServerControl()
        c.state, c.stop_requested, c.on_since, c.last_pid = "on", True, time.time() - 60, None
        calls = []
        c.on_crash = lambda: calls.append(1)
        d = j(srv_dir, "crash-reports")
        shutil.rmtree(d, ignore_errors=True)
        if report:
            os.makedirs(d)
            with open(j(d, "crash-x.txt"), "w") as f:
                f.write("---- Minecraft Crash Report ----\nDescription: Ticking entity\n")
        c.update(None)
        return bool(calls)
    check("crash report after stop_requested is a crash", run_update(True))
    check("stop_requested without crash report is a clean stop", not run_update(False))

    # force stop while starting: during the backup it waits, during the loading it is a stop, not a failed start
    c = panel.ServerControl()
    c.state, c.preparing = "starting", True
    ok, msg = c.kill("test")
    check("force stop during the pre-start backup is refused", not ok and c.state == "starting", msg)
    import subprocess
    c.preparing = False
    c.proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=os.name != "nt")
    ok, _ = c.kill("test")
    c.proc.wait(timeout=20)
    check("force stop while loading: off, no start error", ok and c.state == "off" and c.start_error is None, (ok, c.state))

    # language from Settings > Panel: only the ones with a translation file
    check("languages", panel.languages()[:1] == ["en"] and "it" in panel.languages(), panel.languages())
    form = {"auto_stop_minutes": 30, "backup_on_start": True, "auto_restart": True}
    try:
        panel.save_panel_settings(dict(form, lang="xx"), "test")
        check("unknown language refused", False)
    except ValueError:
        check("unknown language refused", True)
    changed = panel.save_panel_settings(dict(form, lang="it"), "test")
    saved = json.load(open(j(root, "data", "panel-config.json"), encoding="utf-8"))
    check("language saved", "lang" in changed and saved.get("lang") == "it" and panel.CFG["lang"] == "it", (changed, saved.get("lang")))

    # BungeeCord/Velocity forward the real UUIDs: no offline UUIDs then
    check("no proxy config: not forwarded", not panel.uuid_forwarded())
    with open(j(srv_dir, "spigot.yml"), "w") as f:
        f.write("settings:\n  bungeecord: true\n")
    check("spigot.yml bungeecord: forwarded", panel.uuid_forwarded())
    os.remove(j(srv_dir, "spigot.yml"))
    os.makedirs(j(srv_dir, "config"))
    with open(j(srv_dir, "config", "paper-global.yml"), "w") as f:
        f.write("proxies:\n  velocity:\n    enabled: true\n    online-mode: true\n")
    check("paper-global.yml velocity: forwarded", panel.uuid_forwarded())
    with open(j(srv_dir, "config", "paper-global.yml"), "w") as f:
        f.write("proxies:\n  velocity:\n    enabled: false\n")
    check("velocity disabled: not forwarded", not panel.uuid_forwarded())

    # automatic RAM: half of the machine, never above 80% of the free memory, at least 512 MB
    for mem, want in (((900, 1990), "720M"), ((300, 960), "512M"), ((16000, 32000), "4096M")):
        panel.system_memory = lambda mem=mem: mem
        check("auto_ram %s" % (mem,), panel.auto_ram() == want, panel.auto_ram())

    # where Minecraft listens
    props = j(srv_dir, "server.properties")
    for ip, want in (("", "127.0.0.1"), ("0.0.0.0", "127.0.0.1"), ("192.168.1.20", "192.168.1.20")):
        with open(props, "w") as f:
            f.write("server-ip=%s\n" % ip)
        check("mc_host %r" % ip, panel.mc_host() == want, panel.mc_host())

    # icons: the URL carries the version, so a new logo is not hidden by the browser cache
    check("favicon URL versioned", ("/favicon.ico?v=%s" % panel.ICON_V).encode() in panel.login_page() and len(panel.ICON_V) == 8)

    # start script: run directly only with a shebang (an executable without one gives ENOEXEC)
    for name, body in (("bash.sh", "#!/bin/bash\n"), ("plain.sh", "java -jar server.jar\n")):
        p = j(srv_dir, name)
        with open(p, "w") as f:
            f.write(body)
        os.chmod(p, 0o755)
        check("has_shebang %s" % name, panel.has_shebang(p) == body.startswith("#!"))
    check("has_shebang missing file", not panel.has_shebang(j(srv_dir, "nope.sh")))

    # spaces in the install path (systemd unit)
    # setup without the code: loopback or the machine's own LAN address, never through a proxy or from another PC
    def handler(peer, me, headers=None):
        h = type("H", (), {})()
        h.client_address, h.headers = (peer, 50000), headers or {}
        h.connection = type("S", (), {"getsockname": lambda self: (me, 8765)})()
        return h
    check("local: loopback", panel.is_local(handler("127.0.0.1", "127.0.0.1")))
    check("local: own LAN address", panel.is_local(handler("192.168.1.20", "192.168.1.20")))
    check("not local: another PC", not panel.is_local(handler("192.168.1.30", "192.168.1.20")))
    check("not local: proxy", not panel.is_local(handler("127.0.0.1", "127.0.0.1", {"X-Forwarded-For": "1.2.3.4"})))

    # rate-limit key: the client cannot pick it through a local proxy
    check("client_id: last X-Forwarded-For hop", panel.client_id(handler("127.0.0.1", "127.0.0.1", {"X-Forwarded-For": "9.9.9.9, 1.2.3.4", "Tailscale-User-Login": "a"})) == "1.2.3.4")
    check("client_id: tailscale login ignored", panel.client_id(handler("127.0.0.1", "127.0.0.1", {"Tailscale-User-Login": "a@b"})) == "127.0.0.1")
    check("client_id: headers ignored from another PC", panel.client_id(handler("192.168.1.30", "192.168.1.20", {"X-Forwarded-For": "1.1.1.1"})) == "192.168.1.30")
    # login lock: an IPv6 /64 is one client, IPv4 and mapped addresses stay as they are
    check("fail_key: IPv6 by /64", panel.fail_key(handler("2001:db8:1:2:aaaa::1", "::1")) == panel.fail_key(handler("2001:db8:1:2:bbbb::9", "::1")) == "c:2001:db8:1:2::/64")
    check("fail_key: IPv4", panel.fail_key(handler("192.168.1.30", "192.168.1.20")) == "c:192.168.1.30")
    check("fail_key: mapped IPv4", panel.fail_key(handler("::ffff:10.0.0.5", "::1")) == "c:::ffff:10.0.0.5")
    # rotating addresses do not fill the memory: past 10000 keys the expired ones go
    old = time.time() - panel.FAIL_WINDOW - 1
    panel.FAILS.update({"c:x%d" % i: [old] for i in range(10001)}); panel.LOCKED.update({"c:y": old})
    panel.note_fail(["c:new"])
    check("note_fail prunes expired keys", set(panel.FAILS) == {"c:new"} and not panel.LOCKED, len(panel.FAILS))
    panel.FAILS.clear(); panel.RECENT_FAILS.clear()
    # setup without the code: Host must be an IP or localhost, Origin the same host
    for host, want in (("127.0.0.1:8765", True), ("localhost:8765", True), ("[::1]:8765", True), ("192.168.1.20", True), ("evil.example.com:8765", False), ("", False)):
        check("host_is_literal %r" % host, panel.host_is_literal(handler("127.0.0.1", "127.0.0.1", {"Host": host})) == want)
    check("setup: rebinding host needs the code", not panel.setup_allowed(handler("127.0.0.1", "127.0.0.1", {"Host": "evil.example.com"}), ""))
    check("setup: local IP host ok", panel.setup_allowed(handler("127.0.0.1", "127.0.0.1", {"Host": "127.0.0.1:8765"}), ""))
    check("same_origin: none", panel.same_origin(handler("127.0.0.1", "127.0.0.1", {"Host": "localhost:1"})))
    check("same_origin: same", panel.same_origin(handler("127.0.0.1", "127.0.0.1", {"Host": "localhost:1", "Origin": "http://localhost:1"})))
    check("same_origin: cross-site", not panel.same_origin(handler("127.0.0.1", "127.0.0.1", {"Host": "localhost:1", "Origin": "http://evil.com"})))
    check("same_origin: proxy without the port", panel.same_origin(handler("127.0.0.1", "127.0.0.1", {"Host": "panel.example.com", "Origin": "https://panel.example.com:8443"})))
    check("same_origin: IPv6", panel.same_origin(handler("127.0.0.1", "127.0.0.1", {"Host": "[::1]:1", "Origin": "http://[::1]:1"})))
    check("same_origin: null", not panel.same_origin(handler("127.0.0.1", "127.0.0.1", {"Host": "localhost:1", "Origin": "null"})))
    check("same_origin: null without Host", not panel.same_origin(handler("127.0.0.1", "127.0.0.1", {"Origin": "null"})))
    check("same_origin: malformed IPv6", not panel.same_origin(handler("127.0.0.1", "127.0.0.1", {"Host": "localhost:1", "Origin": "http://[::1"})))
    check("same_origin: look-alike", not panel.same_origin(handler("127.0.0.1", "127.0.0.1", {"Host": "localhost:1", "Origin": "http://localhost.evil.com:1"})))

    # "Stopping server" only as a system line, never in chat
    for line, want in (("[12:00:00] [Server thread/INFO]: Stopping server", True), ("[12:00:00 INFO]: Stopping the server", True),
                       ("[12:00:00] [Server thread/INFO]: <bob> Stopping server", False), ("[12:00:00] [Server thread/INFO]: [Not Secure] <bob> Stopping the server", False)):
        check("STOPPING_RE %r" % line, bool(panel.STOPPING_RE.match(line)) == want)

    # java flags allowlist
    save = lambda a: panel.save_panel_settings({"java_args": a, "lang": panel.CFG["lang"]}, "t")
    for a in ("", "-XX:+UseG1GC -XX:MaxGCPauseMillis=200 -Dfile.encoding=UTF-8 -Xss1M -Xlog:gc:file=gc.log --add-opens=java.base/java.lang=ALL-UNNAMED",
              "-XX:+HeapDumpOnOutOfMemoryError -Xlog:gc*:stdout:time -Xlog:disable -Xlog:gc:gc-%p.log -Dpaper.disableChannelLimit=true"):
        try:
            save(a); good = True
        except ValueError:
            good = False
        check("java_args ok %r" % a[:30], good)
    for a in ("-XX:OnError=calc", "-XX:+OnOutOfMemoryError=x", "-XX:Flags=f", "-XX:VMOptionsFile=f", "-javaagent:x.jar", "-agentlib:jdwp", "@args.txt", "-cp x", "-Xbootclasspath/a:x", "-jar x.jar", "-Xmx4G", "-XX:+UseG1GC"+chr(10)+"-cp x",
              "-Dcom.sun.management.jmxremote.port=9010", "-Dlog4j.configurationFile=x", "-Djava.security.manager=x",
              # files written or loaded anywhere
              "-XX:HeapDumpPath=/home/a/.bashrc", "-XX:ErrorFile=x", "-XX:SharedArchiveFile=x.jsa", "-XX:AOTCache=x", "-XX:CRaCRestoreFrom=x",
              "-Xlog:gc:file=/etc/x", "-Xlog:gc:/tmp/x", "-Dfabric.addMods=/x.jar", "-Dloader.addMods=x",
              "-XX:ArchiveClassesAtExit=x", "-XX:StartFlightRecording:filename=/x.jfr", "-XX:FlightRecorderOptions=repository=/x") + (
              # Windows keeps the \ when it launches Java; on Linux it is an escape and the flag becomes a harmless ..x
              ("-Xlog:gc:file=..\\x",) if os.name == "nt" else ()):
        try:
            save(a); good = True
        except ValueError:
            good = False
        check("java_args refused %r" % a, not good)
    panel.CFG["java_args"] = "-server"   # written by hand in the config: other settings can still be saved
    try:
        save("-server"); good = True
    except ValueError:
        good = False
    check("java_args from the config file kept", good)
    # automatic RAM: while on, the value the server got, not an estimate from the RAM still free
    panel.SRV.state, panel.SRV.ram = "on", "4096M"
    check("ram_auto while on", panel.panel_settings()["ram_auto"] == "4096M", panel.panel_settings()["ram_auto"])
    panel.SRV.state = "off"
    check("ram_auto while off", panel.panel_settings()["ram_auto"] == panel.auto_ram())
    check("panel settings keep ram_total", panel.panel_settings().get("ram_total", 0) > 0)
    panel.SRV.ram = None
    panel.CFG["java_args"] = ""
    check("STOPPING_RE vanilla/Paper/Fabric, not chat", all(panel.STOPPING_RE.match(l) for l in (
        "[12:00:00] [Server thread/INFO]: Stopping server", "[12:00:00 INFO]: Stopping the server",
        "[12:00:00] [Server thread/INFO] (Minecraft) Stopping server")) and not any(panel.STOPPING_RE.match(l) for l in (
        "[12:00:00] [Server thread/INFO]: <bob> Stopping server", "[12:00:00] [Server thread/INFO]: System chat: Stopping the server")))

    # level-name goes into a path
    for v, want in (("world", True), ("my world", True), ("", False), ("../x", False), ("a/b", False), ("a"+chr(92)+"b", False), ("/abs", False), ("C:x", False), (".", False), ("world ", False), ("world.", False)):
        check("level_name_ok %r" % v, panel.level_name_ok(v) == want)

    # session valid only while the password it was created with is the current one
    panel.CFG["users_file"] = j(root, "data", "u.json")
    us = {}; panel.set_password(us, "bob", "a-long-password"); v1 = panel.pw_version(us["bob"])
    panel.set_password(us, "bob", "another-long-password")
    check("pw_version changes with the password", panel.pw_version(us["bob"]) != v1)
    panel.save_users(us)
    check("users file exists", os.path.exists(panel.CFG["users_file"]))

    NL = chr(10)
    # server.properties not in UTF-8: read and rewritten as read
    with open(props, "wb") as f:
        f.write(("motd=caf"+chr(233)+NL+"level-name=world"+NL).encode("latin-1"))
    check("server_props latin-1", panel.server_props().get("motd") == "caf"+chr(233), panel.server_props())
    panel.write_props({"level-name": "w2"}, "t")
    check("write_props rewrites as UTF-8", open(props, "rb").read() == ("motd=caf"+chr(233)+NL+"level-name=w2"+NL).encode("utf-8"))

    # usercache: bad entries are skipped
    with open(j(srv_dir, "usercache.json"), "w") as f:
        json.dump([{"name": "x"}, {"uuid": "../../etc", "name": "y"}, {"uuid": "12345678-1234-1234-1234-123456789abc", "name": "ok"}], f)
    check("known_players filters entries", panel.known_players() == {"12345678-1234-1234-1234-123456789abc": "ok"}, panel.known_players())

    if os.name != "nt":
        panel.HERE = j(root, "with space")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            try:
                panel.service()
            except SystemExit:   # root: the unit is not printed
                pass
        if os.geteuid() != 0:
            check("ExecStart quotes the paths", 'ExecStart="' in buf.getvalue(), buf.getvalue())

    # the update notice survives a restart (a language change restarts the panel) and goes away after the update
    panel.known_update()
    check("no saved check: no notice", not panel.UPDATE.get("newer"))
    for saved, newer in (("99.0.0", True), (panel.VERSION, False)):
        paths.write_json(panel.UPDATE_FILE, {"latest": saved})
        panel.known_update()
        check("saved check %s: newer=%s" % (saved, newer), panel.UPDATE.get("newer") is newer, panel.UPDATE)
    with open(panel.UPDATE_FILE, "w") as f:
        f.write("[1")
    panel.known_update()
    check("broken saved check: kept as it was", panel.UPDATE.get("newer") is False, panel.UPDATE)

    # who is online comes from "list", not from the ping sample (Anonymous Player, hide-online-players, max 12)
    check("list: vanilla/Paper", panel.list_names("There are 2 of a max of 10 players online: Zed, Alice") == ["Alice", "Zed"])
    check("list: nobody", panel.list_names("There are 0 of a max of 10 players online: ") == [])
    check("list: Spigot, colors", panel.list_names("§6There are §c1§6/§c20§6 players online:\n§fAlice") == ["Alice"])
    check("list: other answer -> ping fallback", panel.list_names("Unknown command") is None)
    check("list: EssentialsX groups", panel.list_names("There are 3 out of maximum 10 players online.\nDefault: A, C\nAdmin: B")
          == ["A", "B", "C"])
    # assets download: the map cache is emptied after the wait for a backup, so a tile drawn meanwhile (old colours) does not stay
    mapdir = j(panel.CACHE, "map", "overworld")
    real = (panel.AS.install, panel.backup_busy, panel.restart_soon, panel.job, panel.time.sleep)
    busy, restarted = [True, True], []
    def nap(_):   # a tile request during the wait
        os.makedirs(mapdir, exist_ok=True)
        open(j(mapdir, "r.0.0.png"), "w").close()
    panel.AS.install = lambda *a, **k: None
    panel.backup_busy = lambda: bool(busy) and busy.pop()
    panel.time.sleep, panel.job = nap, lambda name, fn: fn()
    panel.restart_soon = lambda: restarted.append(os.path.exists(j(mapdir, "r.0.0.png")))
    try:
        panel.download_assets()
    finally:
        panel.AS.install, panel.backup_busy, panel.restart_soon, panel.job, panel.time.sleep = real
    check("assets: waited, then map cache emptied, then restart", restarted == [False] and not busy, (restarted, busy))

    # Minecraft 26.3 with the player in a team with prefix and suffix: "list" gives the display name, "list uuids" the name
    check("list uuids: real name", panel.list_names(
        "There are 2 of a max of 5 players online: BotTest (6d677ae7-3bc4-396f-872a-777fca8f354d), Al_1 (0f1e2d3c4b5a69788796a5b4c3d2e1f0)")
        == ["Al_1", "BotTest"])

    def run_sampler(answers):   # one round of the sampler with a fake ping and fake RCON answers; the commands sent
        real, calls = (panel.ping, panel.RCON.command), []
        panel.ping = lambda port: (0, 10, [])   # hide-online-players: no names (some servers no count either)
        panel.RCON.command = lambda text, timeout=5: calls.append(text) or answers.get(text, "Unknown or incomplete command")
        panel.SAMPLE_SECONDS, sleep = 0, panel.time.sleep
        def stop(_):
            raise SystemExit
        panel.time.sleep = stop
        try:
            with contextlib.suppress(SystemExit):
                panel.sampler()
        finally:
            panel.ping, panel.RCON.command = real
            panel.time.sleep = sleep
        return calls
    calls = run_sampler({"list uuids": "There are 1 of a max of 10 players online: Alice (6d677ae7-3bc4-396f-872a-777fca8f354d)"})
    check("sampler: hidden players found by list uuids", panel.online_names() == {"Alice"}, panel.online_names())
    check("sampler: count from list, so not 'empty'", panel.SRV.info[0] == 1 and panel.SRV.empty_since is None, panel.SRV.info)
    check("sampler: list uuids asked once, no plain list", calls.count("list uuids") == 1 and "list" not in calls, calls)
    calls = run_sampler({"list": "There are 1 out of maximum 10 players online.\nDefault: Bob"})
    check("sampler: plugin list without uuids -> plain list", panel.online_names() == {"Bob"} and "list" in calls, calls)

    # "Start at login" on Windows writes the Startup script (here a temp path, never the real Startup folder)
    panel.STARTUP_CMD = j(root, "ByzzCraft.cmd")
    if os.name == "nt":
        with contextlib.redirect_stdout(io.StringIO()):
            done = panel.fix_health("service", "admin")
        check("start at login: Startup script", done and "panel.py" in open(panel.STARTUP_CMD, encoding="utf-8").read())
    else:
        check("start at login: no button on Linux (needs sudo)", not panel.fix_health("service", "admin") and not os.path.exists(panel.STARTUP_CMD))
finally:
    shutil.rmtree(root, ignore_errors=True)

print("PASS %d  FAIL %d" % (OK, FAIL))
sys.exit(1 if FAIL else 0)
