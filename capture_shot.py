"""Launch the app window, grab it with PrintWindow, save docs/screenshot.png."""
import subprocess, sys, time
from pathlib import Path
import win32gui, win32ui
from ctypes import windll
from PIL import Image

here = Path(__file__).parent
(here / "docs").mkdir(exist_ok=True)

proc = subprocess.Popen([sys.executable.replace("python.exe", "pythonw.exe"),
                         str(here / "keyboard_light.pyw")])
time.sleep(3.0)  # let the window draw + apply color
try:
    hwnd = 0
    for _ in range(20):
        hwnd = win32gui.FindWindow(None, "Keyboard Light")
        if hwnd:
            break
        time.sleep(0.3)
    if not hwnd:
        raise SystemExit("window not found")
    try:
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        pass  # PrintWindow captures background windows fine
    time.sleep(0.4)
    l, t, r, b = win32gui.GetWindowRect(hwnd)
    w, h = r - l, b - t
    hdc = win32gui.GetWindowDC(hwnd)
    mfc = win32ui.CreateDCFromHandle(hdc)
    save = mfc.CreateCompatibleDC()
    bmp = win32ui.CreateBitmap()
    bmp.CreateCompatibleBitmap(mfc, w, h)
    save.SelectObject(bmp)
    windll.user32.PrintWindow(hwnd, save.GetSafeHdc(), 2)  # PW_RENDERFULLCONTENT
    info = bmp.GetInfo()
    bits = bmp.GetBitmapBits(True)
    img = Image.frombuffer("RGB", (info["bmWidth"], info["bmHeight"]),
                           bits, "raw", "BGRX", 0, 1)
    out = here / "docs" / "screenshot.png"
    img.save(out)
    print("saved", out, img.size)
finally:
    proc.terminate()
