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

    # start script: run directly only with a shebang (an executable without one gives ENOEXEC)
    for name, body in (("bash.sh", "#!/bin/bash\n"), ("plain.sh", "java -jar server.jar\n")):
        p = j(srv_dir, name)
        with open(p, "w") as f:
            f.write(body)
        os.chmod(p, 0o755)
        check("has_shebang %s" % name, panel.has_shebang(p) == body.startswith("#!"))
    check("has_shebang missing file", not panel.has_shebang(j(srv_dir, "nope.sh")))

    # spaces in the install path (systemd unit)
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
finally:
    shutil.rmtree(root, ignore_errors=True)

print("PASS %d  FAIL %d" % (OK, FAIL))
sys.exit(1 if FAIL else 0)
