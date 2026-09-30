"""EMI power reader: decode EMI_METADATA_V2 + EMI_MEASUREMENT_DATA_V2 properly, and delta->watts."""
import ctypes, ctypes.wintypes as w, struct, time, sys, os, multiprocessing

setupapi = ctypes.WinDLL('setupapi', use_last_error=True)
kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)

class GUID(ctypes.Structure):
    _fields_=[("D1",ctypes.c_ulong),("D2",ctypes.c_ushort),("D3",ctypes.c_ushort),("D4",ctypes.c_ubyte*8)]
    def __init__(s_, t):
        super().__init__(); p=t.strip('{}').split('-')
        s_.D1=int(p[0],16); s_.D2=int(p[1],16); s_.D3=int(p[2],16)
        s_.D4=(ctypes.c_ubyte*8)(*bytes.fromhex(p[3]+p[4]))
class SPDID(ctypes.Structure):
    _fields_=[("cbSize",w.DWORD),("g",GUID),("Flags",w.DWORD),("Res",ctypes.POINTER(ctypes.c_ulonglong))]

EMI_GUID = GUID("45BD8344-7ED6-49cf-A440-C276C933B053")
setupapi.SetupDiGetClassDevsW.restype=ctypes.c_void_p
setupapi.SetupDiGetClassDevsW.argtypes=[ctypes.POINTER(GUID),w.LPCWSTR,w.HWND,w.DWORD]
setupapi.SetupDiEnumDeviceInterfaces.argtypes=[ctypes.c_void_p,ctypes.c_void_p,ctypes.POINTER(GUID),w.DWORD,ctypes.POINTER(SPDID)]
setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes=[ctypes.c_void_p,ctypes.POINTER(SPDID),ctypes.c_void_p,w.DWORD,ctypes.POINTER(w.DWORD),ctypes.c_void_p]
kernel32.CreateFileW.restype=ctypes.c_void_p
kernel32.CreateFileW.argtypes=[w.LPCWSTR,w.DWORD,w.DWORD,ctypes.c_void_p,w.DWORD,w.DWORD,ctypes.c_void_p]
kernel32.DeviceIoControl.argtypes=[ctypes.c_void_p,w.DWORD,ctypes.c_void_p,w.DWORD,ctypes.c_void_p,w.DWORD,ctypes.POINTER(w.DWORD),ctypes.c_void_p]
kernel32.CloseHandle.argtypes=[ctypes.c_void_p]
INVALID=ctypes.c_void_p(-1).value

def CTL(f): return (0x22<<16)|(1<<14)|(f<<2)   # FILE_DEVICE_UNKNOWN, FILE_READ_ACCESS, METHOD_BUFFERED
IOCTL_VER, IOCTL_MDSIZE, IOCTL_MD, IOCTL_MEAS = CTL(0), CTL(1), CTL(2), CTL(3)

def emi_paths():
    h=setupapi.SetupDiGetClassDevsW(ctypes.byref(EMI_GUID),None,None,0x12)
    out=[];i=0
    while True:
        d=SPDID(); d.cbSize=ctypes.sizeof(SPDID)
        if not setupapi.SetupDiEnumDeviceInterfaces(h,None,ctypes.byref(EMI_GUID),i,ctypes.byref(d)): break
        req=w.DWORD(0)
        setupapi.SetupDiGetDeviceInterfaceDetailW(h,ctypes.byref(d),None,0,ctypes.byref(req),None)
        buf=ctypes.create_string_buffer(req.value); ctypes.memmove(buf,struct.pack('<I',8),4)
        if setupapi.SetupDiGetDeviceInterfaceDetailW(h,ctypes.byref(d),buf,req.value,ctypes.byref(req),None):
            out.append(ctypes.wstring_at(ctypes.addressof(buf)+4))
        i+=1
    return out

def ioctl(hf,code,n):
    o=ctypes.create_string_buffer(n); r=w.DWORD(0)
    if not kernel32.DeviceIoControl(hf,code,None,0,o,n,ctypes.byref(r),None):
        raise OSError(ctypes.get_last_error())
    return o.raw[:r.value]

def parse_md_v2(b):
    # WCHAR HardwareOEM[16]; WCHAR HardwareModel[16]; USHORT HardwareRevision; USHORT ChannelCount; EMI_CHANNEL_V2[]
    oem=b[0:32].decode('utf-16-le').split('\x00')[0]
    model=b[32:64].decode('utf-16-le').split('\x00')[0]
    rev,cnt=struct.unpack_from('<HH',b,64)
    off=68; chans=[]
    for _ in range(cnt):
        unit,namesz=struct.unpack_from('<IH',b,off)   # ULONG enum + USHORT
        name=b[off+6:off+6+namesz].decode('utf-16-le').split('\x00')[0]
        chans.append((name,unit))
        clen=6+namesz
        off+=clen + (clen % 2)   # try natural; adjust below if mis-parsed
    return oem,model,rev,cnt,chans

class Emi:
    def __init__(self,path):
        self.path=path
        self.h=kernel32.CreateFileW(path,0x80000000,3,None,3,0,None)
        if self.h==INVALID: raise OSError(ctypes.get_last_error())
        self.ver=struct.unpack('<H',ioctl(self.h,IOCTL_VER,8)[:2])[0]
        self.mdsize=struct.unpack('<I',ioctl(self.h,IOCTL_MDSIZE,8)[:4])[0]
        md=ioctl(self.h,IOCTL_MD,self.mdsize)
        self.oem,self.model,self.rev,self.count,self.chans=parse_md_v2(md)
    def read(self):
        b=ioctl(self.h,IOCTL_MEAS,16*self.count)
        return [struct.unpack_from('<QQ',b,16*i) for i in range(self.count)]  # (AbsoluteEnergy pWh, AbsoluteTime)

def is_admin():
    try: return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except: return False

if __name__=='__main__':
    print("elevated:", is_admin())
    devs=[]
    for p in emi_paths():
        try: devs.append(Emi(p))
        except OSError as e: print("open/ioctl fail",p,e)
    print(f"{len(devs)} EMI devices opened")
    for d in devs:
        print(f"  ver={d.ver} oem={d.oem!r} model={d.model!r} rev={d.rev} chans={[c[0] for c in d.chans]} units={[c[1] for c in d.chans]}")
    # find PKG
    pkg=None
    for d in devs:
        for i,(n,u) in enumerate(d.chans):
            if n.endswith('_PKG'): pkg=(d,i,n)
    print("PKG channel:", pkg[2] if pkg else None)
    # raw first read, show AbsoluteTime scale vs uptime
    t0=[ (d, d.read()) for d in devs ]
    upt = ctypes.windll.kernel32.GetTickCount64()/1000.0
    d0,m0=t0[0]
    print(f"sample: energy={m0[0][0]} time={m0[0][1]}  | uptime_s={upt:.1f}  time/1e7={m0[0][1]/1e7:.1f}s  time/1e4={m0[0][1]/1e4:.1f}s")
