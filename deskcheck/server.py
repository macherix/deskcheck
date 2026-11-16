"""DeskCheck MCP server: lets coding agents verify Linux desktop apps.

Speaks the Model Context Protocol over stdio (newline-delimited JSON-RPC 2.0).
Standard library only, plus the system's PyGObject/AT-SPI.
"""
import json
import sys
import traceback

from . import __version__, a11y, procs, shot
from .apps import Registry, error_lines

INSTRUCTIONS = (
    "DeskCheck verifies Linux desktop (GUI) apps. Typical loop after changing app code: "
    "app_launch -> app_ui (find widgets, get [ref]s) -> app_act (click/type) -> "
    "app_screenshot (see it) -> app_measure (CPU/RAM/GPU while idle or under use) -> "
    "app_logs (errors) -> app_stop. Prefer app_ui over screenshots: it costs far fewer tokens."
)

APP = {"type": "string", "description": "app id returned by app_launch/app_attach "
                                         "(may be omitted when only one app is tracked)"}

TOOLS = [
    {
        "name": "app_launch",
        "description": "Start a GUI app and wait until its window is up. Output (stdout+stderr) "
                       "is captured. Returns app id, pid, startup time and early errors.",
        "inputSchema": {"type": "object", "properties": {
            "command": {"type": "string", "description": "shell command, e.g. 'python3 main.py'"},
            "cwd": {"type": "string"},
            "env": {"type": "object", "additionalProperties": {"type": "string"}},
            "timeout": {"type": "number", "default": 15,
                        "description": "max seconds to wait for a window"},
        }, "required": ["command"]},
    },
    {
        "name": "app_attach",
        "description": "Track an app that is already running, by pid or exact process name "
                       "(never matches DeskCheck's own shell, unlike pgrep -f).",
        "inputSchema": {"type": "object", "properties": {
            "target": {"type": "string", "description": "pid or process name"},
        }, "required": ["target"]},
    },
    {
        "name": "app_ui",
        "description": "Compact outline of the app's visible UI from the accessibility tree: "
                       "role, name, text, value, states, actions. Each line has a [ref] for "
                       "app_act. Much cheaper than a screenshot.",
        "inputSchema": {"type": "object", "properties": {
            "app": APP,
            "find": {"type": "string", "description": "only nodes whose role/name/text "
                                                      "contains this (case-insensitive)"},
            "max_nodes": {"type": "integer", "default": 300},
            "include_hidden": {"type": "boolean", "default": False},
        }},
    },
    {
        "name": "app_act",
        "description": "Interact with a UI element by [ref] from app_ui: click (default action), "
                       "a named action, set_text for text fields, or focus.",
        "inputSchema": {"type": "object", "properties": {
            "app": APP,
            "ref": {"type": "string"},
            "action": {"type": "string", "default": "click",
                       "description": "click | set_text | focus | any action name from app_ui"},
            "text": {"type": "string", "description": "for set_text"},
        }, "required": ["ref"]},
    },
    {
        "name": "app_screenshot",
        "description": "Screenshot of the app's own window (KDE Plasma raises it first; "
                       "other desktops fall back to full screen). Downscaled to save tokens.",
        "inputSchema": {"type": "object", "properties": {
            "app": APP,
            "max_width": {"type": "integer", "default": 1280},
        }},
    },
    {
        "name": "app_measure",
        "description": "Measure the app's whole process tree for N seconds: CPU avg/max, "
                       "memory (PSS) and growth, threads, disk I/O, NVIDIA GPU/VRAM, plus "
                       "plain-language verdicts (busy-loop, leak, continuous redraw).",
        "inputSchema": {"type": "object", "properties": {
            "app": APP,
            "seconds": {"type": "number", "default": 5},
        }},
    },
    {
        "name": "app_logs",
        "description": "Captured output of a launched app: error-looking lines by default, "
                       "or the last N lines.",
        "inputSchema": {"type": "object", "properties": {
            "app": APP,
            "lines": {"type": "integer", "default": 40},
            "errors_only": {"type": "boolean", "default": True},
        }},
    },
    {
        "name": "app_stop",
        "description": "Stop a launched app (SIGTERM, then SIGKILL) and report its exit code.",
        "inputSchema": {"type": "object", "properties": {"app": APP}},
    },
    {
        "name": "app_list",
        "description": "Apps currently tracked by DeskCheck.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]

registry = Registry()


def text(s):
    return {"content": [{"type": "text", "text": s}]}


def need_alive(app):
    if not app.alive():
        tail = "\n".join(app.log_lines()[-20:])
        raise ValueError(f"{app.id} is not running (exit code {app.exit_code()}).\n"
                         f"Last output:\n{tail}")


def call(name, a):
    if name == "app_launch":
        app, acc, window, startup = registry.launch(a["command"], a.get("cwd"), a.get("env"),
                                                    float(a.get("timeout", 15)))
        lines = [f"app: {app.id}  pid: {app.pid}"]
        if not app.alive():
            lines.append(f"EXITED during startup with code {app.exit_code()} after {startup:.1f}s")
            lines += ["Last output:"] + app.log_lines()[-25:]
            return text("\n".join(lines))
        if window:
            lines.append(f"window ready after {startup:.1f}s")
        elif acc:
            lines.append(f"accessible after {startup:.1f}s but no window seen yet")
        else:
            lines.append(f"no accessible window after {startup:.1f}s (toolkit without AT-SPI, "
                         "or still loading). Screenshots and measurements still work.")
        errs = error_lines(app.log_lines())
        if errs:
            lines.append("early errors/warnings:")
            lines += [f"  {e}" for e in errs]
        return text("\n".join(lines))

    if name == "app_attach":
        app = registry.attach(a["target"])
        st = procs.stat(app.pid)
        return text(f"app: {app.id}  pid: {app.pid}  process: {st[0] if st else '?'}  "
                    f"(output not captured for attached apps)")

    if name == "app_list":
        if not registry.apps:
            return text("no apps tracked")
        rows = []
        for app in registry.apps.values():
            state = "running" if app.alive() else f"exited ({app.exit_code()})"
            rows.append(f"{app.id}  pid {app.pid}  {state}  {app.cmd or '(attached)'}")
        return text("\n".join(rows))

    app = registry.get(a.get("app"))

    if name == "app_logs":
        lines = app.log_lines()
        if app.log is None:
            return text("attached app: output is not captured")
        n = int(a.get("lines", 40))
        sel = error_lines(lines, n) if a.get("errors_only", True) else lines[-n:]
        head = f"{len(lines)} lines total, showing {len(sel)}" + (
            " error-looking lines" if a.get("errors_only", True) else "")
        return text(head + ("\n" + "\n".join(sel) if sel else "\n(none)"))

    if name == "app_stop":
        code = registry.stop(app)
        errs = error_lines(app.log_lines())
        msg = f"{app.id} stopped, exit code {code}"
        if errs:
            msg += "\nerror-looking lines in its output:\n" + "\n".join(errs)
        return text(msg)

    need_alive(app)

    if name == "app_ui":
        acc = a11y.find_app(app.pids())
        if acc is None:
            return text("app is not on the accessibility bus. GTK/Qt apps launched by "
                        "app_launch are; for Electron pass --force-renderer-accessibility; "
                        "games/SDL/raw OpenGL apps have no UI tree, use app_screenshot.")
        out, app.refs = a11y.dump(acc, int(a.get("max_nodes", 300)),
                                  not a.get("include_hidden"), a.get("find"))
        return text(out or "(no matching visible elements)")

    if name == "app_act":
        acc = a11y.find_app(app.pids())
        if acc is None:
            raise ValueError("app is not on the accessibility bus")
        ref = str(a["ref"]).strip("[]")
        path = getattr(app, "refs", {}).get(ref, ref)
        if path.startswith("e"):
            raise ValueError(f"unknown ref {ref}: call app_ui again (refs reset on every dump)")
        return text(a11y.act(acc, path, a.get("action", "click"), a.get("text")))

    if name == "app_screenshot":
        data, note = shot.capture(app.pids(), int(a.get("max_width", 1280)))
        return {"content": [{"type": "image", "data": data, "mimeType": "image/png"},
                            {"type": "text", "text": f"{app.id}: {note}"}]}

    if name == "app_measure":
        m = procs.measure(app.pid, float(a.get("seconds", 5)))
        fb = procs.fmt_bytes
        lines = [
            f"{app.id} over {m['seconds']:.0f}s — {m['processes']} processes, {m['threads']} threads",
            f"CPU: avg {m['cpu_avg']:.1f}%  max {m['cpu_max']:.1f}%  (100% = one core, "
            f"{m['cpu_cores']} available)",
            f"memory (PSS): {fb(m['mem_now'])}  change {'+' if m['mem_growth'] >= 0 else ''}"
            f"{fb(m['mem_growth'])}",
            f"disk I/O: {fb(m['io_rate'])}/s",
        ]
        if m["gpu"]:
            lines.append(f"GPU: {m['gpu'][0]:.0f}%  VRAM {fb(m['gpu'][1])}")
        lines.append("verdict: " + " ".join(procs.verdicts(m)))
        return text("\n".join(lines))

    raise ValueError(f"unknown tool {name}")


def handle(msg):
    method = msg.get("method")
    mid = msg.get("id")
    if mid is None:  # notification
        return None
    if method == "initialize":
        ver = msg.get("params", {}).get("protocolVersion", "2025-06-18")
        result = {"protocolVersion": ver, "capabilities": {"tools": {}},
                  "serverInfo": {"name": "deskcheck", "version": __version__},
                  "instructions": INSTRUCTIONS}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        p = msg.get("params", {})
        try:
            result = call(p.get("name"), p.get("arguments") or {})
        except Exception as e:  # tool errors are reported to the agent, not as protocol errors
            if not isinstance(e, (ValueError, RuntimeError)):
                traceback.print_exc(file=sys.stderr)
            result = {"content": [{"type": "text", "text": f"Error: {e}"}], "isError": True}
    else:
        return {"jsonrpc": "2.0", "id": mid,
                "error": {"code": -32601, "message": f"method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        reply = handle(msg)
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()
    for app in list(registry.apps.values()):
        if app.popen is not None and app.alive():
            try:
                registry.stop(app)
            except Exception:
                pass


if __name__ == "__main__":
    main()
