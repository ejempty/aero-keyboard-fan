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
import time
import uuid
import winreg
import tkinter as tk
from ctypes import wintypes
from pathlib import Path
from tkinter import colorchooser

import hid
import pystray
from PIL import Image, ImageDraw, ImageTk

VID, PID = 0x0414, 0x8104
LAMPARRAY_USAGE_PAGE = 0x59

# Max Fan toggle drives Gigabyte's ACPI-WMI fan control, which needs admin.
# setup_fan_task.ps1 registers these scheduled tasks (highest privileges) once;
# triggering them with `schtasks /run` then avoids a UAC prompt per click.
FAN_MAX_TASK = "AeroFanMax"
FAN_NORMAL_TASK = "AeroFanNormal"
# Resetting the dGPU driver needs admin too, so it gets the same treatment.
GPU_RESET_TASK = "AeroGpuReset"
CREATE_NO_WINDOW = 0x08000000

APP_NAME = "KeyboardLight"

# ---------------------------------------------------------------------------
# Power modes (AERO X16 / Ryzen AI 7 350 "Krackan Point")
#
# The APU power limits are firmware ACPI-WMI methods (root\WMI GB_WMIACPI_Set),
# reverse-engineered from GiMATE's CPUOC.dll and verified to actually clamp the
# CPU on this machine (a low SPL cut sustained all-core throughput ~24%):
#   SetApuParameter1 = fPPT (fast/burst PPT)   SetApuParameter2 = sPPT (slow PPT)
#   SetApuParameter3 = SPL  (sustained / STAPM)   -- all values in milliwatts.
# SetNvPowerConfig biases NVIDIA Dynamic Boost toward the dGPU (1) or not (0).
#
# All values below are within GiMATE's own shipped range (SPL <=25W, PPT <=80W),
# so the AMD SMU firmware clamps the real electrical limits regardless. Battery
# keeps a HIGH burst (fPPT) with a LOW sustained (SPL): snappy on clicks, sips on
# long loads -- deliberately less "neutered" than GiMATE's own Eco (30/30/15).
#
# fPPT / sPPT / SPL (mW), Dynamic-Boost bias, fan mode:
POWER_FW = {
    "battery":     ((54000, 30000, 15000), 0, "off"),
    "balanced":    ((65000, 54000, 20000), 0, "off"),
    "performance": ((80000, 80000, 25000), 1, "max"),
}

# The firmware writes need admin, so (like the fans) each mode runs from an
# elevated, no-UAC scheduled task triggered with `schtasks /run`.
MODE_TASKS = [
    ("AeroModeBattery", "--applyfw battery"),
    ("AeroModeBalanced", "--applyfw balanced"),
    ("AeroModePerformance", "--applyfw performance"),
]
MODE_TASK_FOR = {
    "battery": "AeroModeBattery",
    "balanced": "AeroModeBalanced",
    "performance": "AeroModePerformance",
}

# Windows power-overlay ("power mode" slider) scheme GUIDs. Setting these needs
# no admin and is instant.
OVERLAY_EFFICIENCY = "961cc777-2547-4f9d-8174-7d86181b8a7a"
OVERLAY_BALANCED = "00000000-0000-0000-0000-000000000000"
OVERLAY_PERFORMANCE = "ded574b5-45a0-4f42-8737-46345c09c238"

# Non-firmware (OS) levers applied per mode by the GUI process (no admin):
#   overlay = Windows power slider; brightness = panel % (None = leave alone);
#   refresh = target Hz (None = leave, 0 = restore to the panel's maximum).
POWER_OS = {
    "battery":     dict(overlay=OVERLAY_EFFICIENCY, brightness=40, refresh=60),
    "balanced":    dict(overlay=OVERLAY_BALANCED, brightness=None, refresh=None),
    "performance": dict(overlay=OVERLAY_PERFORMANCE, brightness=None, refresh=0),
}

# Battery firmware tiers the Watcher can pick between after profiling a real
# 10-minute slice of use. All are within the proven-safe envelope; "default"
# equals POWER_FW["battery"]. Lower SPL only bites during sustained load, so a
# machine that idles + bursts (typical) loses nothing on "light".
BATTERY_TIERS = {
    "light":   (40000, 24000, 10000),   # bursts fine, sustained capped 10 W
    "medium":  (48000, 28000, 12000),
    "default": (54000, 30000, 15000),
}


def battery_fw_vals():
    """Battery-mode fPPT/sPPT/SPL: Watcher-tuned tier if set, else default."""
    tier = _read_config().get("battery_tier")
    return BATTERY_TIERS.get(tier, POWER_FW["battery"][0])


# Three user-selectable positions. "battery" and "performance" pin a fixed
# profile; "auto" runs the adaptive controller (picks a firmware profile from the
# power source -- battery on DC, balanced on AC -- and re-applies on plug/unplug).
UI_MODES = ["battery", "auto", "performance"]
MODE_LABEL = {"battery": "Battery", "auto": "Auto", "performance": "Performance"}
MODE_COLOR = {"battery": "#2ea043", "auto": "#0078d7", "performance": "#d1242f"}

# Light / dark UI palettes. Gaming-style: sleek surfaces, vivid accents.
THEMES = {
    "dark": dict(
        bg="#0f1117", panel="#171a21", fg="#e9ecf2", sub="#7d8598",
        entry="#1e222b", btn="#232834", btn_hover="#2c3340", border="#2a3040",
        accent="#00e0c6", titlebar_dark=True),
    "light": dict(
        bg="#eceff4", panel="#ffffff", fg="#141821", sub="#5a6273",
        entry="#ffffff", btn="#e4e8ef", btn_hover="#d6dbe4", border="#d0d6e0",
        accent="#0aa89a", titlebar_dark=False),
}
BASE_FONT = "Segoe UI"


def set_titlebar_dark(root, dark):
    """Darken (or lighten) the Windows title bar to match the theme, via the
    DWM immersive-dark-mode attribute. Best-effort; no-op on old builds."""
    try:
        root.update_idletasks()
        try:
            hwnd = int(root.wm_frame(), 16)   # the decorated top-level frame HWND
        except Exception:
            hwnd = ctypes.windll.user32.GetParent(root.winfo_id()) or root.winfo_id()
        val = ctypes.c_int(1 if dark else 0)
        dwm = ctypes.windll.dwmapi.DwmSetWindowAttribute
        for attr in (20, 19):   # DWMWA_USE_IMMERSIVE_DARK_MODE (20, or 19 pre-20H1)
            dwm(hwnd, attr, ctypes.byref(val), ctypes.sizeof(val))
        # Repaint just the non-client frame so the new caption color shows without
        # hiding/reshowing the window (which flickers).
        if root.winfo_viewable():
            SWP = 0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020  # NOSIZE|MOVE|ZORDER|ACTIVATE|FRAMECHANGED
            ctypes.windll.user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, SWP)
    except Exception:
        pass


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
            delay = 1.0
            while not self._write(rgb, brightness):
                # Device not there yet (early logon, re-enumeration after
                # sleep/dock): keep retrying this request with backoff, but
                # yield immediately if a newer request arrives.
                with self._cond:
                    if self._cond.wait(delay) or self._pending is not None:
                        break
                delay = min(delay * 2, 15.0)

    def _write(self, rgb, brightness):
        for attempt in (0, 1):
            try:
                if self._dev is None:
                    path = find_lamparray_path()
                    if path is None:
                        return False
                    dev = hid.device()
                    dev.open_path(path)
                    self._dev = dev
                _send_color(self._dev, rgb, brightness)
                return True
            except OSError:
                # Stale handle (e.g. after sleep): drop it and retry once fresh
                if self._dev is not None:
                    try:
                        self._dev.close()
                    except OSError:
                        pass
                    self._dev = None
        return False


def start_listener(on_up, on_down, on_resume, on_power_change=None,
                   on_battery_pct=None, on_device_change=None):
    """Background thread with a hidden window that receives Ctrl+Up / Ctrl+Down
    global hotkeys, resume-from-sleep, and (if given) AC/DC power-source changes
    -- on_power_change(is_ac: bool) --, battery-percent changes --
    on_battery_pct(pct: int) --, and device arrivals/removals --
    on_device_change()."""
    WM_HOTKEY, WM_POWERBROADCAST = 0x0312, 0x0218
    WM_DEVICECHANGE, DBT_DEVNODES_CHANGED = 0x0219, 0x0007
    PBT_APMRESUMESUSPEND, PBT_APMRESUMEAUTOMATIC = 0x7, 0x12
    PBT_POWERSETTINGCHANGE = 0x8013
    MOD_CONTROL, VK_UP, VK_DOWN = 0x0002, 0x26, 0x28
    GUID_ACDC_POWER_SOURCE = _guid("5D3E9A59-E9D5-4B00-A6BD-FF34FF516548")
    GUID_BATTERY_PERCENTAGE = _guid("A7AD8041-B45A-4CAE-87A3-EECBB468A9E1")

    def loop():
        user32 = ctypes.windll.user32
        LRESULT = ctypes.c_ssize_t
        WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, ctypes.c_uint,
                                     wintypes.WPARAM, wintypes.LPARAM)
        user32.DefWindowProcW.restype = LRESULT
        user32.DefWindowProcW.argtypes = [wintypes.HWND, ctypes.c_uint,
                                          wintypes.WPARAM, wintypes.LPARAM]

        class PBSETTING(ctypes.Structure):
            _fields_ = [("PowerSetting", _GUID), ("DataLength", wintypes.DWORD),
                        ("Data", ctypes.c_ubyte * 4)]

        def _same_guid(a, b):
            return (a.Data1 == b.Data1 and a.Data2 == b.Data2 and a.Data3 == b.Data3
                    and bytes(a.Data4) == bytes(b.Data4))

        def wndproc(hwnd, msg, wparam, lparam):
            if msg == WM_HOTKEY:
                (on_up if wparam == 1 else on_down)()
            elif msg == WM_POWERBROADCAST and wparam in (
                    PBT_APMRESUMESUSPEND, PBT_APMRESUMEAUTOMATIC):
                on_resume()
            elif (msg == WM_POWERBROADCAST and wparam == PBT_POWERSETTINGCHANGE
                  and lparam):
                ps = ctypes.cast(lparam, ctypes.POINTER(PBSETTING)).contents
                if (on_power_change is not None
                        and _same_guid(ps.PowerSetting, GUID_ACDC_POWER_SOURCE)):
                    on_power_change(ps.Data[0] == 0)  # 0=AC, 1=battery, 2=hot
                elif (on_battery_pct is not None
                      and _same_guid(ps.PowerSetting, GUID_BATTERY_PERCENTAGE)):
                    on_battery_pct(ps.Data[0])        # 0-100
            elif (msg == WM_DEVICECHANGE and wparam == DBT_DEVNODES_CHANGED
                  and on_device_change is not None):
                on_device_change()
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
        if on_power_change is not None:
            try:
                user32.RegisterPowerSettingNotification(
                    hwnd, ctypes.byref(GUID_ACDC_POWER_SOURCE), 0)
            except Exception:
                pass
        if on_battery_pct is not None:
            try:
                user32.RegisterPowerSettingNotification(
                    hwnd, ctypes.byref(GUID_BATTERY_PERCENTAGE), 0)
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


def _read_config():
    try:
        return json.loads(CONFIG_PATH.read_text())
    except Exception:
        return {}


def save_config(rgb, brightness, rainbow=False):
    cfg = _read_config()
    cfg.update({"color": list(rgb), "brightness": brightness, "rainbow": rainbow})
    CONFIG_PATH.write_text(json.dumps(cfg))


def load_mode():
    m = _read_config().get("power_mode", "auto")
    return m if m in UI_MODES else "auto"


def save_mode(mode):
    cfg = _read_config()
    cfg["power_mode"] = mode
    CONFIG_PATH.write_text(json.dumps(cfg))


def load_opts():
    c = _read_config()
    return dict(
        igpu=bool(c.get("igpu_battery", True)),
        ab=bool(c.get("ab_oc", False)),
        ab_oc=int(c.get("ab_oc_profile", 1)),
        ab_stock=int(c.get("ab_stock_profile", 5)),
        game=bool(c.get("auto_game", False)),
        acdc=bool(c.get("acdc_switch", True)),
        gpufix=bool(c.get("gpu_autoreset", True)),
    )


def save_opt(key, val):
    c = _read_config()
    c[key] = val
    CONFIG_PATH.write_text(json.dumps(c))


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


# -- power-mode firmware worker (runs elevated, from a scheduled task) --------

def apu_apply(fppt, sppt, spl, nv=None):
    """Write the AMD APU power limits (milliwatts) over ACPI-WMI. Needs admin."""
    import wmi
    inst = wmi.WMI(namespace="root/WMI").GB_WMIACPI_Set()[0]
    inst.SetApuParameter1(Data=int(fppt))   # fast PPT (burst)
    inst.SetApuParameter2(Data=int(sppt))   # slow PPT
    inst.SetApuParameter3(Data=int(spl))    # SPL / STAPM (sustained)
    if nv is not None:
        try:
            inst.SetNvPowerConfig(Data=int(nv))   # NVIDIA Dynamic Boost bias
        except Exception:
            pass  # not all units expose it; the APU limits are what matter


def tune_battery_power_plan():
    """Invisible platform hygiene for the DC (battery) power plan: enable USB
    selective suspend and set PCIe ASPM to maximum power savings. These only
    touch the battery-side values, cost no responsiveness, and are idempotent.
    Needs admin (runs from the elevated battery worker)."""
    USB_SUB = "2a737441-1930-4402-8d77-b2bebba308a3"
    USB_SUSPEND = "48e6b7a6-50f5-4782-a5d4-53bb8f07e226"    # 1 = enabled
    PCI_SUB = "501a4d13-42af-4429-9fd1-a8218c268e20"
    PCI_ASPM = "ee12f906-d277-404b-b6da-e5fa1a576df5"       # 2 = max power savings
    CPU_SUB = "54533251-82be-4824-96c1-47b60b740d00"
    BOOSTMODE = "be337238-0d82-4146-a960-4f3749d470c7"      # 3 = efficient enabled
    SCHEDPOL = "93b8b6dc-0698-4d1c-9ee4-0644e900c85d"       # hetero thread policy
    SHORTPOL = "bae08b81-2d5e-4688-ad6a-13243356654b"       # hetero short-thread policy
    WIFI_SUB = "19cbb8fa-5279-450e-9fac-8a3d5fedd0c1"
    WIFI_PS = "12bbebe6-58d6-4636-95bb-3217ef867c1a"        # 3 = maximum power saving
    cmds = [
        ["powercfg", "/setdcvalueindex", "SCHEME_CURRENT", USB_SUB, USB_SUSPEND, "1"],
        ["powercfg", "/setdcvalueindex", "SCHEME_CURRENT", PCI_SUB, PCI_ASPM, "2"],
        # Turbo bias: background blips stop spiking Zen5 cores to 5 GHz on DC;
        # real demand still boosts (efficient mode), so clicks stay snappy.
        ["powercfg", "/setdcvalueindex", "SCHEME_CURRENT", CPU_SUB, BOOSTMODE, "3"],
        # Heterogeneous scheduling: prefer the Zen5c efficiency cores on DC (5 =
        # prefer efficient). Silently ignored if the OS doesn't expose the knob.
        ["powercfg", "/setdcvalueindex", "SCHEME_CURRENT", CPU_SUB, SCHEDPOL, "5"],
        ["powercfg", "/setdcvalueindex", "SCHEME_CURRENT", CPU_SUB, SHORTPOL, "5"],
        ["powercfg", "/setdcvalueindex", "SCHEME_CURRENT", WIFI_SUB, WIFI_PS, "3"],
        ["powercfg", "/setactive", "SCHEME_CURRENT"],
    ]
    for c in cmds:
        try:
            subprocess.run(c, creationflags=CREATE_NO_WINDOW,
                           capture_output=True, timeout=8)
        except Exception:
            pass


def mode_firmware_apply(mode):
    """Apply the firmware half of a power mode: APU limits, Dynamic-Boost bias,
    and fans. `mode` is a firmware profile key in POWER_FW."""
    vals, nv, fan = POWER_FW[mode]
    if mode == "battery":
        vals = battery_fw_vals()   # Watcher-tuned tier, if one was chosen
    apu_apply(*vals, nv=nv)
    fan_apply("max" if fan == "max" else "off")
    # MSI Afterburner runs elevated, so this elevated worker owns all of it:
    # the de-elevated GUI can neither launch nor kill it.
    opts = load_opts()
    if opts["ab"] and afterburner_exe():
        if mode == "performance":
            apply_afterburner_profile(opts["ab_oc"])
        elif afterburner_running():
            apply_afterburner_profile(opts["ab_stock"])
            if mode == "battery":
                time.sleep(4)   # let the -Profile command land before closing
    if mode == "battery":
        tune_battery_power_plan()
        # Afterburner's monitoring polls the dGPU every second, holding it out
        # of D3cold (~8-9 W). Applied offsets persist after exit, so close it.
        if opts["igpu"] and afterburner_running():
            close_afterburner()


def run_mode_task(mode):
    """Trigger the elevated scheduled task that applies a firmware profile.
    Returns None on success, 'setup' if not registered, else an error string."""
    task = MODE_TASK_FOR.get(mode)
    if not task:
        return f"unknown mode {mode}"
    return run_fan_task(task)  # same schtasks /run mechanism


# -- OS-level levers (run in the GUI process; no admin needed) ----------------

class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]


def _guid(s):
    u = uuid.UUID(s)
    return _GUID(u.time_low, u.time_mid, u.time_hi_version,
                 (ctypes.c_ubyte * 8)(*u.bytes[8:16]))


def set_power_overlay(guid_str):
    """Set the Windows power-mode slider (Best efficiency / Balanced / Best
    performance). Returns True on success."""
    try:
        fn = ctypes.windll.powrprof.PowerSetActiveOverlayScheme
        fn.argtypes = [_GUID]
        fn.restype = wintypes.DWORD
        return fn(_guid(guid_str)) == 0
    except Exception:
        return False


def get_brightness():
    try:
        import wmi
        m = wmi.WMI(namespace="root/wmi").WmiMonitorBrightness()[0]
        return int(m.CurrentBrightness)
    except Exception:
        return None


_APP_SET = [None, 0.0]    # (level, monotonic time) of our last brightness write


def brightness_is_ours(pct):
    """True if a brightness change event is the echo of our own write, not the
    user's F5/F6. Used so snap-to-5 only ever corrects the firmware's keys."""
    return _APP_SET[0] == pct and time.monotonic() - _APP_SET[1] < 3.0


def set_brightness(pct):
    """Set internal-panel brightness (0-100). Best-effort; silently no-ops on
    desktops / external-only displays."""
    _APP_SET[0], _APP_SET[1] = max(0, min(100, int(pct))), time.monotonic()
    try:
        import wmi
        m = wmi.WMI(namespace="root/wmi").WmiMonitorBrightnessMethods()[0]
        m.WmiSetBrightness(Brightness=max(0, min(100, int(pct))), Timeout=0)
        return True
    except Exception:
        return False


def _wbem_timed_out(e):
    """True if a COM error is just SWbemEventSource's NextEvent timeout.

    The timeout does NOT arrive as e.args[0] -- that is a generic
    DISP_E_EXCEPTION. The real WBEM_S_TIMEDOUT (0x80043001) is buried in the
    excepinfo tuple at e.args[2][5]. Reading the wrong slot turns every idle
    second into a fatal error and kills the watcher."""
    TIMED_OUT = (0x80043001, 0x80043001 - 0x100000000)
    try:
        if e.args and e.args[0] in TIMED_OUT:
            return True
        info = e.args[2] if len(e.args) > 2 else None
        if info:
            if len(info) > 5 and info[5] in TIMED_OUT:
                return True
            if len(info) > 2 and isinstance(info[2], str) and "Timed out" in info[2]:
                return True
    except Exception:
        pass
    return False


# Refresh-rate switching via ChangeDisplaySettingsEx.
_DM_DISPLAYFREQUENCY = 0x00400000
_CDS_UPDATEREGISTRY = 0x00000001
_CDS_TEST = 0x00000002
_ENUM_CURRENT_SETTINGS = -1


class _DEVMODE(ctypes.Structure):
    _fields_ = [("dmDeviceName", wintypes.WCHAR * 32), ("dmSpecVersion", wintypes.WORD),
                ("dmDriverVersion", wintypes.WORD), ("dmSize", wintypes.WORD),
                ("dmDriverExtra", wintypes.WORD), ("dmFields", wintypes.DWORD),
                ("dmOrientation", ctypes.c_short), ("dmPaperSize", ctypes.c_short),
                ("dmPaperLength", ctypes.c_short), ("dmPaperWidth", ctypes.c_short),
                ("dmScale", ctypes.c_short), ("dmCopies", ctypes.c_short),
                ("dmDefaultSource", ctypes.c_short), ("dmPrintQuality", ctypes.c_short),
                ("dmColor", ctypes.c_short), ("dmDuplex", ctypes.c_short),
                ("dmYResolution", ctypes.c_short), ("dmTTOption", ctypes.c_short),
                ("dmCollate", ctypes.c_short), ("dmFormName", wintypes.WCHAR * 32),
                ("dmLogPixels", wintypes.WORD), ("dmBitsPerPel", wintypes.DWORD),
                ("dmPelsWidth", wintypes.DWORD), ("dmPelsHeight", wintypes.DWORD),
                ("dmDisplayFlags", wintypes.DWORD), ("dmDisplayFrequency", wintypes.DWORD),
                ("dmICMMethod", wintypes.DWORD), ("dmICMIntent", wintypes.DWORD),
                ("dmMediaType", wintypes.DWORD), ("dmDitherType", wintypes.DWORD),
                ("dmReserved1", wintypes.DWORD), ("dmReserved2", wintypes.DWORD),
                ("dmPanningWidth", wintypes.DWORD), ("dmPanningHeight", wintypes.DWORD)]


def _current_devmode():
    dm = _DEVMODE()
    dm.dmSize = ctypes.sizeof(_DEVMODE)
    if not ctypes.windll.user32.EnumDisplaySettingsW(None, _ENUM_CURRENT_SETTINGS,
                                                     ctypes.byref(dm)):
        return None
    return dm


def _max_refresh_for_current_mode():
    """Highest refresh available at the current resolution."""
    cur = _current_devmode()
    if not cur:
        return None
    best = cur.dmDisplayFrequency
    dm = _DEVMODE()
    dm.dmSize = ctypes.sizeof(_DEVMODE)
    i = 0
    while ctypes.windll.user32.EnumDisplaySettingsW(None, i, ctypes.byref(dm)):
        if (dm.dmPelsWidth == cur.dmPelsWidth and dm.dmPelsHeight == cur.dmPelsHeight
                and dm.dmDisplayFrequency > best):
            best = dm.dmDisplayFrequency
        i += 1
    return best


def set_refresh(hz):
    """Set the internal panel's refresh rate. hz=0 restores the panel maximum.
    Best-effort; returns True if a change was committed."""
    try:
        if hz == 0:
            hz = _max_refresh_for_current_mode()
            if not hz:
                return False
        dm = _current_devmode()
        if not dm or dm.dmDisplayFrequency == hz:
            return False
        dm.dmFields = _DM_DISPLAYFREQUENCY
        dm.dmDisplayFrequency = hz
        cdse = ctypes.windll.user32.ChangeDisplaySettingsExW
        if cdse(None, ctypes.byref(dm), None, _CDS_TEST, None) != 0:
            return False
        return cdse(None, ctypes.byref(dm), None, _CDS_UPDATEREGISTRY, None) == 0
    except Exception:
        return False


def power_source_is_ac():
    """True if on AC, False on battery, None if unknown."""
    class SPS(ctypes.Structure):
        _fields_ = [("ACLineStatus", ctypes.c_byte), ("BatteryFlag", ctypes.c_byte),
                    ("BatteryLifePercent", ctypes.c_byte), ("SystemStatusFlag", ctypes.c_byte),
                    ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]
    sps = SPS()
    if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(sps)):
        return None
    if sps.ACLineStatus == 1:
        return True
    if sps.ACLineStatus == 0:
        return False
    return None


# -- dGPU discipline: nudge idle apps to the iGPU so the RTX can D3cold --------
# Windows honors a per-app GPU preference at
#   HKCU\Software\Microsoft\DirectX\UserGpuPreferences
# value name = full exe path, data = "GpuPreference=1;" (1 = power-saving / iGPU,
# 2 = high-performance / dGPU). Takes effect the NEXT time the app launches.
_GPU_PREF_KEY = r"Software\Microsoft\DirectX\UserGpuPreferences"

# Common apps that keep a laptop dGPU awake. Standard install paths; only ones
# that actually exist on disk are touched.
DGPU_APP_PATHS = [
    r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles%\Mozilla Firefox\firefox.exe",
    r"%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe",
    r"%APPDATA%\Spotify\Spotify.exe",
    r"%ProgramFiles%\Microsoft\Teams\current\Teams.exe",
    r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe",
    r"%LOCALAPPDATA%\Programs\obsidian\Obsidian.exe",
    r"%LOCALAPPDATA%\slack\slack.exe",
]


def set_apps_gpu(pref):
    """pref: 1 = force iGPU (power-saving), 2 = force dGPU, None = clear (let
    Windows/Optimus decide). Applies to the known culprit apps that exist."""
    try:
        key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, _GPU_PREF_KEY, 0,
                                 winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE)
    except Exception:
        return
    for raw in DGPU_APP_PATHS:
        p = os.path.expandvars(raw)
        if not os.path.exists(p):
            continue
        try:
            if pref is None:
                try:
                    winreg.DeleteValue(key, p)
                except FileNotFoundError:
                    pass
            else:
                winreg.SetValueEx(key, p, 0, winreg.REG_SZ, "GpuPreference=%d;" % pref)
        except Exception:
            pass
    try:
        winreg.CloseKey(key)
    except Exception:
        pass


# -- dGPU power state, read WITHOUT waking the GPU ----------------------------
# nvidia-smi and the GPU perf counters both spin an Optimus dGPU up just to
# answer, which is exactly the thing we're trying to avoid. DEVPKEY_Device_
# PowerData instead returns the PnP manager's *cached* CM_POWER_DATA for the
# devnode, so reading it costs nothing. Second DWORD is PD_MostRecentPowerState:
# 1 = D0 (awake, ~8-15 W on an RTX laptop part), 4 = D3 (RTD3/D3cold, ~0 W).

_CR_SUCCESS = 0
_CM_GETIDLIST_PRESENT_PCI = 0x101   # FILTER_ENUMERATOR | FILTER_PRESENT
_PWR_STATE = {1: "D0", 2: "D1", 3: "D2", 4: "D3"}


class _DEVPROPKEY(ctypes.Structure):
    _fields_ = [("fmtid", _GUID), ("pid", wintypes.ULONG)]


def _devpropkey(guid_str, pid):
    return _DEVPROPKEY(_guid(guid_str), pid)


# Both live in the DEVPKEY_Device_* property set (NOT the DEVPKEY_DeviceClass_*
# set {4321918B-...}, which is the setup-class store and has no values here).
_DEVPKEY_Device_PowerData = _devpropkey("A45C254E-DF1C-4EFD-8020-67D146A850E0", 32)
_DEVPKEY_Device_Class = _devpropkey("A45C254E-DF1C-4EFD-8020-67D146A850E0", 9)


def _devnode(dev_id):
    """DEVINST for a device instance ID, or None."""
    inst = wintypes.DWORD()
    if ctypes.windll.cfgmgr32.CM_Locate_DevNodeW(
            ctypes.byref(inst), ctypes.c_wchar_p(dev_id), 0) != _CR_SUCCESS:
        return None
    return inst


def _devnode_prop(inst, key, nbytes=128):
    """Raw bytes of a devnode property, or None."""
    ptype = wintypes.ULONG()
    size = wintypes.ULONG(nbytes)
    buf = (ctypes.c_ubyte * nbytes)()
    if ctypes.windll.cfgmgr32.CM_Get_DevNode_PropertyW(
            inst, ctypes.byref(key), ctypes.byref(ptype), buf,
            ctypes.byref(size), 0) != _CR_SUCCESS:
        return None
    return bytes(buf[:size.value])


def find_dgpu_id():
    """Instance ID of the NVIDIA display adapter (not its HD-Audio sibling)."""
    try:
        cfg = ctypes.windll.cfgmgr32
        size = wintypes.ULONG()
        flt = ctypes.c_wchar_p("PCI")
        if cfg.CM_Get_Device_ID_List_SizeW(
                ctypes.byref(size), flt, _CM_GETIDLIST_PRESENT_PCI) != _CR_SUCCESS:
            return None
        buf = ctypes.create_unicode_buffer(size.value)
        if cfg.CM_Get_Device_ID_ListW(
                flt, buf, size.value, _CM_GETIDLIST_PRESENT_PCI) != _CR_SUCCESS:
            return None
        for dev_id in buf[:size.value].split("\0"):
            if not dev_id or "VEN_10DE" not in dev_id.upper():
                continue
            inst = _devnode(dev_id)
            if inst is None:
                continue
            raw = _devnode_prop(inst, _DEVPKEY_Device_Class, 64)
            if raw and raw.decode("utf-16-le", "ignore").rstrip("\0") == "Display":
                return dev_id
    except Exception:
        pass
    return None


def dgpu_power_state(dev_id):
    """'D0'/'D3'/None for a PCI devnode. Passive -- never wakes the device."""
    if not dev_id:
        return None
    try:
        inst = _devnode(dev_id)
        if inst is None:
            return None
        raw = _devnode_prop(inst, _DEVPKEY_Device_PowerData, 64)
        if not raw or len(raw) < 8:
            return None
        return _PWR_STATE.get(int.from_bytes(raw[4:8], "little"))
    except Exception:
        return None


def dgpu_busy():
    """True if a real process (not the kernel) holds memory on a non-display
    adapter -- i.e. the dGPU is genuinely working, not just stuck awake.

    ONLY call this when the dGPU is already in D0. Reading GPU perf counters
    wakes an Optimus dGPU, which is the whole thing we're avoiding -- but if
    it's already awake the read costs nothing, so this is a free safety check
    before we go resetting the device out from under someone's render."""
    try:
        import win32pdh
        paths = win32pdh.ExpandCounterPath(
            r"\GPU Process Memory(*)\Dedicated Usage")
    except Exception:
        return True          # can't tell -> assume busy, never reset blindly
    import re
    seen = []
    for path in paths:
        m = re.search(r"pid_(\d+)_luid_(0x[0-9a-fA-F]+_0x[0-9a-fA-F]+)", path)
        if m:
            seen.append((int(m.group(1)), m.group(2).lower()))
    if not seen:
        return False
    # dwm always renders on the display adapter, so its LUID identifies the
    # iGPU; anything on another adapter is the dGPU.
    dwm = set(pids_by_names(["dwm"]))
    display_luids = {luid for pid, luid in seen if pid in dwm}
    for pid, luid in seen:
        if luid in display_luids or pid in (0, 4):   # 4 = System (kernel)
            continue
        return True
    return False


def gpu_reset():
    """Reload the dGPU driver so it re-evaluates power state and can drop back
    into RTD3. This is an atomic disable+enable that RELOADS the driver -- not
    a Device Manager 'Disable', which unloads the power-policy owner and makes
    idle draw worse. Needs admin, so it runs from an elevated task."""
    dev = find_dgpu_id()
    if not dev:
        return
    try:
        subprocess.run(["pnputil", "/restart-device", dev],
                       creationflags=CREATE_NO_WINDOW,
                       capture_output=True, timeout=60)
    except Exception:
        pass


# -- MSI Afterburner GPU overclock (Performance mode) -------------------------
# Afterburner applies real GPU core/mem offsets + power limit from a saved
# profile via its command line (MSIAfterburner.exe -Profile<N>). We never invent
# offsets: the USER tunes a stable OC once and saves it as a profile; we just
# trigger it. Configurable, opt-in.
AFTERBURNER_PATHS = [
    r"%ProgramFiles(x86)%\MSI Afterburner\MSIAfterburner.exe",
    r"%ProgramFiles%\MSI Afterburner\MSIAfterburner.exe",
]


def afterburner_exe():
    for raw in AFTERBURNER_PATHS:
        p = os.path.expandvars(raw)
        if os.path.exists(p):
            return p
    return None


def apply_afterburner_profile(n):
    """Load MSI Afterburner profile N (1-5) if Afterburner is installed."""
    exe = afterburner_exe()
    if not exe or not n:
        return False
    try:
        subprocess.Popen([exe, "-Profile%d" % int(n)],
                         creationflags=CREATE_NO_WINDOW)
        return True
    except Exception:
        return False


def afterburner_running():
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq MSIAfterburner.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, creationflags=CREATE_NO_WINDOW, timeout=10)
        return "MSIAfterburner.exe" in (out.stdout or "")
    except Exception:
        return False


def close_afterburner():
    """Close Afterburner (and its RTSS overlay): their hardware monitoring polls
    the dGPU every second, which holds it out of D3cold and burns ~8-9 W on
    battery. Applied clock offsets persist after the process exits, so closing
    it costs nothing. Graceful close first, then kill."""
    try:
        subprocess.run(["taskkill", "/IM", "MSIAfterburner.exe"],
                       capture_output=True, creationflags=CREATE_NO_WINDOW, timeout=10)
        subprocess.run(["taskkill", "/IM", "MSIAfterburner.exe", "/F"],
                       capture_output=True, creationflags=CREATE_NO_WINDOW, timeout=10)
        subprocess.run(["taskkill", "/IM", "RTSS.exe", "/F"],
                       capture_output=True, creationflags=CREATE_NO_WINDOW, timeout=10)
    except Exception:
        pass


# -- Watcher: 10-minute lightweight profile of real use, then auto-tune -------

def _sys_times():
    """(idle, kernel, user) 100ns ticks. One syscall; effectively free."""
    k32 = ctypes.windll.kernel32
    i, k, u = ctypes.c_ulonglong(), ctypes.c_ulonglong(), ctypes.c_ulonglong()
    if not k32.GetSystemTimes(ctypes.byref(i), ctypes.byref(k), ctypes.byref(u)):
        return None
    return i.value, k.value, u.value


def _cpu_pct(prev, cur):
    """Total CPU %% between two _sys_times() readings (kernel includes idle)."""
    di = cur[0] - prev[0]
    busy = (cur[1] - prev[1] - di) + (cur[2] - prev[2])
    tot = (cur[1] - prev[1]) + (cur[2] - prev[2])
    return max(0.0, min(100.0, 100.0 * busy / tot)) if tot > 0 else 0.0


def _own_cpu_seconds():
    """This process's own cumulative CPU seconds (kernel+user)."""
    k32 = ctypes.windll.kernel32
    k32.GetCurrentProcess.restype = ctypes.c_void_p
    c, e, kt, ut = (ctypes.c_ulonglong() for _ in range(4))
    ok = k32.GetProcessTimes(ctypes.c_void_p(k32.GetCurrentProcess()),
                             ctypes.byref(c), ctypes.byref(e),
                             ctypes.byref(kt), ctypes.byref(ut))
    if not ok:
        return time.process_time()   # stdlib fallback, same meaning
    return (kt.value + ut.value) / 1e7


def proc_cpu_snapshot():
    """{pid: (name, cpu_seconds)} for every process. One PowerShell spawn --
    only called at the start and end of a watch, never inside the window."""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-Process | Select-Object Name,Id,CPU | ConvertTo-Json -Compress"],
            capture_output=True, text=True, creationflags=CREATE_NO_WINDOW, timeout=30)
        return {int(p["Id"]): (p["Name"], float(p["CPU"] or 0))
                for p in json.loads(out.stdout)}
    except Exception:
        return {}


# -- EcoQoS: throttle known background burners on battery ---------------------
# PROCESS_POWER_THROTTLING_EXECUTION_SPEED tags a process "efficiency class":
# Windows runs it at low clocks on the Zen5c cores. The foreground app is left
# alone, so nothing the user is actively using feels slower.

class _PPT(ctypes.Structure):
    _fields_ = [("Version", ctypes.c_ulong), ("ControlMask", ctypes.c_ulong),
                ("StateMask", ctypes.c_ulong)]


def set_process_eco(pid, enable):
    """EcoQoS one process on/off. Same-user processes only (no admin needed).
    Returns True if the call stuck."""
    PROCESS_SET_INFORMATION = 0x0200
    ProcessPowerThrottling = 4
    SPEED = 0x1
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(PROCESS_SET_INFORMATION, False, int(pid))
    if not h:
        return False
    try:
        p = _PPT(Version=1,
                 ControlMask=SPEED if enable else 0,   # 0 = back to system default
                 StateMask=SPEED if enable else 0)
        return bool(k32.SetProcessInformation(h, ProcessPowerThrottling,
                                              ctypes.byref(p), ctypes.sizeof(p)))
    finally:
        k32.CloseHandle(h)


def pids_by_names(names):
    """{pid: name} for running processes whose image name (sans .exe) is in
    `names` (case-insensitive). One tasklist spawn."""
    want = {n.lower() for n in names}
    found = {}
    try:
        out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True,
                             creationflags=CREATE_NO_WINDOW, timeout=15)
        for line in (out.stdout or "").splitlines():
            parts = [p.strip('"') for p in line.split('","')]
            if len(parts) >= 2:
                stem = parts[0].lower().removesuffix(".exe")
                if stem in want:
                    try:
                        found[int(parts[1])] = parts[0]
                    except ValueError:
                        pass
    except Exception:
        pass
    return found


def user_idle_ms():
    """Milliseconds since the last keystroke / mouse move."""
    class LII(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]
    try:
        lii = LII(cbSize=ctypes.sizeof(LII))
        if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
            return max(0, ctypes.windll.kernel32.GetTickCount() - lii.dwTime)
    except Exception:
        pass
    return 0


def foreground_pid():
    try:
        u32 = ctypes.windll.user32
        hwnd = u32.GetForegroundWindow()
        pid = ctypes.c_ulong()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value
    except Exception:
        return None


# -- game detection (for Auto's optional Performance boost) -------------------

def gpu_3d_util():
    """Total GPU 3D-engine utilization % across all processes (no admin). Tells a
    real game from a static fullscreen window. Returns None if unavailable."""
    try:
        import win32pdh
    except Exception:
        return None
    q = None
    try:
        q = win32pdh.OpenQuery()
        try:
            paths = win32pdh.ExpandCounterPath(
                r"\GPU Engine(*engtype_3D)\Utilization Percentage")
        except Exception:
            paths = []
        if not paths:
            return None
        counters = []
        for p in paths:
            try:
                counters.append(win32pdh.AddCounter(q, p))
            except Exception:
                pass
        win32pdh.CollectQueryData(q)
        time.sleep(0.15)
        win32pdh.CollectQueryData(q)
        total = 0.0
        for c in counters:
            try:
                _, v = win32pdh.GetFormattedCounterValue(c, win32pdh.PDH_FMT_DOUBLE)
                total += max(0.0, v)
            except Exception:
                pass
        return total
    except Exception:
        return None
    finally:
        if q is not None:
            try:
                win32pdh.CloseQuery(q)
            except Exception:
                pass


def foreground_is_fullscreen():
    """True if the foreground window covers the whole primary monitor (a game or
    fullscreen app), excluding the desktop/shell."""
    try:
        u = ctypes.windll.user32
        hwnd = u.GetForegroundWindow()
        if not hwnd:
            return False
        cls = ctypes.create_unicode_buffer(256)
        u.GetClassNameW(hwnd, cls, 256)
        if cls.value in ("Progman", "WorkerW", "Shell_TrayWnd", "Button"):
            return False
        r = wintypes.RECT()
        u.GetWindowRect(hwnd, ctypes.byref(r))
        sw = u.GetSystemMetrics(0)
        sh = u.GetSystemMetrics(1)
        return (r.right - r.left) >= sw and (r.bottom - r.top) >= sh
    except Exception:
        return False


class SYSTEM_BATTERY_STATE(ctypes.Structure):
    _fields_ = [("AcOnLine", ctypes.c_ubyte), ("BatteryPresent", ctypes.c_ubyte),
                ("Charging", ctypes.c_ubyte), ("Discharging", ctypes.c_ubyte),
                ("Spare1", ctypes.c_ubyte * 3), ("Tag", ctypes.c_ubyte),
                ("MaxCapacity", wintypes.DWORD), ("RemainingCapacity", wintypes.DWORD),
                ("Rate", ctypes.c_long), ("EstimatedTime", wintypes.DWORD),
                ("DefaultAlert1", wintypes.DWORD), ("DefaultAlert2", wintypes.DWORD)]


_SystemBatteryState = 5
_BATTERY_UNKNOWN_RATE = -0x80000000   # 0x80000000 read back as a signed LONG

_CallNtPowerInformation = ctypes.WinDLL("powrprof").CallNtPowerInformation
_CallNtPowerInformation.argtypes = [ctypes.c_int, ctypes.c_void_p, wintypes.ULONG,
                                    ctypes.c_void_p, wintypes.ULONG]
_CallNtPowerInformation.restype = ctypes.c_long


def battery_watts():
    """Live battery draw in watts, or None if it cannot be read.

    Uses CallNtPowerInformation(SystemBatteryState) instead of WMI's
    root/WMI BatteryStatus.DischargeRate. Measured on this hardware: ~7 us here
    vs ~8800 us for WMI returning the same number. The WMI path is a COM
    round-trip into WmiPrvSE.exe, which then evaluates an ACPI method against
    the EC -- so every poll woke two extra processes and ran an EC transaction.
    At a 3 s poll that was most of this app's own idle CPU cost.

    Rate is signed mW and is negative while discharging.
    """
    s = SYSTEM_BATTERY_STATE()
    if _CallNtPowerInformation(_SystemBatteryState, None, 0,
                               ctypes.byref(s), ctypes.sizeof(s)) != 0:
        return None
    if not s.BatteryPresent or s.Rate == _BATTERY_UNKNOWN_RATE:
        return None
    return (abs(s.Rate) / 1000.0) if s.Discharging else 0.0


class PowerMeter:
    """Samples live battery draw in a background thread. Battery counters are
    always safe, but the GPU 3D-engine counter WAKES the dGPU on Optimus (the
    driver has to spin it up to service the perf counter), so it is polled only
    when poll_gpu is set -- i.e. in Auto mode, where game detection needs it.
    On battery/performance poll_gpu stays False and the dGPU is left to sleep."""

    def __init__(self):
        self.watts = None
        self.pct = None
        self.mins = None
        self.on_ac = None
        self.gpu_util = None   # total GPU 3D-engine utilization %
        self.poll_gpu = False  # only true in Auto mode; polling wakes the dGPU
        threading.Thread(target=self._run, daemon=True).start()

    def _sps(self):
        class SPS(ctypes.Structure):
            _fields_ = [("ACLineStatus", ctypes.c_byte), ("BatteryFlag", ctypes.c_byte),
                        ("BatteryLifePercent", ctypes.c_byte), ("SystemStatusFlag", ctypes.c_byte),
                        ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]
        s = SPS()
        if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(s)):
            self.on_ac = s.ACLineStatus == 1
            self.pct = s.BatteryLifePercent if s.BatteryLifePercent != 255 else None
            self.mins = (s.BatteryLifeTime // 60) if s.BatteryLifeTime not in (0xFFFFFFFF, 0) else None

    def _run(self):
        # No COM here any more -- battery_watts() is a plain syscall, and the
        # GPU counter path uses win32pdh, so this thread never needs CoInitialize.
        while True:
            self._sps()
            try:
                self.watts = battery_watts()
            except Exception:
                self.watts = None
            # Only touch the GPU counter when asked AND on AC -- doing so wakes
            # the dGPU, and game-boost (the only consumer) only runs on AC.
            self.gpu_util = gpu_3d_util() if (self.poll_gpu and self.on_ac) else None
            time.sleep(3)


class ModernSlider(tk.Canvas):
    """Flat, themed brightness slider: thin rounded track with an accent fill and
    a circular knob. Drop-in for tk.Scale (get()/set()/command)."""

    SS = 3  # supersample factor for smooth, anti-aliased edges

    def __init__(self, master, from_=0, to=100, length=260, command=None, scale=1.0):
        self.sc = scale
        self.h = int(38 * scale)
        super().__init__(master, width=length, height=self.h,
                         highlightthickness=0, bd=0)
        self.from_, self.to, self.length, self.command = from_, to, length, command
        self._val = from_
        self._t = None
        self.pad = int(11 * scale)
        self.trackh = max(4, int(6 * scale))
        self.knobr = int(9 * scale)
        self._img = None      # keep a ref so Tk doesn't GC the PhotoImage
        self._imgid = None
        self._txtid = None
        self._release = None  # optional callback fired on drag end
        self.bind("<Button-1>", self._drag)
        self.bind("<B1-Motion>", self._drag)
        self.bind("<ButtonRelease-1>", self._end)
        self.bind("<Configure>", lambda e: self._redraw())

    def on_release(self, fn):
        self._release = fn

    def theme(self, t):
        self._t = t
        self.configure(bg=t["bg"])
        self._redraw()

    def get(self):
        return int(round(self._val))

    def set(self, v):
        self._val = max(self.from_, min(self.to, v))
        self._redraw()

    def _geom(self):
        w = self.winfo_width()
        if w <= 2 * self.pad:      # not mapped yet -> use requested length
            w = self.length
        x0, x1 = self.pad, w - self.pad
        frac = (self._val - self.from_) / ((self.to - self.from_) or 1)
        kx = x0 + frac * (x1 - x0)
        cy = int(24 * self.sc)
        return x0, x1, kx, cy

    def _drag(self, e):
        x0, x1, _, _ = self._geom()
        frac = max(0.0, min(1.0, (e.x - x0) / max(1, (x1 - x0))))
        self._val = self.from_ + frac * (self.to - self.from_)
        self._redraw()
        if self.command:
            self.command(self.get())

    def _end(self, e):
        if self._release:
            self._release(self.get())

    @staticmethod
    def _rgb(h):
        h = h.lstrip("#")
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))

    def _redraw(self):
        if not self._t:
            return
        t = self._t
        x0, x1, kx, cy = self._geom()
        w = x1 + self.pad
        h = self.h
        ss = self.SS
        th = self.trackh
        im = Image.new("RGB", (w * ss, h * ss), self._rgb(t["bg"]))
        d = ImageDraw.Draw(im)
        d.rounded_rectangle([x0 * ss, (cy - th / 2) * ss, x1 * ss, (cy + th / 2) * ss],
                            radius=(th / 2) * ss, fill=self._rgb(t["entry"]))
        if kx > x0 + 1:
            d.rounded_rectangle([x0 * ss, (cy - th / 2) * ss, kx * ss, (cy + th / 2) * ss],
                                radius=(th / 2) * ss, fill=self._rgb(t["accent"]))
        r = self.knobr
        d.ellipse([(kx - r) * ss, (cy - r) * ss, (kx + r) * ss, (cy + r) * ss],
                  fill=self._rgb(t["accent"]), outline=self._rgb(t["bg"]),
                  width=max(1, int(2 * self.sc)) * ss)
        self._img = ImageTk.PhotoImage(im.resize((w, h), Image.LANCZOS))
        if self._imgid is None:
            self._imgid = self.create_image(0, 0, anchor="nw", image=self._img)
        else:
            self.itemconfigure(self._imgid, image=self._img)
        txt = str(self.get())
        if self._txtid is None:
            self._txtid = self.create_text(x1, int(9 * self.sc), text=txt, anchor="e",
                                           fill=t["sub"], font=(BASE_FONT, int(8 * self.sc)))
        else:
            self.coords(self._txtid, x1, int(9 * self.sc))
            self.itemconfigure(self._txtid, text=txt, fill=t["sub"])
        self.tag_raise(self._txtid)


class RoundedButton(tk.Canvas):
    """Flat pill button rendered with PIL supersampling for smooth corners.
    Drop-in-ish for tk.Button: text + command, plus hover state and a colors()
    call so themed and state-colored (mode/fan) buttons share one widget."""

    SS = 3

    def __init__(self, master, text="", command=None, scale=1.0,
                 width=120, height=32, radius=8, font_size=10, bold=False):
        self.sc = scale
        self.w = int(width * scale)
        self.hgt = int(height * scale)
        self.radius = int(radius * scale)
        super().__init__(master, width=self.w, height=self.hgt,
                         highlightthickness=0, bd=0, cursor="hand2")
        self.command = command
        self._text = text
        self._behind = "#0f1117"
        self._bg = "#232834"
        self._hover = "#2c3340"
        self._fg = "#e9ecf2"
        self._hovering = False
        self._img = None
        self._imgid = None
        self._txtid = None
        self._font = (BASE_FONT, int(font_size), "bold" if bold else "normal")
        self.bind("<Enter>", lambda e: self._hover_set(True))
        self.bind("<Leave>", lambda e: self._hover_set(False))
        self.bind("<Button-1>", lambda e: self.command and self.command())
        self.bind("<Configure>", lambda e: self._redraw())

    def _hover_set(self, on):
        self._hovering = on
        self._redraw()

    def set_text(self, text):
        if text == self._text:
            return
        self._text = text
        # Text-only change: skip the PIL re-render (the pill hasn't changed).
        if self._txtid is not None:
            self.itemconfigure(self._txtid, text=text)
        else:
            self._redraw()

    def colors(self, behind, bg, hover, fg, bold=None):
        self._behind, self._bg, self._hover, self._fg = behind, bg, hover, fg
        if bold is not None:
            self._font = (BASE_FONT, self._font[1], "bold" if bold else "normal")
        self._redraw()

    @staticmethod
    def _rgb(h):
        h = h.lstrip("#")
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))

    def _redraw(self):
        w = self.winfo_width()
        if w <= 2:
            w = self.w
        h = self.hgt
        ss = self.SS
        fill = self._hover if self._hovering else self._bg
        im = Image.new("RGB", (w * ss, h * ss), self._rgb(self._behind))
        d = ImageDraw.Draw(im)
        d.rounded_rectangle([0, 0, w * ss - 1, h * ss - 1],
                            radius=self.radius * ss, fill=self._rgb(fill))
        self._img = ImageTk.PhotoImage(im.resize((w, h), Image.LANCZOS))
        if self._imgid is None:
            self._imgid = self.create_image(0, 0, anchor="nw", image=self._img)
        else:
            self.itemconfigure(self._imgid, image=self._img)
        if self._txtid is None:
            self._txtid = self.create_text(w // 2, h // 2, text=self._text,
                                           fill=self._fg, font=self._font)
        else:
            self.coords(self._txtid, w // 2, h // 2)
            self.itemconfigure(self._txtid, text=self._text, fill=self._fg,
                               font=self._font)
        self.tag_raise(self._txtid)


class App:
    def __init__(self, root):
        self.root = root
        root.title("Aero Control")
        root.resizable(False, False)

        self.rgb, brightness, self.rainbow = load_config()
        # A pinned startup brightness wins over whatever was live at last exit.
        bd = _read_config().get("brightness_default")
        if bd is not None:
            brightness = max(0, min(100, int(bd)))
        self.lamp = Lamp()
        self._rainbow_hue = 0.0
        self._rainbow_job = None

        body = tk.Frame(root, padx=16, pady=14)
        body.pack()

        self.ui_scale = max(1.0, root.winfo_fpixels("1i") / 96.0)
        self.S = lambda px: int(round(px * self.ui_scale))

        self.theme = _read_config().get("theme", "dark")
        self._fg = THEMES[self.theme]["fg"]
        header = tk.Frame(body)
        header.pack(fill=tk.X)
        self.title_lbl = tk.Label(header, text="AERO CONTROL",
                                  font=(BASE_FONT, 13, "bold"))
        self.title_lbl.pack(side=tk.LEFT)
        self.theme_btn = tk.Button(header, width=3, bd=0, relief=tk.FLAT,
                                   cursor="hand2", command=self.toggle_theme)
        self.theme_btn.pack(side=tk.RIGHT)
        self.accent_bar = tk.Frame(body, height=2)
        self.accent_bar.pack(fill=tk.X, pady=(6, 12))

        self.swatch = tk.Canvas(body, width=self.S(260), height=self.S(56),
                                highlightthickness=1, highlightbackground="#999")
        self.swatch.pack()
        self.swatch.bind("<Button-1>", lambda e: self.pick_color())

        presets = tk.Frame(body)
        presets.pack(pady=(self.S(10), 0))
        for hex_color, name in PRESETS:
            sw = tk.Canvas(presets, width=self.S(24), height=self.S(24), bg=hex_color,
                           highlightthickness=1, highlightbackground="#999",
                           cursor="hand2")
            sw.pack(side=tk.LEFT, padx=self.S(3))
            sw.bind("<Button-1>", lambda e, c=hex_color: self.set_hex(c))

        self._round_btns = []   # plain themed pill buttons, re-styled on theme change
        pick = RoundedButton(body, text="Pick color…", command=self.pick_color,
                             scale=self.ui_scale, width=260)
        pick.pack(pady=(self.S(10), 0))
        self._round_btns.append(pick)

        # Rainbow pulse toggle: a hue-gradient strip, no text. Accent border = active.
        rw, rh = self.S(260), self.S(26)
        self.rainbow_btn = tk.Canvas(body, width=rw, height=rh, cursor="hand2",
                                     highlightthickness=2, highlightbackground="#999")
        for px in range(rw):
            c = colorsys.hsv_to_rgb(px / rw, 1, 1)
            self.rainbow_btn.create_line(px, 0, px, rh,
                                         fill="#{:02x}{:02x}{:02x}".format(
                                             *(round(v * 255) for v in c)))
        self.rainbow_btn.pack(pady=(self.S(10), 0))
        self.rainbow_btn.bind("<Button-1>", lambda e: self.toggle_rainbow())

        brow = tk.Frame(body)
        brow.pack(fill=tk.X, pady=(self.S(12), 0))
        tk.Label(brow, text="Brightness").pack(side=tk.LEFT)
        setdef = RoundedButton(brow, text="Set default", command=self.set_brightness_default,
                               scale=self.ui_scale, width=74, height=20,
                               radius=6, font_size=8)
        setdef.pack(side=tk.RIGHT)
        self._round_btns.append(setdef)
        self.brightness = ModernSlider(body, from_=0, to=100, length=self.S(260),
                                       command=self.on_slide, scale=self.ui_scale)
        self.brightness.on_release(self.on_slide_done)
        self.brightness.set(brightness)
        self.brightness.pack()

        loff = RoundedButton(body, text="Lights off", command=self.lights_off,
                             scale=self.ui_scale, width=260)
        loff.pack(pady=(self.S(12), 0))
        self._round_btns.append(loff)

        # Screen brightness, stepped. One step below the dimmest turns the panel
        # off; any input brings it back.
        self._screen_restore = None    # brightness to put back after a blank
        self._last_pct = None          # last brightness seen (F5 snap-to-5)
        self._overlay = None           # fullscreen black cover, if showing
        self._overlay_at = 0.0         # when it went up (input debounce)
        self._overlay_space_only = False  # Screen-off button: only Space wakes
        srow = tk.Frame(body)
        srow.pack(fill=tk.X, pady=(self.S(12), 0))
        tk.Label(srow, text="Screen").pack(side=tk.LEFT)
        self.screen_lbl = tk.Label(srow, text="--")
        self.screen_lbl.pack(side=tk.LEFT, padx=(self.S(6), 0))
        sup = RoundedButton(srow, text="+", command=lambda: self.step_screen(+1),
                            scale=self.ui_scale, width=40, height=22,
                            radius=6, font_size=9, bold=True)
        sup.pack(side=tk.RIGHT)
        sdn = RoundedButton(srow, text="−", command=lambda: self.step_screen(-1),
                            scale=self.ui_scale, width=40, height=22,
                            radius=6, font_size=9, bold=True)
        sdn.pack(side=tk.RIGHT, padx=(0, self.S(4)))
        self._round_btns += [sup, sdn]
        soff = RoundedButton(body, text="Screen off  (Space to wake)",
                             command=lambda: self.screen_black(space_only=True),
                             scale=self.ui_scale, width=260)
        soff.pack(pady=(self.S(6), 0))
        self._round_btns.append(soff)
        self._refresh_screen_lbl()
        self._start_brightness_watch()

        # Max Fan toggle: one button to slam the fans to full for gaming.
        self.fan_on = False
        self.fan_btn = RoundedButton(body, text="Max Fan: OFF", command=self.toggle_fan,
                                     scale=self.ui_scale, width=260)
        self.fan_btn.pack(pady=(self.S(6), 0))

        # Power mode: 3-position switch (Battery / Auto / Performance).
        self.mode = load_mode()
        self._pre_brightness = None   # user brightness before a battery dim
        self._lowered_refresh = False  # whether we dropped the panel to 60Hz
        self._auto_job = None         # debounce timer for AC/DC changes
        self._auto_last = None        # firmware profile Auto last applied
        tk.Label(body, text="Power mode").pack(pady=(14, 0), anchor=tk.W)
        seg = tk.Frame(body)
        seg.pack(fill=tk.X)
        self.mode_btns = {}
        for i, key in enumerate(UI_MODES):
            b = RoundedButton(seg, text=MODE_LABEL[key],
                              command=lambda k=key: self.select_mode(k),
                              scale=self.ui_scale, width=82, font_size=8, bold=True)
            b.pack(side=tk.LEFT, expand=True, padx=(0 if i == 0 else self.S(4), 0))
            self.mode_btns[key] = b
        self.mode_status = tk.Label(body, text="", fg="#555", anchor=tk.W,
                                    justify=tk.LEFT, wraplength=252)
        self.mode_status.pack(fill=tk.X, pady=(4, 0))

        # Per-mode extras (opt-in).
        opts = load_opts()
        self._auto_game_on = False   # Auto currently boosted to Performance
        self._game_miss = 0          # consecutive no-game ticks (revert hysteresis)
        self.ab_oc_profile = opts["ab_oc"]
        self.ab_stock_profile = opts["ab_stock"]
        self.opt_igpu = tk.BooleanVar(value=opts["igpu"])
        self.opt_ab = tk.BooleanVar(value=opts["ab"])
        self.opt_game = tk.BooleanVar(value=opts["game"])
        self.opt_acdc = tk.BooleanVar(value=opts["acdc"])
        self.opt_gpufix = tk.BooleanVar(value=opts["gpufix"])
        of = tk.Frame(body)
        of.pack(fill=tk.X, pady=(6, 0))
        self.opts_frame = of
        tk.Checkbutton(of, text="Battery: apps → iGPU (let dGPU sleep)",
                       variable=self.opt_igpu, anchor=tk.W,
                       command=lambda: save_opt("igpu_battery", self.opt_igpu.get())
                       ).pack(fill=tk.X)
        tk.Checkbutton(of, text="Auto: boost to Performance for games (on AC)",
                       variable=self.opt_game, anchor=tk.W,
                       command=lambda: save_opt("auto_game", self.opt_game.get())
                       ).pack(fill=tk.X)
        tk.Checkbutton(of, text="Performance: overclock GPU (Afterburner)",
                       variable=self.opt_ab, anchor=tk.W,
                       command=self._toggle_ab).pack(fill=tk.X)
        tk.Checkbutton(of, text="Auto-switch: AC → Auto, unplugged → Battery",
                       variable=self.opt_acdc, anchor=tk.W,
                       command=lambda: save_opt("acdc_switch", self.opt_acdc.get())
                       ).pack(fill=tk.X)
        tk.Checkbutton(of, text="Battery: reset dGPU if it gets stuck awake",
                       variable=self.opt_gpufix, anchor=tk.W,
                       command=lambda: save_opt("gpu_autoreset", self.opt_gpufix.get())
                       ).pack(fill=tk.X)

        # Live power meter (battery watts / % / time-left). No GPU polling.
        self.meter = PowerMeter()
        # Located once: the passive power-state read is cheap, but walking the
        # whole PCI list every 2 s would not be.
        self._dgpu_id = find_dgpu_id()
        self.meter_label = tk.Label(body, text="", fg="#333", anchor=tk.W,
                                    justify=tk.LEFT,
                                    font=("Segoe UI", 9, "bold"))
        self.meter_label.pack(fill=tk.X, pady=(8, 0))

        # EcoQoS state (throttled pids while in battery profile).
        self._eco_on = False
        self._eco_pids = set()

        # Worker-thread lever plumbing (mode applies never block the GUI).
        self._apply_gen = 0
        self._apply_lock = threading.Lock()
        self._status_pending = None
        self._setup_prompt_pending = False
        self._pending_refresh = None
        self._refresh_wait = 0

        # Watcher: 10-min lightweight profile of real use -> auto-tune battery.
        self._watching = False
        self._watch_job = None
        self.watch_btn = RoundedButton(body, text="Watch & tune battery (10 min)",
                                       command=self.toggle_watch,
                                       scale=self.ui_scale, width=260)
        self.watch_btn.pack(pady=(self.S(6), 0))
        self._round_btns.append(self.watch_btn)

        self._slide_job = None
        self._last_hid = 0.0
        self.tray = self.make_tray()
        self.tray.run_detached()
        root.protocol("WM_DELETE_WINDOW", self.hide_to_tray)
        self._devchg_job = None
        start_listener(on_up=lambda: root.after(0, self.step_brightness, +1),
                       on_down=lambda: root.after(0, self.step_brightness, -1),
                       on_resume=lambda: root.after(1500, self.reapply_after_resume),
                       on_power_change=lambda ac: root.after(0, self._on_power_change, ac),
                       on_battery_pct=lambda p: root.after(0, self._on_battery_pct, p),
                       on_device_change=lambda: root.after(0, self._on_device_change))
        self.refresh_swatch()
        if self.rainbow:
            self.start_rainbow()
        else:
            self.apply()
        # Reflect + re-assert the saved power mode (silent on first run before
        # the elevated tasks are installed). With auto-switching on, the power
        # source decides the starting mode instead of whatever was saved.
        if self.opt_acdc.get():
            self.mode = "auto" if power_source_is_ac() else "battery"
        self.select_mode(self.mode, initial=True)
        self._tick_meter()
        self._levers_tick()
        self.root.after(8000, self._game_tick)
        self.root.after(300_000, self._lamp_guard_tick)
        self._dgpu_d0 = 0            # consecutive minutes seen awake
        self._dgpu_last_reset = 0.0
        self.root.after(90_000, self._dgpu_tick)
        self.apply_theme(self.theme)
        # Re-assert the dark title bar once the window is mapped (the DWM change
        # doesn't stick if applied before the frame exists).
        self.root.after(400, lambda: set_titlebar_dark(
            self.root, THEMES.get(self.theme, {}).get("titlebar_dark", False)))

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
        # ModernSlider.set() doesn't fire the command callback (tk.Scale did),
        # so drive the lamp NOW and debounce the save behind held-down repeats.
        self.refresh_swatch()
        if not self.rainbow:
            self.lamp.set(self.rgb, nxt)
        if self._slide_job:
            self.root.after_cancel(self._slide_job)
        self._slide_job = self.root.after(300, self.apply)

    # 0 is a real, firmware-accepted level on this panel (verified: asked for 0,
    # reported 0, not clamped). Reaching 0 also raises the black overlay, so 0%
    # is the "screen off" -- a window plus the backlight floor, with no power
    # state change at all. Nothing sleeps, locks or gets suspended.
    SCREEN_STEPS = [0, 5, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100]

    def _refresh_screen_lbl(self):
        b = get_brightness()
        if b is not None:
            self._last_pct = b
        self.screen_lbl.config(text="--" if b is None else "%d%%" % b)

    def step_screen(self, direction):
        """Step panel brightness. The bottom step is 0%, which also raises the
        black overlay -- that is the "screen off". No power state change, so
        nothing sleeps and the session never locks."""
        cur = get_brightness()
        if cur is None:
            self._set_mode_status("No software brightness control on this display.")
            return
        if direction > 0:
            nxt = next((s for s in self.SCREEN_STEPS if s > cur), 100)
        else:
            nxt = next((s for s in reversed(self.SCREEN_STEPS) if s < cur), None)
            if nxt is None:
                return                      # already at 0 with the cover up
        if nxt == 0:
            self.screen_black()             # sets 0% and covers the screen
            return
        set_brightness(nxt)
        self._refresh_screen_lbl()

    def _start_brightness_watch(self):
        """Track panel brightness from ANY source -- the F5/F6 hardware keys
        included -- without polling.

        Polling measured 5.36 ms per WMI read: 9 mW at 1 Hz, against an app
        whose entire periodic budget is 0.17 mW. So instead this blocks on a
        WMI extrinsic event and costs nothing while brightness is not moving.
        The 10 s NextEvent timeout is only so the thread is not parked forever;
        a timeout is not an error (see _wbem_timed_out)."""
        def work():
            import pythoncom
            import win32com.client
            pythoncom.CoInitialize()
            try:
                loc = win32com.client.Dispatch("WbemScripting.SWbemLocator")
                svc = loc.ConnectServer(".", r"root\wmi")
                q = svc.ExecNotificationQuery(
                    "SELECT * FROM WmiMonitorBrightnessEvent", "WQL",
                    48)          # forwardOnly | returnImmediately
            except Exception:
                return           # no watcher; the +/- buttons still work
            while True:
                try:
                    evt = q.NextEvent(10000)
                except Exception as e:
                    if _wbem_timed_out(e):
                        continue
                    return
                try:
                    pct = int(evt.Brightness)
                except Exception:
                    continue
                self.root.after(0, self._on_brightness_event, pct)

        threading.Thread(target=work, daemon=True).start()

    def _on_brightness_event(self, pct):
        """Brightness moved -- by F5/F6, our own buttons, or a power profile.
        Keep the readout in step, and treat 0% as 'screen off' no matter who
        caused it, so F5 all the way down raises the cover."""
        prev, self._last_pct = self._last_pct, pct
        if self._overlay is not None and self._overlay_space_only:
            # Only Space ends this cover. F6 or a power profile raising the
            # level would light the backlight under it, so push it back to 0.
            if pct > 0:
                set_brightness(0)
            return
        ours = brightness_is_ours(pct)
        if (not ours and pct == 0 and self._overlay is None
                and prev is not None and prev > 5):
            # F5 is firmware-stepped and jumps straight past 5 (e.g. 10 -> 0).
            # Land on 5 first; the next F5 (5 -> 0) raises the cover.
            set_brightness(5)
            return
        if not ours and prev == 0 and pct > 5:
            # Same on the way up: F6 from 0 jumps to 10. Stop at 5, drop the
            # cover, and don't restore the pre-blank level over the user's key.
            self._screen_restore = None
            if self._overlay is not None:
                self.screen_black_off()
            set_brightness(5)
            return
        try:
            self.screen_lbl.config(text="%d%%" % pct)
        except Exception:
            return
        if pct == 0:
            if self._overlay is None:
                self.screen_black()
        elif self._overlay is not None:
            # Brightness was raised (F6) while covered. Drop the cover, but do
            # NOT restore the old level -- the user just picked this one.
            self._screen_restore = None
            self.screen_black_off()

    def screen_black(self, space_only=False):
        """Cover every screen with black and drop brightness to 0, WITHOUT any
        power-state change. Nothing sleeps, nothing locks, nothing is suspended
        -- this is just a window. About as dark as an IPS panel gets while fully
        awake (the backlight floor still glows a little).

        Dismissal is deliberately belt-and-braces. The window's own key/click/
        motion bindings are the fast path, but _levers_tick ALSO watches
        user_idle_ms() once a second and tears the overlay down on any input.
        That second path does not depend on Tk delivering an event, so the
        overlay still goes away even if something steals focus.

        space_only (the Screen-off button): mouse, touchpad and every other key
        are swallowed; only Space wakes. The idle watchdog is off for this mode,
        so its independent path is _space_poll, which reads the GLOBAL key state
        every 50 ms -- Space still works even if the cover has lost focus."""
        if self._overlay is not None:
            return
        ov = tk.Toplevel(self.root)
        ov.overrideredirect(True)          # no title bar, no close button
        ov.configure(bg="black", cursor="none")
        ov.attributes("-topmost", True)
        # Cover the whole virtual desktop, not just the primary monitor.
        g = ctypes.windll.user32.GetSystemMetrics
        vx, vy, vw, vh = g(76), g(77), g(78), g(79)   # SM_*VIRTUALSCREEN
        ov.geometry("%dx%d+%d+%d" % (vw, vh, vx, vy))
        if space_only:
            ov.bind("<KeyPress-space>", self._overlay_event)
        else:
            for seq in ("<Key>", "<Button>", "<Motion>"):
                ov.bind(seq, self._overlay_event)
        self._overlay = ov
        self._overlay_at = time.monotonic()
        self._overlay_space_only = space_only
        try:
            ov.focus_force()
        except Exception:
            pass
        set_brightness(0)
        if space_only:
            ctypes.windll.user32.GetAsyncKeyState(0x20)   # clear stale "was pressed"
            self._space_poll(0)

    def _space_poll(self, n):
        """Focus-independent Space detector for the space-only cover. Bit 15 =
        down now, bit 0 = pressed since last call (catches taps under 50 ms)."""
        ov = self._overlay
        if ov is None or not self._overlay_space_only:
            return
        st = ctypes.windll.user32.GetAsyncKeyState(0x20)
        if (st & 0x8001) and time.monotonic() - self._overlay_at > 0.5:
            self.screen_black_off()
            return
        if n % 20 == 0:                       # ~1 s: stay above anything new
            try:
                ov.attributes("-topmost", True)
                ov.lift()
            except Exception:
                pass
        self.root.after(50, self._space_poll, n + 1)

    def _overlay_event(self, _evt=None):
        # The click that opened it, and the pointer already sitting under the
        # new window, both fire immediately -- ignore the first half second.
        if time.monotonic() - self._overlay_at < 0.5:
            return
        self.screen_black_off()

    def screen_black_off(self):
        """Take the overlay down and wake at 5% -- whatever woke it and however
        it went dark -- so one more F5 (5 -> 0) goes straight back to dark.
        Idempotent."""
        ov, self._overlay = self._overlay, None
        self._overlay_space_only = False
        if ov is not None:
            try:
                ov.destroy()
            except Exception:
                pass
        if ov is not None:
            self._screen_restore = None
            set_brightness(5)
            try:
                self.screen_lbl.config(text="5%")
            except Exception:
                pass

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
        # Swatch (the on-screen color) tracks the knob with zero latency.
        self.refresh_swatch()
        # LED itself updates live but throttled (~33 Hz) so we don't flood the
        # HID controller; the final value + disk save land on release.
        now = time.monotonic()
        if not self.rainbow and now - self._last_hid >= 0.03:
            self._last_hid = now
            self.lamp.set(self.rgb, self.brightness.get())

    def on_slide_done(self, _value):
        # Fired when the drag ends: assert the final level and persist.
        self.apply()

    def set_brightness_default(self):
        """Pin the current slider level as the startup brightness."""
        lvl = self.brightness.get()
        save_opt("brightness_default", lvl)
        self._set_mode_status(f"Startup brightness set to {lvl}%.")

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
        t = getattr(self, "_t", THEMES["dark"])
        if self.fan_on:
            self.fan_btn.set_text("Max Fan: ON")
            self.fan_btn.colors(t["bg"], "#0078d7", "#1a86e0", "white")
        else:
            self.fan_btn.set_text("Max Fan: OFF")
            self.fan_btn.colors(t["bg"], t["btn"], t["btn_hover"], t["fg"])

    # -- power modes -----------------------------------------------------

    def select_mode(self, key, initial=False):
        """User picked a power position (or startup re-assert)."""
        if key not in UI_MODES:
            return
        self.mode = key
        save_mode(key)
        self._update_mode_btns()
        # GPU util polling wakes the dGPU, so nothing turns it on here -- only
        # _game_tick does, and only while a fullscreen window is actually up.
        if getattr(self, "meter", None) and key != "auto":
            self.meter.poll_gpu = False
        if key == "auto":
            self._auto_apply(initial=initial)
        else:
            self._apply_profile(key, initial=initial, displays=True)
        if hasattr(self, "tray"):
            self.tray.update_menu()

    def _apply_profile(self, profile, initial=False, displays=True, note=None):
        """Apply a firmware profile (via the elevated task) plus OS levers.
        `profile` is a POWER_FW key. `displays` gates the visible levers
        (brightness/refresh) so Auto never yanks them on plug/unplug."""
        # All levers run on a worker thread: schtasks, WMI brightness, and the
        # rest would otherwise stall the GUI (and compound the system's own
        # plug/unplug hitch). A generation counter makes rapid switches
        # last-wins; the lock keeps two applies from interleaving.
        self._apply_gen += 1
        gen = self._apply_gen

        def work():
            with self._apply_lock:
                if gen != self._apply_gen:
                    return              # a newer mode was picked; skip this one
                err = run_mode_task(profile)
                if err == "setup":
                    if not initial:
                        self._setup_prompt_pending = True
                    self._status_pending = "⚠ Install the app to enable power modes."
                    return
                if err:
                    self._status_pending = f"⚠ {err}"
                    return
                self._apply_os(profile, displays=displays)
                self._apply_gpu_extras(profile, displays=displays)
                self._auto_last = profile
                self._status_pending = (note or
                                        f"{MODE_LABEL.get(self.mode, profile)} active.")

        self._eco_set(profile == "battery")   # scheduling stays on the GUI side
        threading.Thread(target=work, daemon=True).start()

    def _apply_gpu_extras(self, profile, displays=True):
        # dGPU discipline: nudge apps to iGPU on battery, dGPU on performance
        # (invisible; safe in Auto). Applies to each app on its next launch.
        if self.opt_igpu.get():
            set_apps_gpu({"battery": 1, "performance": 2}.get(profile))
        # (Afterburner profile load/close happens in the elevated --applyfw
        # worker: Afterburner runs as admin, out of this process's reach.)

    def _toggle_ab(self):
        save_opt("ab_oc", self.opt_ab.get())
        if self.opt_ab.get() and not afterburner_exe():
            from tkinter import messagebox
            messagebox.showinfo(
                "MSI Afterburner not found",
                "Install MSI Afterburner, then in it tune a STABLE GPU overclock "
                "and save it as Profile 1 (and save a stock/default Profile 5).\n\n"
                "Performance mode will load Profile 1; the other modes load "
                "Profile 5. The app never invents overclock values.")

    def _apply_os(self, profile, displays=True):
        os_cfg = POWER_OS.get(profile, {})
        if os_cfg.get("overlay"):
            set_power_overlay(os_cfg["overlay"])
        br = os_cfg.get("brightness")
        rf = os_cfg.get("refresh")
        if profile == "battery":
            br = int(_read_config().get("battery_brightness", br or 40))
        # Always undo a previous battery dim / 60Hz drop when moving to a
        # non-dimming profile -- even in Auto -- so the screen never stays dark
        # or stuck at 60Hz.
        if br is None and self._pre_brightness is not None:
            set_brightness(self._pre_brightness)
            self._pre_brightness = None
        # Refresh-rate changes stall the compositor (~1 s system-wide cursor
        # freeze), so they are never applied inline: _levers_tick fires them
        # once the user's input has paused, when the freeze can't be felt.
        if (rf is None or rf == 0) and self._lowered_refresh:
            self._pending_refresh = 0
        if not displays:
            return
        if br is not None:
            cur = get_brightness()
            if self._pre_brightness is None and cur is not None:
                self._pre_brightness = cur          # remember to restore later
            if cur is None or br < cur:
                set_brightness(br)                  # only ever dim, never brighten
        if rf is not None and rf != 0:
            self._pending_refresh = rf

    def _auto_apply(self, initial=False):
        """Adaptive middle mode. On AC: Balanced (or Performance if a game is
        detected and enabled). On battery: Battery-saver, escalating the visible
        levers (dim + 60Hz) as the charge drops -- <=20% low, <=10% critical."""
        ac = power_source_is_ac()
        if ac:
            self._auto_game_on = False
            self._apply_profile("balanced", initial=initial, displays=False,
                                note="Auto: on AC → Balanced.")
            return
        pct = self.meter.pct if self.meter else None
        if pct is not None and pct <= 10:
            note, disp = f"on battery {pct}% (critical) → max saving", True
        elif pct is not None and pct <= 20:
            note, disp = f"on battery {pct}% (low) → saving harder", True
        else:
            note, disp = "on battery → Battery-saver", False
        self._apply_profile("battery", initial=initial, displays=disp,
                            note=f"Auto: {note}.")

    def _on_power_change(self, is_ac):
        # Debounce plug/unplug bounces (docks/loose plugs can chatter), then
        # settle on whatever the power source actually is.
        if self._auto_job:
            self.root.after_cancel(self._auto_job)
        self._auto_job = self.root.after(4000, self._acdc_settle)

    def _acdc_settle(self):
        """4 s after the last plug/unplug event: if auto-switching is on, move
        the whole app to Auto (AC) / Battery (unplugged). Same code path as a
        manual click, so it is exactly as stable; no-op if already there."""
        self._auto_job = None
        ac = power_source_is_ac()
        if self.opt_acdc.get():
            target = "auto" if ac else "battery"
            if self.mode != target:
                self.select_mode(target)
                self._set_mode_status("Plugged in → Auto." if ac
                                      else "Unplugged → Battery.")
                return
        if self.mode == "auto":
            self._auto_apply()

    def _on_battery_pct(self, pct):
        # Re-evaluate Auto's escalation when the charge level crosses a step.
        if self.mode != "auto" or self._auto_game_on:
            return
        if self._auto_job:
            self.root.after_cancel(self._auto_job)
        self._auto_job = self.root.after(1500, self._auto_apply)

    def _game_tick(self):
        """Auto-only: when a real game is running on AC, boost to Performance;
        drop back when it exits. GPU-load gated so fullscreen video / desktop
        don't trigger it. Runs every ~8s."""
        try:
            if (self.mode == "auto" and self.opt_game.get()
                    and power_source_is_ac()):
                # The GPU 3D-engine counter enumerates EVERY adapter, dGPU
                # included, and reading it wakes an Optimus dGPU. Blackwell
                # laptop parts only re-enter RTD3 after ~21 s idle, so a 3 s
                # poll pins the card in D0 forever -- ~5-10 W, even on AC.
                # Fullscreen is the cheap precondition for "a game might be
                # running", so only poll while one is actually in front.
                fs = foreground_is_fullscreen()
                if self.meter:
                    self.meter.poll_gpu = fs
                util = self.meter.gpu_util if self.meter else None
                gaming = fs and (util is not None and util >= 40)
                if gaming and not self._auto_game_on:
                    self._auto_game_on = True
                    self._game_miss = 0
                    self._apply_profile("performance", displays=True,
                                        note="Auto: game detected → Performance.")
                elif not gaming and self._auto_game_on:
                    self._game_miss += 1
                    if self._game_miss >= 2:   # ~16s of no game before dropping
                        self._auto_game_on = False
                        self._auto_apply()
            else:
                # Outside the game-detect path entirely (battery, boost off,
                # not Auto): never leave the dGPU-waking counter poll running.
                if self.meter:
                    self.meter.poll_gpu = False
                if self._auto_game_on and self.mode != "auto":
                    self._auto_game_on = False
        finally:
            self.root.after(8000, self._game_tick)

    def _update_mode_btns(self):
        t = getattr(self, "_t", THEMES["dark"])
        for key, b in self.mode_btns.items():
            if key == self.mode:
                c = MODE_COLOR[key]
                b.colors(t["bg"], c, c, "white")
            else:
                b.colors(t["bg"], t["btn"], t["btn_hover"], t["fg"])

    def _set_mode_status(self, text):
        if hasattr(self, "mode_status"):
            self.mode_status.configure(text=text)

    # -- theme -----------------------------------------------------------

    def toggle_theme(self):
        self.apply_theme("light" if self.theme == "dark" else "dark")

    def _walk_theme(self, w, t):
        cls = w.winfo_class()
        try:
            if cls in ("Frame", "Labelframe"):
                w.configure(bg=t["bg"])
            elif cls == "Label":
                w.configure(bg=t["bg"], fg=t["fg"], font=(BASE_FONT, 10))
            elif cls == "Button":
                w.configure(bg=t["btn"], fg=t["fg"], activebackground=t["btn_hover"],
                            activeforeground=t["fg"], relief=tk.FLAT, bd=0,
                            highlightthickness=0, cursor="hand2",
                            font=(BASE_FONT, 10), padx=10, pady=6)
            elif cls == "Checkbutton":
                w.configure(bg=t["bg"], fg=t["sub"], selectcolor=t["entry"],
                            activebackground=t["bg"], activeforeground=t["fg"],
                            highlightthickness=0, font=(BASE_FONT, 9))
            elif cls == "Scale":
                w.configure(bg=t["bg"], fg=t["sub"], troughcolor=t["entry"],
                            highlightthickness=0, activebackground=t["accent"],
                            bd=0, font=(BASE_FONT, 8))
            elif cls == "Canvas":
                w.configure(highlightbackground=t["border"])
        except tk.TclError:
            pass
        for c in w.winfo_children():
            self._walk_theme(c, t)

    def apply_theme(self, name):
        t = THEMES.get(name, THEMES["dark"])
        self._t = t
        self.theme = name
        self._fg = t["fg"]
        self.root.configure(bg=t["bg"])
        self._walk_theme(self.root, t)
        self.accent_bar.configure(bg=t["accent"])
        self.title_lbl.configure(bg=t["bg"], fg=t["accent"], font=(BASE_FONT, 13, "bold"))
        # Restore widgets whose colors carry state (canvases keep their content).
        for b in getattr(self, "_round_btns", []):
            b.colors(t["bg"], t["btn"], t["btn_hover"], t["fg"])
        self._update_mode_btns()
        self.update_fan_btn()
        self.mode_status.configure(fg=t["sub"], font=(BASE_FONT, 9))
        self.meter_label.configure(fg=t["accent"], font=(BASE_FONT, 11, "bold"))
        if isinstance(getattr(self, "brightness", None), ModernSlider):
            self.brightness.theme(t)
        # Sun to switch to light (while dark), moon to switch to dark (while light).
        self.theme_btn.configure(text=("☀" if name == "dark" else "☾"),
                                 bg=t["bg"], fg=t["accent"], activebackground=t["bg"],
                                 activeforeground=t["accent"],
                                 font=("Segoe UI Symbol", 13), padx=2, pady=0)
        set_titlebar_dark(self.root, t.get("titlebar_dark", False))
        save_opt("theme", name)

    def _tick_meter(self):
        m = self.meter
        if m.on_ac:
            txt = "On AC" + (f"  ·  {m.pct}%" if m.pct is not None else "")
        else:
            parts = []
            if m.watts:
                parts.append(f"{m.watts:.1f} W")
            if m.pct is not None:
                parts.append(f"{m.pct}%")
            if m.mins:
                parts.append(f"~{m.mins // 60}h{m.mins % 60:02d}m left")
            txt = "Battery:  " + "  ·  ".join(parts) if parts else "Battery"
        # dGPU state, read from the PnP cache -- showing it costs nothing and,
        # unlike nvidia-smi, asking the question doesn't wake the thing up.
        dg = dgpu_power_state(self._dgpu_id)
        if dg:
            txt += "\ndGPU " + ("asleep" if dg.startswith("D3") else "AWAKE")
        self.meter_label.config(text=txt)
        self.root.after(2000, self._tick_meter)

    def _mode_setup_prompt(self):
        from tkinter import messagebox
        messagebox.showinfo(
            "Power modes setup needed",
            "The power-mode tasks aren't registered yet.\n\n"
            "Install the app (it self-elevates once), or from source run, as "
            "administrator:\n  setup_fan_task.ps1\n\n"
            "That registers the elevated tasks the power modes use. One time only.")

    def tray_set_mode(self, key):
        self.root.after(0, self.select_mode, key)

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

    def _on_device_change(self):
        """HID devices re-enumerate in bursts (boot, dock, sleep/wake). Settle
        2 s after the last one, then re-assert our color in case the keyboard
        controller was among them (it reverts to its firmware effect)."""
        if self._devchg_job:
            self.root.after_cancel(self._devchg_job)
        self._devchg_job = self.root.after(2000, self._devchg_settle)

    def _devchg_settle(self):
        self._devchg_job = None
        if not self.rainbow:
            self.lamp.set(self.rgb, self.brightness.get())

    # A Blackwell laptop dGPU only re-enters RTD3 after ~21 s untouched, so one
    # stray poll can pin it in D0 for the rest of the session. Measured here:
    # 16.3 W stuck awake vs 10.9 W once it drops back to D3. Reloading the
    # driver makes it re-evaluate. The guards below keep us from ever yanking
    # the device out from under real work.
    DGPU_STUCK_TICKS = 3         # ticks are 60 s apart -> 3 min of solid D0
    DGPU_COOLDOWN_S = 900        # and never more often than once per 15 min

    def _dgpu_tick(self):
        try:
            if (not self.opt_gpufix.get() or not self._dgpu_id
                    or power_source_is_ac()):
                self._dgpu_d0 = 0
                return
            if dgpu_power_state(self._dgpu_id) != "D0":
                self._dgpu_d0 = 0
                return
            # Awake. A reset drops the device for a second, so never while a
            # fullscreen app is up or while the user is actually working.
            if foreground_is_fullscreen() or user_idle_ms() < 120_000:
                return
            self._dgpu_d0 += 1
            if (self._dgpu_d0 < self.DGPU_STUCK_TICKS
                    or time.monotonic() - self._dgpu_last_reset
                    < self.DGPU_COOLDOWN_S):
                return
            # It is already awake, so reading the GPU counters costs nothing
            # here -- and it tells us whether anything is genuinely using it.
            if dgpu_busy():
                self._dgpu_d0 = 0
                return
            self._dgpu_d0 = 0
            self._dgpu_last_reset = time.monotonic()
            threading.Thread(target=self._dgpu_reset_work, daemon=True).start()
        finally:
            self.root.after(60_000, self._dgpu_tick)

    def _dgpu_reset_work(self):
        """Fire the elevated reset task, then report what it achieved."""
        if run_fan_task(GPU_RESET_TASK):
            self._status_pending = ("⚠ dGPU stuck awake; reset task not "
                                    "registered (reinstall to add it).")
            return
        time.sleep(8)            # driver reload, then the RTD3 idle window
        st = dgpu_power_state(self._dgpu_id)
        self._status_pending = (
            "dGPU was stuck awake — driver reloaded"
            + (" (now asleep, ~5 W saved)." if st == "D3" else "."))

    def _lamp_guard_tick(self):
        """Every 5 min, quietly re-assert the color: cheap insurance against
        the firmware reclaiming the lamp (Fn shortcuts, driver blips)."""
        if not self.rainbow:
            self.lamp.set(self.rgb, self.brightness.get())
        self.root.after(300_000, self._lamp_guard_tick)

    def reapply_after_resume(self, attempt=0):
        """Firmware falls back to its own rainbow effect after sleep; put our
        settings back. Rainbow mode recovers by itself on the next tick."""
        if attempt == 0 and not self._overlay_space_only and (
                self._overlay is not None or self._screen_restore is not None):
            self.screen_black_off()
        if self.rainbow:
            return
        self.lamp.set(self.rgb, self.brightness.get())
        if attempt < 3:
            # The controller can come back late after resume; reassert a few times
            self.root.after(2500, self.reapply_after_resume, attempt + 1)

    # -- deferred levers (GUI-side 1 s tick) ------------------------------

    def _levers_tick(self):
        """Relays worker-thread results to the UI and fires the deferred
        refresh-rate switch only once the user's hands are still (>=1.5 s idle,
        or 15 s cap) -- the compositor stall then lands where nobody feels it."""
        if (self._overlay is not None and not self._overlay_space_only
                and time.monotonic() - self._overlay_at > 1.5
                and user_idle_ms() < 1200):
            # Independent of Tk event delivery -- the overlay always goes away.
            self.screen_black_off()
        s = self._status_pending
        if s is not None:
            self._status_pending = None
            self._set_mode_status(s)
        if self._setup_prompt_pending:
            self._setup_prompt_pending = False
            self._mode_setup_prompt()
        if self._pending_refresh is not None:
            self._refresh_wait += 1
            if user_idle_ms() >= 1500 or self._refresh_wait >= 15:
                hz = self._pending_refresh
                self._pending_refresh = None
                self._refresh_wait = 0

                def do_switch():
                    ok = set_refresh(hz)
                    self._lowered_refresh = bool(hz) and (ok or self._lowered_refresh)
                threading.Thread(target=do_switch, daemon=True).start()
        else:
            self._refresh_wait = 0
        self.root.after(1000, self._levers_tick)

    # -- EcoQoS (battery: throttle the Watcher's recorded burners) --------

    ECO_NEVER = {"keyboardlight", "explorer", "dwm", "csrss", "winlogon"}

    def _eco_targets(self):
        """Process names to throttle, learned by the Watcher (config), minus
        ourselves and the Windows shell. Empty until a watch has run."""
        names = _read_config().get("eco_targets") or []
        return [n for n in names if n.lower() not in self.ECO_NEVER]

    def _eco_set(self, on):
        """Enter/leave battery EcoQoS. Off restores every throttled pid."""
        if on:
            if not self._eco_on:
                self._eco_on = True
                self._eco_tick()
        else:
            self._eco_on = False
            for pid in self._eco_pids:
                set_process_eco(pid, False)
            self._eco_pids.clear()

    def _eco_tick(self):
        """Re-scan every 60 s while active: throttle new pids of target names,
        but always leave the current foreground process at full speed. The scan
        (a tasklist spawn) runs on a worker thread so the GUI never blocks."""
        if not self._eco_on:
            return

        def scan():
            targets = self._eco_targets()
            if not targets or not self._eco_on:
                return
            fg = foreground_pid()
            current = pids_by_names(targets)
            for pid in current:
                if pid == fg:
                    if pid in self._eco_pids:     # user brought it forward
                        set_process_eco(pid, False)
                        self._eco_pids.discard(pid)
                elif pid not in self._eco_pids and set_process_eco(pid, True):
                    self._eco_pids.add(pid)
            self._eco_pids &= set(current)   # exited pids need no restore

        threading.Thread(target=scan, daemon=True).start()
        self.root.after(60000, self._eco_tick)

    # -- watcher (10-min profile -> auto-tune battery) --------------------

    WATCH_SECS = 600
    WATCH_IDLE_MS = 60_000      # no keyboard/mouse for a minute = "idle" sample
    # A process must burn this many CPU-seconds in the window to earn EcoQoS.
    # 30 was too high: it sat right on top of CrossDeviceService's real rate
    # (~28 CPU-sec/10 min measured), so the biggest background burner on the
    # machine never made the list.
    ECO_MIN_CPU_SEC = 15

    def toggle_watch(self):
        if self._watching:
            self._watch_stop(cancelled=True)
            return
        if self.meter and self.meter.on_ac:
            from tkinter import messagebox
            messagebox.showinfo(
                "Watcher", "Unplug from AC first — the watcher measures real "
                "battery draw, which reads 0 while plugged in.")
            return
        self._watching = True
        # Suspend EcoQoS for the window: measure processes' NATURAL burn, not
        # the throttled version (comparisons vs pre-EcoQoS baselines stay fair).
        self._watch_eco_was = self._eco_on
        self._eco_set(False)
        self._watch_left = self.WATCH_SECS
        # (watts, cpu_pct, user_was_idle, dgpu_state) every ~5 s
        self._watch_samples = []
        self._watch_ac_break = False
        self._watch_dgpu_id = find_dgpu_id()
        self._watch_bright = []         # panel brightness %, sampled alongside
        self._watch_t0 = time.monotonic()
        self._watch_own0 = _own_cpu_seconds()
        self._watch_cpu_prev = _sys_times()
        self._watch_snap0 = proc_cpu_snapshot()   # heavy bit, outside the window
        self._watch_tick()

    def _watch_tick(self):
        if not self._watching:
            return
        self._watch_left -= 1
        if self._watch_left % 5 == 0:
            cur = _sys_times()
            if cur and self._watch_cpu_prev:
                pct = _cpu_pct(self._watch_cpu_prev, cur)
                self._watch_cpu_prev = cur
                if self.meter and self.meter.on_ac:
                    self._watch_ac_break = True   # plugged in: watts invalid
                elif self.meter and self.meter.watts:
                    # Idle vs active matters more than the blended average:
                    # runtime on a shelf is what battery life actually means.
                    idle = user_idle_ms() >= self.WATCH_IDLE_MS
                    # Passive read -- profiling the dGPU must never wake it.
                    dg = dgpu_power_state(self._watch_dgpu_id)
                    self._watch_samples.append((self.meter.watts, pct, idle, dg))
        if self._watch_left % 30 == 0:
            b = get_brightness()
            if b is not None:
                self._watch_bright.append(b)
        m, s = divmod(max(0, self._watch_left), 60)
        self.watch_btn.set_text(f"Watching… {m}:{s:02d}  (click to cancel)")
        if self._watch_left <= 0:
            self._watch_stop(cancelled=False)
            return
        self._watch_job = self.root.after(1000, self._watch_tick)

    def _watch_stop(self, cancelled):
        self._watching = False
        if self._watch_job:
            self.root.after_cancel(self._watch_job)
            self._watch_job = None
        self.watch_btn.set_text("Watch & tune battery (10 min)")
        if getattr(self, "_watch_eco_was", False):
            self._eco_set(True)          # resume the throttling we paused
        if cancelled:
            self._set_mode_status("Watch cancelled.")
            return
        self._watch_finish()

    def _watch_finish(self):
        from tkinter import messagebox
        snap1 = proc_cpu_snapshot()
        elapsed = time.monotonic() - self._watch_t0
        own = _own_cpu_seconds() - self._watch_own0   # the watcher watching itself
        samples = self._watch_samples
        if len(samples) < 12:   # < 1 minute of valid battery data
            messagebox.showinfo(
                "Watcher", "Not enough on-battery data to tune "
                "(was the laptop plugged in most of the time?). Nothing changed.")
            return
        watts = sorted(s[0] for s in samples)
        cpus = sorted(s[1] for s in samples)
        avg_w = sum(watts) / len(watts)
        p95_c = cpus[int(len(cpus) * 0.95) - 1]
        avg_c = sum(cpus) / len(cpus)
        # Idle draw is what actually sets battery runtime -- the blended average
        # just tells you how hard you happened to be working. Split them.
        idle_w = [s[0] for s in samples if s[2]]
        act_w = [s[0] for s in samples if not s[2]]
        avg_idle = sum(idle_w) / len(idle_w) if idle_w else None
        avg_act = sum(act_w) / len(act_w) if act_w else None
        # How much of the window the dGPU was awake. An RTX laptop part held out
        # of RTD3 costs ~8-15 W, which dwarfs every other lever here.
        seen = [s[3] for s in samples if s[3]]
        d0_pct = (100.0 * sum(1 for d in seen if d == "D0") / len(seen)) if seen else None
        avg_b = (sum(self._watch_bright) / len(self._watch_bright)
                 if self._watch_bright else None)
        # Top offenders: CPU-seconds burned by each process across the window.
        offenders = []
        for pid, (name, cpu1) in snap1.items():
            base = self._watch_snap0.get(pid)
            burn = cpu1 - base[1] if base and base[0] == name else cpu1
            if burn > 2 and name.lower() not in ("idle", "system"):
                offenders.append((burn, name))
        offenders.sort(reverse=True)
        # Pick the battery firmware tier from the observed load shape: SPL only
        # bites during SUSTAINED load, so low p95 CPU means a lower cap is free.
        tier = "light" if p95_c < 25 else ("medium" if p95_c < 50 else "default")
        save_opt("battery_tier", tier)
        # Dim harder if the machine draws a lot while doing nothing -- idle draw,
        # not the blended average, is what a dimmer actually buys back.
        bb = 30 if (avg_idle if avg_idle is not None else avg_w) > 10 else 40
        save_opt("battery_brightness", bb)
        # Teach battery mode its EcoQoS hit-list.
        eco = sorted({n for b, n in offenders
                      if b >= self.ECO_MIN_CPU_SEC and n.lower() != "keyboardlight"})
        save_opt("eco_targets", eco)
        spl = BATTERY_TIERS[tier][2] // 1000
        top = "\n".join(f"   {n}: {b/60:.1f} CPU-min" for b, n in offenders[:6]) or "   (none)"
        # Findings the tier/dim knobs can't fix, ranked by what they cost.
        flags = []
        if d0_pct is not None and d0_pct >= 20:
            flags.append(f"⚠ dGPU was awake {d0_pct:.0f}% of the window. An RTX "
                         f"laptop GPU held out of RTD3 burns ~8-15 W — far more "
                         f"than any setting below. Find what is waking it.")
        elif d0_pct is not None:
            flags.append(f"✓ dGPU asleep {100 - d0_pct:.0f}% of the window.")
        if avg_b is not None and avg_b >= 60:
            flags.append(f"⚠ Panel averaged {avg_b:.0f}% brightness. Dropping to "
                         f"~35% is worth roughly 2-3 W on this panel.")
        draw = (f"Idle draw: {avg_idle:.1f} W" if avg_idle is not None
                else "Idle draw: (never idle this window)")
        if avg_act is not None:
            draw += f"   ·   Active draw: {avg_act:.1f} W"
        note = (f"Watched {elapsed/60:.1f} min ({len(samples)} samples)"
                + (" — some time on AC was excluded" if self._watch_ac_break else "") + "\n"
                f"{draw}   ·   blended {avg_w:.1f} W\n"
                f"CPU avg {avg_c:.0f}% / p95 {p95_c:.0f}%\n"
                f"Watcher's own cost: {own:.1f} CPU-sec ({100*own/max(1e-9,elapsed):.2f}% of one core)\n\n"
                + ("\n".join(flags) + "\n\n" if flags else "")
                + f"Top CPU burners:\n{top}\n\n"
                f"Tuned battery mode → {tier} tier (sustained cap {spl} W), "
                f"dim target {bb}%.\n"
                f"EcoQoS on battery: {', '.join(eco) if eco else '(none)'}")
        try:
            log = app_data_dir() / "watch_log.json"
            payload = json.dumps(dict(
                when=time.strftime("%Y-%m-%d %H:%M:%S"), minutes=round(elapsed / 60, 1),
                samples=len(samples), avg_watts=round(avg_w, 2), avg_cpu=round(avg_c, 1),
                idle_watts=round(avg_idle, 2) if avg_idle is not None else None,
                active_watts=round(avg_act, 2) if avg_act is not None else None,
                idle_samples=len(idle_w),
                dgpu_d0_pct=round(d0_pct, 1) if d0_pct is not None else None,
                avg_panel_brightness=round(avg_b, 1) if avg_b is not None else None,
                p95_cpu=round(p95_c, 1), watcher_cpu_sec=round(own, 2), tier=tier,
                brightness=bb, eco_targets=eco, ac_break=self._watch_ac_break,
                offenders=[dict(name=n, cpu_sec=round(b, 1)) for b, n in offenders[:10]]))
            log.write_text(payload)                       # latest run
            with open(app_data_dir() / "watch_history.jsonl", "a",
                      encoding="utf-8") as f:             # every run, appended
                f.write(payload + "\n")
        except Exception:
            pass
        if self.mode == "battery":
            self._apply_profile("battery", displays=True,
                                note=f"Battery tuned: {tier} tier ({spl} W sustained).")
        messagebox.showinfo("Watcher — battery tuned", note)

    # -- system tray -----------------------------------------------------

    def make_tray(self):
        def mode_item(k):
            return pystray.MenuItem(
                MODE_LABEL[k], lambda icon, item: self.tray_set_mode(k),
                checked=lambda item: self.mode == k, radio=True)
        power_menu = pystray.Menu(*[mode_item(k) for k in UI_MODES])
        menu = pystray.Menu(
            pystray.MenuItem("Open", self.tray_open, default=True),
            pystray.MenuItem("Power mode", power_menu),
            pystray.MenuItem("Max fan", self.tray_toggle_fan,
                             checked=lambda item: self.fan_on),
            pystray.MenuItem("Lights off", self.tray_lights_off),
            pystray.MenuItem("Exit", self.tray_exit),
        )
        return pystray.Icon("keyboard_light", self.tray_image(), "Aero Control", menu)

    def tray_image(self):
        # Feather glyph, pre-scaled with LANCZOS to the DPI-correct small-icon
        # size so Windows doesn't blur it.
        try:
            size = max(16, int(ctypes.windll.user32.GetSystemMetrics(49)))  # SM_CXSMICON
        except Exception:
            size = 32
        img = Image.open(FEATHER_PNG).resize((size, size), Image.LANCZOS)
        from PIL import ImageFilter
        return img.filter(ImageFilter.UnsharpMask(radius=1, percent=120, threshold=0))

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

FAN_TASKS = [(FAN_MAX_TASK, "--fan max"), (FAN_NORMAL_TASK, "--fan off"),
             (GPU_RESET_TASK, "--gpureset")]


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
    """Create the elevated, no-UAC scheduled tasks that drive the fans and the
    power modes, via the Task Scheduler COM API (RunLevel HIGHEST, interactive
    token)."""
    import win32com.client
    svc = win32com.client.Dispatch("Schedule.Service")
    svc.Connect()
    folder = svc.GetFolder("\\")
    for name, arg in FAN_TASKS + MODE_TASKS:
        td = svc.NewTask(0)
        td.RegistrationInfo.Description = "AERO keyboard app: %s" % arg
        td.Principal.RunLevel = 1        # TASK_RUNLEVEL_HIGHEST
        td.Principal.LogonType = 3       # TASK_LOGON_INTERACTIVE_TOKEN
        td.Settings.StopIfGoingOnBatteries = False
        td.Settings.DisallowStartIfOnBatteries = False
        td.Settings.ExecutionTimeLimit = "PT0S"
        # Low CPU priority: the onefile exe unpacks itself on every launch
        # (~1-2 s CPU/disk burst); at priority 8 that burst can never preempt
        # the user's input, so mode switches stay hitch-free. The firmware
        # writes themselves are instant once the process is up.
        td.Settings.Priority = 8         # BELOW_NORMAL / background
        act = td.Actions.Create(0)       # TASK_ACTION_EXEC
        act.Path = exe
        act.Arguments = arg
        # 6 = TASK_CREATE_OR_UPDATE, 3 = interactive-token logon
        folder.RegisterTaskDefinition(name, td, 6, None, None, 3)


LIGHT_BOOT_TASK = "AeroLightBoot"
APP_LOGON_TASK = "AeroControlLogon"


def register_startup_tasks(exe):
    """Two tasks that shrink the boot 'rainbow window':
    - AeroLightBoot (SYSTEM, boot trigger): sets the lamp during boot, so the
      color is right at the login screen already.
    - AeroControlLogon (logon trigger): starts the tray app seconds after
      login; Startup-folder items are staggered by Windows and could take a
      minute."""
    import win32com.client
    svc = win32com.client.Dispatch("Schedule.Service")
    svc.Connect()
    folder = svc.GetFolder("\\")

    td = svc.NewTask(0)
    td.RegistrationInfo.Description = "Aero Control: set keyboard color at boot"
    td.Settings.StopIfGoingOnBatteries = False
    td.Settings.DisallowStartIfOnBatteries = False
    td.Settings.ExecutionTimeLimit = "PT2M"
    td.Triggers.Create(8)                # TASK_TRIGGER_BOOT
    act = td.Actions.Create(0)           # TASK_ACTION_EXEC
    act.Path = exe
    act.Arguments = '--apply --cfg "%s"' % CONFIG_PATH
    # 5 = TASK_LOGON_SERVICE_ACCOUNT: run as SYSTEM, before any user session
    folder.RegisterTaskDefinition(LIGHT_BOOT_TASK, td, 6, "SYSTEM", None, 5)

    td = svc.NewTask(0)
    td.RegistrationInfo.Description = "Aero Control: start in tray at login"
    td.Principal.LogonType = 3           # interactive token; RunLevel stays LUA
    td.Settings.StopIfGoingOnBatteries = False
    td.Settings.DisallowStartIfOnBatteries = False
    td.Settings.ExecutionTimeLimit = "PT0S"
    td.Settings.Priority = 5             # normal: this is the interactive app
    td.Triggers.Create(9)                # TASK_TRIGGER_LOGON
    act = td.Actions.Create(0)
    act.Path = exe
    act.Arguments = "--tray"
    folder.RegisterTaskDefinition(APP_LOGON_TASK, td, 6, None, None, 3)


def unregister_fan_tasks():
    import win32com.client
    svc = win32com.client.Dispatch("Schedule.Service")
    svc.Connect()
    folder = svc.GetFolder("\\")
    for name, _arg in FAN_TASKS + MODE_TASKS:
        try:
            folder.DeleteTask(name, 0)
        except Exception:
            pass
    for name in (LIGHT_BOOT_TASK, APP_LOGON_TASK):
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
    # Stop any previously-installed instance holding the exe. It may be running
    # elevated (older installs launched it as admin), which a user-shell taskkill
    # can't reach -- but we're elevated here, so stop it by its exact path.
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-Process KeyboardLight -ErrorAction SilentlyContinue | "
             "Where-Object { $_.Path -eq '" + str(exe) + "' } | Stop-Process -Force"],
            creationflags=CREATE_NO_WINDOW, capture_output=True, timeout=10)
        time.sleep(0.6)
    except Exception:
        pass
    # Copy ourselves + bundled assets into the install dir.
    import shutil
    if Path(sys.executable).resolve() != exe.resolve():
        for attempt in range(5):
            try:
                shutil.copy2(sys.executable, exe)
                break
            except PermissionError:
                time.sleep(0.6)   # exe still unlocking after the stop above
        else:
            ctypes.windll.user32.MessageBoxW(
                None, "Couldn't update the installed app -- it's still running.\n"
                "Close Aero Control (right-click the tray icon -> Exit) and run "
                "the installer again.", "Aero Control", 0x10)
            return
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
        try:
            register_startup_tasks(str(exe))
        except Exception as e:
            print("startup task registration failed:", e)

    # Shortcuts: Start menu (open). Autostart is the AeroControlLogon task
    # (fires seconds after login; Startup-folder items are staggered for ~1 min).
    ico = dest / "feather.ico"
    try:
        make_shortcut(_programs_dir() / "Aero Control.lnk", exe, "", ico)
        # Retire the old-name shortcuts and the Startup-folder autostart.
        for stale in (_programs_dir() / "Keyboard Light.lnk",
                      _startup_dir() / "Keyboard Light.lnk",
                      _startup_dir() / "Keyboard Light Apply.lnk",
                      _startup_dir() / "Aero Control.lnk"):
            try:
                stale.unlink(missing_ok=True)
            except Exception:
                pass
    except Exception as e:
        print("shortcut creation failed:", e)

    ctypes.windll.user32.MessageBoxW(
        None,
        "Aero Control installed."
        + ("\n\nMax Fan and Power modes (Battery / Auto / Performance) are ready."
           if ok_fan else "\n\n(Fan / power control unavailable on this machine.)"),
        "Aero Control", 0x40)
    # Launch it DE-ELEVATED: we're running as admin, and a normal Popen would
    # inherit that (leaving the tray app elevated, which then locks its own exe
    # against the next update). Launching via explorer.exe hands it to the normal
    # user shell instead.
    try:
        subprocess.Popen(["explorer.exe", str(exe)])
    except Exception:
        subprocess.Popen([str(exe), "--tray"])


def uninstall():
    if not is_admin():
        relaunch_as_admin("--uninstall")
        return
    unregister_fan_tasks()
    for p in (_programs_dir() / "Aero Control.lnk",
              _startup_dir() / "Aero Control.lnk"):
        try:
            p.unlink()
        except Exception:
            pass
    ctypes.windll.user32.MessageBoxW(
        None, "Aero Control uninstalled.\n\nYou can delete the folder:\n"
        + str(app_data_dir()), "Aero Control", 0x40)


_SINGLE_INSTANCE_HANDLE = None  # keep the mutex handle alive for process lifetime


def acquire_single_instance():
    """Return True if this is the only GUI instance. If another is already
    running, focus its window and return False. Only the GUI process holds the
    mutex; the short-lived --fan/--applyfw/--apply workers don't take it."""
    global _SINGLE_INSTANCE_HANDLE
    k32 = ctypes.windll.kernel32
    h = k32.CreateMutexW(None, False, "KeyboardLight_SingleInstance_Mutex")
    if not h:
        return True  # couldn't create the mutex; don't block startup
    if k32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        try:
            hwnd = ctypes.windll.user32.FindWindowW(None, "Aero Control")
            if hwnd:
                ctypes.windll.user32.ShowWindow(hwnd, 9)   # SW_RESTORE
                ctypes.windll.user32.SetForegroundWindow(hwnd)
        except Exception:
            pass
        k32.CloseHandle(h)
        return False
    _SINGLE_INSTANCE_HANDLE = h
    return True


def main():
    if "--cfg" in sys.argv:
        # Explicit config path: the SYSTEM boot task runs outside the user's
        # profile, where %LOCALAPPDATA% points at systemprofile instead.
        global CONFIG_PATH
        i = sys.argv.index("--cfg")
        if i + 1 < len(sys.argv):
            CONFIG_PATH = Path(sys.argv[i + 1])
    if "--install" in sys.argv:
        install()
        return
    if "--uninstall" in sys.argv:
        uninstall()
        return
    if "--gpureset" in sys.argv:
        gpu_reset()
        return
    if "--fan" in sys.argv:
        i = sys.argv.index("--fan")
        mode = sys.argv[i + 1] if i + 1 < len(sys.argv) else "max"
        fan_apply("max" if mode == "max" else "off")
        return
    if "--applyfw" in sys.argv:
        i = sys.argv.index("--applyfw")
        prof = sys.argv[i + 1] if i + 1 < len(sys.argv) else "balanced"
        if prof in POWER_FW:
            mode_firmware_apply(prof)
        return
    if "--apply" in sys.argv:
        rgb, brightness, _rainbow = load_config()
        bd = _read_config().get("brightness_default")
        if bd is not None:
            brightness = max(0, min(100, int(bd)))
        # At boot the HID stack can come up after us: retry until the keyboard
        # enumerates (the boot task caps us at 2 min via ExecutionTimeLimit).
        deadline = time.monotonic() + 90
        while True:
            try:
                apply_color(rgb, brightness)
                return
            except OSError:
                if time.monotonic() > deadline:
                    return
                time.sleep(2)
    # First run of the distributable exe (i.e. not yet copied into the install
    # dir): offer to install. Answering No just runs it portably this once.
    if getattr(sys, "frozen", False) and "--tray" not in sys.argv:
        installed = app_data_dir() / "KeyboardLight.exe"
        if Path(sys.executable).resolve() != installed.resolve():
            yes = ctypes.windll.user32.MessageBoxW(
                None, "Install Aero Control on this PC?\n\n"
                "Adds it to the Start menu, starts it at login, and enables the "
                "Max Fan button.", "Aero Control", 0x4 | 0x20)  # YesNo | Question
            if yes == 6:  # IDYES
                install()
                return

    # Only one GUI instance: if another is already in the tray, focus it and exit.
    if not acquire_single_instance():
        return

    # Per-monitor DPI awareness so Tk renders at native resolution (crisp text)
    # instead of being bitmap-scaled (blurry) on high-DPI / scaled displays.
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

    # Give this app its own taskbar identity so Windows shows the window's
    # feather icon instead of pythonw's.
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Elliott.KeyboardLight")
    root = tk.Tk()
    try:
        # Scale point-based fonts to the real DPI so they stay crisp and sized.
        root.tk.call("tk", "scaling", root.winfo_fpixels("1i") / 72.0)
    except Exception:
        pass
    if FEATHER_ICO.exists():
        root.iconbitmap(default=str(FEATHER_ICO))
    App(root)
    if "--tray" in sys.argv:
        root.withdraw()
    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        tb = traceback.format_exc()
        try:
            (app_data_dir() / "crash.log").write_text(tb)
        except Exception:
            pass
        try:
            ctypes.windll.user32.MessageBoxW(None, tb[-1500:], "Aero Control crashed", 0x10)
        except Exception:
            pass
        raise
