import sys,os,time,subprocess
sys.path.insert(0,os.path.dirname(os.path.abspath(__file__)))
from emi_power import Emi, emi_paths
BURN = "import time,sys\nt=time.time()+float(sys.argv[1])\nx=0\nwhile time.time()<t: x=(x*1103515245+12345)&0xFFFFFFFF\n"
devs=[]
for p in emi_paths():
    try: devs.append(Emi(p))
    except OSError: pass
def snap():
    o={}
    for d in devs:
        m=d.read()
        for i,(n,u) in enumerate(d.chans): o[n]=m[i]
    return o
def W(a,b,n):
    de=b[n][0]-a[n][0]; dt=(b[n][1]-a[n][1])/1e7
    return de*3.6e-9/dt if dt>0 else 0
def batt():
    o=subprocess.run(["powershell","-NoProfile","-Command","(Get-CimInstance -Namespace root/wmi -ClassName BatteryStatus).DischargeRate"],capture_output=True,text=True,timeout=20).stdout.strip()
    return int(o)/1000 if o.isdigit() else float('nan')
print(f"{'phase':<12}{'PKG W':>8}{'cores W':>9}{'non-core':>10}{'battery W':>11}{'PKG/batt':>10}")
rows=[]
for label,n in (("idle",0),("1 thread",1),("4 threads",4),("16 threads",16)):
    ps=[subprocess.Popen([sys.executable,"-c",BURN,"12"],stdout=subprocess.DEVNULL) for _ in range(n)]
    time.sleep(2.0)
    a=snap(); time.sleep(6); b=snap(); bt=batt()
    for p in ps: p.wait()
    pkg=W(a,b,'RAPL_Package0_PKG'); cores=sum(W(a,b,k) for k in a if k.endswith('_CORE'))
    print(f"{label:<12}{pkg:8.2f}{cores:9.2f}{pkg-cores:10.2f}{bt:11.2f}{(pkg/bt*100 if bt==bt else 0):9.0f}%")
    rows.append((label,pkg,bt))
    time.sleep(3)
i=rows[0]; l=rows[-1]
print(f"\nCPU-attributable delta idle->16thr: PKG {l[1]-i[1]:+.2f} W ; total system {l[2]-i[2]:+.2f} W")
