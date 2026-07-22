"""AERO X16 keyboard light — set color and brightness.

The keyboard controller (VID 0414, PID 8104) exposes a standard HID LampArray
interface (usage page 0x59) with a single 1-zone RGB lamp. This app takes host
control of the lamp and sets its color; brightness is applied by scaling the
RGB values (the lamp's intensity channel is binary).

Run without arguments for the GUI. Run with --apply to silently re-apply the
last saved color (for a Startup shortcut) and exit.
"""

import colorsys
import ctypes
import json
import os
import struct
import subprocess
import sys
import threading
import tkinter as tk
from ctypes import wintypes
from pathlib import Path
from tkinter import colorchooser

import hid
import pystray
from PIL import Image

VID, PID = 0x0414, 0x8104
LAMPARRAY_USAGE_PAGE = 0x59

# Max Fan toggle drives Gigabyte's ACPI-WMI fan control, which needs admin.
# setup_fan_task.ps1 registers these scheduled tasks (highest privileges) once;
# triggering them with `schtasks /run` then avoids a UAC prompt per click.
FAN_MAX_TASK = "AeroFanMax"
FAN_NORMAL_TASK = "AeroFanNormal"
CREATE_NO_WINDOW = 0x08000000

APP_NAME = "KeyboardLight"


def app_data_dir():
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    d = Path(base) / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def resource_path(name):
    """Locate a bundled data file, whether running from source or as a
    PyInstaller-frozen exe (where data lives in the _MEIPASS temp dir)."""
    base = getattr(sys, "_MEIPASS", None)
    return Path(base) / name if base else Path(__file__).with_name(name)


# Config is written at runtime, so it must live in a writable place (the frozen
# exe's own dir may be read-only / temporary). Assets are read-only and bundled.
CONFIG_PATH = app_data_dir() / "config.json"
FEATHER_PNG = resource_path("feather.png")
FEATHER_ICO = resource_path("feather.ico")
MOF_NAME = "aero_fan.mof"

PRESETS = [
    ("#ffffff", "White"),
    ("#ff0000", "Red"),
    ("#ff8000", "Orange"),
    ("#00ff00", "Green"),
    ("#00ffff", "Cyan"),
    ("#0060ff", "Blue"),
    ("#a020f0", "Purple"),
    ("#ff00aa", "Pink"),
]


def find_lamparray_path():
    for d in hid.enumerate(VID, PID):
        if d["usage_page"] == LAMPARRAY_USAGE_PAGE:
            return d["path"]
    return None


def apply_color(rgb, brightness):
    """Set the keyboard to rgb (0-255 each) scaled by brightness (0-100)."""
    path = find_lamparray_path()
    if path is None:
        raise OSError("Keyboard LampArray device not found (VID 0414 PID 8104).")
    h = hid.device()
    h.open_path(path)
    try:
        _send_color(h, rgb, brightness)
    finally:
        h.close()


def _send_color(dev, rgb, brightness):
    r, g, b = (round(c * brightness / 100) for c in rgb)
    # LampArrayControl (report 6): AutonomousMode = 0 -> host controls the lamp
    dev.send_feature_report(bytes([6, 0]))
    # LampRangeUpdate (report 5): flags=1 (update complete), lamps 0..0, RGBI
    dev.send_feature_report(bytes([5, 1]) + struct.pack("<HH", 0, 0) + bytes([r, g, b, 255]))


class Lamp:
    """Writes colors to the keyboard from a background thread so HID I/O never
    blocks the UI (device enumeration + feature reports take tens of ms, which
    made the window stutter, especially in rainbow mode). Keeps the device open
    between writes; only the most recent request is applied."""

    def __init__(self):
        self._cond = threading.Condition()
        self._pending = None
        self._dev = None
        threading.Thread(target=self._run, daemon=True).start()

    def set(self, rgb, brightness):
        with self._cond:
            self._pending = (tuple(rgb), brightness)
            self._cond.notify()

    def _run(self):
        while True:
            with self._cond:
                while self._pending is None:
                    self._cond.wait()
                rgb, brightness = self._pending
                self._pending = None
            self._write(rgb, brightness)

    def _write(self, rgb, brightness):
        for attempt in (0, 1):
            try:
                if self._dev is None:
                    path = find_lamparray_path()
                    if path is None:
                        return
                    dev = hid.device()
                    dev.open_path(path)
                    self._dev = dev
                _send_color(self._dev, rgb, brightness)
                return
            except OSError:
                # Stale handle (e.g. after sleep): drop it and retry once fresh
                if self._dev is not None:
                    try:
                        self._dev.close()
                    except OSError:
                        pass
                    self._dev = None


def start_listener(on_up, on_down, on_resume):
    """Background thread with a hidden window that receives Ctrl+Up / Ctrl+Down
    global hotkeys and system resume-from-sleep notifications."""
    WM_HOTKEY, WM_POWERBROADCAST = 0x0312, 0x0218
    PBT_APMRESUMESUSPEND, PBT_APMRESUMEAUTOMATIC = 0x7, 0x12
    MOD_CONTROL, VK_UP, VK_DOWN = 0x0002, 0x26, 0x28

    def loop():
        user32 = ctypes.windll.user32
        LRESULT = ctypes.c_ssize_t
        WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, ctypes.c_uint,
                                     wintypes.WPARAM, wintypes.LPARAM)
        user32.DefWindowProcW.restype = LRESULT
        user32.DefWindowProcW.argtypes = [wintypes.HWND, ctypes.c_uint,
                                          wintypes.WPARAM, wintypes.LPARAM]

        def wndproc(hwnd, msg, wparam, lparam):
            if msg == WM_HOTKEY:
                (on_up if wparam == 1 else on_down)()
            elif msg == WM_POWERBROADCAST and wparam in (
                    PBT_APMRESUMESUSPEND, PBT_APMRESUMEAUTOMATIC):
                on_resume()
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        proc = WNDPROC(wndproc)  # keep a reference so it isn't GC'd

        class WNDCLASS(ctypes.Structure):
            _fields_ = [("style", ctypes.c_uint), ("lpfnWndProc", WNDPROC),
                        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                        ("hInstance", wintypes.HANDLE), ("hIcon", wintypes.HANDLE),
                        ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HANDLE),
                        ("lpszMenuName", ctypes.c_wchar_p), ("lpszClassName", ctypes.c_wchar_p)]

        wc = WNDCLASS(0, proc, 0, 0, None, None, None, None, None, "KeyboardLightListener")
        user32.RegisterClassW(ctypes.byref(wc))
        # A real (never-shown) top-level window: message-only windows do not
        # receive WM_POWERBROADCAST.
        hwnd = user32.CreateWindowExW(0, wc.lpszClassName, "KeyboardLightListener",
                                      0, 0, 0, 0, 0, None, None, None, None)
        try:
            # Ensure resume notifications on Modern Standby machines
            user32.RegisterSuspendResumeNotification(hwnd, 0)  # DEVICE_NOTIFY_WINDOW_HANDLE
        except Exception:
            pass
        user32.RegisterHotKey(hwnd, 1, MOD_CONTROL, VK_UP)
        user32.RegisterHotKey(hwnd, 2, MOD_CONTROL, VK_DOWN)

        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    threading.Thread(target=loop, daemon=True).start()


def load_config():
    try:
        cfg = json.loads(CONFIG_PATH.read_text())
        rgb = [max(0, min(255, int(c))) for c in cfg["color"]]
        brightness = max(0, min(100, int(cfg["brightness"])))
        return rgb, brightness, bool(cfg.get("rainbow", False))
    except Exception:
        return [255, 255, 255], 100, False


def save_config(rgb, brightness, rainbow=False):
    CONFIG_PATH.write_text(json.dumps(
        {"color": list(rgb), "brightness": brightness, "rainbow": rainbow}))


def fan_apply(mode, duty=100):
    """Drive the Gigabyte ACPI-WMI fan control (root\\WMI GB_WMIACPI_Set).
    Recipe reversed from GiMATE's SetFanMax / SetFanNormal. Needs admin, so
    this runs from an elevated scheduled task, not the GUI process directly."""
    import wmi
    inst = wmi.WMI(namespace="root/WMI").GB_WMIACPI_Set()[0]

    def f(method, value):
        getattr(inst, method)(Data=value)

    if mode == "max":
        f("SetCurrentFanStep", 0)      # clear any step index
        f("SetAutoFanStatus", 0)       # disable the auto/dynamic governor
        f("SetFixedFanSpeed", duty)    # CPU-side fans to fixed duty
        f("SetGPUFanDuty", duty)       # GPU-side fans to fixed duty
        f("SetStepFanStatus", 1)       # enable manual step/fixed mode
        f("SetFixedFanStatus", 1)      # lock the fixed duty in
    else:
        f("SetCurrentFanStep", 0)
        f("SetFixedFanStatus", 0)      # release fixed lock
        f("SetStepFanStatus", 0)       # leave manual mode
        f("SetAutoFanStatus", 0)       # hand control back to firmware default


def run_fan_task(task):
    """Trigger a fan scheduled task. Returns None on success or an error string
    (e.g. the task hasn't been registered yet by setup_fan_task.ps1)."""
    try:
        r = subprocess.run(["schtasks", "/run", "/tn", task],
                           creationflags=CREATE_NO_WINDOW,
                           capture_output=True, text=True, timeout=10)
    except Exception as e:  # schtasks missing, timeout, etc.
        return str(e)
    if r.returncode != 0:
        out = (r.stderr or r.stdout or "").strip()
        if "does not exist" in out.lower() or "cannot find" in out.lower():
            return "setup"
        return out or f"schtasks exited {r.returncode}"
    return None


class App:
    def __init__(self, root):
        self.root = root
        root.title("Keyboard Light")
        root.resizable(False, False)

        self.rgb, brightness, self.rainbow = load_config()
        self.lamp = Lamp()
        self._rainbow_hue = 0.0
        self._rainbow_job = None

        body = tk.Frame(root, padx=16, pady=14)
        body.pack()

        self.swatch = tk.Canvas(body, width=260, height=56, highlightthickness=1,
                                highlightbackground="#999")
        self.swatch.pack()
        self.swatch.bind("<Button-1>", lambda e: self.pick_color())

        presets = tk.Frame(body)
        presets.pack(pady=(10, 0))
        for hex_color, name in PRESETS:
            sw = tk.Canvas(presets, width=24, height=24, bg=hex_color,
                           highlightthickness=1, highlightbackground="#999",
                           cursor="hand2")
            sw.pack(side=tk.LEFT, padx=3)
            sw.bind("<Button-1>", lambda e, c=hex_color: self.set_hex(c))

        tk.Button(body, text="Pick color…", command=self.pick_color).pack(pady=(10, 0), fill=tk.X)

        # Rainbow pulse toggle: a hue-gradient strip, no text. Blue border = active.
        self.rainbow_btn = tk.Canvas(body, width=260, height=26, cursor="hand2",
                                     highlightthickness=2, highlightbackground="#999")
        for px in range(260):
            c = colorsys.hsv_to_rgb(px / 260, 1, 1)
            self.rainbow_btn.create_line(px, 0, px, 26,
                                         fill="#{:02x}{:02x}{:02x}".format(
                                             *(round(v * 255) for v in c)))
        self.rainbow_btn.pack(pady=(10, 0))
        self.rainbow_btn.bind("<Button-1>", lambda e: self.toggle_rainbow())

        tk.Label(body, text="Brightness").pack(pady=(12, 0), anchor=tk.W)
        self.brightness = tk.Scale(body, from_=0, to=100, orient=tk.HORIZONTAL,
                                   length=260, showvalue=True, command=self.on_slide)
        self.brightness.set(brightness)
        self.brightness.pack()

        tk.Button(body, text="Lights off", command=self.lights_off).pack(pady=(12, 0), fill=tk.X)

        # Max Fan toggle: one button to slam the fans to full for gaming.
        self.fan_on = False
        self._fan_default_bg = None
        self.fan_btn = tk.Button(body, text="Max Fan: OFF", command=self.toggle_fan)
        self.fan_btn.pack(pady=(6, 0), fill=tk.X)
        self._fan_default_bg = self.fan_btn.cget("background")

        self._slide_job = None
        self.tray = self.make_tray()
        self.tray.run_detached()
        root.protocol("WM_DELETE_WINDOW", self.hide_to_tray)
        start_listener(on_up=lambda: root.after(0, self.step_brightness, +1),
                       on_down=lambda: root.after(0, self.step_brightness, -1),
                       on_resume=lambda: root.after(1500, self.reapply_after_resume))
        self.refresh_swatch()
        if self.rainbow:
            self.start_rainbow()
        else:
            self.apply()

    BRIGHTNESS_STEPS = [0, 5, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100]

    def step_brightness(self, direction):
        """Move brightness to the next step in the pressed direction. A value
        set off-step (e.g. via the slider) first snaps to the nearest step
        that direction."""
        b = self.brightness.get()
        if direction > 0:
            nxt = next((s for s in self.BRIGHTNESS_STEPS if s > b), 100)
        else:
            nxt = next((s for s in reversed(self.BRIGHTNESS_STEPS) if s < b), 0)
        self.brightness.set(nxt)

    # -- UI actions ------------------------------------------------------

    def pick_color(self):
        rgb, _ = colorchooser.askcolor(color=tuple(self.rgb), title="Keyboard color")
        if rgb:
            self.stop_rainbow()
            self.rgb = [int(c) for c in rgb]
            if self.brightness.get() == 0:
                self.brightness.set(100)
            self.refresh_swatch()
            self.apply()

    def set_hex(self, hex_color):
        self.stop_rainbow()
        self.rgb = [int(hex_color[i:i + 2], 16) for i in (1, 3, 5)]
        if self.brightness.get() == 0:
            self.brightness.set(100)
        self.refresh_swatch()
        self.apply()

    def on_slide(self, _value):
        # Debounce so dragging the slider doesn't flood the controller
        if self._slide_job:
            self.root.after_cancel(self._slide_job)
        self._slide_job = self.root.after(60, self.apply)

    def lights_off(self):
        self.stop_rainbow()
        self.brightness.set(0)
        self.apply()

    # -- max fan ---------------------------------------------------------

    def toggle_fan(self):
        self.set_fan(not self.fan_on)

    def set_fan(self, on):
        err = run_fan_task(FAN_MAX_TASK if on else FAN_NORMAL_TASK)
        if err == "setup":
            from tkinter import messagebox
            messagebox.showinfo(
                "Max Fan setup needed",
                "The fan control tasks aren't registered yet.\n\n"
                "Right-click PowerShell -> Run as administrator, then run:\n"
                "  setup_fan_task.ps1\n\n"
                "(in the keyboard app folder). One time only.")
            return
        if err:
            from tkinter import messagebox
            messagebox.showerror("Max Fan", f"Could not change fan mode:\n{err}")
            return
        self.fan_on = on
        self.update_fan_btn()
        if hasattr(self, "tray"):
            self.tray.update_menu()

    def update_fan_btn(self):
        if self.fan_on:
            self.fan_btn.configure(text="Max Fan: ON", bg="#0078d7", fg="white",
                                   activebackground="#1a86e0", activeforeground="white")
        else:
            self.fan_btn.configure(text="Max Fan: OFF", bg=self._fan_default_bg,
                                   fg="black", activebackground=self._fan_default_bg,
                                   activeforeground="black")

    # -- rainbow pulse -----------------------------------------------------

    def toggle_rainbow(self):
        if self.rainbow:
            self.stop_rainbow()
            self.apply()
        else:
            self.start_rainbow()

    def start_rainbow(self):
        self.rainbow = True
        self.rainbow_btn.configure(highlightbackground="#0078d7")
        save_config(self.rgb, self.brightness.get(), True)
        if self._rainbow_job:
            self.root.after_cancel(self._rainbow_job)
        self.tick_rainbow()

    def stop_rainbow(self):
        self.rainbow = False
        self.rainbow_btn.configure(highlightbackground="#999")

    def tick_rainbow(self):
        if not self.rainbow:
            return
        self._rainbow_hue = (self._rainbow_hue + 0.008) % 1.0
        rgb = [round(c * 255) for c in colorsys.hsv_to_rgb(self._rainbow_hue, 1, 1)]
        self.lamp.set(rgb, self.brightness.get())
        self._rainbow_job = self.root.after(80, self.tick_rainbow)

    def reapply_after_resume(self, attempt=0):
        """Firmware falls back to its own rainbow effect after sleep; put our
        settings back. Rainbow mode recovers by itself on the next tick."""
        if self.rainbow:
            return
        self.lamp.set(self.rgb, self.brightness.get())
        if attempt < 3:
            # The controller can come back late after resume; reassert a few times
            self.root.after(2500, self.reapply_after_resume, attempt + 1)

    # -- system tray -----------------------------------------------------

    def make_tray(self):
        menu = pystray.Menu(
            pystray.MenuItem("Open", self.tray_open, default=True),
            pystray.MenuItem("Max fan", self.tray_toggle_fan,
                             checked=lambda item: self.fan_on),
            pystray.MenuItem("Lights off", self.tray_lights_off),
            pystray.MenuItem("Exit", self.tray_exit),
        )
        return pystray.Icon("keyboard_light", self.tray_image(), "Keyboard Light", menu)

    def tray_image(self):
        return Image.open(FEATHER_PNG)

    def hide_to_tray(self):
        self.root.withdraw()

    def tray_open(self):
        self.root.after(0, lambda: (self.root.deiconify(), self.root.lift()))

    def tray_lights_off(self):
        self.root.after(0, self.lights_off)

    def tray_toggle_fan(self):
        self.root.after(0, self.toggle_fan)

    def tray_exit(self):
        self.tray.stop()
        self.root.after(0, self.root.destroy)

    # -- helpers ---------------------------------------------------------

    def refresh_swatch(self):
        level = self.brightness.get() if hasattr(self, "brightness") else 100
        shown = [round(c * level / 100) for c in self.rgb]
        self.swatch.configure(bg="#{:02x}{:02x}{:02x}".format(*shown))

    def apply(self):
        self._slide_job = None
        self.refresh_swatch()
        if not self.rainbow:
            self.lamp.set(self.rgb, self.brightness.get())
        save_config(self.rgb, self.brightness.get(), self.rainbow)


# ---------------------------------------------------------------------------
# Install / uninstall (the frozen exe doubles as its own installer)
# ---------------------------------------------------------------------------

FAN_TASKS = [(FAN_MAX_TASK, "--fan max"), (FAN_NORMAL_TASK, "--fan off")]


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin(arg):
    """Re-launch this exe elevated with a single argument; returns True if a
    launch was initiated."""
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, arg, None, 1)
    return rc > 32


def register_fan_tasks(exe):
    """Create the elevated, no-UAC scheduled tasks that toggle the fans, via the
    Task Scheduler COM API (RunLevel HIGHEST, interactive token)."""
    import win32com.client
    svc = win32com.client.Dispatch("Schedule.Service")
    svc.Connect()
    folder = svc.GetFolder("\\")
    for name, arg in FAN_TASKS:
        td = svc.NewTask(0)
        td.RegistrationInfo.Description = "AERO keyboard app: %s" % arg
        td.Principal.RunLevel = 1        # TASK_RUNLEVEL_HIGHEST
        td.Principal.LogonType = 3       # TASK_LOGON_INTERACTIVE_TOKEN
        td.Settings.StopIfGoingOnBatteries = False
        td.Settings.DisallowStartIfOnBatteries = False
        td.Settings.ExecutionTimeLimit = "PT0S"
        act = td.Actions.Create(0)       # TASK_ACTION_EXEC
        act.Path = exe
        act.Arguments = arg
        # 6 = TASK_CREATE_OR_UPDATE, 3 = interactive-token logon
        folder.RegisterTaskDefinition(name, td, 6, None, None, 3)


def unregister_fan_tasks():
    import win32com.client
    svc = win32com.client.Dispatch("Schedule.Service")
    svc.Connect()
    folder = svc.GetFolder("\\")
    for name, _arg in FAN_TASKS:
        try:
            folder.DeleteTask(name, 0)
        except Exception:
            pass


def make_shortcut(path, target, args="", icon=None):
    import win32com.client
    sh = win32com.client.Dispatch("WScript.Shell")
    lnk = sh.CreateShortcut(str(path))
    lnk.TargetPath = str(target)
    lnk.Arguments = args
    lnk.WorkingDirectory = str(Path(target).parent)
    if icon:
        lnk.IconLocation = str(icon)
    lnk.Save()


def _startup_dir():
    return Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Startup"


def _programs_dir():
    return Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs"


def install():
    """Copy the exe + assets into LocalAppData, register the fan WMI class and
    scheduled tasks, and create Start-menu / startup shortcuts. Requires admin
    (for mofcomp + task registration); self-elevates if needed."""
    if not getattr(sys, "frozen", False):
        print("Install is only supported from the built exe.")
        return
    if not is_admin():
        relaunch_as_admin("--install")
        return

    dest = app_data_dir()
    exe = dest / "KeyboardLight.exe"
    # Copy ourselves + bundled assets into the install dir.
    import shutil
    if Path(sys.executable).resolve() != exe.resolve():
        shutil.copy2(sys.executable, exe)
    for name in ("feather.ico", "feather.png", MOF_NAME):
        src = resource_path(name)
        if src.exists():
            shutil.copy2(src, dest / name)

    # Register the ACPI-WMI fan class from our clean-room MOF (idempotent).
    mof = dest / MOF_NAME
    ok_fan = False
    if mof.exists():
        subprocess.run(["mofcomp", str(mof)], creationflags=CREATE_NO_WINDOW,
                       capture_output=True)
        try:
            register_fan_tasks(str(exe))
            ok_fan = True
        except Exception as e:
            print("fan task registration failed:", e)

    # Shortcuts: Start menu (open) + startup (run in tray at login).
    ico = dest / "feather.ico"
    try:
        make_shortcut(_programs_dir() / "Keyboard Light.lnk", exe, "", ico)
        make_shortcut(_startup_dir() / "Keyboard Light.lnk", exe, "--tray", ico)
    except Exception as e:
        print("shortcut creation failed:", e)

    ctypes.windll.user32.MessageBoxW(
        None,
        "Keyboard Light installed."
        + ("\n\nMax Fan control is ready." if ok_fan
           else "\n\n(Fan control unavailable on this machine.)"),
        "Keyboard Light", 0x40)
    # Launch it.
    subprocess.Popen([str(exe), "--tray"])


def uninstall():
    if not is_admin():
        relaunch_as_admin("--uninstall")
        return
    unregister_fan_tasks()
    for p in (_programs_dir() / "Keyboard Light.lnk",
              _startup_dir() / "Keyboard Light.lnk"):
        try:
            p.unlink()
        except Exception:
            pass
    ctypes.windll.user32.MessageBoxW(
        None, "Keyboard Light uninstalled.\n\nYou can delete the folder:\n"
        + str(app_data_dir()), "Keyboard Light", 0x40)


def main():
    if "--install" in sys.argv:
        install()
        return
    if "--uninstall" in sys.argv:
        uninstall()
        return
    if "--fan" in sys.argv:
        i = sys.argv.index("--fan")
        mode = sys.argv[i + 1] if i + 1 < len(sys.argv) else "max"
        fan_apply("max" if mode == "max" else "off")
        return
    if "--apply" in sys.argv:
        rgb, brightness, _rainbow = load_config()
        apply_color(rgb, brightness)
        return
    # First run of the distributable exe (i.e. not yet copied into the install
    # dir): offer to install. Answering No just runs it portably this once.
    if getattr(sys, "frozen", False) and "--tray" not in sys.argv:
        installed = app_data_dir() / "KeyboardLight.exe"
        if Path(sys.executable).resolve() != installed.resolve():
            yes = ctypes.windll.user32.MessageBoxW(
                None, "Install Keyboard Light on this PC?\n\n"
                "Adds it to the Start menu, starts it at login, and enables the "
                "Max Fan button.", "Keyboard Light", 0x4 | 0x20)  # YesNo | Question
            if yes == 6:  # IDYES
                install()
                return

    # Give this app its own taskbar identity so Windows shows the window's
    # feather icon instead of pythonw's.
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Elliott.KeyboardLight")
    root = tk.Tk()
    if FEATHER_ICO.exists():
        root.iconbitmap(default=str(FEATHER_ICO))
    App(root)
    if "--tray" in sys.argv:
        root.withdraw()
    root.mainloop()


if __name__ == "__main__":
    main()
