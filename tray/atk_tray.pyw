"""
ATK Battery - Windows tray app for ATK A9-family mice.

  * tray icon shows battery % (green / amber / red, bolt while charging)
  * click (left or right) for the menu: polling rate, refresh, diagnostics
  * serves read-only status on http://127.0.0.1:<port>/status for the
    Stream Deck plugin

Run:  pythonw atk_tray.pyw          (normal)
      python  atk_tray.pyw --probe  (print a diagnostics dump and exit)
"""

import json
import logging
import logging.handlers
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_NAME = "ATK Battery"
APP_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "ATKBattery")
os.makedirs(APP_DIR, exist_ok=True)
SETTINGS_PATH = os.path.join(APP_DIR, "settings.json")
LOG_PATH = os.path.join(APP_DIR, "atk-battery.log")

DEFAULTS = {
    "port": 47231,
    "poll_seconds": 60,          # battery read interval while awake
    "asleep_retry_seconds": 15,  # retry interval while the mouse isn't answering
    "low_battery_alert": 15,     # notify once when it drops to this (0 = off)
    "debug": False,              # log raw frames
}

log = logging.getLogger("atk")


# ---------------------------------------------------------------- settings --

def load_settings():
    s = dict(DEFAULTS)
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as fh:
            s.update(json.load(fh))
    except FileNotFoundError:
        save_settings(s)
    except Exception as e:
        log.warning("settings unreadable, using defaults: %s", e)
    return s


def save_settings(s):
    try:
        with open(SETTINGS_PATH, "w", encoding="utf-8") as fh:
            json.dump(s, fh, indent=2)
    except Exception as e:
        log.warning("could not save settings: %s", e)


def setup_logging(debug):
    root = logging.getLogger()
    root.handlers.clear()
    h = logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=2,
                                             encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(h)
    if sys.stdout and sys.stdout.isatty():
        root.addHandler(logging.StreamHandler(sys.stdout))
    root.setLevel(logging.DEBUG if debug else logging.INFO)


# ------------------------------------------------------------------- state --

class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.connected = False      # receiver/cable found and opened
        self.awake = False          # last request was answered
        self.device = None
        self.percent = None
        self.charging = None
        self.millivolts = None
        self.polling_hz = None
        self.max_polling_hz = 8000
        self.updated = None         # time.time() of last good battery read
        self.message = "Looking for mouse..."

    def snapshot(self):
        with self.lock:
            age = None if self.updated is None else round(time.time() - self.updated, 1)
            return {
                "app": "atk-battery",
                "connected": self.connected,
                "awake": self.awake,
                "device": self.device,
                "percent": self.percent,
                "charging": self.charging,
                "millivolts": self.millivolts,
                "polling_hz": self.polling_hz,
                "updated": self.updated,
                "age_s": age,
                "message": self.message,
            }


# ------------------------------------------------------------------ worker --

class Worker(threading.Thread):
    def __init__(self, state, settings, on_change, notify):
        super().__init__(daemon=True, name="atk-worker")
        import atk_device
        self.dev_mod = atk_device
        self.mouse = atk_device.AtkMouse()
        self.state = state
        self.settings = settings
        self.on_change = on_change
        self.notify = notify
        self.wake = threading.Event()
        self.stop_flag = False
        self.last_poll_read = 0.0
        self.alerted_low = False

    def refresh_now(self):
        self.wake.set()

    def _set(self, **kw):
        with self.state.lock:
            for k, v in kw.items():
                setattr(self.state, k, v)
        self.on_change()

    def run(self):
        while not self.stop_flag:
            delay = self.tick()
            self.wake.wait(delay)
            self.wake.clear()

    def tick(self):
        D = self.dev_mod
        s = self.settings
        if not self.mouse.connected:
            try:
                ok = self.mouse.connect()
            except D.NoAnswer:
                self._set(connected=True, awake=False,
                          message="Mouse asleep or off - move it to wake")
                return s["asleep_retry_seconds"]
            except Exception as e:
                log.exception("connect error")
                self._set(connected=False, awake=False, message=f"Error: {e}")
                return 10
            if not ok:
                self._set(connected=False, awake=False, percent=None, charging=None,
                          millivolts=None, polling_hz=None, device=None,
                          message="Receiver not found")
                return 10
            self._set(connected=True, device=self.mouse.name,
                      max_polling_hz=self.mouse.max_polling_hz())
            self.last_poll_read = 0.0

        try:
            b = self.mouse.read_battery()
        except D.NoAnswer:
            self._set(awake=False, message="Mouse asleep - showing last reading")
            return s["asleep_retry_seconds"]
        except D.NotConnected:
            self.mouse.close()
            self._set(connected=False, awake=False, message="Receiver disconnected")
            return 3
        except Exception as e:
            log.warning("battery read failed: %s", e)
            self._set(awake=False, message=f"Read error: {e}")
            return s["asleep_retry_seconds"]

        self._set(connected=True, awake=True, percent=b["percent"], charging=b["charging"],
                  millivolts=b["millivolts"], updated=time.time(), message="OK")
        log.info("battery %s%% charging=%s mV=%s", b["percent"], b["charging"], b["millivolts"])
        self._low_battery_check(b)

        if time.time() - self.last_poll_read > 300:
            try:
                hz = self.mouse.read_polling()
                self._set(polling_hz=hz)
                self.last_poll_read = time.time()
            except Exception as e:
                log.warning("polling read failed: %s", e)
        return s["poll_seconds"]

    def _low_battery_check(self, b):
        threshold = self.settings.get("low_battery_alert", 0)
        if not threshold:
            return
        if b["charging"] or b["percent"] > threshold + 5:
            self.alerted_low = False
        elif b["percent"] <= threshold and not self.alerted_low:
            self.alerted_low = True
            self.notify(f"Mouse battery at {b['percent']}%", "Time to plug it in.")

    def set_polling(self, hz):
        """Called from a menu thread."""
        D = self.dev_mod
        try:
            if not self.mouse.connected:
                raise D.NotConnected("mouse not connected")
            got = self.mouse.set_polling(hz)
            self._set(polling_hz=got, awake=True)
            self.last_poll_read = time.time()
            if got != hz:
                self.notify("Polling rate not changed",
                            f"The mouse kept {got} Hz. Your receiver may not support {hz} Hz.")
            else:
                log.info("polling rate set to %s Hz", hz)
        except D.NoAnswer:
            self.notify("Mouse didn't answer", "Move the mouse to wake it, then try again.")
        except Exception as e:
            log.exception("set polling failed")
            self.notify("Couldn't change polling rate", str(e))


# -------------------------------------------------------------- http server --

def make_server(state, port):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            # Host check blocks DNS-rebinding pages from reading this.
            if self.headers.get("Host") not in allowed_hosts:
                self.send_error(403)
                return
            if self.path.split("?")[0] != "/status":
                self.send_error(404)
                return
            body = json.dumps(state.snapshot()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.daemon_threads = True
    return srv


# ---------------------------------------------------------------- autostart --

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _launch_command():
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    exe = sys.executable
    if exe.lower().endswith("python.exe"):
        exe = exe[:-10] + "pythonw.exe"
    return f'"{exe}" "{os.path.abspath(__file__)}"'


def autostart_enabled():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP_NAME)
            return True
    except Exception:
        return False


def set_autostart(on):
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, _launch_command())
        else:
            try:
                winreg.DeleteValue(k, APP_NAME)
            except FileNotFoundError:
                pass


def message_box(text, title=APP_NAME):
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, title, 0x40)
    except Exception:
        print(text)


# --------------------------------------------------------------------- tray --

def run_tray(settings):
    import pystray
    from pystray import Menu, MenuItem as Item
    import atk_protocol as p
    import atk_device
    from icons import tray_icon

    state = State()

    class ClickMenuIcon(pystray.Icon):
        """Left click opens the same menu as right click (Windows backend)."""
        def _on_notify(self, wparam, lparam):
            try:
                from pystray._util import win32
                if lparam == win32.WM_LBUTTONUP:
                    lparam = win32.WM_RBUTTONUP
            except Exception:
                pass
            return super()._on_notify(wparam, lparam)

    icon_cls = ClickMenuIcon if hasattr(pystray.Icon, "_on_notify") else pystray.Icon
    icon = None
    last_drawn = {"key": None}

    def tooltip(snap):
        name = snap["device"] or "ATK mouse"
        if snap["percent"] is None:
            return f"{name}: {snap['message']}"[:120]
        parts = [f"{name}: {snap['percent']}%"]
        if snap["charging"]:
            parts.append("charging")
        if not snap["awake"]:
            parts.append("asleep")
        if snap["polling_hz"]:
            parts.append(f"{snap['polling_hz']} Hz")
        return " - ".join(parts)[:120]

    def redraw():
        if icon is None:
            return
        snap = state.snapshot()
        stale = not snap["awake"]
        key = (snap["percent"], bool(snap["charging"]), stale)
        if key != last_drawn["key"]:
            icon.icon = tray_icon(snap["percent"], bool(snap["charging"]), stale)
            last_drawn["key"] = key
        icon.title = tooltip(snap)
        try:
            icon.update_menu()
        except Exception:
            pass

    def notify(title, text):
        try:
            icon.notify(text, title)
        except Exception:
            log.info("notify: %s - %s", title, text)

    worker = Worker(state, settings, redraw, notify)

    # -- menu builders
    def status_text(_):
        snap = state.snapshot()
        if snap["percent"] is None:
            return snap["message"]
        bits = [f"{snap['percent']}%"]
        bits.append("charging" if snap["charging"] else "on battery")
        if snap["millivolts"]:
            bits.append(f"{snap['millivolts']:,} mV")
        if not snap["awake"]:
            bits.append("asleep")
        return " · ".join(bits)

    def device_text(_):
        return state.snapshot()["device"] or "No mouse found"

    def rate_item(hz):
        def action(_icon, _item):
            threading.Thread(target=worker.set_polling, args=(hz,), daemon=True).start()

        def checked(_item):
            return state.snapshot()["polling_hz"] == hz

        def enabled(_item):
            snap = state.snapshot()
            return snap["connected"] and hz <= state.max_polling_hz

        label = f"{hz:,} Hz" if hz < 1000 else f"{hz // 1000}K Hz ({hz:,})"
        return Item(label, action, checked=checked, radio=True, enabled=enabled)

    def toggle_autostart(_icon, _item):
        try:
            set_autostart(not autostart_enabled())
        except Exception as e:
            notify("Couldn't change startup setting", str(e))

    def toggle_debug(_icon, _item):
        settings["debug"] = not settings.get("debug")
        save_settings(settings)
        logging.getLogger().setLevel(logging.DEBUG if settings["debug"] else logging.INFO)

    def run_diagnostics(_icon, _item):
        def go():
            path = os.path.join(APP_DIR, "diagnostics.txt")
            with atk_device_lock():
                text = atk_device.probe_report()
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text + "\n\nstate: " + json.dumps(state.snapshot(), indent=2))
            os.startfile(path)
        threading.Thread(target=go, daemon=True).start()

    def atk_device_lock():
        # pause the worker's handle while probing so they don't collide
        class _Ctx:
            def __enter__(self_):
                worker.mouse._lock.acquire()
                worker.mouse.close()

            def __exit__(self_, *a):
                worker.mouse._lock.release()
                worker.refresh_now()
        return _Ctx()

    def open_folder(_icon, _item):
        os.startfile(APP_DIR)

    def quit_app(_icon, _item):
        worker.stop_flag = True
        worker.wake.set()
        icon.stop()

    menu = Menu(
        Item(device_text, None, enabled=False),
        Item(status_text, None, enabled=False),
        Menu.SEPARATOR,
        Item("Polling rate", Menu(*[rate_item(hz) for hz in p.ALL_RATES])),
        Item("Refresh now", lambda i, it: worker.refresh_now()),
        Menu.SEPARATOR,
        Item("Start with Windows", toggle_autostart, checked=lambda it: autostart_enabled()),
        Item("Debug logging", toggle_debug, checked=lambda it: bool(settings.get("debug"))),
        Item("Run diagnostics", run_diagnostics),
        Item("Open log folder", open_folder),
        Menu.SEPARATOR,
        Item("Quit", quit_app),
    )

    # HTTP status endpoint (also acts as the single-instance guard)
    try:
        server = make_server(state, int(settings["port"]))
    except OSError:
        message_box(f"Port {settings['port']} is already in use - is {APP_NAME} already running?\n\n"
                    f"If another program needs that port, change \"port\" in\n{SETTINGS_PATH}")
        return
    threading.Thread(target=server.serve_forever, daemon=True, name="http").start()

    icon = icon_cls("atk_battery", tray_icon(None), APP_NAME, menu)

    def setup(ic):
        ic.visible = True
        worker.start()

    icon.run(setup=setup)
    server.shutdown()


# --------------------------------------------------------------------- main --

def main():
    settings = load_settings()
    setup_logging(settings.get("debug"))
    if "--probe" in sys.argv:
        import atk_device
        print(atk_device.probe_report())
        return
    log.info("starting %s v1.1 (fresh handle per request)", APP_NAME)
    try:
        run_tray(settings)
    except Exception as e:
        log.exception("fatal")
        message_box(f"{APP_NAME} stopped:\n{e}\n\nLog: {LOG_PATH}")


if __name__ == "__main__":
    main()
