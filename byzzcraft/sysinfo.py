"""Server process, CPU and RAM: Windows API or /proc on Linux (standard library only)."""
import os
import socket
import struct
import sys
import time

NCPU = os.cpu_count() or 1

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    iphl = ctypes.WinDLL("iphlpapi")


    class FILETIME(ctypes.Structure):
        _fields_ = [("lo", wintypes.DWORD), ("hi", wintypes.DWORD)]

        def val(self):
            return (self.hi << 32) | self.lo


    class PMC(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [(n, ctypes.c_size_t) for n in (
            "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
            "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage", "PrivateUsage")]


    class MEMSTAT(ctypes.Structure):
        _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD)] + [(n, ctypes.c_ulonglong) for n in (
            "ullTotalPhys", "ullAvailPhys", "ullTotalPageFile", "ullAvailPageFile", "ullTotalVirtual", "ullAvailVirtual", "ullAvailExtendedVirtual")]


    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(FILETIME)] * 4
    k32.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]


    def listening_pid(port):
        """PID of the process listening on a TCP port (IPv4 or IPv6)."""
        for af, rowsize in ((2, 24), (23, 56)):
            size = wintypes.DWORD(0)
            iphl.GetExtendedTcpTable(None, ctypes.byref(size), False, af, 3, 0)
            buf = ctypes.create_string_buffer(size.value + 512)
            size = wintypes.DWORD(len(buf))
            if iphl.GetExtendedTcpTable(buf, ctypes.byref(size), False, af, 3, 0) != 0:
                continue
            raw = buf.raw
            (n,) = struct.unpack_from("<I", raw, 0)
            for i in range(n):
                off = 4 + i * rowsize
                if af == 2:
                    _state, _la, lport, _ra, _rp, pid = struct.unpack_from("<6I", raw, off)
                else:
                    lport, = struct.unpack_from("<I", raw, off + 20)
                    pid, = struct.unpack_from("<I", raw, off + 52)
                if socket.ntohs(lport & 0xFFFF) == port:
                    return pid
        return None


    class ProcSampler:
        def __init__(self):
            self.pid = None; self.last = None

        def sample(self, pid):
            """(cpu % of the PC, working set MB, private MB, started_epoch) or None."""
            if pid is None:
                self.pid = None; self.last = None
                return None
            h = k32.OpenProcess(0x1000 | 0x0010, False, pid)  # QUERY_LIMITED_INFORMATION | VM_READ
            if not h:
                return None
            try:
                c, e, kt, ut = FILETIME(), FILETIME(), FILETIME(), FILETIME()
                k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(kt), ctypes.byref(ut))
                pmc = PMC(); pmc.cb = ctypes.sizeof(PMC)
                k32.K32GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb)
            finally:
                k32.CloseHandle(h)
            busy, now = kt.val() + ut.val(), time.time()
            cpu = None
            if self.pid == pid and self.last:
                dt = now - self.last[1]
                if dt > 0:
                    cpu = max(0.0, (busy - self.last[0]) / 1e7 / dt / NCPU * 100)
            self.pid, self.last = pid, (busy, now)
            started = c.val() / 1e7 - 11644473600
            return cpu, pmc.WorkingSetSize / 1048576, pmc.PrivateUsage / 1048576, started


    def pid_alive(pid):
        if not pid:
            return False
        h = k32.OpenProcess(0x1000, False, pid)
        if not h:
            return False
        try:
            code = wintypes.DWORD()
            k32.GetExitCodeProcess(h, ctypes.byref(code))
            return code.value == 259  # STILL_ACTIVE
        finally:
            k32.CloseHandle(h)


    def system_times():
        """(idle, total) in 100 ns units of all CPUs: the PC CPU is 1 - d(idle)/d(total)."""
        idle, kern, user = FILETIME(), FILETIME(), FILETIME()
        k32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kern), ctypes.byref(user))
        return idle.val(), kern.val() + user.val()   # kernel time already includes idle time


    def system_memory():
        m = MEMSTAT(); m.dwLength = ctypes.sizeof(MEMSTAT)
        k32.GlobalMemoryStatusEx(ctypes.byref(m))
        return m.ullAvailPhys / 1048576, m.ullTotalPhys / 1048576

else:
    _TCK = os.sysconf("SC_CLK_TCK")
    _PAGE_KB = os.sysconf("SC_PAGE_SIZE") // 1024

    def _btime():
        with open("/proc/stat") as f:
            return next(int(l.split()[1]) for l in f if l.startswith("btime"))

    _BOOT = _btime()

    def _stat(pid):
        """Fields of /proc/<pid>/stat after the process name (which can contain spaces)."""
        with open("/proc/%d/stat" % pid) as f:
            return f.read().rsplit(")", 1)[1].split()

    def listening_pid(port):
        """PID of the process listening on a TCP port (IPv4 or IPv6)."""
        inodes = set()
        for name in ("/proc/net/tcp", "/proc/net/tcp6"):
            try:
                with open(name) as f:
                    next(f)
                    for line in f:
                        c = line.split()
                        if c[3] == "0A" and int(c[1].rsplit(":", 1)[1], 16) == port:   # 0A = LISTEN
                            inodes.add("socket:[%s]" % c[9])
            except OSError:
                pass
        if not inodes:
            return None
        for pid in filter(str.isdigit, os.listdir("/proc")):
            try:
                fds = os.listdir("/proc/%s/fd" % pid)
            except OSError:
                continue   # process of another user, or already gone
            for fd in fds:
                try:
                    if os.readlink("/proc/%s/fd/%s" % (pid, fd)) in inodes:
                        return int(pid)
                except OSError:
                    pass
        return None

    class ProcSampler:
        def __init__(self):
            self.pid = None; self.last = None

        def sample(self, pid):
            """(cpu % of the PC, RSS MB, anonymous memory MB, started_epoch) or None."""
            if pid is None:
                self.pid = None; self.last = None
                return None
            try:
                st = _stat(pid)
                with open("/proc/%d/status" % pid) as f:
                    kb = {l.split(":")[0]: int(l.split()[1]) for l in f if l.startswith(("VmRSS", "RssAnon"))}
            except (OSError, ValueError):
                return None
            busy, now = (int(st[11]) + int(st[12])) / _TCK, time.time()   # utime + stime
            cpu = None
            if self.pid == pid and self.last:
                dt = now - self.last[1]
                if dt > 0:
                    cpu = max(0.0, (busy - self.last[0]) / dt / NCPU * 100)
            self.pid, self.last = pid, (busy, now)
            rss = kb.get("VmRSS", int(st[21]) * _PAGE_KB)
            return cpu, rss / 1024, kb.get("RssAnon", rss) / 1024, _BOOT + int(st[19]) / _TCK

    def pid_alive(pid):
        if not pid:
            return False
        try:
            return _stat(pid)[0] != "Z"   # a zombie has already finished
        except OSError:
            return False

    def system_times():
        """(idle, total) in ticks of all CPUs: the PC CPU is 1 - d(idle)/d(total)."""
        with open("/proc/stat") as f:
            v = [int(x) for x in f.readline().split()[1:9]]
        return v[3] + v[4], sum(v)   # idle + iowait

    def system_memory():
        with open("/proc/meminfo") as f:
            kb = {l.split(":")[0]: int(l.split()[1]) for l in f}
        return kb["MemAvailable"] / 1024, kb["MemTotal"] / 1024
