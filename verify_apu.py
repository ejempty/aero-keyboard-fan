"""
Verify that SetApuParameter1/2/3 actually clamp the CPU on this AERO X16.

Method: peg all logical cores with a fixed compute loop for ~35s at a LOW
sustained power limit (SPL 15W) and again at a HIGH limit (SPL 25W / 80W burst).
If the firmware honors the writes, the low-limit run finishes measurably slower
(sustained clock throttled by STAPM). If both runs are ~equal, the ACPI-WMI
write is a silent no-op and we'd need a RyzenAdj/SMU fallback.

Self-elevates (WMI Set needs admin). Writes results to verify_apu_result.txt
and prints them; window stays open at the end.

Restores a safe Balanced profile (80/65/25 W) on exit no matter what.
"""
import ctypes
import sys
import os
import time
import multiprocessing as mp

RESULT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "verify_apu_result.txt")

# fPPT (Param1), sPPT (Param2), SPL/STAPM (Param3) in milliwatts
HIGH = (80000, 80000, 25000)   # GiMATE "Turbo" — high sustained
LOW  = (30000, 30000, 15000)   # GiMATE "Eco/battery" — low sustained
SAFE = (80000, 65000, 25000)   # GiMATE "Balance/AC" — restore point


def burn(work):
    x = 0
    for _ in range(work):
        x = (x * 1103515245 + 12345) & 0xFFFFFFFF
    return x


def _set_apu(inst, f, s, spl):
    inst.SetApuParameter1(Data=f)
    inst.SetApuParameter2(Data=s)
    inst.SetApuParameter3(Data=spl)


def _discharge_mw():
    """Instantaneous battery discharge in mW (0 on AC / while charging)."""
    try:
        import wmi
        b = wmi.WMI(namespace="root/WMI").BatteryStatus()[0]
        return int(getattr(b, "DischargeRate", 0) or 0)
    except Exception:
        return 0


def _run_load(pool, procs, work, seconds_hint):
    """Run a sustained all-core load; return (elapsed_s, avg_discharge_mw)."""
    samples = []
    t0 = time.time()
    res = pool.map_async(burn, [work] * procs)
    while not res.ready():
        d = _discharge_mw()
        if d:
            samples.append((time.time() - t0, d))
        time.sleep(1.0)
    res.get()
    elapsed = time.time() - t0
    # average discharge over the back half (after STAPM has settled)
    back = [d for (t, d) in samples if t > elapsed * 0.5]
    avg = sum(back) / len(back) if back else 0
    return elapsed, avg


def main():
    lines = []

    def log(msg):
        print(msg, flush=True)
        lines.append(msg)

    import wmi  # noqa: E402
    inst = wmi.WMI(namespace="root/WMI").GB_WMIACPI_Set()[0]

    ac = ctypes.wintypes = None
    # AC line status
    class SPS(ctypes.Structure):
        _fields_ = [("ACLineStatus", ctypes.c_byte), ("BatteryFlag", ctypes.c_byte),
                    ("BatteryLifePercent", ctypes.c_byte), ("SystemStatusFlag", ctypes.c_byte),
                    ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]
    sps = SPS()
    ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(sps))
    on_ac = sps.ACLineStatus == 1
    log("=== AERO X16 APU power-limit verification ===")
    log(f"Power source: {'AC (plugged in)' if on_ac else 'BATTERY'}   Battery: {sps.BatteryLifePercent}%")
    if on_ac:
        log("Note: on AC the battery-watt signal is 0; the throughput delta is the real proof.")
    else:
        log("On battery: both throughput AND discharge-watt delta will show.")

    procs = mp.cpu_count()
    log(f"Logical cores to load: {procs}")

    # calibrate work size for ~35s per run using a short single-core sample
    log("Calibrating workload...")
    cal_work = 20_000_000
    t = time.time()
    burn(cal_work)
    per = (time.time() - t) / cal_work  # seconds per iteration, single core
    work = int(35.0 / per)  # ~35s of work per process (runs concurrently across cores)
    log(f"Sized work = {work:,} iters/core (~35s target)")

    pool = mp.Pool(procs)
    try:
        # HIGH first
        log("\n[1/2] Setting HIGH limit  fPPT/sPPT/SPL = 80/80/25 W ...")
        _set_apu(inst, *HIGH)
        time.sleep(4)  # let STAPM window reset
        log("      Running sustained all-core load (~35s)...")
        t_high, d_high = _run_load(pool, procs, work, 35)
        log(f"      HIGH: {t_high:.1f}s" + (f", ~{d_high/1000:.1f} W discharge" if d_high else ""))

        # cool a moment so STAPM history from the high run doesn't carry over
        time.sleep(6)

        # LOW
        log("\n[2/2] Setting LOW limit   fPPT/sPPT/SPL = 30/30/15 W ...")
        _set_apu(inst, *LOW)
        time.sleep(4)
        log("      Running sustained all-core load (~35s)...")
        t_low, d_low = _run_load(pool, procs, work, 35)
        log(f"      LOW:  {t_low:.1f}s" + (f", ~{d_low/1000:.1f} W discharge" if d_low else ""))
    finally:
        pool.close()
        pool.join()
        _set_apu(inst, *SAFE)
        log("\nRestored safe Balanced profile (80/65/25 W).")

    # verdict
    slow = (t_low / t_high - 1.0) * 100.0 if t_high else 0.0
    log("\n----- VERDICT -----")
    log(f"Low-limit run was {slow:+.0f}% slower than high-limit run.")
    if d_high and d_low:
        log(f"Discharge delta: {(d_high - d_low)/1000:.1f} W lower at the low limit.")
    if slow >= 12:
        log("RESULT: WRITES WORK. Firmware honors SetApuParameter — the power modes are viable. Build it.")
    elif slow >= 5:
        log("RESULT: LIKELY WORKS but weak signal. Re-run (ideally on battery, cooler) to confirm.")
    else:
        log("RESULT: NO EFFECT. WMI write appears to be a silent no-op — we'll need a RyzenAdj/SMU fallback.")

    try:
        with open(RESULT, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        log(f"\nSaved: {RESULT}")
    except Exception as e:
        log(f"(could not save result file: {e})")


if __name__ == "__main__":
    mp.freeze_support()
    if not ctypes.windll.shell32.IsUserAnAdmin():
        # relaunch elevated in a visible console
        params = f'"{os.path.abspath(__file__)}"'
        ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 1)
        sys.exit(0)
    try:
        main()
    except Exception as e:
        import traceback
        traceback.print_exc()
    input("\nPress Enter to close...")
