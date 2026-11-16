"""Read and drive an application's UI through the AT-SPI accessibility bus.

Works with GTK 3/4, Qt 5/6 (with QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1), Electron/Chromium
(with --force-renderer-accessibility) and most toolkits that support screen readers.
"""
import re

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi, GLib  # noqa: E402

Atspi.set_timeout(3000, 15000)

# containers that only add nesting: hidden in the dump, their children are shown
NOISE = {"filler", "panel", "section", "scroll pane", "viewport", "layered pane",
         "glass pane", "root pane", "redundant object", "unknown", "grouping", "split pane",
         "internal frame", "list box", "generic container"}
INTERESTING_STATES = [
    (Atspi.StateType.FOCUSED, "focused"),
    (Atspi.StateType.CHECKED, "checked"),
    (Atspi.StateType.SELECTED, "selected"),
    (Atspi.StateType.PRESSED, "pressed"),
    (Atspi.StateType.EXPANDED, "expanded"),
]
# toolkit actions that exist on every label/row and only add noise
NOISE_ACTIONS = re.compile(r"^(clipboard|selection|link|listitem|menu|text)\.")
ACTION_PREFERENCE = ("click", "press", "activate", "toggle", "jump", "open")


def _safe(fn, default=None):
    try:
        return fn()
    except (GLib.Error, TypeError, AttributeError):
        return default


def find_app(pids):
    """The accessible application whose process is in `pids`."""
    desk = Atspi.get_desktop(0)
    for i in range(_safe(desk.get_child_count, 0)):
        app = _safe(lambda: desk.get_child_at_index(i))
        if app is not None and _safe(app.get_process_id) in pids:
            return app
    return None


def has_window(app):
    for i in range(_safe(app.get_child_count, 0)):
        c = _safe(lambda: app.get_child_at_index(i))
        if c is not None and _safe(c.get_role_name) in ("frame", "window", "dialog", "alert"):
            return True
    return False


def _text(node):
    ti = _safe(node.get_text_iface)
    if ti is None:
        return ""
    n = _safe(ti.get_character_count, 0)
    return (_safe(lambda: ti.get_text(0, min(n, 200)), "") or "").strip()


def _actions(node):
    ai = _safe(node.get_action_iface)
    if ai is None:
        return []
    return [_safe(lambda: ai.get_action_name(i), "") for i in range(_safe(ai.get_n_actions, 0))]


def _useful(acts):
    return [a for a in acts if a and not NOISE_ACTIONS.match(a)]


def _describe(node, ref):
    role = _safe(node.get_role_name, "?")
    name = (_safe(node.get_name, "") or "").strip()
    parts = [f"[{ref}]", role]
    if name:
        parts.append(f'"{name[:80]}"')
    text = _text(node)
    if text and text != name:
        parts.append(f"text={text[:80]!r}")
    vi = _safe(node.get_value_iface)
    if vi is not None:
        parts.append(f"value={_safe(vi.get_current_value)}")
    ss = _safe(node.get_state_set)
    if ss is not None:
        flags = [label for st, label in INTERESTING_STATES if ss.contains(st)]
        if not ss.contains(Atspi.StateType.SENSITIVE) and role not in ("label", "icon", "image"):
            flags.append("disabled")
        if ss.contains(Atspi.StateType.EDITABLE):
            flags.append("editable")
        if flags:
            parts.append("{" + ",".join(flags) + "}")
    acts = [a for a in _actions(node) if a]
    shown = _useful(acts)
    if shown and shown != ["click"]:
        parts.append("actions=" + "|".join(shown[:4]))
    elif shown:
        parts.append("(clickable)")
    return parts, role, name, text, acts


def dump(app, max_nodes=400, only_showing=True, find=None):
    """Compact indented outline of the UI. Each line starts with a short [eN] ref usable
    by act(). Returns (text, {ref: tree path})."""
    lines = []
    refs = {}
    count = 0
    find = (find or "").lower()

    def walk(node, ref, depth, parent_name=""):
        nonlocal count
        if count >= max_nodes:
            return
        ss = _safe(node.get_state_set)
        if only_showing and depth > 0 and ss is not None and not ss.contains(Atspi.StateType.SHOWING):
            return
        short = f"e{len(refs) + 1}"
        parts, role, name, text, acts = _describe(node, short)
        skip = (role in NOISE and not name and not _useful(acts) and not text) or (
            role == "label" and name and name == parent_name)  # button's own caption
        if find:
            show = find in f"{role} {name} {text}".lower()
        else:
            show = not skip
        child_depth = depth
        if show:
            refs[short] = ref
            lines.append("  " * depth + " ".join(parts))
            count += 1
            child_depth = depth + 1
        n = _safe(node.get_child_count, 0)
        for i in range(min(n, 500)):
            child = _safe(lambda: node.get_child_at_index(i))
            if child is not None:
                walk(child, f"{ref}.{i}" if ref else str(i), child_depth if not find else 0,
                     name)

    n = _safe(app.get_child_count, 0)
    for i in range(n):
        child = _safe(lambda: app.get_child_at_index(i))
        if child is not None:
            walk(child, str(i), 0)
    if count >= max_nodes:
        lines.append(f"… truncated at {max_nodes} nodes (use `find` to narrow down)")
    return "\n".join(lines), refs


def resolve(app, ref):
    node = app
    for part in str(ref).split("."):
        node = _safe(lambda: node.get_child_at_index(int(part)))
        if node is None:
            raise ValueError(f"ref {ref} no longer exists: the UI changed, dump it again")
    return node


def act(app, ref, action="click", text=None):
    node = resolve(app, ref)
    parts, role, name, _t, acts = _describe(node, ref)
    label = " ".join(parts[1:3])
    if action == "set_text":
        et = _safe(node.get_editable_text_iface)
        if et is None:
            raise ValueError(f"{label} is not an editable text field")
        if not et.set_text_contents(text or ""):
            raise ValueError(f"could not set text on {label}")
        return f"Text set on {label}"
    if action == "focus":
        comp = _safe(node.get_component_iface)
        if comp is None or not comp.grab_focus():
            raise ValueError(f"could not focus {label}")
        return f"Focused {label}"
    ai = _safe(node.get_action_iface)
    if ai is None or not acts:
        raise ValueError(f"{label} has no actions")
    idx = None
    if action in acts:
        idx = acts.index(action)
    else:
        for pref in ACTION_PREFERENCE:
            if pref in acts:
                idx = acts.index(pref)
                break
    if idx is None:
        idx = 0
    ai.do_action(idx)
    return f"Did '{acts[idx]}' on {label}"
