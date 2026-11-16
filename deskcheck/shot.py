"""Screenshot of one application's window.

KDE Plasma 6 (Wayland/X11): a short KWin script raises the window that belongs to
the app's processes, then Spectacle captures the active window.
Other desktops: full-screen capture with grim (wlroots), gnome-screenshot or import.
"""
import base64
import os
import shutil
import subprocess
import tempfile
import time
import uuid

KWIN_JS = """
const pids = %s;
for (const w of workspace.windowList()) {
    if (pids.includes(w.pid) && w.normalWindow) {
        if (w.minimized) w.minimized = false;
        workspace.activeWindow = w;
        break;
    }
}
"""


def _gdbus(*args):
    return subprocess.run(["gdbus", "call", "--session", *args],
                          capture_output=True, text=True, timeout=5)


def is_kde():
    return "KDE" in os.environ.get("XDG_CURRENT_DESKTOP", "") and shutil.which("spectacle")


def raise_window_kde(pids):
    name = f"deskcheck_{uuid.uuid4().hex[:8]}"
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(KWIN_JS % sorted(pids))
        path = f.name
    try:
        r = _gdbus("--dest", "org.kde.KWin", "--object-path", "/Scripting",
                   "--method", "org.kde.kwin.Scripting.loadScript", path, name)
        sid = "".join(c for c in r.stdout.split(",")[0] if c.isdigit() or c == "-")
        if not sid or sid.startswith("-"):
            return False
        _gdbus("--dest", "org.kde.KWin", "--object-path", f"/Scripting/Script{sid}",
               "--method", "org.kde.kwin.Script.run")
        time.sleep(0.35)
        _gdbus("--dest", "org.kde.KWin", "--object-path", "/Scripting",
               "--method", "org.kde.kwin.Scripting.unloadScript", name)
        return True
    finally:
        os.unlink(path)


def _scale(path, max_width):
    try:
        import gi
        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf
        pb = GdkPixbuf.Pixbuf.new_from_file(path)
        w, h = pb.get_width(), pb.get_height()
        if w > max_width:
            pb = pb.scale_simple(max_width, round(h * max_width / w),
                                 GdkPixbuf.InterpType.BILINEAR)
            pb.savev(path, "png", [], [])
        return pb.get_width(), pb.get_height()
    except Exception:
        return None


def capture(pids, max_width=1280):
    """Return (png_base64, note)."""
    out = tempfile.mktemp(suffix=".png", prefix="deskcheck_")
    note = ""
    try:
        if is_kde():
            raised = raise_window_kde(pids)
            mode = "-a" if raised else "-f"
            note = "window" if raised else "full screen (window not found by KWin)"
            subprocess.run(["spectacle", "-b", "-n", mode, "-o", out],
                           capture_output=True, timeout=15)
        elif shutil.which("grim") and os.environ.get("WAYLAND_DISPLAY"):
            subprocess.run(["grim", out], capture_output=True, timeout=15)
            note = "full screen (grim)"
        elif shutil.which("gnome-screenshot"):
            subprocess.run(["gnome-screenshot", "-w", "-f", out], capture_output=True, timeout=15)
            note = "active window (gnome-screenshot)"
        elif shutil.which("import"):
            subprocess.run(["import", "-window", "root", out], capture_output=True, timeout=15)
            note = "full screen (ImageMagick)"
        else:
            raise RuntimeError("no screenshot tool found (install spectacle, grim or gnome-screenshot)")
        if not os.path.exists(out) or os.path.getsize(out) == 0:
            raise RuntimeError("screenshot tool produced no image")
        size = _scale(out, max_width)
        if size:
            note += f", {size[0]}x{size[1]}"
        with open(out, "rb") as f:
            return base64.b64encode(f.read()).decode(), note
    finally:
        if os.path.exists(out):
            os.unlink(out)
