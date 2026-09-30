# CPU & platform idle power on the AERO X16 — consolidated research

**Machine:** Gigabyte AERO X16, AMD Ryzen AI 7 350 (Krackan Point, Family 0x1A Model 0x60),
Radeon 860M, RTX 5060, Windows 11 26100/26200, hostname `tincan`.
**Measured idle:** ~10.2 W system, ~6–9 W CPU package.

Sources: 19 parallel research agents + direct measurement on this machine.
Raw reports: `$CLAUDE_JOB_DIR/tmp/agents/*.md` (576 KB).

Tags used below: **[MEASURED]** = run live on this machine. **[DOC]** = vendor/Microsoft
documented. **[FOLKLORE]** = widely repeated, unsupported.

---

## 0. The headline: we now have instruments

Before this round we were guessing. Two free, unprivileged meters exist on this box and
both were verified end-to-end.

### 0.1 CPU package watts — Windows Energy Meter Interface (EMI) **[MEASURED]**

Windows' in-box PPM driver (`amdppm.sys`) republishes the AMD RAPL MSRs through the
documented Energy Meter Interface. **No kernel driver, no admin, ~7–9 µs per read.**

```
8 EMI devices, EMI v2, OEM="Microsoft" Model="PPM", 9 channels:
  RAPL_Package0_PKG            <- CPU package power
  RAPL_Package0_Core0..7_CORE  <- per-core
elevated: False
```

Working reader: `probe/emi_power.py`. API is `emi_paths()`, `Emi(path)` with `.chans` and
`.read()` → `[(AbsoluteEnergy_pWh, AbsoluteTime_100ns)]`.

```
watts = dE_pWh * 3.6e-9 / (dT_100ns / 1e7)
```

Also reachable with zero struct parsing via PDH: `\Energy Meter(RAPL_Package0_PKG)\Power`
and `\Power Meter(*)\Power`.

This obsoletes WinRing0, InpOut32, HWiNFO and every "you need a kernel driver to read
package power" claim. See §5.1 for why that matters beyond convenience.

### 0.2 Deep-idle residency — `Processor Information` counters **[MEASURED]**

The "C-state perf counters are always zero on AMD" claim is **[FOLKLORE]** and false on
this part. Live, non-elevated, verified twice (once by an agent, once directly):

```
\Processor Information(_Total)\% C1 Time                 0.92
\Processor Information(_Total)\% C2 Time                 0.44
\Processor Information(_Total)\% C3 Time                89.95   <- deep idle residency
\Processor Information(_Total)\C3 Transitions/sec     4656
\Processor Information(_Total)\% DPC Time                0.39
\Processor Information(_Total)\% Interrupt Time          0.73
\Processor Information(_Total)\Idle Break Events/sec   8189
\Processor Information(_Total)\Average Idle Time    1432108
```

Caveat: on AMD the "C1/C2/C3" labels are Windows PPM idle-state *indices*, not literal
ACPI C-states. Use `\Processor Information(0,0)` etc. for per-logical-CPU breakdown — that
is how you spot a single-core DPC storm. Use `PdhAddEnglishCounterW` so it is
locale-independent.

**What this does not give you is attribution** — no driver names, no DPC routine
addresses. That is exactly and only what ETW buys (§5.3).

**Consequence: every remaining lever is now A/B testable.** Package watts on one axis,
C3 residency and transitions/sec on the other. Nothing below should be shipped without
running that pair before and after.

---

## 1. What actually shipped

### 1.1 The WMI battery poll — fixed **[MEASURED]**

`PowerMeter._run` polled `root/WMI BatteryStatus` every 3 s, forever, on AC and battery,
visible or in tray. Replaced with `CallNtPowerInformation(SystemBatteryState)`.

| path | median | mechanism |
|---|---|---|
| `wmi BatteryStatus()` | **8,759 µs** | COM → `WmiPrvSE.exe` → ACPI method → EC transaction |
| `CallNtPowerInformation` | **6.7 µs** | plain syscall |

~1,300×, identical `Rate` value in mW, and strictly more data (signed rate, charging flag,
capacity, AC state — WMI reports `DischargeRate = 0` on AC so it could not even tell you
that). Every poll was waking two extra processes. This was most of the app's own measured
1.14%-of-a-core. The thread no longer needs COM, so `pythoncom.CoInitialize` is gone.

Verified by AST-extracting the shipped function and executing it: `battery_watts() → 0.0`,
median 6.7 µs.

**The generalisable lesson**, and the one correction to the framing that started this:
the metric is **CPU-milliseconds per wakeup, not wakeups per second**. The app's wakeup
rate was ~1.8/s against a system floor of 6,500–9,800/s — 0.03%, worth under a milliwatt.
The wakeup count never mattered. The work done per wakeup did, by a factor of 1,300.

---

## 2. Corrections to earlier conclusions

Three things stated earlier in this project were wrong. Recording them so they don't get
re-derived.

### 2.1 Core parking was backwards

Earlier claim: "Windows already parks the four Zen5 cores and runs on Zen5c."
**[MEASURED]** reality: all 8 Zen5c threads parked, all 8 Zen5 threads unparked.

This is not a defect — it is the missing half of our own burst measurement. Marginal power
came out at **1.57 W (Zen5) vs 1.56 W (Zen5c)** for the same work, with Zen5c costing +5.0%
energy for +5.5% more wall time. Zen5c is a *density* core, not an efficiency core: same
IPC, lower clock ceiling, smaller area. There is no efficiency reason to prefer it, which
is precisely why Windows parks it first. The two facts agree.

**Consequence: hard CPU affinity pinning to Zen5c is dead.** All 16 logical CPUs share
`LastLevelCacheIndex=0`, the classes are interleaved (Zen5 = LP 0,1,4,5,8,9,12,13;
Zen5c = LP 2,3,6,7,10,11,14,15), and there is no measured gain to chase.

Parking is off on AC, so reproducing this needs a battery-side read.

### 2.2 SPL — wrong end of the curve

Earlier claim: efficiency falls monotonically with TDP, so a low cap is directionally
right. The fall is monotonic *above 20 W*. Notebookcheck's HX 370 sweep (Strix Point —
same Zen5/5c cores, same N4P, same SoC IP family), total system power at the wall:

| TDP | CB2024 MT | System efficiency (pts/W) |
|---|---|---|
| 15 W | 621 | 21.3 |
| 20 W | 760 | 21.1 |
| 28 W | 927 | 19.5 |
| 45 W | 1107 | 15.7 |
| 65 W | 1200 | 12.3 |
| 80 W | — | 10.2 |

15→20 W is 21.3 → 21.1 pts/W — a 1% difference, inside noise. **The curve has already gone
flat by 15–20 W; it is turning over, not still climbing.** Fitting their numbers recovers
the platform floor:

```
P_wall = 8.6 W + 1.37 × TDP     (R² ≈ 0.999)
```

That 8.6 W intercept is fixed cost paid for the entire duration of the work. Below ~15 W
efficiency should therefore *reverse* — stretching a task just means sitting at the floor
longer. **SPL = 12 W is below the energy-optimal band.** 18–20 W buys ~40% more sustained
performance at statistically identical energy per unit of work.

Cross-check worth noting: 8.6 W intercept + internal 16" panel (~2–3 W) − PSU loss lands
at ~9.5–11 W on battery, which reproduces our measured 10.2 W from a completely
independent direction.

**But this is a low-priority change**, because SPL does not bind at idle. **[MEASURED]**
on battery under real load: `Performance Limit Flags = 0`, `% Performance Limit = 100`.
Nothing is limiting the CPU. Idle is what battery life is made of, so this is a small win
on bursty work and exactly zero at idle.

**[MEASURED]** caveat with teeth: no measured Zen5-*mobile* race-to-idle evidence exists
anywhere — not Notebookcheck, not Phoronix, not Chips & Cheese (their Strix Point article
contains zero power measurements). Anyone asserting "raise SPL for better battery" on this
chip is extrapolating. So is anyone asserting the reverse.

### 2.3 `PROFILE_ECO` is already tuned

Two agents contradicted each other. Settled by direct query **[MEASURED]** —
SCHEME_BALANCED, `PROFILE_ECO`: `PERFEPP` DC = 100, `PERFEPP1` DC = 100, `SCHEDPOLICY` = 4
(Prefer efficient processors) AC+DC, `SHORTSCHEDPOLICY` = 4 AC+DC, `PROCFREQMAX1`
DC = 2500 MHz. Already at maximum efficiency bias. The "every EcoQos slot is empty and
unclaimed" claim was wrong.

So our per-process EcoQoS tagging is landing in a well-tuned profile, not an empty one.
The remaining headroom there is one step: `SCHEDPOLICY` 4 → 3 (hard "Efficient
processors" instead of "Prefer"), and given §2.1 that is likely worth little.

Default (non-Eco) profile for contrast **[MEASURED]** — `SCHEDPOLICY` DC = **5**
(Automatic), `SHORTSCHEDPOLICY` DC = 5, `PERFBOOSTMODE` DC = 3, `PROCTHROTTLEMAX`
DC = 100. Everything else inherits its default. Note the value is 5/Automatic, not 4 —
if any of our code or comments claims "prefer efficient" for the default profile, it is
wrong.

---

## 3. Live levers, ranked

Nothing here is measured. That is the point of §0 — measure each one.

| # | Lever | Effort | Expected | Risk |
|---|---|---|---|---|
| 1 | Find + neutralise the 1 ms timer holder; apply `IGNORE_TIMER_RESOLUTION` | low | 0.3–2 W, see §3.1 | none |
| 2 | Gate app ticks on visibility; subscribe power-setting notifications instead of polling | low | removes remaining always-on ticks | none |
| 3 | Tune `PROFILE_SCREENOFF` on `OVERLAY_SCHEME_MIN` — completely untuned, and this box has no S3 | medium | unmeasured, screen-off window only | low |
| 4 | `SUB_INTSTEER` interrupt steering onto one core | low | unmeasured, mechanically sound | low |
| 5 | `GPUPREFERENCEPOLICY = 1` on DC | trivial | attacks dGPU wake at the source | low |
| 6 | Per-thread EcoQoS (`ThreadPowerThrottling`) for worker pools | low | small | none |
| 7 | Raise battery SPL 12 → 18 W | trivial | +40% burst perf at equal energy; 0 at idle | none |
| 8 | `SCHEDPOLICY` 4 → 3 in `PROFILE_ECO` | 1 value | likely ~0 given §2.1 | low |

### 3.1 Timer resolution — real, but smaller than advertised

Status is **intermittent**: two agents caught the global timer at **1.000 ms**; a later
direct read caught **15.625 ms** (the default; min 0.5, max 15.625). So something raises
it periodically and releases it. That materially lowers its priority versus the "permanent
0.3 W tax" framing.

The evidence base is thinner than its reputation. The strongest citable number is
Microsoft's own — *"battery drains at least 20 percent faster"* — but that is unchanged
Windows 8.1-era boilerplate with no modern measurement behind it. The only real
measurement found anywhere is Bruce Dawson's **0.3 W on a Sandy Bridge laptop (2013)**,
which he explicitly disowns as non-general. The circulating "~25% battery" figure is
**[FOLKLORE]**. Treat 0.3–2 W as an unvalidated range and *measure it* with §0.

Note Win10 2004+ changed the semantics: `timeBeginPeriod` is now per-process rather than
global, except for processes that opt out. `PROCESS_POWER_THROTTLING_IGNORE_TIMER_RESOLUTION`
(0x4) is **[MEASURED]** working from Python and is the correct tool for our own processes.

### 3.2 Stop polling — subscribe instead

`PowerSettingRegisterNotification` (powrprof.dll) takes a `DEVICE_NOTIFY_CALLBACK` — a
plain function pointer, **no HWND required**, Win7+. This is the right API for a tray app;
`RegisterPowerSettingNotification` needs a window. Bonus documented behaviour: *"Immediately
after registration, the callback will be invoked with the current value"* — so initial
state is free, no startup poll.

The GUIDs that matter for us:

| GUID | Fires on | Data |
|---|---|---|
| `GUID_ACDC_POWER_SOURCE` | AC/DC change | `PoAc`(0) / `PoDc`(1) / `PoHot`(2) |
| `GUID_BATTERY_PERCENTAGE_REMAINING` | battery % (1% granularity) | DWORD 0–100 |
| `GUID_SESSION_DISPLAY_STATUS` | monitor state (**use this**, user-mode) | Off(0) / On(1) / Dim(2) |
| `GUID_SESSION_USER_PRESENCE` | user present/inactive | Present(0) / NotPresent(1) / Inactive(2) |
| `GUID_POWER_SAVING_STATUS` | battery saver on/off | 0/1 |
| `GUID_LIDSWITCH_STATE_CHANGE` | lid | 0 closed / 1 open |
| `GUID_IDLE_BACKGROUND_TASK` | good time for background work | none |

`GUID_SESSION_DISPLAY_STATUS` + `GUID_SESSION_USER_PRESENCE` are exactly the signals for
"cancel every timer this app owns". WMI event subscriptions are polling in disguise — dead
end.

### 3.3 New in Windows 2025.05 — user-presence QoS demotion

Windows now auto-demotes the foreground app to Medium QoS after user inactivity **on
battery**. `HKLM\SYSTEM\CurrentControlSet\Control\Power\PowerThrottling` →
`DisableUserPresenceQos`. **[MEASURED]** the key is empty on this machine, so the feature
is active. Leave it on. Turn it off only while benchmarking, or our own A/B numbers get
skewed by it. Same key takes `PowerThrottlingOff=1` as a full escape hatch.

---

## 4. Dead ends — closed, with reasons

Recording these so nobody re-opens them.

| Item | Why it's dead |
|---|---|
| **RyzenAdj `--power-saving` on DC** | It is `\_SB.ALIB(0x01, ...)`. **[MEASURED]** this laptop's own DSDT: `Device(ACAD)._PSR` already calls exactly that on every unplug. Firmware got there first. |
| **ALIB Fn 1 as a "power slider"** | It is **Report AC/DC State**, not `SetPerformanceMode`. Proven from AMD's spec and this machine's firmware. Any plan built on the other reading is void. |
| **WinRing0 / InpOut32** | CVE-2020-14979; Defender flags `VulnerableDriver:WinNT/Winring0`. HVCI/Memory Integrity is **enabled** here so it will not load anyway. EMI (§0.1) makes it unnecessary. **Never ship it.** |
| **Direct MSR reads** | Same driver problem, same answer as EMI. |
| **CPU affinity / CPU-Set pinning to Zen5c** | §2.1. Measured parity, shared L3, interleaved classes. |
| **The "AMD debloat" registry list** | **[MEASURED]** against this driver's own INF: `EnableUlps=0`, `PP_SclkDeepSleepDisable=1`, `StutterMode=0` are real keys that *disable* power saving, and one disables thermal throttling. It is a latency-tweak list mislabelled as optimisation. **Do not apply.** |
| **Windows DRR "Dynamic"** | Panel is 48–165 Hz VRR-capable, WDDM 3.2, so it is available — and it measures **−0.5 to −0.7 W worse** than fixed 60 Hz at idle. Anti-lever. |
| **MPO disable** | Already on. Disabling *costs* power. |
| **`powercfg /energy` for DPC/ISR** | **[MEASURED]** by decoding a real `energy-trace.etl`: it collects **zero** DPC and zero ISR events. Still useful for the timer holder and wake sources; useless for interrupts. |
| **LatencyMon for idle-power work** | It forces a 1 ms tick and injects its own DPCs — it changes the thing it measures. Its own docs call the thresholds *"chosen arbitrarily"*. |
| **HWiNFO** | Wakes the dGPU. Same class of error. |
| **Enumerating timer-resolution holders programmatically** | No API. ETW only. |
| **Most iGPU/display knobs** | PSR, PSR-SU, GFXOFF, ULPS, clock+power gating all **already on** in AMD's INF for the Krackan section; panel already at 60 Hz. Under 0.5 W left in the entire category. |
| **Panel Replay** | No Krackan knob exists. |
| **Radeon Chill / HYPR-RX Eco** | Gaming-only, needs Adrenalin, zero at idle. |
| **Windows idle knobs (`IDLEDISABLE`, `IDLESTATEMAX`)** | Measured no effect on AMD Zen here. |

One live substitute worth noting from the iGPU category: **just lowering the backlight is
larger than every GPU knob in it combined.**

---

## 5. Capabilities discovered, not yet used

### 5.1 PawnIO — the signed replacement for WinRing0

If SMU access is ever genuinely needed, UXTU migrated off WinRing0 to **PawnIO** in late
2025 — a signed kernel driver whose AMD module explicitly whitelists the SMN range this
chip's SMU mailbox lives in. Krackan Point (Family 0x1A / Model 0x60) is supported by both
RyzenAdj and UXTU. Krackan's mailbox moved to `0x3b10928/978/998`.

This is the *only* kernel option that should ever be considered, and §0.1 means it is not
needed for measurement — only for control we do not currently have (MP1 `0x12`/`0x11`
boost-delay, `0x4a` fused-default readback).

### 5.2 PMF ordering — a probable latent bug

AMD PMF is live on this machine, and Windows power-overlay changes rewrite APU limits
*through firmware*. That means our `SetApuParameter` values get clobbered on every AC/DC
and overlay transition — which matches the clobbering already observed. Fix is ordering:
re-assert limits *after* every transition, not before.

### 5.3 ETW without installing WPT

`wpr.exe` is **inbox** (`C:\Windows\System32\wpr.exe`, 10.0.26100.8875), as is
`tracerpt.exe`. `xperf`/`wpa` are absent but not needed. A non-WPT process can start and
consume the kernel logger with `EVENT_TRACE_FLAG_DPC | EVENT_TRACE_FLAG_INTERRUPT` in
real-time mode — documented, no restriction, needs admin. This is the only path to DPC/ISR
*attribution* (§0.2 gives the rates but not the names). Volume is high; sample, don't
stream continuously.

---

## 6. Ranked suspects if idle power is ever worse than expected

By (documented mechanism strength × likelihood on this platform):

1. Timer resolution held at 1 ms/0.5 ms by a background app — **[DOC]** Microsoft:
   prevents the CPU power management system from entering power-saving modes.
2. An open audio stream with a small buffer — **[DOC]** 1 kHz wake train. Check
   `powercfg /requests` for AUDIO.
3. Monitoring software polling per-core MSRs — code-verified in LibreHardwareMonitor: one
   refresh wakes *every* core via `SetThreadGroupAffinity`. (This is the category our own
   WMI poll was in.)
4. NVMe F-state latency tolerance too tight / ASPM L1.2 not engaged.
5. USB device not selectively suspending → host controller DMA-walks its schedule.
6. dGPU held out of D3cold by any driver-stack client — 3–11 W if it ever fails. Ours is
   in RTD3; protect it.
7. Wi-Fi power save disabled / short DTIM — target is <10 mW connected-idle.
8. USB4/TB host router awake — only if something is plugged in.

Known-good baseline target: **C3 ≥ 99%, transitions well under 1,000/s, screen on, nothing
running.**

---

## 7. Platform facts worth not re-deriving

- **No S3.** `powercfg /a` → S0 Low Power Idle (Modern Standby) + Hibernate only. S3 is
  additionally disabled by Device Guard/HVCI. Fast Startup is disabled by policy. So idle
  watts *are* the sleep story, and `PROFILE_SCREENOFF` is the real "sleep" lever.
- **`PERFAUTONOMOUS = 1`** on AC and DC — Windows is already running CPPC in autonomous
  mode, the same hardware mechanism Linux's `amd_pstate=active` uses.
- **8 physical cores, 2 efficiency classes, 4 each**, interleaved. Windows exposes separate
  EPP/freq/parking knobs per class (`PERFEPP`/`PERFEPP1`, `PROCFREQMAX`/`PROCFREQMAX1`,
  `CPMINCORES`/`CPMINCORES1`). `PERFEPP2`+ exist as unused slots.
- **Panel:** BOE0C80, 2560×1600, EDID range 48–165 Hz, max pixel clock 770 MHz.
- **iGPU registry key** is `...\Class\{4d36e968-...}\0000`. `0001` is the RTX 5060 — do not
  confuse them.
- **`SUB_PCIEXPRESS\ASPM`** is already at 2 (Maximum power savings).
- Windows userspace **cannot** call `\_SB.ALIB` directly.

---

## 8. Open questions

- Battery-side core parking read (§2.1) — AC blocks it.
- Whether an idle Zen5 cluster genuinely power-gates (CC6 residency), as opposed to merely
  being parked.
- Which process intermittently raises the global timer to 1 ms.
- `PROFILE_SCREENOFF` — untuned, unmeasured, and the highest-leverage unknown given no S3.
- Radeon 860M's actual idle wattage on Krackan — no published measurement exists anywhere.
- Vari-Bright / ABM measured saving — estimated +0.3–1.0 W, unmeasured, currently off.
