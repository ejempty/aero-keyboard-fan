"""Enumerate all HID interfaces of the Gigabyte AERO X16 keyboard controller (0414:8104).

Read-only: lists paths, usage pages, and report descriptors. Sends nothing to the device.
"""
import hid

VID, PID = 0x0414, 0x8104

for d in hid.enumerate(VID, PID):
    print(f"path        : {d['path'].decode()}")
    print(f"  interface : {d['interface_number']}")
    print(f"  usage_page: {d['usage_page']:#06x}  usage: {d['usage']:#06x}")
    print(f"  product   : {d.get('product_string')!r}  serial: {d.get('serial_number')!r}")
    try:
        h = hid.device()
        h.open_path(d['path'])
        try:
            desc = h.get_report_descriptor()
            print(f"  descriptor: {bytes(desc).hex()}")
        except Exception as e:
            print(f"  descriptor: <error {e}>")
        h.close()
    except Exception as e:
        print(f"  open      : <error {e}>")
    print()
