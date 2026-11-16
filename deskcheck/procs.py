"""Process utilities that read /proc directly (no psutil dependency)."""
import os
import shutil
import subprocess
import time

CLK = os.sysconf("SC_CLK_TCK")
PAGE = os.sysconf("SC_PAGE_SIZE")


def _read(path, default=None):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return default


def stat(pid):
    """(name, ppid, cpu_ticks, threads) or None if the process is gone."""
    s = _read(f"/proc/{pid}/stat")
    if not s:
        return None
    lp, rp = s.find("("), s.rfind(")")
    name = s[lp + 1:rp]
    f = s[rp + 2:].split()
    # f[0]=state f[1]=ppid ... f[11]=utime f[12]=stime f[17]=num_threads
    return name, int(f[1]), int(f[11]) + int(f[12]), int(f[17])


def all_pids():
    return [int(p) for p in os.listdir("/proc") if p.isdigit()]


def ppid_map():
    m = {}
    for pid in all_pids():
        st = stat(pid)
        if st:
            m[pid] = st[1]
    return m


def tree(root):
    """root and all of its descendants."""
    children = {}
    for pid, ppid in ppid_map().items():
        children.setdefault(ppid, []).append(pid)
    out, todo = [], [root]
    while todo:
        p = todo.pop()
        if os.path.exists(f"/proc/{p}"):
            out.append(p)
            todo.extend(children.get(p, []))
    return out


def ancestors(pid):
    out = set()
    m = ppid_map()
    while pid and pid not in out:
        out.add(pid)
        pid = m.get(pid, 0)
    return out


SHELLS = {"bash", "sh", "zsh", "fish", "dash", "pgrep", "timeout", "sudo", "pkexec"}


def find_by_name(name):
    """PIDs whose process name or executable basename equals `name`.

    Excludes this server's own ancestry and shells, so a search for "python3"
    never matches the command that is doing the searching (a classic pgrep -f trap).
    """
    mine = ancestors(os.getpid())
    hits = []
    for pid in all_pids():
        if pid in mine:
            continue
        st = stat(pid)
        if not st or st[0] in SHELLS:
            continue
        exe = os.path.basename(os.readlink(f"/proc/{pid}/exe")) if os.access(
            f"/proc/{pid}/exe", os.R_OK) else ""
        cmd = (_read(f"/proc/{pid}/cmdline", "") or "").split("\0")
        script = os.path.basename(cmd[1]) if len(cmd) > 1 and cmd[1] else ""
        if name in (st[0], exe, script, script.rsplit(".", 1)[0]):
            hits.append(pid)
    # keep only the top-most matches (drop children of other matches)
    m = ppid_map()
    return [p for p in hits if m.get(p) not in hits]


def mem(pid):
    """Proportional set size (shared pages split between users), falling back to RSS."""
    s = _read(f"/proc/{pid}/smaps_rollup")
    if s:
        for line in s.splitlines():
            if line.startswith("Pss:"):
                return int(line.split()[1]) * 1024
    s = _read(f"/proc/{pid}/statm")
    return int(s.split()[1]) * PAGE if s else 0


def io(pid):
    s = _read(f"/proc/{pid}/io")
    if not s:
        return None
    d = dict(line.split(": ") for line in s.splitlines() if ": " in line)
    return int(d.get("read_bytes", 0)) + int(d.get("write_bytes", 0))


def nvidia_usage(pids):
    """(gpu %, vram bytes) summed over pids, or None without an NVIDIA GPU."""
    if not shutil.which("nvidia-smi"):
        return None
    try:
        out = subprocess.run(["nvidia-smi", "pmon", "-c", "1", "-s", "um"],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    header, util, vram = None, 0.0, 0.0
    for line in out.splitlines():
        if line.startswith("#"):
            header = header or line[1:].split()
            continue
        row = dict(zip(header or [], line.split()))
        try:
            if int(row.get("pid", -1)) not in pids:
                continue
        except ValueError:
            continue
        for key, scale, acc in (("sm", 1, "u"), ("fb", 1024 ** 2, "v")):
            try:
                v = float(row.get(key, "-")) * scale
            except ValueError:
                continue
            if acc == "u":
                util += v
            else:
                vram += v
    return util, vram


def fmt_bytes(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def measure(root, seconds=5.0, interval=0.5):
    """Sample a process tree and return a dict of resource usage."""
    ncpu = os.cpu_count() or 1
    prev = {}
    io_prev = {}
    cpu_samples, mem_samples = [], []
    io_total = 0
    gpu = None
    t_end = time.monotonic() + seconds
    t_last = time.monotonic()
    first = True
    while True:
        pids = tree(root)
        if not pids:
            break
        now = time.monotonic()
        ticks = {}
        threads = 0
        for p in pids:
            st = stat(p)
            if st:
                ticks[p] = st[2]
                threads += st[3]
            b = io(p)
            if b is not None:
                if p in io_prev and not first:
                    io_total += max(b - io_prev[p], 0)
                io_prev[p] = b
        if not first:
            dt = max(now - t_last, 1e-3)
            used = sum(max(t - prev.get(p, t), 0) for p, t in ticks.items())
            cpu_samples.append(used / CLK / dt * 100)
        mem_samples.append(sum(mem(p) for p in pids))
        if gpu is None and now > t_end - seconds / 2:
            gpu = nvidia_usage(set(pids)) or False
        prev, t_last, first = ticks, now, False
        if now >= t_end:
            break
        time.sleep(interval)
    alive = bool(tree(root))
    res = {
        "alive": alive,
        "processes": len(tree(root)) if alive else 0,
        "threads": threads if alive else 0,
        "cpu_avg": sum(cpu_samples) / len(cpu_samples) if cpu_samples else 0.0,
        "cpu_max": max(cpu_samples) if cpu_samples else 0.0,
        "cpu_cores": ncpu,
        "mem_now": mem_samples[-1] if mem_samples else 0,
        "mem_growth": (mem_samples[-1] - mem_samples[0]) if len(mem_samples) > 1 else 0,
        "io_rate": io_total / seconds if seconds else 0,
        "gpu": gpu or None,
        "seconds": seconds,
    }
    return res


def verdicts(m):
    """Plain-language hints an agent can act on."""
    out = []
    if not m["alive"]:
        out.append("Process exited during the measurement.")
        return out
    if m["cpu_avg"] >= 5:
        out.append(f"Uses {m['cpu_avg']:.1f}% of one core on average: if the app should be "
                   "idle, something is polling, busy-looping or redrawing continuously.")
    if m["cpu_max"] >= 90:
        out.append("Spikes to a full core: check for blocking work on the UI thread.")
    if m["mem_growth"] > 20 * 1024 ** 2:
        out.append(f"Memory grew by {fmt_bytes(m['mem_growth'])} in {m['seconds']:.0f}s: "
                   "possible leak or unbounded cache.")
    if m["gpu"] and m["gpu"][0] >= 10:
        out.append(f"GPU at {m['gpu'][0]:.0f}%: continuous rendering or animation.")
    if m["io_rate"] > 1024 ** 2:
        out.append(f"Disk I/O {fmt_bytes(m['io_rate'])}/s: frequent writes or reads.")
    if not out:
        out.append("No resource problems detected.")
    return out
