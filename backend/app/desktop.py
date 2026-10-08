"""macOS menu-bar shell for the Voitta-chainlit backend.

Wraps the FastAPI / Chainlit app in a ``rumps.App`` so the user can
launch Voitta from a tray icon instead of running ``./start.sh``. The
backend itself is unchanged: uvicorn runs on a daemon thread, the
rumps event loop owns the main thread (Cocoa requires it).

Menu items:
  About Voitta
  Open in browser
  Copy bookmarklet
  Settings…              (status dialog + MCP-debug toggle)
  Show data folder
  (Re)create TLS certificates…
  Reset…
  Quit

macOS-only — rumps + AppKit. On other platforms just run
``./start.sh`` directly. The Settings dialog summarises BE state and
flips the ``mcpDebugEnabled`` switch that gates the ``/mcp``
debugging endpoint.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

# Sequencing note (carried over from the legacy build): this import
# must come before importing rumps. rumps eagerly initialises NSApp on
# import; if env vars that affect ``app.config`` are set later, the
# path constants are already frozen at the wrong location.
from app.config import (  # noqa: E402
    HOST,
    PORT,
    PLAINTEXT_PORT,
    PROJECT_ROOT,
    TLS_CERT_PATH,
    TLS_KEY_PATH,
    USER_DATA_ROOT,
)

import time

import rumps  # noqa: E402

# ---------------------------------------------------------------------------
# Uvicorn state (module-level so menu and setup share it)
# ---------------------------------------------------------------------------

_uvicorn_server: "uvicorn.Server | None" = None  # set on first start
_uvicorn_thread: threading.Thread | None = None
_uvicorn_plaintext_server: "uvicorn.Server | None" = None  # bridge listener
_uvicorn_plaintext_thread: threading.Thread | None = None
_uvicorn_lock = threading.Lock()
_installing: bool = False  # True while the first-time setup is running


APP_NAME = "Voitta"

def _get_app_version() -> str:
    try:
        from app.installer import current_app_version
        return current_app_version()
    except Exception:
        return "unknown"

ABOUT_TEXT = (
    f"Version {_get_app_version()}\n\n"
    "Local Voitta backend.\n"
    "Click the bookmarklet on any page to open the chat."
)


# ---------------------------------------------------------------------------
# AppKit helpers — modal dialogs from a status-bar (accessory) app
# ---------------------------------------------------------------------------


def _alert(title=None, message="", ok=None, cancel=None, other=None):
    """A modal alert that is actually visible from a status-bar app.

    Same return contract as ``rumps.alert``: ``1`` for ok, ``0`` for cancel,
    ``-1`` for other — callers test truthiness (``if not confirm``).

    Why not ``rumps.alert`` behind an activation-policy flip, which is what
    this used to be: the app is ``LSUIElement`` (no Dock icon). Flipping to
    the Regular policy makes the Dock create an icon, but it does so
    asynchronously — and on the very first flip the icon does not exist yet
    when ``activateIgnoringOtherApps_`` fires. macOS refuses the activation
    (an inactive app may not steal focus on its own) and falls back to
    bouncing the Dock icon as an attention request. The alert is open the
    whole time, just behind the front app; clicking the bouncing icon is
    the user granting the activation by hand. The second time round the
    Dock has the icon cached, the activation is synchronous, and it "works".

    So the visibility must not depend on activation succeeding. The alert's
    window is raised to the modal-panel level and ordered front regardless —
    it floats above the front app either way — and the policy flip is kept
    only so the panel can take keyboard focus once it is up. This is the
    path ``_settings_alert_with_switch`` already used, which is why that
    dialog never bounced.
    """
    from AppKit import (
        NSAlert,
        NSApplication,
        NSApplicationActivationPolicyAccessory,
        NSApplicationActivationPolicyRegular,
        NSModalPanelWindowLevel,
    )

    alert = NSAlert.alloc().init()
    alert.setMessageText_(str(title or APP_NAME))
    alert.setInformativeText_(str(message or ""))
    alert.setAlertStyle_(0)  # informational
    # Button order matters: NSAlert returns 1000 + index of the button
    # pressed, and the FIRST button added is the default (Return key).
    alert.addButtonWithTitle_(str(ok or "OK"))
    if cancel is not None:
        alert.addButtonWithTitle_(str(cancel) if isinstance(cancel, str) else "Cancel")
    if other is not None:
        alert.addButtonWithTitle_(str(other))

    nsapp = NSApplication.sharedApplication()
    try:
        nsapp.setActivationPolicy_(NSApplicationActivationPolicyRegular)
    except Exception:
        pass
    try:
        win = alert.window()
        win.setLevel_(NSModalPanelWindowLevel)
        win.orderFrontRegardless()
        win.makeKeyAndOrderFront_(None)
    except Exception:
        pass
    try:
        # Best effort, and fine if refused: the panel is already visible.
        nsapp.activateIgnoringOtherApps_(True)
    except Exception:
        pass
    try:
        raw = int(alert.runModal())
    finally:
        try:
            nsapp.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
        except Exception:
            pass

    # 1000 = ok, 1001 = cancel, 1002 = other -> rumps' 1 / 0 / -1.
    if raw == 1000:
        return 1
    if raw == 1001:
        return 0
    if raw == 1002:
        return -1
    return raw


def _settings_alert_with_switches(
    title: str,
    message: str,
    ok: str,
    cancel: str | None,
    switches: list[tuple[str, bool]],
) -> tuple[int, list[bool]]:
    """NSAlert with a column of labelled NSSwitches as its accessory view.
    ``switches`` is ``[(label, initially_on), …]``, top to bottom. Returns
    ``(rumps-style response, final switch states in the same order)``.

    Response: ``1`` = ok pressed, ``0`` = cancel pressed (only when
    ``cancel`` is non-None).
    """
    from AppKit import (
        NSAlert,
        NSApplication,
        NSApplicationActivationPolicyAccessory,
        NSApplicationActivationPolicyRegular,
        NSControlStateValueOff,
        NSControlStateValueOn,
        NSMakeRect,
        NSModalPanelWindowLevel,
        NSSwitch,
        NSTextField,
        NSView,
    )

    alert = NSAlert.alloc().init()
    alert.setMessageText_(title)
    alert.setInformativeText_(message)
    alert.addButtonWithTitle_(ok)
    if cancel:
        alert.addButtonWithTitle_(cancel)

    row_h = 32
    container = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 340, row_h * len(switches)))
    controls = []
    for i, (switch_label, switch_on) in enumerate(switches):
        y = row_h * (len(switches) - 1 - i)  # AppKit origin is bottom-left
        label = NSTextField.alloc().initWithFrame_(NSMakeRect(0, y + 4, 270, 20))
        label.setStringValue_(switch_label)
        label.setBezeled_(False)
        label.setDrawsBackground_(False)
        label.setEditable_(False)
        label.setSelectable_(False)
        container.addSubview_(label)
        switch = NSSwitch.alloc().initWithFrame_(NSMakeRect(280, y, 50, 28))
        switch.setState_(NSControlStateValueOn if switch_on else NSControlStateValueOff)
        container.addSubview_(switch)
        controls.append(switch)
    alert.setAccessoryView_(container)

    nsapp = NSApplication.sharedApplication()
    try:
        nsapp.setActivationPolicy_(NSApplicationActivationPolicyRegular)
        nsapp.activateIgnoringOtherApps_(True)
    except Exception:
        pass
    try:
        win = alert.window()
        win.setLevel_(NSModalPanelWindowLevel)
        win.makeKeyAndOrderFront_(None)
    except Exception:
        pass
    try:
        raw = alert.runModal()
    finally:
        try:
            nsapp.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
        except Exception:
            pass

    # NSAlert returns 1000 + button-index. Map to rumps codes.
    response = 1 if raw == 1000 else (0 if raw == 1001 else int(raw))
    return response, [bool(s.state() == NSControlStateValueOn) for s in controls]


# ---------------------------------------------------------------------------
# URL / bookmarklet / clipboard helpers
# ---------------------------------------------------------------------------


def _server_url() -> str:
    scheme = "https" if TLS_CERT_PATH.exists() and TLS_KEY_PATH.exists() else "http"
    return f"{scheme}://{HOST}:{PORT}"


def _bookmarklet_text() -> str:
    """Return the ``javascript:`` URL the user pastes into a browser bookmark.
    Origin is the tray's local server URL — unchanged from before; the string
    builder now lives in app.bookmarklets so the server can reuse it."""
    from app.bookmarklets import normal_bookmarklet
    return normal_bookmarklet(_server_url())


def _bridge_bookmarklet_text() -> str:
    """Bookmarklet for hardened-CSP pages (e.g. Salesforce Lightning). Origin is
    the tray's plain-http loopback sibling — unchanged from before."""
    from app.bookmarklets import bridge_bookmarklet
    return bridge_bookmarklet(f"http://{HOST}:{PLAINTEXT_PORT}")


def _copy_to_clipboard(text: str) -> bool:
    try:
        from AppKit import NSPasteboard, NSPasteboardTypeString
    except ImportError:
        try:
            p = subprocess.run(["pbcopy"], input=text, text=True, check=True)
            return p.returncode == 0
        except (OSError, subprocess.CalledProcessError):
            return False
    pb = NSPasteboard.generalPasteboard()
    pb.clearContents()
    return bool(pb.setString_forType_(text, NSPasteboardTypeString))


def _human_path(p: Path) -> str:
    s = str(p)
    home = str(Path.home())
    return s.replace(home, "~", 1) if s.startswith(home) else s


# ---------------------------------------------------------------------------
# Uvicorn launcher
# ---------------------------------------------------------------------------


def _start_uvicorn(log: logging.Logger) -> threading.Thread:
    """Launch uvicorn on a daemon thread. Stores server ref for restart/status."""
    global _uvicorn_server, _uvicorn_thread, _installing
    _installing = False

    import uvicorn

    kwargs: dict = {"host": HOST, "port": PORT, "log_level": "info"}
    if TLS_CERT_PATH.exists() and TLS_KEY_PATH.exists():
        kwargs["ssl_certfile"] = str(TLS_CERT_PATH)
        kwargs["ssl_keyfile"] = str(TLS_KEY_PATH)
    else:
        # Should not happen: phase-0 setup aborts startup if the CA can't be
        # provisioned. Log loudly rather than silently serving plain HTTP.
        log.error("TLS cert missing at uvicorn start — serving HTTP (unexpected)")

    config = uvicorn.Config("app.main:app", **kwargs)
    server = uvicorn.Server(config)

    with _uvicorn_lock:
        _uvicorn_server = server

    def _run() -> None:
        log.info("starting uvicorn on %s", _server_url())
        try:
            server.run()
        except Exception:
            log.exception("uvicorn died")

    t = threading.Thread(target=_run, name="voitta-uvicorn", daemon=True)
    t.start()
    with _uvicorn_lock:
        _uvicorn_thread = t

    # Sibling plain-HTTP listener for the hardened-site bridge popup. Same
    # ASGI app, no TLS, on PLAINTEXT_PORT. Runs in the same process so it
    # shares all in-memory state (sessions, sockets) with the TLS listener.
    _start_uvicorn_plaintext(log)

    global _watchdog_started
    if not _watchdog_started:
        _watchdog_started = True
        _start_health_watchdog(log)

    return t


def _start_uvicorn_plaintext(log: logging.Logger) -> threading.Thread:
    """Launch a second uvicorn (plain http, no TLS) on PLAINTEXT_PORT.

    The bridge popup loads from http://127.0.0.1:PLAINTEXT_PORT so it needs
    no trusted cert. Daemon thread; failures are logged but non-fatal — the
    ordinary https bookmarklet keeps working regardless.
    """
    global _uvicorn_plaintext_server, _uvicorn_plaintext_thread
    import uvicorn

    config = uvicorn.Config(
        "app.main:app", host=HOST, port=PLAINTEXT_PORT, log_level="warning"
    )
    server = uvicorn.Server(config)
    with _uvicorn_lock:
        _uvicorn_plaintext_server = server

    def _run() -> None:
        log.info("starting bridge listener on http://%s:%s", HOST, PLAINTEXT_PORT)
        try:
            server.run()
        except Exception:
            log.exception("bridge listener died")

    t = threading.Thread(target=_run, name="voitta-uvicorn-bridge", daemon=True)
    t.start()
    with _uvicorn_lock:
        _uvicorn_plaintext_thread = t
    return t


def _uvicorn_status() -> str:
    """Return a short status string for the menu label."""
    if _installing:
        return "starting…"
    with _uvicorn_lock:
        t = _uvicorn_thread
        srv = _uvicorn_server
    if t is None:
        return "stopped"
    if not t.is_alive():
        return "crashed"
    if srv is not None and getattr(srv, "started", False):
        return "unresponsive" if _health_probe_failures >= 3 else "running"
    return "starting…"


# Consecutive /health probe failures against the TLS listener. A live thread
# with a spinning event loop reads as "running" to thread-liveness checks —
# an actual HTTP round-trip is the only reliable signal. Written by the
# watchdog thread, read by _uvicorn_status.
_health_probe_failures = 0
_watchdog_started = False


def _start_health_watchdog(log: logging.Logger) -> None:
    """Probe the TLS listener's /health every 20 s (5 s timeout) on a daemon
    thread. Three consecutive failures flip the menu status to "unresponsive",
    log loudly, and dump all Python thread stacks to voitta-stacks.log — the
    post-mortem we were missing when the event loop wedged at 100 % CPU."""

    def _probe() -> None:
        global _health_probe_failures
        import ssl
        import urllib.request

        insecure = ssl._create_unverified_context()  # self-signed local cert
        url = f"{_server_url()}/health"
        dumped = False
        while True:
            time.sleep(20)
            with _uvicorn_lock:
                srv = _uvicorn_server
            if srv is None or not getattr(srv, "started", False):
                continue  # not up yet — nothing to probe
            try:
                with urllib.request.urlopen(url, timeout=5, context=insecure):
                    pass
                if _health_probe_failures:
                    log.info("health watchdog: TLS listener recovered")
                _health_probe_failures = 0
                dumped = False
            except Exception as exc:
                _health_probe_failures += 1
                log.warning(
                    "health watchdog: probe %d failed: %s",
                    _health_probe_failures, exc,
                )
                if _health_probe_failures == 3 and not dumped:
                    dumped = True
                    log.error(
                        "health watchdog: TLS listener unresponsive — dumping "
                        "thread stacks to voitta-stacks.log"
                    )
                    try:
                        import faulthandler
                        with (Path(USER_DATA_ROOT) / "logs" / "voitta-stacks.log").open("a") as fp:
                            from datetime import datetime as _dt
                            fp.write(f"\n=== watchdog dump {_dt.now().isoformat()} ===\n")
                            faulthandler.dump_traceback(file=fp, all_threads=True)
                    except Exception:
                        log.exception("health watchdog: stack dump failed")

    threading.Thread(target=_probe, name="voitta-health-watchdog", daemon=True).start()


# ---------------------------------------------------------------------------
# Status / diagnostics helpers
# ---------------------------------------------------------------------------


def _rag_status_line() -> str:
    """One-line RAG state for the Settings dialog."""
    try:
        from app.tools.rag.index import index_status
        st = index_status("docs")
    except Exception as exc:
        return f"RAG      ·  ? ({exc})"
    if st.get("built"):
        return (
            f"RAG      ·  ✓ {st.get('chunk_count', 0)} chunks across "
            f"{st.get('files_count', 0)} docs"
        )
    return "RAG      ·  ⚠ not built — run scripts/build_rag.py"


def _plugins_status_line() -> str:
    try:
        from app.plugins import all_plugins
        plugins = all_plugins()
    except Exception:
        return "Plugins  ·  ?"
    return f"Plugins  ·  {len(plugins)} loaded: {', '.join(p.name for p in plugins)}"


def _mcp_connectors_line() -> str:
    try:
        from app.services.mcp.registry import list_connectors
        conns = list_connectors()
    except Exception:
        return "MCP      ·  ?"
    if not conns:
        return "MCP      ·  no connectors declared"
    parts = [f"{c.decl.plugin_name}:{c.decl.id}={c.status}" for c in conns]
    return "MCP      ·  " + ", ".join(parts)


# ---------------------------------------------------------------------------
# Menu-bar app
# ---------------------------------------------------------------------------


class VoittaMenuBarApp(rumps.App):
    def __init__(self) -> None:
        # title="V" is the idle-state glyph. The activity poller swaps
        # its colour while tools run — see app.activity for the
        # category → colour mapping.
        super().__init__(APP_NAME, title="V", quit_button=None)
        self._server_status_item = rumps.MenuItem("Server: checking…")
        self._server_status_item.set_callback(None)  # display-only
        self.menu = [
            rumps.MenuItem(f"About {APP_NAME}", callback=self.show_about),
            None,  # separator
            rumps.MenuItem("Open in browser", callback=self.open_browser),
            rumps.MenuItem("Copy bookmarklet", callback=self.copy_bookmarklet),
            rumps.MenuItem("Copy bookmarklet (Salesforce/strict CSP)", callback=self.copy_bridge_bookmarklet),
            rumps.MenuItem("Settings…", callback=self.show_settings),
            self._make_voice_item(),
            self._make_mic_sensitivity_item(),
            rumps.MenuItem("Active sessions…", callback=self.show_sessions),
            None,
            self._server_status_item,
            rumps.MenuItem("Restart server", callback=self.restart_server),
            None,
            rumps.MenuItem("Workspace…", callback=self.open_workspace),
            rumps.MenuItem("Open logs folder…", callback=self.open_logs_folder),
            rumps.MenuItem("(Re)create TLS certificates…", callback=self.recreate_certs),
            rumps.MenuItem("Rebuild…", callback=self.rebuild),
            rumps.MenuItem("Reset…", callback=self.reset),
            None,
            rumps.MenuItem(f"Quit {APP_NAME}", callback=self.quit_app),
        ]
        self._log = logging.getLogger("voitta.desktop")

        # Activity poller — 400 ms feels live without burning cycles.
        # Always-on while the app runs; cheap (one dict read).
        self._activity_timer = rumps.Timer(self._tick_activity, 0.4)
        self._activity_timer.start()
        self._last_color: str | None = None
        self._spin_frame: int = 0
        self._last_server_status: str | None = None

        # Voice feedback poller — created on demand, runs at 120 ms only
        # while the voice assistant is on (live braille level meter needs
        # a faster cadence than the 400 ms activity tick).
        self._voice_timer: rumps.Timer | None = None
        self._voice_last_seq: int = -1
        self._voice_spin: int = 0
        self._voice_autostart_done = False
        self._voice_installing = False
        self._sessions_window = None  # lazily built SessionsWindow
        # Session ids in displayed (frozen) order while the window is open —
        # so "task two" always means the same row even as sessions refresh.
        self._frozen_order: list[str] = []

        # Register the voice command hook now (harmless if voice never
        # starts) so "tasks voitta" / "task N" / "task quit" route here.
        # The hook fires on the voice thread → hop to the Cocoa main thread.
        try:
            from app.services import voice
            from PyObjCTools import AppHelper
            voice.set_command_hook(
                lambda phrase: AppHelper.callAfter(self._on_voice_command, phrase)
            )
        except Exception:
            self._log.exception("voice command hook registration failed")

    # Spoken number word → 1-based row index.
    _TASK_WORDS = {
        "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
        "six": 6, "seven": 7, "eight": 8, "nine": 9,
    }

    def _on_voice_command(self, phrase: str) -> None:
        """Dispatch a recognized command phrase. MAIN THREAD (via callAfter)."""
        if phrase == "tasks voitta":
            self._show_sessions_main()
            return
        if phrase == "task quit":
            self._close_sessions_window()
            return
        word = phrase[len("task "):] if phrase.startswith("task ") else ""
        idx = self._TASK_WORDS.get(word)
        if idx is None:
            return
        order = self._frozen_order
        if idx > len(order):
            # Spoken a number past the visible rows — ignore (the menu-bar
            # already flashed the recognized phrase).
            self._log.info("voice: task %d out of range (%d shown)", idx, len(order))
            return
        self._on_session_selected(order[idx - 1])
        # Selecting raises the browser tab, which buries this list — so close
        # it and return to BASE mode. Otherwise we'd stay in TASKS mode with
        # the window hidden and "tasks voitta"/"hey voitta" suspended, with no
        # voice way out. "tasks voitta" reopens the list for the next pick.
        self._close_sessions_window()

    def _make_voice_item(self) -> rumps.MenuItem:
        item = rumps.MenuItem("Voice", callback=self.toggle_voice)
        try:
            from app.services import user_settings as _us
            item.state = 1 if _us.voice_enabled() else 0
        except Exception:
            item.state = 0
        self._voice_item = item
        return item

    # Mic-sensitivity levels (label → adaptive-gain ceiling). AutoGain
    # boosts quiet/distant mics toward a target level so the wake word
    # triggers without yelling; the ceiling only caps how much help a
    # very quiet mic gets — a normal voice is never over-amplified.
    _MIC_LEVELS = [
        ("Off (raw)", 1.0),
        ("Low", 3.0),
        ("Normal", 6.0),
        ("High", 12.0),
        ("Max", 24.0),
    ]

    def _make_mic_sensitivity_item(self) -> rumps.MenuItem:
        """Submenu of discrete mic-gain levels with a checkmark on the
        active one. Applies live (no restart) and persists."""
        parent = rumps.MenuItem("Mic sensitivity")
        try:
            from app.services import user_settings as _us
            current = _us.mic_gain()
        except Exception:
            current = 1.0
        # Pick the closest preset for the checkmark.
        active_label = min(
            self._MIC_LEVELS, key=lambda lv: abs(lv[1] - current)
        )[0]
        self._mic_items: dict[str, rumps.MenuItem] = {}
        for label, _gain in self._MIC_LEVELS:
            mi = rumps.MenuItem(label, callback=self._set_mic_sensitivity)
            mi.state = 1 if label == active_label else 0
            self._mic_items[label] = mi
            parent.add(mi)
        self._mic_sensitivity_item = parent
        return parent

    def _set_mic_sensitivity(self, sender) -> None:
        gain = dict(self._MIC_LEVELS).get(sender.title, 1.0)
        try:
            from app.services import user_settings as _us
            from app.services import voice
            _us.set_mic_gain(gain)
            voice.set_mic_gain_runtime(gain)  # live — no restart
            self._log.info("voice: mic sensitivity → %s (gain %.1f)", sender.title, gain)
        except Exception:
            self._log.exception("set mic sensitivity failed")
        for label, mi in self._mic_items.items():
            mi.state = 1 if label == sender.title else 0

    # ---- activity poller -------------------------------------------------

    _SPIN_FRAMES = ["⣾", "⣽", "⣻", "⢿", "⡿", "⣟", "⣯", "⣷"]

    def _tick_activity(self, _timer) -> None:
        from app import activity
        status = _uvicorn_status()

        self._maybe_voice_autostart(status)
        voice_active = self._sync_voice_timer()

        if voice_active:
            # _tick_voice owns the title while voice is on. Forget the
            # last-applied color so the normal glyph is re-applied the
            # moment voice goes off.
            self._last_color = None
        elif status == "starting…":
            # Animate a rotating braille ring around V while server boots.
            frame = self._SPIN_FRAMES[self._spin_frame % len(self._SPIN_FRAMES)]
            self._spin_frame += 1
            self._apply_colored_title(f"{frame}V", "idle")
            self._last_color = "idle"
        else:
            self._spin_frame = 0
            color_name = activity.current_color()
            if color_name != self._last_color or status != self._last_server_status:
                self._last_color = color_name
                self._apply_colored_title(activity.current_glyph(), color_name)

        self._last_server_status = status

        icons = {"running": "✓", "starting…": "⋯", "stopped": "✕", "crashed": "⚠",
                 "unresponsive": "⚠"}
        label = f"Server: {icons.get(status, '')} {status}"
        if self._server_status_item.title != label:
            self._server_status_item.title = label

        # Keep the sessions window live while it's open (sessions come and
        # go, titles change) — cheap dict read + diff'd Cocoa update.
        sw = self._sessions_window
        if sw is not None and sw.is_visible():
            self._refresh_sessions()

    # ---- voice feedback ----------------------------------------------------

    # Braille level meter: dots fill bottom-up with the mic level.
    _METER_FRAMES = ["⠀", "⡀", "⣀", "⣄", "⣤", "⣦", "⣶", "⣷", "⣿"]

    def _maybe_voice_autostart(self, status: str) -> None:
        """Start the voice pipeline once the server is up, if the user had
        Voice enabled in a previous session."""
        if self._voice_autostart_done or status != "running":
            return
        self._voice_autostart_done = True
        try:
            from app.services import user_settings as _us
            from app.services import voice, voice_install
            if not _us.voice_enabled() or voice.is_running():
                return
            if voice_install.is_ready():
                self._log.info("voice: autostarting (was enabled)")
                voice.start()
            else:
                # Version bump wiped the userbase packages. Don't pop a
                # modal uninvited — uncheck and ask for a re-toggle.
                _us.set_voice_enabled(False)
                self._voice_item.state = 0
                rumps.notification(
                    title=APP_NAME,
                    subtitle="Voice needs to reinstall components",
                    message="Open the Voitta menu and click Voice to set it up again.",
                )
        except Exception:
            self._log.exception("voice autostart failed")

    def _sync_voice_timer(self) -> bool:
        """Start/stop the fast voice render timer to match the pipeline
        state. Returns True while voice owns the menu-bar title."""
        try:
            from app.services import voice
            active = voice.snapshot()["state"] != "off"
        except Exception:
            active = False
        running = self._voice_timer is not None
        if active and not running:
            self._voice_timer = rumps.Timer(self._tick_voice, 0.12)
            self._voice_timer.start()
        elif not active and running:
            self._voice_timer.stop()
            self._voice_timer = None
        return active

    def _tick_voice(self, _timer) -> None:
        from app import activity
        from app.services import voice

        snap = voice.snapshot()
        state = snap["state"]
        if state == "off":
            return

        if snap["seq"] != self._voice_last_seq:
            self._voice_last_seq = snap["seq"]
            self._voice_notify(state, snap["detail"])

        self._voice_spin += 1
        spinner = self._SPIN_FRAMES[self._voice_spin % len(self._SPIN_FRAMES)]
        meter = self._METER_FRAMES[
            max(0, min(len(self._METER_FRAMES) - 1,
                       round(snap["level"] * (len(self._METER_FRAMES) - 1))))
        ]

        if state == "loading":
            segments = [(spinner, "gray"), ("V", "gray")]
        elif state == "listening":
            # Teal level meter + V in whatever the activity color is.
            segments = [(meter, "teal"), ("V", activity.current_color())]
        elif state == "recording":
            # Red meter; V blinks red to make "it's capturing" unmissable.
            v_col = "red" if (self._voice_spin // 3) % 2 == 0 else "gray"
            segments = [(meter, "red"), ("V", v_col)]
        elif state == "transcribing":
            segments = [(spinner, "purple"), ("V", "purple")]
        elif state == "sending":
            segments = [(spinner, "blue"), ("V", "blue")]
        elif state == "sent":
            segments = [("✓", "green"), ("V", "green")]
        elif state == "no_chat":
            segments = [("!", "orange"), ("V", "orange")]
        else:  # error
            segments = [("!", "red"), ("V", "red")]

        self._apply_title_segments(segments)

    def _voice_notify(self, state: str, detail: str) -> None:
        """One-shot notifications on voice state transitions."""
        try:
            if state == "no_chat":
                rumps.notification(
                    title=APP_NAME,
                    subtitle="Heard you — but no active chat",
                    message=(f"“{detail}” — open a page with the Voitta widget "
                             "and click into it.") if detail else
                            "Open a page with the Voitta widget and click into it.",
                )
            elif state == "error":
                rumps.notification(
                    title=APP_NAME,
                    subtitle="Voice assistant problem",
                    message=detail or "unknown error",
                )
        except Exception:
            pass

    @staticmethod
    def _ns_color_for(name: str):
        from AppKit import NSColor
        if name == "label":
            return NSColor.labelColor()
        method = f"system{name.capitalize()}Color"
        if hasattr(NSColor, method):
            return getattr(NSColor, method)()
        return NSColor.labelColor()

    def _apply_colored_title(self, text: str, color_name: str) -> None:
        try:
            from AppKit import (
                NSAttributedString,
                NSFont,
                NSFontAttributeName,
                NSForegroundColorAttributeName,
            )
        except ImportError:
            self.title = text
            return
        attrs = {
            NSForegroundColorAttributeName: self._ns_color_for(color_name),
            NSFontAttributeName: NSFont.boldSystemFontOfSize_(14),
        }
        attributed = NSAttributedString.alloc().initWithString_attributes_(text, attrs)
        try:
            button = self._nsapp.nsstatusitem.button()
        except Exception:
            self.title = text
            return
        if button is None:
            self.title = text
            return
        button.setAttributedTitle_(attributed)

    def _apply_title_segments(self, segments: list[tuple[str, str]]) -> None:
        """Render the status-item title from (text, color) segments —
        lets the voice meter and the V glyph carry different colors."""
        try:
            from AppKit import (
                NSFont,
                NSFontAttributeName,
                NSForegroundColorAttributeName,
                NSMutableAttributedString,
            )
            from Foundation import NSAttributedString
        except ImportError:
            self.title = "".join(t for t, _ in segments)
            return
        out = NSMutableAttributedString.alloc().init()
        font = NSFont.boldSystemFontOfSize_(14)
        for text, color_name in segments:
            attrs = {
                NSForegroundColorAttributeName: self._ns_color_for(color_name),
                NSFontAttributeName: font,
            }
            out.appendAttributedString_(
                NSAttributedString.alloc().initWithString_attributes_(text, attrs)
            )
        try:
            button = self._nsapp.nsstatusitem.button()
        except Exception:
            button = None
        if button is None:
            self.title = "".join(t for t, _ in segments)
            return
        button.setAttributedTitle_(out)

    # ---- menu callbacks --------------------------------------------------

    def restart_server(self, _sender) -> None:
        log = self._log
        def _do_restart() -> None:
            with _uvicorn_lock:
                srv = _uvicorn_server
                bridge_srv = _uvicorn_plaintext_server
            if srv is not None:
                log.info("restart_server: signalling uvicorn to exit")
                srv.should_exit = True
                if bridge_srv is not None:
                    bridge_srv.should_exit = True
                # Give it a moment to free the ports before restarting.
                time.sleep(1.5)
            log.info("restart_server: starting fresh uvicorn")
            _start_uvicorn(log)
        threading.Thread(target=_do_restart, name="voitta-restart", daemon=True).start()

    def show_about(self, _sender) -> None:
        _alert(title=APP_NAME, message=ABOUT_TEXT, ok="OK")

    def open_browser(self, _sender) -> None:
        webbrowser.open(_server_url())

    def copy_bookmarklet(self, _sender) -> None:
        text = _bookmarklet_text()
        if _copy_to_clipboard(text):
            try:
                rumps.notification(
                    title=APP_NAME,
                    subtitle="Bookmarklet copied",
                    message="Paste into your bookmarks bar.",
                )
            except Exception:
                pass
        else:
            _alert(title=APP_NAME, message="Couldn't access the clipboard.", ok="OK")

    def copy_bridge_bookmarklet(self, _sender) -> None:
        text = _bridge_bookmarklet_text()
        if _copy_to_clipboard(text):
            try:
                rumps.notification(
                    title=APP_NAME,
                    subtitle="Salesforce bookmarklet copied",
                    message="For pages with a strict CSP. Opens a small popup; keep it open.",
                )
            except Exception:
                pass
        else:
            _alert(title=APP_NAME, message="Couldn't access the clipboard.", ok="OK")

    def toggle_voice(self, sender) -> None:
        """Voice menu item: toggle the "hey voitta" assistant.

        First enable lazy-installs the voice packages + models behind a
        progress window (they're ~1.3 GB on disk all-in — not something
        to force on users who never speak to their Mac).
        """
        try:
            from app.services import user_settings as _us
            from app.services import voice, voice_install
        except Exception:
            self._log.exception("voice imports failed")
            return

        if voice.is_running():
            voice.stop()
            sender.state = 0
            _us.set_voice_enabled(False)
            self._log.info("voice: disabled via menu")
            return

        if _installing or self._voice_installing:
            _alert(
                title=APP_NAME,
                message="Setup is already in progress — try again when it finishes.",
                ok="OK",
            )
            return

        if voice_install.is_ready():
            voice.start()
            sender.state = 1
            _us.set_voice_enabled(True)
            self._log.info("voice: enabled via menu")
            return

        self._voice_setup_then_start(sender)

    def _voice_setup_then_start(self, sender) -> None:
        """Show the voice install window and run the 3 phases on a worker
        thread; start the pipeline and check the menu item on success."""
        from PyObjCTools import AppHelper
        from app.install_window import InstallWindow
        from app.services import user_settings as _us
        from app.services import voice, voice_install

        log = self._log
        self._voice_installing = True
        win = InstallWindow(
            phases=["Voice packages", "Wake-word model (15 MB)", "Speech model (1.6 GB)"],
            window_title="Voitta — Voice Setup",
            heading="Setting up the voice assistant",
            subtitle_text="One-time download. This window closes automatically.",
            footer_text="Models run entirely on this Mac — nothing is sent to the cloud.",
        )

        def _worker() -> None:
            try:
                # Phase 0 — pip packages
                win.start_phase(0, "Preparing…")
                if not voice_install.packages_missing():
                    win.skip_phase(0, "Already installed")
                else:
                    ok = voice_install.install_packages(
                        lambda c, t, label: win.update_phase(0, c, t, label)
                    )
                    if not ok:
                        _fail(0, "Install failed — see log")
                        return
                    win.finish_phase(0)

                # Phase 1 — wake-word + VAD models (download_kws_model
                # fetches both; skip only when BOTH are present)
                win.start_phase(1, "Downloading…")
                if not voice_install.kws_model_missing() and not voice_install.vad_model_missing():
                    win.skip_phase(1, "Already downloaded")
                elif voice_install.download_kws_model(
                    lambda c, t, label: win.update_phase(1, c, t, label)
                ):
                    win.finish_phase(1)
                else:
                    _fail(1, "Download failed — see log")
                    return

                # Phase 2 — whisper model
                win.start_phase(2, "Downloading…")
                if not voice_install.whisper_model_missing():
                    win.skip_phase(2, "Already downloaded")
                elif voice_install.download_whisper_model(
                    lambda c, t, label: win.update_phase(2, c, t, label)
                ):
                    win.finish_phase(2)
                else:
                    _fail(2, "Download failed — see log")
                    return

                def _done() -> None:
                    win.close()
                    voice.start()
                    sender.state = 1
                    _us.set_voice_enabled(True)
                    log.info("voice: installed and enabled")
                AppHelper.callAfter(_done)
            finally:
                self._voice_installing = False

        def _fail(phase: int, note: str) -> None:
            win.fail_phase(phase, note)
            detail = voice_install.last_failure_detail or "Unknown error"
            log.error("voice setup failed (phase %d): %s", phase, detail)

            def _show() -> None:
                win.close()
                _alert(
                    title="Voice setup failed",
                    message=f"{detail[:800]}\n\nClick Voice in the menu to retry.",
                    ok="OK",
                )
            AppHelper.callAfter(_show)

        win.show()
        threading.Thread(target=_worker, name="voitta-voice-setup", daemon=True).start()

    def show_settings(self, _sender) -> None:
        """Status dialog with the MCP-debug toggle.

        The bookmarklet's in-pane Settings panel is the place to edit
        API keys / providers / Google OAuth / per-plugin config. This
        tray dialog is for backend-level state and the privileged
        ``/mcp`` switch (which we don't want in the in-page UI because
        the bookmarklet runs on third-party origins).
        """
        try:
            self._show_settings_impl()
        except Exception:
            self._log.exception("show_settings raised")

    def _show_settings_impl(self) -> None:
        from app import activity
        from app.services import user_settings as _us

        tls_on = TLS_CERT_PATH.exists() and TLS_KEY_PATH.exists()
        active_now = activity.snapshot()

        rag_line = _rag_status_line()
        plugins_line = _plugins_status_line()
        mcp_line = _mcp_connectors_line()

        if active_now:
            preview = ", ".join(
                f"{a['detail']} ({a['category']})" for a in active_now[:3]
            )
            if len(active_now) > 3:
                preview += f" +{len(active_now) - 3} more"
            activity_line = f"Activity ·  ▶ {preview}"
        else:
            activity_line = "Activity ·  idle"

        mcp_on = _us.mcp_debug_enabled()
        tools_on = _us.mcp_tools_enabled()
        chat_on = _us.mcp_chat_enabled()
        mcp_url = f"{_server_url()}/mcp/"
        mcp_dbg_line = (
            f"MCP endpoint · URL: {mcp_url}\n"
            f"           Transport: streamable-http (loopback only)\n"
            f"           Debugging tools (mcp_*): {'on' if mcp_on else 'off'}\n"
            f"           Agent tools for external agents (vb_*): {'on' if tools_on else 'off'}\n"
            f"           Messages to the in-app agent: {'on' if chat_on else 'off'}"
        )

        body = (
            f"Server  ·  {_server_url()}  ·  {'TLS' if tls_on else 'HTTP only'}\n\n"
            f"{plugins_line}\n"
            f"{rag_line}\n"
            f"{mcp_line}\n"
            f"{activity_line}\n"
            f"{mcp_dbg_line}\n\n"
            f"Data folder\n  {_human_path(PROJECT_ROOT.parent)}\n\n"
            "Chat settings — API keys, model, theme, plugin config —\n"
            "live in the bookmarklet sidebar. Click the gear icon there."
        )

        response, (new_mcp, new_tools, new_chat) = _settings_alert_with_switches(
            title=APP_NAME,
            message=body,
            ok="Close",
            cancel=None,
            switches=[
                ("Enable MCP debugging tools", mcp_on),
                ("Expose agent tools to external agents", tools_on),
                ("Let external agents message the in-app agent", chat_on),
            ],
        )
        if new_mcp != mcp_on:
            _us.set_mcp_debug_enabled(new_mcp)
            self._log.info("MCP debug toggle: %s → %s", mcp_on, new_mcp)
        if new_tools != tools_on:
            _us.set_mcp_tools_enabled(new_tools)
            self._log.info("MCP agent-tools toggle: %s → %s", tools_on, new_tools)
        if new_chat != chat_on:
            _us.set_mcp_chat_enabled(new_chat)
            self._log.info("MCP in-app-agent toggle: %s → %s", chat_on, new_chat)
        _ = response  # rumps-style response — single-button dialog, ignored

    def open_workspace(self, _sender) -> None:
        # Open the Voitta page with ?workspace=1 so the frontend auto-opens
        # the workspace panel on load.
        webbrowser.open(f"{_server_url()}?workspace=1")

    def open_logs_folder(self, _sender) -> None:
        """Reveal logs/ in Finder so the user can drag the log files into an
        email for support. All logs live here — voitta.log (launcher),
        voitta-app.log (+ rotations), voitta-stacks.log — plus previous/run-*,
        archives of the last few runs. Each launch starts fresh (see
        __main__.py)."""
        logs_dir = Path(USER_DATA_ROOT) / "logs"
        try:
            logs_dir.mkdir(parents=True, exist_ok=True)
            subprocess.run(["open", str(logs_dir)], check=False)
        except Exception:
            self._log.exception("open logs folder failed")

    # ---- active sessions window ("tasks voitta") ---------------------------

    def _collect_sessions(self):
        """(sessions, active_id) for the window — connected first, newest
        first. Read straight from the in-process session registry."""
        try:
            from app.services import cl_sessions
            records = cl_sessions.snapshot()
            active_id = cl_sessions.active_session_id()
        except Exception:
            self._log.exception("collect sessions failed")
            return [], None
        # Connected sessions only, most-recently-seen first.
        live = [r for r in records if r.get("connected")]
        live.sort(key=lambda r: r.get("last_seen", 0.0), reverse=True)
        return live, active_id

    def show_sessions(self, _sender=None) -> None:
        """Menu callback — already on the main thread."""
        self._show_sessions_main()

    def _show_sessions_main(self) -> None:
        """Build (once) and show the sessions window. MAIN THREAD ONLY —
        NSWindow creation off the main thread is unsafe."""
        try:
            if self._sessions_window is None:
                from app.sessions_window import SessionsWindow
                self._sessions_window = SessionsWindow(
                    on_select=self._on_session_selected
                )
                self._sessions_window.set_refresh_handler(self._refresh_sessions)
                self._sessions_window.set_close_handler(self._on_sessions_closed)
            # Re-snapshot row order on each explicit open so numbering starts
            # fresh; it then stays frozen while the window is up.
            self._frozen_order = []
            sessions, active_id = self._collect_sessions()
            sessions = self._ordered_for_display(sessions)
            self._sessions_window.show(sessions, active_id)
            # Visibility drives the voice state machine → enter TASKS mode.
            try:
                from app.services import voice
                voice.enter_task_mode()
            except Exception:
                self._log.exception("enter task mode failed")
        except Exception:
            self._log.exception("show sessions window failed")

    def _close_sessions_window(self) -> None:
        """Close the sessions list (voice 'task quit'). MAIN THREAD."""
        win = self._sessions_window
        if win is not None and win.is_visible():
            win.close()  # → _on_sessions_closed via windowWillClose

    def _on_sessions_closed(self) -> None:
        """Sessions window closed (X button, 'task quit', click-away) — leave
        TASKS mode and drop the frozen order. Fires on the main thread."""
        self._frozen_order = []
        try:
            from app.services import voice
            voice.exit_task_mode()
        except Exception:
            self._log.exception("exit task mode failed")

    def _ordered_for_display(self, sessions):
        """Apply the frozen display order: rows already shown keep their slot;
        new sessions append at the bottom. Updates ``_frozen_order`` to match
        what's rendered so 'task N' resolves to row N."""
        pos = {sid: i for i, sid in enumerate(self._frozen_order)}
        known = sorted(
            (s for s in sessions if s.get("session_id") in pos),
            key=lambda s: pos[s.get("session_id")],
        )
        new = [s for s in sessions if s.get("session_id") not in pos]
        ordered = list(known) + new
        self._frozen_order = [s.get("session_id") for s in ordered]
        return ordered

    def _refresh_sessions(self) -> None:
        if self._sessions_window is None:
            return
        sessions, active_id = self._collect_sessions()
        sessions = self._ordered_for_display(sessions)
        # Skip the Cocoa rebuild unless something visible actually changed
        # (the 0.4s poller would otherwise rebuild rows continuously).
        sig = (active_id, tuple(
            (s.get("session_id"), s.get("title"), s.get("url"),
             s.get("host"), s.get("connected"))
            for s in sessions
        ))
        if sig == getattr(self, "_sessions_sig", None):
            return
        self._sessions_sig = sig
        self._sessions_window.update(sessions, active_id)

    def _on_session_selected(self, session_id: str) -> None:
        """Row click: make this session the active (voice-routed) one and
        bring its Chrome tab to the foreground.

        ``window.focus()`` from inside the page is sandboxed and can't raise a
        background tab, so we drive Chrome natively via AppleScript, matching
        on the session's tracked URL (title as a tiebreaker)."""
        try:
            from app.services import cl_sessions
            cl_sessions.set_active(session_id)
            self._log.info("sessions: active set to %s", session_id)
        except Exception:
            self._log.exception("set active session failed")
        # Raise the real browser tab from the native side (off the UI thread).
        try:
            from app.services import cl_sessions, chrome_focus
            info = cl_sessions.get(session_id)
            if info and info.url:
                chrome_focus.focus_tab_async(info.url, info.title)
            else:
                self._log.info("sessions: no URL for %s — can't raise tab", session_id)
        except Exception:
            self._log.exception("chrome focus failed")
        self._refresh_sessions()

    def recreate_certs(self, _sender) -> None:
        """Regenerate the TLS cert pair via mkcert.

        Resolves mkcert the same way first-run provisioning does: the
        binary bundled in the .app first, then PATH (GUI apps get
        launchd's minimal PATH, so brew installs are invisible here).
        """
        from app.certs import _mkcert_path

        mkcert = _mkcert_path()
        if mkcert is None:
            _alert(
                title="mkcert not installed",
                message=(
                    "Install with:\n\n  brew install mkcert\n  mkcert -install\n\n"
                    "Then re-run this menu item."
                ),
                ok="OK",
            )
            return
        confirm = _alert(
            title="Recreate TLS certificates?",
            message=(
                "Regenerates the local TLS cert pair via mkcert and "
                "installs the local CA into your trust store.\n\n"
                f"Cert location: {_human_path(TLS_CERT_PATH.parent)}\n\n"
                "Restart Voitta after this completes so uvicorn picks up "
                "the new cert."
            ),
            ok="Generate",
            cancel="Cancel",
        )
        if not confirm:
            return
        certs_dir = TLS_CERT_PATH.parent
        certs_dir.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.run(
                [mkcert, "-install"],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                [
                    mkcert,
                    "-cert-file", str(TLS_CERT_PATH),
                    "-key-file", str(TLS_KEY_PATH),
                    "127.0.0.1", "localhost",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            _alert(
                title="Cert generation failed",
                message=(exc.stderr or exc.stdout or str(exc))[:500],
                ok="OK",
            )
            return
        _alert(
            title="Certificates regenerated",
            message=(
                f"New cert pair at:\n  {_human_path(TLS_CERT_PATH)}\n\n"
                "Restart Voitta so uvicorn loads the new cert."
            ),
            ok="OK",
        )

    def rebuild(self, _sender) -> None:
        confirm = _alert(
            title="Rebuild Voitta?",
            message=(
                "Re-runs pip install and rebuilds all RAG indexes.\n\n"
                "The server will restart automatically when done.\n"
                "This may take a few minutes."
            ),
            ok="Rebuild",
            cancel="Cancel",
        )
        if not confirm:
            return
        with _uvicorn_lock:
            srv = _uvicorn_server
            bridge_srv = _uvicorn_plaintext_server
        if srv is not None:
            srv.should_exit = True
        if bridge_srv is not None:
            bridge_srv.should_exit = True

        from app.installer import force_rebuild_stamps
        force_rebuild_stamps()
        self._log.info("rebuild: stamps wiped, re-running setup")

        from PyObjCTools import AppHelper
        AppHelper.callAfter(_run_first_time_setup, self._log)

    def reset(self, _sender) -> None:
        """Wipe all runtime artefacts (rag indexes, settings, scripts state,
        python_storage) and restart. The source tree is never touched."""
        _data_dirs = [
            PROJECT_ROOT.parent / "rag",
            USER_DATA_ROOT / "scripts_state",
            USER_DATA_ROOT / "python_storage",
        ]
        from app.config import USER_SETTINGS_PATH
        _data_files = [USER_SETTINGS_PATH]
        items_listed = "\n  ".join(
            _human_path(p) for p in _data_dirs + _data_files if p.exists()
        ) or "(nothing to delete)"
        confirm = _alert(
            title=f"Reset {APP_NAME}?",
            message=(
                f"This deletes:\n  {items_listed}\n\n"
                "All settings, RAG indexes, snapshots, and per-script\n"
                "render state will be removed. This cannot be undone.\n\n"
                "Restart Voitta after this completes."
            ),
            ok="Reset",
            cancel="Cancel",
        )
        if not confirm:
            return
        for p in _data_dirs:
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
        for p in _data_files:
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass

    def quit_app(self, _sender) -> None:
        # rumps's default Quit bypasses our cleanup. Funnel through here
        # so we can shut uvicorn down explicitly in future revisions
        # (currently relies on daemon-thread reaping).
        try:
            from app.services import voice
            if voice.is_running():
                voice.stop(timeout=3.0)
        except Exception:
            pass
        rumps.quit_application()


def main() -> None:
    """Entry point: run the installer if needed, then start uvicorn + rumps."""
    log = logging.getLogger("voitta.desktop")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Dedicated, uvicorn-proof log for our own code. When uvicorn boots it
    # runs logging.config.dictConfig (on app import), which stops voitta.*
    # records from reaching the stderr→voitta.log redirect — so everything
    # after startup (voice errors, tool dispatch, session events) used to
    # vanish. A RotatingFileHandler attached directly to the "voitta"
    # logger is not touched by that dictConfig, so it keeps writing.
    try:
        from logging.handlers import RotatingFileHandler
        _logs_dir = Path(USER_DATA_ROOT) / "logs"
        _logs_dir.mkdir(parents=True, exist_ok=True)
        _app_log = _logs_dir / "voitta-app.log"
        _h = RotatingFileHandler(_app_log, maxBytes=5_000_000, backupCount=3)
        _h.setLevel(logging.DEBUG)
        _h.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s"
        ))
        # "voitta" alone isn't enough: the entire backend logs under "app.*"
        # (app.chainlit_app, app.routes.*, app.services.*) and those records
        # were lost after uvicorn's dictConfig — an incident left NO runtime
        # trace on disk. Attach the same file handler to every namespace we
        # care about.
        #
        # Our own namespaces run at DEBUG so install/startup/tool-dispatch
        # detail lands on disk for support (logs are wiped + archived each
        # launch, so verbosity has a bounded footprint). uvicorn stays at INFO
        # — DEBUG there is per-request access-log flood that would bury the
        # detail we actually want.
        _debug_ns = {"voitta", "app", "chainlit"}
        for _name in ("voitta", "app", "chainlit", "uvicorn", "uvicorn.error"):
            _l = logging.getLogger(_name)
            _l.setLevel(logging.DEBUG if _name in _debug_ns else logging.INFO)
            # Avoid duplicate handlers across reloads / re-entry.
            if not any(isinstance(h, RotatingFileHandler) for h in _l.handlers):
                _l.addHandler(_h)
        log.info("voitta-app.log handler installed at %s", _app_log)
    except Exception:
        log.exception("could not install voitta-app.log handler")

    # Post-mortem lever for a frozen/spinning backend: `kill -USR2 <pid>`
    # dumps every thread's Python stack to voitta-stacks.log. Costless at
    # runtime; invaluable when the event loop wedges and HTTP is dead.
    try:
        import faulthandler
        import signal as _signal
        _stacks_path = Path(USER_DATA_ROOT) / "logs" / "voitta-stacks.log"
        _stacks_path.parent.mkdir(parents=True, exist_ok=True)
        _stacks_fp = _stacks_path.open("a")  # kept open for process lifetime
        faulthandler.register(_signal.SIGUSR2, file=_stacks_fp, all_threads=True)
        log.info("SIGUSR2 stack-dump registered → %s", _stacks_path)
    except Exception:
        log.exception("could not register SIGUSR2 stack dump")

    # Chainlit reads CHAINLIT_APP_ROOT at module import time to set
    # FILES_DIRECTORY. Inside a frozen .app getcwd() returns "/" (read-only),
    # so set it early — before any chainlit import can happen.
    os.environ.setdefault("CHAINLIT_APP_ROOT", str(PROJECT_ROOT.parent))


    # The frozen .app bundles the frontend under voitta_bookmarklet/resources/.
    # Set VOITTA_FRONTEND_DIST so app.config.FRONTEND_DIST resolves correctly
    # before any import of app.config happens inside the uvicorn worker.
    _bundle_fe = Path(__file__).resolve().parents[2] / "voitta_bookmarklet" / "resources" / "frontend_dist"
    if _bundle_fe.is_dir():
        os.environ.setdefault("VOITTA_FRONTEND_DIST", str(_bundle_fe))

    # Recover the user's real PATH. Launched from Finder/Dock we inherit
    # launchd's bare /usr/bin:/bin:/usr/sbin:/sbin — no login shell runs, so
    # pyenv, Homebrew, nvm and ~/.local/bin are all invisible, and python3
    # resolves to macOS's 3.9. Must run before the installer (it shells out to
    # pip and git) and before anything caches a shutil.which() result.
    try:
        from app.shell_env import augment_path
        augment_path()
    except Exception:
        log.exception("shell PATH augmentation raised")

    # Wipe userbase/ + rag/ when the app version has changed.
    try:
        from app.installer import ensure_fresh_deploy
        ensure_fresh_deploy(log)
    except Exception:
        log.exception("ensure_fresh_deploy raised")

    from app.certs import is_present as certs_present
    from app.installer import is_complete as packages_complete, lib_sources_need_update
    from app.rag_build import is_built as rag_built

    needs_setup = (
        not certs_present()
        or not packages_complete()
        or lib_sources_need_update()
        or not rag_built()
    )

    # Start the menu-bar app first so the V icon appears immediately.
    # Both branches schedule their work via AppHelper.callAfter so they
    # fire after the Cocoa runloop starts.
    from PyObjCTools import AppHelper

    if needs_setup:
        global _installing
        _installing = True
        AppHelper.callAfter(_run_first_time_setup, log)
    else:
        AppHelper.callAfter(_start_uvicorn, log)

    VoittaMenuBarApp().run()


def _run_first_time_setup(log: logging.Logger) -> None:
    """Show the 3-phase installer window and run setup on a worker thread.

    The window is shown by scheduling via ``AppHelper.callAfter`` so it
    fires after the rumps/NSApp runloop starts. Uvicorn is launched only
    after all 3 phases complete successfully.
    """
    from PyObjCTools import AppHelper
    from app.install_window import InstallWindow, show_error_alert

    win = InstallWindow()

    def _worker() -> None:
        # Phase 0 — Certificates
        win.start_phase(0)
        try:
            from app.certs import provision_with_progress
            ok = provision_with_progress(lambda msg: win.log(msg))
        except Exception as exc:
            ok = False
            win.log(f"!!! cert error: {exc}")
            log.exception("cert provisioning raised")
        if ok:
            win.finish_phase(0)
        else:
            # No insecure fallback: if the local HTTPS CA can't be installed and
            # trusted, abort startup rather than silently serving over HTTP.
            win.fail_phase(0, "TLS setup failed — see log")
            log.error("TLS certificate provisioning failed — aborting startup")
            AppHelper.callAfter(
                show_error_alert,
                "TLS certificate setup failed.\n\nThe local HTTPS CA could not "
                "be installed and trusted, so the app will not start (no "
                "insecure fallback).\n\nSee ~/Library/Application Support/Voitta "
                "Compute/backend/logs/voitta-app.log",
            )
            AppHelper.callAfter(__import__("rumps").quit_application)
            return

        # Phase 1 — Python packages
        win.start_phase(1, "Preparing…")
        from app.installer import install_all, last_failure_detail, is_complete

        def _pkg_progress(current, total, label, log_line):
            win.update_phase(1, current, total, label)
            if log_line:
                win.log(log_line)

        if is_complete():
            win.skip_phase(1, "Already installed")
        else:
            ok = install_all(_pkg_progress)
            if ok:
                win.finish_phase(1)
            else:
                import app.installer as _inst
                win.fail_phase(1, "Install failed — see log")
                AppHelper.callAfter(
                    show_error_alert,
                    f"Package install failed:\n\n{_inst.last_failure_detail or 'Unknown error'}",
                )
                AppHelper.callAfter(__import__("rumps").quit_application)
                return

        # Phase 2 — Source libraries (shallow-clone submodules) + plugin docs
        win.start_phase(2, "Cloning source libraries…")
        from app.installer import (
            clone_lib_sources,
            lib_sources_need_update,
            sync_plugin_docs,
        )

        lib_failed = False
        if not lib_sources_need_update():
            win.log("lib-sources: already up to date")
        else:
            if not clone_lib_sources(lambda msg: win.log(msg)):
                import app.installer as _inst2
                log.warning("lib-sources clone failed: %s", _inst2.last_failure_detail)
                lib_failed = True

        # Third-party docs declared by plugin manifests. Must run before the
        # RAG phase: it is what makes freshly-fetched docs change the docs
        # content hash, which is what un-skips the index build below.
        try:
            sync_plugin_docs(lambda msg: win.log(msg))
        except Exception:
            log.exception("plugin-docs sync raised")
            win.log("plugin-docs: sync failed — continuing without them")

        if lib_failed:
            win.fail_phase(2, "Clone failed — RAG code corpus unavailable")
        else:
            win.finish_phase(2)

        # Phase 3 — RAG indexes
        win.start_phase(3, "Indexing documentation and source code…")
        from app.rag_build import build_all, is_built

        rag_ok = True
        if is_built():
            win.skip_phase(3, "Indexes up to date")
        else:
            def _rag_progress(line: str) -> None:
                log.info("rag: %s", line)
                win.log(line)

            ok = build_all(_rag_progress)
            if ok:
                win.finish_phase(3)
            else:
                import app.rag_build as _rb
                detail = _rb.last_failure_detail or "unknown error"
                log.warning("RAG build failed: %s", detail)
                win.fail_phase(3, "RAG build failed — see log below")
                rag_ok = False

        # Mark deployment complete and start serving.
        try:
            from app.installer import mark_deploy_complete
            mark_deploy_complete()
        except Exception:
            pass

        if not rag_ok:
            # Keep the window visible so the user can read the error.
            # Add a Continue button via callAfter so they can dismiss and proceed.
            from PyObjCTools import AppHelper as _AH
            def _show_rag_error():
                win.log("─" * 60)
                win.log("RAG build failed. Search will not work this session.")
                win.log("The app will start anyway. You can retry via the menu.")
                win.add_continue_button(lambda: (win.close(), _start_uvicorn(log)))
            _AH.callAfter(_show_rag_error)
        else:
            # win.close() and _start_uvicorn must run on the main AppKit thread.
            from PyObjCTools import AppHelper as _AH
            _AH.callAfter(win.close)
            _AH.callAfter(_start_uvicorn, log)

    # Schedule: show window first, then kick the worker thread.
    def _kick() -> None:
        win.show()
        t = threading.Thread(target=_worker, name="voitta-setup", daemon=True)
        t.start()

    AppHelper.callAfter(_kick)


if __name__ == "__main__":
    main()
