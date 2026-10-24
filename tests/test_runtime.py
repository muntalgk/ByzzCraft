import io, json, os, shutil, sys, tarfile, tempfile, zipfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))  # cartella pannello
import runtime as RT

OK = FAIL = 0
def check(name, cond, extra=""):
    global OK, FAIL
    OK, FAIL = (OK + 1, FAIL) if cond else (OK, FAIL + 1)
    if not cond:
        print("FAIL:", name, extra)

# java -version di vari produttori (esce su stderr)
check("openjdk 25", RT.parse_version('openjdk version "25.0.1" 2025-10-21 LTS') == 25)
check("oracle 8", RT.parse_version('java version "1.8.0_401"') == 8)
check("temurin 21", RT.parse_version('openjdk version "21" 2023-09-19') == 21)
check("niente", RT.parse_version("bash: java: command not found") is None)

d = tempfile.mkdtemp(prefix="rt_test_")
try:
    # versione chiesta da server.jar
    with zipfile.ZipFile(os.path.join(d, "server.jar"), "w") as z:
        z.writestr("version.json", json.dumps({"id": "26.3", "java_version": 25}))
    check("required", RT.required(d) == 25)
    check("required senza jar", RT.required(os.path.join(d, "nope")) is None)

    # java scaricato dal pannello: runtime/java-25/<cartella>/bin/java
    RT.RUNTIME = os.path.join(d, "runtime")
    exe = os.path.join(RT.RUNTIME, "java-25", "jdk-25.0.1+8-jre", "bin", RT.EXE)
    os.makedirs(os.path.dirname(exe))
    open(exe, "w").close()
    check("bundled trovato", RT.bundled(25) == exe, RT.bundled(25))
    check("bundled altra versione", RT.bundled(21) is None)
    check("java_for preferisce quello scaricato", RT.java_for(25) == (exe, 25))

    # estrazione: un archivio normale passa, uno che esce dalla cartella no
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
    # percorso assoluto: il filtro "data" (3.12, 3.11.4+) toglie la / e lo tiene dentro; il controllo a mano lo rifiuta
    x3 = os.path.join(d, "x3")
    try:
        RT._extract(tgz("abs.tar.gz", [("/rt_evil_abs", None)]), x3)
        check("tar assoluto resta dentro", os.path.isfile(os.path.join(x3, "rt_evil_abs")))
    except (OSError, tarfile.TarError):
        check("tar assoluto rifiutato", True)
    for name, members in (("dots.tar.gz", [("../evil", None)]), ("link.tar.gz", [("jdk/lnk", "../../../etc/passwd")])):
        try:
            RT._extract(tgz(name, members), os.path.join(d, "x-" + name))
            check("tar pericoloso rifiutato: " + name, False)
        except (OSError, tarfile.TarError):
            check("tar pericoloso rifiutato: " + name, True)
    zp = os.path.join(d, "evil.zip")
    with zipfile.ZipFile(zp, "w") as z:
        z.writestr("../evil.txt", "x")
    try:
        RT._extract(zp, os.path.join(d, "x2"))
        check("zip pericoloso rifiutato", False)
    except OSError:
        check("zip pericoloso rifiutato", True)
    check("niente fuori dalla cartella", not os.path.exists(os.path.join(d, "evil")) and not os.path.exists(os.path.join(d, "evil.txt")))
finally:
    shutil.rmtree(d, ignore_errors=True)
print("PASS %d  FAIL %d" % (OK, FAIL))
sys.exit(1 if FAIL else 0)
