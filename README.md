# DeskCheck

**Let your AI coding agent actually *use* the Linux desktop app it just wrote.**

Claude Code, Cursor, Codex and friends can write a GTK, Qt or Electron app in minutes — and then
declare it "done" without ever seeing it run. DeskCheck is an MCP server that gives the agent
eyes, hands and a stopwatch for desktop software:

| Tool | What the agent gets |
|---|---|
| `app_launch` | Starts the app, waits for its window, captures stdout/stderr, reports startup time and early errors — or the crash output if it died. |
| `app_ui` | A compact outline of the visible UI from the accessibility tree (buttons, fields, values, states) with short refs like `[e12]`. 10–50× cheaper than a screenshot. |
| `app_act` | Click, type into fields, focus, or run any accessible action by ref. |
| `app_screenshot` | A screenshot of *that app's window* (not whatever is in front), downscaled to save tokens. |
| `app_measure` | CPU avg/max over the whole process tree, real memory (PSS) and growth, threads, disk I/O, NVIDIA GPU/VRAM — plus plain verdicts: busy loop, leak, continuous redraw. |
| `app_logs` | Error-looking lines from the app's output, or the tail. |
| `app_attach` | Inspect an app that is already running, by pid or exact name (no `pgrep -f` self-matches). |
| `app_stop` / `app_list` | Clean shutdown with exit code; list tracked apps. |

### Why

The bottleneck of AI coding is no longer writing code, it is **verifying** it. For web apps
there are browser tools. For desktop apps agents were blind: they guessed from screenshots
of the wrong window, measured CPU by hand in `/proc`, and never noticed that the app they
wrote burns 14% of a core doing nothing. (That is a real finding from DeskCheck's first test.)

### Example session

```
> app_launch {"command": "python3 main.py"}
app: main-1  pid: 31247
window ready after 0.8s

> app_ui {"find": "save"}
[e1] push button "Save" {disabled} (clickable)

> app_act {"ref": "e1"}
Error: ... (the agent now knows Save is disabled and why its test failed)

> app_measure {"seconds": 5}
CPU: avg 14.0%  max 29.5%  (100% = one core, 12 available)
memory (PSS): 141.7 MB  change +480.0 KB
verdict: Uses 14.0% of one core on average: if the app should be idle, something is
polling, busy-looping or redrawing continuously.
```

### Requirements

- Linux with Python ≥ 3.10, PyGObject and AT-SPI (`python-gobject` + `at-spi2-core`; present
  on every GNOME/KDE desktop).
- Screenshots of a single window: KDE Plasma 6 (uses KWin + Spectacle). Other desktops fall back
  to full-screen capture with `grim`, `gnome-screenshot` or ImageMagick.
- GPU metrics: NVIDIA (`nvidia-smi`). Everything else works on any GPU.
- Toolkits: GTK 3/4, Qt 5/6, Electron (`--force-renderer-accessibility`), anything screen-reader
  friendly. Games and raw OpenGL/SDL windows get screenshots and measurements, not a UI tree.

No pip dependencies.

### Install

Claude Code:

```sh
claude mcp add deskcheck -s user -e PYTHONPATH=/path/to/deskcheck -- python3 -m deskcheck
```

Any other MCP client: run `python3 -m deskcheck` (stdio transport) with `PYTHONPATH` pointing
to this folder.

### Privacy

Everything runs locally. DeskCheck never sends data anywhere; screenshots and UI text go only
to the agent you connected it to.

### Contributing

Issues and pull requests are welcome, especially for:

- single-window screenshots on GNOME and wlroots compositors,
- AMD and Intel per-process GPU usage (DRM fdinfo),
- toolkits whose accessibility tree needs extra flags.

### License

MIT — free to use, modify and share. Built with the help of Claude Code.
