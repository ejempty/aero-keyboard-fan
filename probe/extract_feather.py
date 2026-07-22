"""Extract the Tk feather icon from tk86t.dll and save it as feather.png."""
import ctypes
from ctypes import wintypes

from PIL import Image

DLL = r"C:\Python314\DLLs\tk86t.dll"
OUT = r"C:\Users\ellio\Claude Conversations\Aero x16 keyboard app\feather.png"
SIZE = 64

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
shell32 = ctypes.windll.shell32

# Pull the first (large) icon resource out of the DLL
hicon = wintypes.HICON()
count = shell32.ExtractIconExW(DLL, 0, ctypes.byref(hicon), None, 1)
if count < 1 or not hicon:
    raise SystemExit("no icon resource found in " + DLL)


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


bmi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), SIZE, -SIZE, 1, 32, 0,
                       0, 0, 0, 0, 0)
bits = ctypes.c_void_p()
screen_dc = user32.GetDC(None)
mem_dc = gdi32.CreateCompatibleDC(screen_dc)
dib = gdi32.CreateDIBSection(mem_dc, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
old = gdi32.SelectObject(mem_dc, dib)

DI_NORMAL = 3
user32.DrawIconEx(mem_dc, 0, 0, hicon, SIZE, SIZE, 0, None, DI_NORMAL)

buf = ctypes.string_at(bits, SIZE * SIZE * 4)
img = Image.frombuffer("RGBA", (SIZE, SIZE), buf, "raw", "BGRA", 0, 1)

# DrawIconEx leaves alpha unset for non-alpha icons; if fully transparent, redraw
# on white and treat white as background.
if img.getextrema()[3][1] == 0:
    gdi32.SelectObject(mem_dc, dib)
    ctypes.memset(bits, 0xFF, SIZE * SIZE * 4)
    user32.DrawIconEx(mem_dc, 0, 0, hicon, SIZE, SIZE, 0, None, DI_NORMAL)
    buf = ctypes.string_at(bits, SIZE * SIZE * 4)
    img = Image.frombuffer("RGB", (SIZE, SIZE), buf, "raw", "BGRX", 0, 1).convert("RGBA")

gdi32.SelectObject(mem_dc, old)
gdi32.DeleteObject(dib)
gdi32.DeleteDC(mem_dc)
user32.ReleaseDC(None, screen_dc)
user32.DestroyIcon(hicon)

img.save(OUT)
print("saved", OUT)
