"""Read LampArrayAttributes from the AERO X16 keyboard (0414:8104, MI_04).

Read-only: issues HID GET_FEATURE for report 1 only.
"""
import struct
import hid

PATH = rb"\\?\HID#VID_0414&PID_8104&MI_04#8&e863455&0&0000#{4d1e55b2-f16f-11cf-88cb-001111000030}"

h = hid.device()
h.open_path(PATH)
print("opened:", h.get_product_string())

# Report 1: LampArrayAttributesReport
# u16 LampCount, u32 Width, u32 Height, u32 Depth, u32 LampArrayKind, u32 MinUpdateIntervalUs
data = bytes(h.get_feature_report(1, 64))
print("raw:", data.hex())
rid = data[0]
lamp_count, w, hgt, d, kind, interval = struct.unpack_from("<HIIIII", data, 1)
kinds = {1: "Keyboard", 2: "Mouse", 3: "GameController", 4: "Peripheral",
         5: "Scene", 6: "Notification", 7: "Chassis", 8: "Wearable",
         9: "Furniture", 10: "Art"}
print(f"report_id          : {rid}")
print(f"lamp_count         : {lamp_count}")
print(f"bounding_box_mm(um): {w} x {hgt} x {d}")
print(f"kind               : {kind} ({kinds.get(kind, '?')})")
print(f"min_update_us      : {interval}")
h.close()
