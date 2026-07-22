"""LampArray probe: read lamp 0 attributes, then set keyboard color via standard
HID LampArray reports (range update). Usage:

    python lamparray_test.py [r g b intensity]

Defaults to red at full intensity.
"""
import struct
import sys
import hid

PATH = rb"\\?\HID#VID_0414&PID_8104&MI_04#8&e863455&0&0000#{4d1e55b2-f16f-11cf-88cb-001111000030}"

r, g, b, i = (int(x) for x in sys.argv[1:5]) if len(sys.argv) >= 5 else (255, 0, 0, 255)

h = hid.device()
h.open_path(PATH)

# --- Lamp 0 attributes: request (report 2), response (report 3) ---
h.send_feature_report(bytes([2]) + struct.pack("<H", 0))
resp = bytes(h.get_feature_report(3, 64))
print("lamp attr raw:", resp.hex())
(lamp_id, x, y, z, latency, purposes,
 red_n, green_n, blue_n, int_n, prog, binding) = struct.unpack_from("<HIIIII4B2B", resp, 1)
print(f"lamp {lamp_id}: pos=({x},{y},{z})um latency={latency}us purposes={purposes:#x}")
print(f"levels R/G/B/I = {red_n}/{green_n}/{blue_n}/{int_n} programmable={prog}")

# --- Take host control: LampArrayControl (report 6), AutonomousMode = 0 ---
h.send_feature_report(bytes([6, 0]))
print("autonomous mode off (host control)")

# --- Set color: LampRangeUpdate (report 5) ---
# [id][flags][start u16][end u16][R][G][B][I]; flags bit0 = update complete
h.send_feature_report(bytes([5, 1]) + struct.pack("<HH", 0, 0) + bytes([r, g, b, i]))
print(f"sent color R={r} G={g} B={b} I={i}")
h.close()
