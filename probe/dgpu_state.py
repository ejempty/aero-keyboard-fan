"""Passive dGPU state probe -- uses the same PnP property read the app uses,
which does NOT wake an Optimus card. Also reports AC/DC."""
import ctypes, uuid
from ctypes import wintypes

class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

def _guid(s):
    u = uuid.UUID(s)
    return _GUID(u.time_low, u.time_mid, u.time_hi_version,
                 (ctypes.c_ubyte * 8)(*u.bytes[8:16]))

class _DEVPROPKEY(ctypes.Structure):
    _fields_ = [("fmtid", _GUID), ("pid", wintypes.ULONG)]

_PWR = {1: "D0 (AWAKE)", 2: "D1", 3: "D2", 4: "D3 (asleep)"}
PK_POWER = _DEVPROPKEY(_guid("A45C254E-DF1C-4EFD-8020-67D146A850E0"), 32)
PK_CLASS = _DEVPROPKEY(_guid("A45C254E-DF1C-4EFD-8020-67D146A850E0"), 9)
cfg = ctypes.windll.cfgmgr32

def dev_prop(inst, key, n=64):
    t, sz = wintypes.ULONG(), wintypes.ULONG(n)
    b = (ctypes.c_ubyte * n)()
    if cfg.CM_Get_DevNode_PropertyW(inst, ctypes.byref(key), ctypes.byref(t),
                                    b, ctypes.byref(sz), 0) != 0:
        return None
    return bytes(b[:sz.value])

size = wintypes.ULONG()
cfg.CM_Get_Device_ID_List_SizeW(ctypes.byref(size), ctypes.c_wchar_p("PCI"), 0x101)
buf = ctypes.create_unicode_buffer(size.value)
cfg.CM_Get_Device_ID_ListW(ctypes.c_wchar_p("PCI"), buf, size.value, 0x101)

for dev in buf[:size.value].split("\0"):
    if not dev:
        continue
    inst = wintypes.DWORD()
    if cfg.CM_Locate_DevNodeW(ctypes.byref(inst), ctypes.c_wchar_p(dev), 0) != 0:
        continue
    cls = dev_prop(inst, PK_CLASS)
    if not cls or cls.decode("utf-16-le", "ignore").rstrip("\0") != "Display":
        continue
    raw = dev_prop(inst, PK_POWER)
    st = _PWR.get(int.from_bytes(raw[4:8], "little"), "?") if raw and len(raw) >= 8 else "?"
    vendor = "NVIDIA dGPU" if "VEN_10DE" in dev.upper() else "AMD iGPU"
    print(f"{vendor:12} {st:12} {dev}")

class SPS(ctypes.Structure):
    _fields_ = [("ACLineStatus", ctypes.c_byte), ("BatteryFlag", ctypes.c_byte),
                ("BatteryLifePercent", ctypes.c_byte), ("SystemStatusFlag", ctypes.c_byte),
                ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]
s = SPS()
ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(s))
print(f"\npower source: {'AC' if s.ACLineStatus == 1 else 'BATTERY'}   battery {s.BatteryLifePercent}%")
