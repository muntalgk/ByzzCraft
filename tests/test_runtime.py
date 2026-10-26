import io, json, os, shutil, sys, tarfile, tempfile, zipfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))  # panel folder
from byzzcraft import runtime as RT

OK = FAIL = 0
def check(name, cond, extra=""):
    global OK, FAIL
    OK, FAIL = (OK + 1, FAIL) if cond else (OK, FAIL + 1)
    if not cond:
        print("FAIL:", name, extra)

# java -version of several vendors (printed on stderr)
check("openjdk 25", RT.parse_version('openjdk version "25.0.1" 2025-10-21 LTS') == 25)
check("oracle 8", RT.parse_version('java version "1.8.0_401"') == 8)
check("temurin 21", RT.parse_version('openjdk version "21" 2023-09-19') == 21)
check("nothing", RT.parse_version("bash: java: command not found") is None)

d = tempfile.mkdtemp(prefix="rt_test_")
try:
    # version server.jar asks for
    with zipfile.ZipFile(os.path.join(d, "server.jar"), "w") as z:
        z.writestr("version.json", json.dumps({"id": "26.3", "java_version": 25}))
    check("required", RT.required(d) == 25)
    check("required without jar", RT.required(os.path.join(d, "nope")) is None)

    # java downloaded by the panel: runtime/java-25/<folder>/bin/java
    RT.RUNTIME = os.path.join(d, "runtime")
    exe = os.path.join(RT.RUNTIME, "java-25", "jdk-25.0.1+8-jre", "bin", RT.EXE)
    os.makedirs(os.path.dirname(exe))
    open(exe, "w").close()
    check("bundled found", RT.bundled(25) == exe, RT.bundled(25))
    check("bundled other version", RT.bundled(21) is None)
    check("java_for prefers the downloaded one", RT.java_for(25) == (exe, 25))

    # extraction: a normal archive passes, one that leaves the folder does not
    def tgz(name, members):
        p = os.path.join(d, name)
        with tarfile.open(p, "w:gz") as tf:
            for n, link in members:
                ti = tarfile.TarInfo(n)
                if link:
                    ti.type, ti.linkname = tarfile.SYMTYPE, link
                    tf.addfile(ti)
                else:
                    ti.size = 2
                    tf.addfile(ti, io.BytesIO(b"ok"))
        return p
    out = os.path.join(d, "x1")
    RT._extract(tgz("good.tar.gz", [("jdk/bin/java", None), ("jdk/legal/x", None)]), out)
    check("tar normale", os.path.isfile(os.path.join(out, "jdk", "bin", "java")))
    # absolute path: the "data" filter (3.12, 3.11.4+) strips the / and keeps it inside; the manual check refuses it
    x3 = os.path.join(d, "x3")
    try:
        RT._extract(tgz("abs.tar.gz", [("/rt_evil_abs", None)]), x3)
        check("absolute tar stays inside", os.path.isfile(os.path.join(x3, "rt_evil_abs")))
    except (OSError, tarfile.TarError):
        check("absolute tar refused", True)
    for name, members in (("dots.tar.gz", [("../evil", None)]), ("link.tar.gz", [("jdk/lnk", "../../../etc/passwd")])):
        try:
            RT._extract(tgz(name, members), os.path.join(d, "x-" + name))
            check("dangerous tar refused: " + name, False)
        except (OSError, tarfile.TarError):
            check("dangerous tar refused: " + name, True)
    zp = os.path.join(d, "evil.zip")
    with zipfile.ZipFile(zp, "w") as z:
        z.writestr("../evil.txt", "x")
    try:
        RT._extract(zp, os.path.join(d, "x2"))
        check("dangerous zip refused", False)
    except OSError:
        check("dangerous zip refused", True)
    check("nothing outside the folder", not os.path.exists(os.path.join(d, "evil")) and not os.path.exists(os.path.join(d, "evil.txt")))
finally:
    shutil.rmtree(d, ignore_errors=True)
print("PASS %d  FAIL %d" % (OK, FAIL))
sys.exit(1 if FAIL else 0)
