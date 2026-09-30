"""A/B power baseline recorder for the AERO X16.

Records the two axes that make every power lever testable, neither of which needs
admin or a kernel driver:

  * CPU package + per-core watts, via the Windows Energy Meter Interface (EMI),
    which republishes the AMD RAPL MSRs. See probe/emi_power.py.
  * Deep-idle residency and wake behaviour, via the Processor Information PDH
    counters. The "C-state counters are always zero on AMD" claim is false on
    this part -- verified live.

Plus battery discharge in watts, so a run on battery is directly comparable to
what the app's own meter reports.

Usage
-----
    py probe\\power_bench.py record baseline --minutes 10
    py probe\\power_bench.py record screenoff-tuned --minutes 10
    py probe\\power_bench.py compare baseline screenoff-tuned

Runs are written to probe/benchmarks/<label>.json. The recorder is deliberately
cheap: one EMI ioctl and one PDH collect per sample, ~1 ms of work at a 2 s
interval, so it does not meaningfully perturb what it is measuring.

Read docs/POWER_RESEARCH.md for what the numbers mean and which levers are worth
pointing this at.
"""

import argparse
import ctypes
import json
import statistics
import sys
import time
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from emi_power import Emi, emi_paths  # noqa: E402

OUT_DIR = Path(__file__).resolve().parent / "benchmarks"

# Counters worth sampling. % C3 Time is the headline: deep-idle residency.
# Transitions/sec and Idle Break Events/sec say how often something is dragging
# the core back out, which is the thing most levers actually move.
COUNTERS = [
    (r"\Processor Information(_Total)\% C1 Time", "c1_pct"),
    (r"\Processor Information(_Total)\% C2 Time", "c2_pct"),
    (r"\Processor Information(_Total)\% C3 Time", "c3_pct"),
    (r"\Processor Information(_Total)\C1 Transitions/sec", "c1_trans"),
    (r"\Processor Information(_Total)\C2 Transitions/sec", "c2_trans"),
    (r"\Processor Information(_Total)\C3 Transitions/sec", "c3_trans"),
    (r"\Processor Information(_Total)\Idle Break Events/sec", "idle_breaks"),
    (r"\Processor Information(_Total)\% DPC Time", "dpc_pct"),
    (r"\Processor Information(_Total)\% Interrupt Time", "isr_pct"),
    (r"\Processor Information(_Total)\Interrupts/sec", "interrupts"),
    (r"\Processor Information(_Total)\% Processor Time", "cpu_pct"),
    (r"\Processor Information(_Total)\Processor Frequency", "mhz"),
]

# ---------------------------------------------------------------- battery draw


class SYSTEM_BATTERY_STATE(ctypes.Structure):
    _fields_ = [("AcOnLine", ctypes.c_ubyte), ("BatteryPresent", ctypes.c_ubyte),
                ("Charging", ctypes.c_ubyte), ("Discharging", ctypes.c_ubyte),
                ("Spare1", ctypes.c_ubyte * 3), ("Tag", ctypes.c_ubyte),
                ("MaxCapacity", wintypes.DWORD), ("RemainingCapacity", wintypes.DWORD),
                ("Rate", ctypes.c_long), ("EstimatedTime", wintypes.DWORD),
                ("DefaultAlert1", wintypes.DWORD), ("DefaultAlert2", wintypes.DWORD)]


_SystemBatteryState = 5
_BATTERY_UNKNOWN_RATE = -0x80000000

_CallNtPowerInformation = ctypes.WinDLL("powrprof").CallNtPowerInformation
_CallNtPowerInformation.argtypes = [ctypes.c_int, ctypes.c_void_p, wintypes.ULONG,
                                    ctypes.c_void_p, wintypes.ULONG]
_CallNtPowerInformation.restype = ctypes.c_long


def battery_state():
    """(watts_or_None, on_ac, percent_or_None). Same call the app's meter uses."""
    s = SYSTEM_BATTERY_STATE()
    if _CallNtPowerInformation(_SystemBatteryState, None, 0,
                               ctypes.byref(s), ctypes.sizeof(s)) != 0:
        return None, None, None
    on_ac = bool(s.AcOnLine)
    pct = None
    if s.BatteryPresent and s.MaxCapacity:
        pct = round(100.0 * s.RemainingCapacity / s.MaxCapacity, 1)
    if not s.BatteryPresent or s.Rate == _BATTERY_UNKNOWN_RATE:
        return None, on_ac, pct
    return ((abs(s.Rate) / 1000.0) if s.Discharging else 0.0), on_ac, pct


# ------------------------------------------------------------------------ PDH

PDH_FMT_DOUBLE = 0x00000200
_pdh = ctypes.WinDLL("pdh")


class _PdhValue(ctypes.Structure):
    _fields_ = [("CStatus", wintypes.DWORD), ("doubleValue", ctypes.c_double)]


class Pdh:
    """Minimal PDH wrapper. Uses PdhAddEnglishCounterW so counter paths do not
    depend on the machine's display language."""

    def __init__(self, paths):
        self.q = wintypes.HANDLE()
        if _pdh.PdhOpenQueryW(None, 0, ctypes.byref(self.q)) != 0:
            raise OSError("PdhOpenQueryW failed")
        self.h = {}
        for path, key in paths:
            h = wintypes.HANDLE()
            if _pdh.PdhAddEnglishCounterW(self.q, path, 0, ctypes.byref(h)) == 0:
                self.h[key] = h
        # Rate counters need two collections to produce a value; prime the first.
        _pdh.PdhCollectQueryData(self.q)

    def read(self):
        if _pdh.PdhCollectQueryData(self.q) != 0:
            return {}
        out = {}
        for key, h in self.h.items():
            v = _PdhValue()
            if _pdh.PdhGetFormattedCounterValue(h, PDH_FMT_DOUBLE, None,
                                                ctypes.byref(v)) == 0:
                out[key] = v.doubleValue
        return out

    def close(self):
        if self.q:
            _pdh.PdhCloseQuery(self.q)
            self.q = None


# ------------------------------------------------------------------------ EMI


class PkgPower:
    """Package and per-core watts from EMI, differenced between reads."""

    def __init__(self):
        self.emi = None
        self.prev = None
        self.pkg_i = None
        self.core_i = []
        for p in emi_paths():
            try:
                e = Emi(p)
            except Exception:
                continue
            names = [c[0] for c in e.chans]
            pkg = [i for i, n in enumerate(names) if n.endswith("_PKG")]
            if not pkg:
                continue
            self.emi = e
            self.pkg_i = pkg[0]
            self.core_i = [i for i, n in enumerate(names) if n.endswith("_CORE")]
            break
        if self.emi:
            self.prev = self.emi.read()

    @property
    def ok(self):
        return self.emi is not None

    def sample(self):
        """(pkg_w, cores_w) since the previous call, or (None, None)."""
        if not self.emi:
            return None, None
        cur = self.emi.read()
        if self.prev is None:
            self.prev = cur
            return None, None

        def watts(i):
            de = cur[i][0] - self.prev[i][0]          # pWh
            dt = cur[i][1] - self.prev[i][1]          # 100 ns ticks
            if dt <= 0:
                return None
            return de * 3.6e-9 / (dt / 1e7)

        pkg = watts(self.pkg_i)
        cores = [watts(i) for i in self.core_i]
        cores = [c for c in cores if c is not None]
        self.prev = cur
        return pkg, (sum(cores) if cores else None)


# --------------------------------------------------------------------- record


def record(label, minutes, interval):
    pkg = PkgPower()
    if not pkg.ok:
        print("WARNING: no EMI package channel found -- CPU watts will be missing.")
    pdh = Pdh(COUNTERS)

    _, on_ac, pct = battery_state()
    if on_ac:
        print("NOTE: on AC. Battery watts will read 0.0 and idle behaviour differs")
        print("      from battery (core parking is disabled on AC). For a battery")
        print("      comparison, unplug first.")

    n = max(1, int(minutes * 60 / interval))
    print(f"\nRecording '{label}': {n} samples, {interval}s apart "
          f"(~{minutes:g} min). Leave the machine alone.\n")

    samples = []
    t0 = time.time()
    try:
        for i in range(n):
            time.sleep(interval)
            pkg_w, cores_w = pkg.sample()
            c = pdh.read()
            batt_w, on_ac, pct = battery_state()
            # noncpu_w is everything the battery feeds that is not the CPU
            # package: panel, NVMe, radios, VRM losses. It is the larger half of
            # the draw on this machine, so track it explicitly.
            noncpu = (batt_w - pkg_w) if (batt_w and pkg_w) else None
            row = {"t": round(time.time() - t0, 1), "pkg_w": pkg_w,
                   "cores_w": cores_w, "batt_w": batt_w, "noncpu_w": noncpu,
                   "ac": on_ac, "pct": pct}
            row.update({k: round(v, 3) for k, v in c.items()})
            samples.append(row)
            if (i + 1) % 10 == 0 or i == 0:
                print(f"  [{i+1:>4}/{n}] pkg={_f(pkg_w)}W batt={_f(batt_w)}W "
                      f"C3={_f(c.get('c3_pct'))}% breaks={_f(c.get('idle_breaks'), 0)}/s")
    except KeyboardInterrupt:
        print("\n  interrupted -- saving what we have")
    finally:
        pdh.close()

    OUT_DIR.mkdir(exist_ok=True)
    run = {"label": label, "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
           "interval": interval, "samples": samples}
    path = OUT_DIR / f"{label}.json"
    path.write_text(json.dumps(run, indent=1), encoding="utf-8")
    print(f"\nSaved {len(samples)} samples -> {path}")
    _summary(run)
    return run


def _f(v, nd=2):
    return "--" if v is None else f"{v:.{nd}f}"


# Fields that get a median, in report order, with the decimal places each one
# actually needs. Median rather than mean: a single background spike should not
# move the number we compare runs on.
#
# cores_w needs 3dp -- at idle the cores sit around 0.03 W and 2dp prints "0.00",
# which reads as a broken sensor rather than the (correct, and important) answer
# that the cores are power-gated to near nothing.
#
# Processor Frequency is deliberately absent: on this part the counter reports
# the nominal 2000 MHz in every single sample regardless of actual clock, so it
# carries no information. Use pkg_w instead.
FIELDS = [("pkg_w", "CPU package W", 2), ("cores_w", "  of which cores W", 3),
          ("batt_w", "Battery draw W", 2), ("noncpu_w", "  non-CPU rest W", 2),
          ("c3_pct", "C3 residency %", 2), ("c3_trans", "C3 transitions/s", 0),
          ("idle_breaks", "Idle breaks/s", 0), ("dpc_pct", "DPC time %", 2),
          ("isr_pct", "Interrupt time %", 2), ("interrupts", "Interrupts/s", 0),
          ("cpu_pct", "CPU busy %", 2)]

# Above this, the machine was not idle and the run is not a usable baseline.
BUSY_WARN_PCT = 5.0


def _med(samples, key):
    vals = [s[key] for s in samples if s.get(key) is not None]
    return statistics.median(vals) if vals else None


def _summary(run):
    s = run["samples"]
    if not s:
        return
    ac = "AC" if s[-1].get("ac") else "BATTERY"
    print(f"\n  {run['label']}  ({len(s)} samples, {ac})")
    for key, name, nd in FIELDS:
        v = _med(s, key)
        if v is not None:
            print(f"    {name:<20} {v:>10.{nd}f}")

    pkg, batt = _med(s, "pkg_w"), _med(s, "batt_w")
    if pkg and batt:
        print(f"    {'CPU share of draw':<20} {100*pkg/batt:>9.0f} %")

    busy = _med(s, "cpu_pct")
    if busy is not None and busy > BUSY_WARN_PCT:
        print(f"\n  WARNING: median CPU busy was {busy:.1f}% -- the machine was not idle.")
        print("  This is a load measurement, not an idle baseline. Close everything")
        print("  (including whatever terminal launched this) and re-record.")


def compare(a_label, b_label):
    def load(lbl):
        p = OUT_DIR / f"{lbl}.json"
        if not p.exists():
            sys.exit(f"no such run: {p}")
        return json.loads(p.read_text(encoding="utf-8"))

    a, b = load(a_label), load(b_label)
    print(f"\n  {'':<20} {a_label[:12]:>12} {b_label[:12]:>12} {'delta':>10}")
    print(f"  {'-'*20} {'-'*12} {'-'*12} {'-'*10}")
    for key, name, nd in FIELDS:
        va, vb = _med(a["samples"], key), _med(b["samples"], key)
        if va is None or vb is None:
            continue
        d = vb - va
        # Lower is better for everything except C3 residency.
        good = (d > 0) if key == "c3_pct" else (d < 0)
        mark = "" if abs(d) < 1e-9 else ("  better" if good else "  worse")
        print(f"  {name:<20} {va:>12.{nd}f} {vb:>12.{nd}f} {d:>+10.{nd}f}{mark}")

    aw, bw = _med(a["samples"], "pkg_w"), _med(b["samples"], "pkg_w")
    if aw and bw:
        print(f"\n  Package power {((bw-aw)/aw)*100:+.1f}%")

    # Battery draw is the number that actually matters, and it is far noisier
    # than package power -- it moves with backlight, radios and NVMe. Say so
    # rather than letting a 0.5 W wobble read as a result.
    for run, lbl in ((a, a_label), (b, b_label)):
        busy = _med(run["samples"], "cpu_pct")
        if busy is not None and busy > BUSY_WARN_PCT:
            print(f"  WARNING: '{lbl}' ran at {busy:.1f}% CPU -- not an idle run.")
    print("\n  Caution: package deltas under ~0.1 W and battery deltas under ~1 W are")
    print("  inside this instrument's noise. Re-run both sides before believing one.")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("record", help="record a run")
    r.add_argument("label")
    r.add_argument("--minutes", type=float, default=10)
    r.add_argument("--interval", type=float, default=2.0)

    c = sub.add_parser("compare", help="compare two runs")
    c.add_argument("before")
    c.add_argument("after")

    sub.add_parser("list", help="list saved runs")
    sub.add_parser("now", help="one-shot reading, no recording")

    a = ap.parse_args()
    if a.cmd == "record":
        record(a.label, a.minutes, a.interval)
    elif a.cmd == "compare":
        compare(a.before, a.after)
    elif a.cmd == "list":
        if not OUT_DIR.exists():
            print("no runs recorded yet")
            return
        for p in sorted(OUT_DIR.glob("*.json")):
            run = json.loads(p.read_text(encoding="utf-8"))
            print(f"  {run['label']:<24} {len(run['samples']):>5} samples  "
                  f"{run.get('started','')}")
    elif a.cmd == "now":
        pkg, pdh = PkgPower(), Pdh(COUNTERS)
        time.sleep(2)
        pkg_w, cores_w = pkg.sample()
        c = pdh.read()
        batt_w, on_ac, pct = battery_state()
        pdh.close()
        print(f"  power source     {'AC' if on_ac else 'BATTERY'}  ({pct}%)")
        print(f"  CPU package      {_f(pkg_w)} W")
        print(f"    of which cores {_f(cores_w)} W")
        print(f"  battery draw     {_f(batt_w)} W")
        print(f"  C3 residency     {_f(c.get('c3_pct'))} %")
        print(f"  C3 transitions   {_f(c.get('c3_trans'), 0)} /s")
        print(f"  idle breaks      {_f(c.get('idle_breaks'), 0)} /s")
        print(f"  DPC / interrupt  {_f(c.get('dpc_pct'))} % / {_f(c.get('isr_pct'))} %")
        print(f"  CPU busy         {_f(c.get('cpu_pct'))} %  @ {_f(c.get('mhz'), 0)} MHz")


if __name__ == "__main__":
    main()
