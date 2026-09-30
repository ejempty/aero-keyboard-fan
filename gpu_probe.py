"""
READ-ONLY probe of the AERO X16 GPU/MUX state over ACPI-WMI.

Writes NOTHING. Just reads the GB_WMIACPI_Get values that describe the current
graphics mode, so we can learn how SetPEGorSG encodes iGPU-only / hybrid / dGPU
BEFORE ever writing it (a wrong MUX write can black-screen a panel).

Self-elevates (the getters may need admin). Prints + saves gpu_probe_result.txt.
"""
import ctypes
import os
import sys

RESULT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gpu_probe_result.txt")

GETTERS = [
    ("GetPEGorSG", "MUX/graphics mode (PEG=dGPU vs SG=switchable)"),
    ("GetPEG2orSG2", "secondary MUX/graphics mode"),
    ("getSecondDisplay", "second display routing"),
    ("GetNvPowerConfig", "NVIDIA Dynamic Boost config"),
    ("GetDynamicBoostStatus", "Dynamic Boost on/off"),
    ("getGpuTemp1", "dGPU temp 1"),
    ("getGpuTemp2", "dGPU temp 2"),
    ("GetNvThermalTarget", "dGPU thermal target"),
]


def main():
    lines = []

    def log(m):
        print(m, flush=True)
        lines.append(m)

    import wmi
    get = wmi.WMI(namespace="root/WMI").GB_WMIACPI_Get()[0]
    log("=== AERO X16 GPU / MUX state (read-only) ===")
    for name, desc in GETTERS:
        try:
            fn = getattr(get, name, None)
            if fn is None:
                log(f"{name:22} : (method not present)   {desc}")
                continue
            r = fn()
            # wmi returns out-params; try to surface Data / all fields
            val = None
            for attr in ("Data", "Thermal1"):
                if hasattr(r, attr):
                    val = getattr(r, attr)
                    break
            if val is None:
                val = repr(r)
            log(f"{name:22} : {val}   ({desc})")
        except Exception as e:
            log(f"{name:22} : ERROR {e}   ({desc})")

    # Also list which GPUs Windows sees.
    try:
        import wmi as _w
        for v in _w.WMI().Win32_VideoController():
            log(f"  video: {v.Name}  (status {v.Status})")
    except Exception:
        pass

    try:
        with open(RESULT, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        log(f"\nSaved: {RESULT}")
    except Exception as e:
        log(f"(could not save: {e})")


if __name__ == "__main__":
    if not ctypes.windll.shell32.IsUserAnAdmin():
        ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, f'"{os.path.abspath(__file__)}"', None, 1)
        sys.exit(0)
    try:
        main()
    except Exception:
        import traceback
        traceback.print_exc()
    input("\nPress Enter to close...")
