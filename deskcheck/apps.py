"""Launch, track, inspect and stop the applications under test."""
import os
import re
import signal
import subprocess
import tempfile
import time

from . import a11y, procs

ERROR_RE = re.compile(
    r"Traceback|Error|ERROR|CRITICAL|FATAL|Exception|panic|Segmentation fault|"
    r"core dumped|Gtk-WARNING|Qt.*[Ww]arning|undefined|not found|failed", re.I)

ACCESSIBLE_ENV = {
    "QT_LINUX_ACCESSIBILITY_ALWAYS_ON": "1",   # Qt only exposes AT-SPI on request
    "QT_ACCESSIBILITY": "1",
    "GTK_A11Y": "atspi",
    "NO_AT_BRIDGE": "0",
    "ACCESSIBILITY_ENABLED": "1",
    "PYTHONUNBUFFERED": "1",                   # see Python app output immediately
}


class App:
    def __init__(self, app_id, pid, cmd=None, popen=None, log=None):
        self.id = app_id
        self.pid = pid
        self.cmd = cmd
        self.popen = popen
        self.log = log
        self.started = time.time()

    def pids(self):
        return set(procs.tree(self.pid))

    def alive(self):
        if self.popen is not None:
            return self.popen.poll() is None or bool(procs.tree(self.pid)[1:])
        return os.path.exists(f"/proc/{self.pid}")

    def exit_code(self):
        return self.popen.poll() if self.popen is not None else None

    def log_lines(self):
        if not self.log or not os.path.exists(self.log):
            return []
        with open(self.log, errors="replace") as f:
            return f.read().splitlines()


class Registry:
    def __init__(self):
        self.apps = {}
        self._n = 0

    def _new_id(self, base):
        self._n += 1
        slug = re.sub(r"[^a-z0-9]+", "-", os.path.basename(base).lower()).strip("-")[:20]
        return f"{slug or 'app'}-{self._n}"

    def launch(self, command, cwd=None, env=None, timeout=15.0):
        log = tempfile.mktemp(prefix="deskcheck_", suffix=".log")
        full_env = dict(os.environ, **ACCESSIBLE_ENV, **(env or {}))
        t0 = time.monotonic()
        with open(log, "w") as lf:
            p = subprocess.Popen(["/bin/sh", "-c", f"exec {command}"], cwd=cwd or None,
                                 env=full_env, stdout=lf, stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL, start_new_session=True)
        words = [w for w in command.split() if not w.startswith("-")] or ["app"]
        first = words[0]
        if os.path.basename(first) in ("python", "python3", "node", "bash", "sh", "java",
                                        "ruby", "perl", "electron", "uv", "npx") and len(words) > 1:
            first = os.path.splitext(os.path.basename(words[1]))[0]
        app = App(self._new_id(first), p.pid, command, p, log)
        self.apps[app.id] = app

        a11y_found = window = False
        while time.monotonic() - t0 < timeout:
            if not app.alive():
                break
            acc = a11y.find_app(app.pids())
            if acc is not None:
                a11y_found = True
                if a11y.has_window(acc):
                    window = True
                    break
            time.sleep(0.25)
        startup = time.monotonic() - t0
        return app, a11y_found, window, startup

    def attach(self, target):
        """Track an already-running process by pid or name."""
        target = str(target).strip()
        if target.isdigit():
            pid = int(target)
        else:
            hits = procs.find_by_name(target)
            if not hits:
                raise ValueError(f"no running process named {target!r}")
            if len(hits) > 1:
                desc = ", ".join(f"{h} ({procs.stat(h)[0]})" for h in hits[:8])
                raise ValueError(f"{len(hits)} processes match {target!r}: {desc}. "
                                 "Attach by pid instead.")
            pid = hits[0]
        if not os.path.exists(f"/proc/{pid}"):
            raise ValueError(f"no process with pid {pid}")
        app = App(self._new_id(procs.stat(pid)[0]), pid)
        self.apps[app.id] = app
        return app

    def get(self, app_id):
        if app_id in self.apps:
            return self.apps[app_id]
        if len(self.apps) == 1 and not app_id:
            return next(iter(self.apps.values()))
        known = ", ".join(self.apps) or "none"
        raise ValueError(f"unknown app {app_id!r} (known: {known}). "
                         "Use app_launch or app_attach first.")

    def stop(self, app):
        if app.popen is None:
            raise ValueError("this app was attached, not launched: DeskCheck won't kill it")
        try:
            os.killpg(app.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        for _ in range(30):
            if app.popen.poll() is not None and not procs.tree(app.pid)[1:]:
                break
            time.sleep(0.1)
        else:
            try:
                os.killpg(app.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        code = app.popen.wait(timeout=5)
        self.apps.pop(app.id, None)
        return code


def error_lines(lines, limit=15):
    hits = [ln for ln in lines if ERROR_RE.search(ln)]
    return hits[-limit:]
