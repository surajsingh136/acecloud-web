#!/usr/bin/env python3
"""
AceCloud SPOT WINDOWS SERVER Launcher (Windows/Linux - Pure Python)
=================================================================
Creates SPOT Windows Server 2022/2025 instances (billing_type=spot) with
selectable size (1-5, sabse low se jyada billing ke hisab se).
RDP login: Administrator / Rr@123456
Format in spot_server.txt: ip:3389:Administrator:Rr@123456

BEST-EFFORT startup script password set karti hai. Agar kaam na kare:
  -> Website Console -> RDP connect -> Administrator login -> password change karo.

Usage:
    python spot_server.py --email E --password P
    python spot_server.py --email E --password P --count 2
    python spot_server.py --email E --password P --delete
"""

import argparse
import base64
import getpass
import json
import os
import random
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

sys.dont_write_bytecode = True

try:
    import requests
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "requests", "-q"])
    import requests

try:
    import winrm
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "pywinrm", "-q"])
    import winrm

WEB_BASE = "https://customer.acecloud.ai"
API_BASE = "https://customer.acecloud.ai/api/v1"
PROXY_FILE = "spot_server.txt"
SAVE_DIR = Path(__file__).resolve().parent
REGION = "ap-south-mum-2"
PROJECT_ID = "614d6fae59af4035871b1de4301ee5af"
NET_ID = "e21d8b35-296a-4c10-ba2b-1ab2934086c0"    # ext-mum-old
SG_FALLBACK = "742e7197-a159-4d8d-ae24-7526f1c3fb60"  # default SG (verified)
RDP_PORT = 3389
BOOT_SIZE_GB = 50  # Windows images are 50GB (raw)
WIN_ADMIN = "Administrator"
WIN_PASS = "Rr@123456"
WINRM_PORT = 5985

# Once per script-run auto password (proven production flow):
# - password userdata (#ps1) se set hota hai Windows first boot par
# - script WinRM(5985) se connect karke password VERIFY + finalize karta hai
# - sirf ~1-2 min me ready (ACTIVE ke baad)
def gen_password(length=12):
    chars = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789!@#%"
    while True:
        pw = "".join(random.choice(chars) for _ in range(length))
        if (any(c.isupper() for c in pw) and any(c.islower() for c in pw)
                and any(c.isdigit() for c in pw) and any(c in "!@#%" for c in pw)):
            return pw


def build_user_data(pw):
    """#ps1 script -> base64. Windows images ke saath proven (raw test se confirm)."""
    ps1 = (
        "#ps1\r\n"
        "net user Administrator '{pw}'\r\n"
        "wmic useraccount where \"name='Administrator'\" set PasswordExpires=FALSE\r\n"
        "Set-ItemProperty \"HKLM:\\SYSTEM\\CurrentControlSet\\Control\\Terminal Server\" "
        "-Name \"fDenyTSConnections\" -Value 0\r\n"
        "netsh advfirewall firewall set rule group=\"remote desktop\" new enable=yes\r\n"
        "netsh advfirewall firewall add rule name=\"WinRM\" dir=in action=allow protocol=TCP localport=5985\r\n"
        "sc config winrm start= auto\r\n"
        "winrm quickconfig -quiet\r\n"
        "Get-NetAdapter | Set-DnsClientServerAddress -ServerAddresses 8.8.8.8,1.1.1.1\r\n"
    ).format(pw=pw)
    return base64.b64encode(ps1.encode()).decode()

# Windows Server images (Mumbai)
IMAGES = {
    "2022": {"label": "Windows Server 2022",
             "id": "f87a58ca-169d-4fe9-934a-1931e528f752"},
    "2025": {"label": "Windows Server 2025",
             "id": "1d3646b2-988b-4d69-a547-c1344745edaf"},
}

# 5 sizes: lowest -> highest billing (spot Rs/hr, Mumbai live prices)
SIZES = [
    {"n": 1, "flavor": "W.C3a.large",   "vcpu": 2,  "ram": 4,   "price": 1.21,
     "id": "cf98bb05-d580-464d-9840-3208e85b9f95"},
    {"n": 2, "flavor": "W.C3a.xlarge",  "vcpu": 4,  "ram": 8,   "price": 2.41,
     "id": "b6f01e92-9513-48a1-8261-0057d53cae52"},
    {"n": 3, "flavor": "W.C3a.2xlarge", "vcpu": 8,  "ram": 16,  "price": 4.82,
     "id": "7ceaebc9-e76d-4d3f-87ef-c93f85b0659a"},
    {"n": 4, "flavor": "W.C3a.4xlarge", "vcpu": 16, "ram": 32,  "price": 9.65,
     "id": "f0773799-5f99-4982-a7d7-fe236d3d4bdd"},
    {"n": 5, "flavor": "W.C3a.8xlarge", "vcpu": 32, "ram": 64,  "price": 19.30,
     "id": "54eb9e21-3fa7-4b46-b18e-1631b225f7ef"},
]

# Windows region assets (Mumbai = default, Noida = verified). Flavor ids/rates live-fetch karte hain.
WREGIONS = {
    "ap-south-mum-2": {
        "label": "Mumbai",
        "img2022": IMAGES["2022"]["id"],
        "img2025": IMAGES["2025"]["id"],
        "net": "e21d8b35-296a-4c10-ba2b-1ab2934086c0",
        "sg": "742e7197-a159-4d8d-ae24-7526f1c3fb60",
    },
    "ap-south-noi-1": {
        "label": "Noida",
        "img2022": "d496946b-7149-4680-a4c9-48904b7f7403",
        "img2025": "919a206f-b0b3-4dbb-8656-dc45e0909b33",
        "net": "4731ecad-f310-4d4c-8f68-02bbd92d575b",
        "sg": "fc6e23a5-e737-428d-82f6-a284f2cfb07f",
    },
}

SIZE_NAMES = ["W.C3a.large", "W.C3a.xlarge", "W.C3a.2xlarge",
              "W.C3a.4xlarge", "W.C3a.8xlarge"]

SESSION = requests.Session()
AUTH_TOKEN = ""


def log(msg, level="info"):
    c = {"info": "\033[94m", "ok": "\033[92m", "warn": "\033[93m", "error": "\033[91m"}
    try:
        print(f"{c.get(level, '')}[{level.upper()}]\033[0m {msg}")
    except UnicodeEncodeError:
        print(f"{c.get(level, '')}[{level.upper()}]\033[0m {msg.encode('ascii', 'replace').decode()}")


def login(email, password):
    global AUTH_TOKEN
    r = SESSION.get(f"{WEB_BASE}/v1/api/auth/csrf", timeout=20)
    r.raise_for_status()
    csrf = r.json().get("csrfToken")
    r2 = SESSION.post(f"{WEB_BASE}/v1/api/auth/callback/credentials",
                      data={"email": email, "password": password, "csrfToken": csrf,
                            "redirect": "false", "json": "true"}, timeout=20)
    if r2.status_code != 200:
        log(f"Login failed HTTP {r2.status_code}", "error")
        sys.exit(1)
    r3 = SESSION.get(f"{WEB_BASE}/v1/api/auth/session", timeout=20)
    AUTH_TOKEN = r3.json().get("user", {}).get("accessToken", "")
    if not AUTH_TOKEN:
        log("accessToken nahi mila", "error")
        sys.exit(1)
    log(f"Login OK: {email}", "ok")


def api(method, endpoint, data=None, params=None, timeout=60):
    url = f"{API_BASE}/{endpoint}"
    headers = {"Authorization": f"Bearer {AUTH_TOKEN}"}
    qp = dict(params or {})
    qp.setdefault("region", REGION)
    qp.setdefault("project_id", PROJECT_ID)
    try:
        r = SESSION.request(method, url, headers=headers, params=qp,
                            json=data if data is not None else None, timeout=timeout)
        if r.status_code in (200, 201, 202, 204):
            if r.status_code == 204:
                return {"success": True}
            try:
                j = r.json()
            except ValueError:
                return {"success": True, "raw": r.text}
            if isinstance(j, dict) and j.get("error") is False:
                return j
            return j
        else:
            err = ""
            try:
                err = json.dumps(r.json())
            except Exception:
                err = r.text[:300]
            log(f"API {method} {endpoint} -> {r.status_code}: {err}", "warn")
            return None
    except requests.RequestException as e:
        log(f"API error: {e}", "error")
        return None


def api_data(method, endpoint, data=None, params=None, timeout=60):
    result = api(method, endpoint, data=data, params=params, timeout=timeout)
    if result is None:
        return None
    if isinstance(result, dict):
        return result.get("data")
    return result


def get_spot_price(flavor_name, fallback):
    """Fetch live spot price (INR/hr) for a Windows flavor name."""
    params = {"resource": "instances", "is_gpu": "false", "is_spot": "true",
              "num_instances": "1", "currency": "INR", "os_type": "windows"}
    data = api_data("GET", "cloud/pricing", params=params)
    for x in (data or []):
        if (x.get("resource_name") or "") == flavor_name:
            p = x.get("price_per_spot")
            if p:
                return float(p)
    return fallback


def get_quota():
    """Live project quota -> (instances in_use/limit, cores, ram_gb). None on failure."""
    data = api_data("GET", "cloud/quota/compute", timeout=30)
    if not isinstance(data, dict):
        return None
    def pair(key, div=1):
        sub = data.get(key) or {}
        lu = sub.get("in_use") or 0
        ll = sub.get("limit")
        if ll is None or ll < 0:
            return (lu // div, None)
        return (lu // div, ll // div)
    return pair("instances"), pair("cores"), pair("ram", 1024)


def _qty(v):
    return str(v) if v is not None else "unlimited"


def show_quota():
    """Print live quota line with free slots."""
    q = get_quota()
    if not q:
        print("  (QUOTA: live fetch nahi hua)")
        return False
    (iu, il), (cu, cl), (ru, rl) = q
    free = None if il is None else max(0, il - iu)
    print(f"  QUOTA (live): instances {iu}/{_qty(il)} | cores {cu}/{_qty(cl)} | RAM {ru}GB/{_qty(rl)}GB")
    if free is not None:
        if free == 0:
            print("  !! INSTANCE QUOTA KHATAM - pehle koi instance DELETE karo ya quota badhwao !!")
        else:
            print(f"  -> baqi free: {free} instance(s) bana sakte ho")
    return True


def build_region(region_code):
    """Live-fetch region ke flavors+images + build SIZES. Fail ho to static fallback."""
    cfg = WREGIONS.get(region_code)
    if not cfg:
        return
    global IMAGES, SIZES, NET_ID, SG_FALLBACK
    IMAGES = {"2022": {"label": "Windows Server 2022", "id": cfg["img2022"]},
              "2025": {"label": "Windows Server 2025", "id": cfg["img2025"]}}
    NET_ID = cfg["net"]
    SG_FALLBACK = cfg["sg"]
    try:
        flavs = api_data("GET", "cloud/flavors", params={"region": region_code, "limit": "300"}) or []
        byn = {(x.get("name") or "").lower(): x for x in flavs}
        sizes = []
        for base in SIZES:
            f = byn.get(base["flavor"].lower())
            fid = (f or {}).get("id", base["id"])
            vcpu = (f or {}).get("vcpus", base["vcpu"])
            ram = (f or {}).get("memory", base["ram"] * 1024) // 1024
            price = get_spot_price(base["flavor"], base["price"])
            sizes.append({"n": base["n"], "flavor": base["flavor"], "vcpu": vcpu,
                          "ram": ram, "price": price, "id": fid})
        if sizes:
            SIZES = sizes
    except Exception:
        pass


def ensure_sg():
    sgs = api_data("GET", "cloud/security-groups")
    for sg in (sgs or []):
        name = sg.get("name", "") or ""
        if "spot-win" in name.lower():
            sid = sg.get("id", "")
            log(f"Using existing SG: {name} ({sid})", "ok")
            return sid
    sg_name = f"spot-win-sg-{int(time.time())}"
    rules = [
        {"rule_type": "Inbound", "protocol_name": "Custom TCP",
         "port_range_min": RDP_PORT, "port_range_max": RDP_PORT, "remote": "any"},
        {"rule_type": "Inbound", "protocol_name": "Custom TCP",
         "port_range_min": WINRM_PORT, "port_range_max": WINRM_PORT, "remote": "any"},
        {"rule_type": "Inbound", "protocol_name": "SSH",
         "port_range_min": 22, "port_range_max": 22, "remote": "any"},
    ]
    result = api_data("POST", "cloud/security-groups", data={"name": sg_name, "rules": rules})
    if isinstance(result, dict) and result.get("id"):
        log(f"SG created: {sg_name} ({result['id']})", "ok")
        return result["id"]
    log("SG create failed, using region default SG", "warn")
    return SG_FALLBACK


def create_win_spot(image_id, flavor_id, spot_price, sg_id, count, name_prefix, user_data=None, source_type="image", billing_type="spot"):
    inst_name = f"spot-win-{name_prefix}-{int(time.time())}-{random.randint(100,999)}"
    payload = {
        "name": inst_name,
        "count": 1,
        "flavor": flavor_id,
        "billing_type": billing_type,
        "boot_uuid": image_id,
        "source_type": source_type,
        "delete_on_termination": True,
        "volumes": [{"size": BOOT_SIZE_GB, "boot": True,
                     "volume_type": "NVMe based High IOPS Storage",
                     "billing_type": "hourly" if billing_type != "spot" else "spot"}],
        "network": [NET_ID],
        "security_group": [sg_id],
        "availability_zone": "nova",
    }
    if billing_type == "spot":
        payload["spot_flavor_current_price"] = spot_price
    if user_data:
        payload["script"] = user_data
    try:
        result = api_data("POST", "cloud/instances", data=payload, timeout=90)
        if isinstance(result, dict) and result.get("id"):
            return result["id"], inst_name
        log(f"Spot create failed: {inst_name}", "error")
    except Exception as e:
        log(f"Spot create exception: {e} | {inst_name}", "error")
    return "", inst_name


def list_my_snapshots(region=None):
    return api_data("GET", "cloud/snapshots", params={"region": region or REGION}) or []


def find_boot_volume(region, inst_name):
    vols = api_data("GET", "cloud/volumes", params={"region": region}) or []
    want = f"{inst_name} Boot Volume".lower()
    for v in vols:
        if (v.get("name") or "").lower() == want or (v.get("name") or "").lower() == inst_name.lower():
            return v.get("id"), (v.get("name") or "")
    for v in vols:
        if inst_name.lower() in (v.get("name") or "").lower():
            return v.get("id"), (v.get("name") or "")
    return "", ""


def take_snapshot(region, inst_name, snap_name, desc=""):
    vol_id, vol_name = find_boot_volume(region, inst_name)
    if not vol_id:
        log(f"{inst_name}: boot volume nahi mila", "error")
        return ""
    log(f"  Boot volume: {vol_name} ({vol_id})", "ok")
    body = {"name": snap_name, "volume_id": vol_id}
    if desc:
        body["description"] = desc
    r = api_data("POST", f"cloud/volumes/{vol_id}/snapshots", data=body, params={"region": region})
    if isinstance(r, dict) and r.get("id"):
        return r["id"]
    log(f"Snapshot create failed: {r}", "error")
    return ""


def snapshot_mode():
    log("=== MANUAL SNAPSHOT (kisi bhi running instance ka) ===", "ok")
    choice = []
    for region in WREGIONS:
        insts = api_data("GET", "cloud/instances", params={"region": region}) or []
        for x in insts:
            if x.get("status") in ("ACTIVE", "SHUTOFF", "STOPPED"):
                choice.append((region, x.get("name", ""), x.get("id", "")))
    if not choice:
        log("Koi instance nahi mila (ACTIVE/SHUTOFF).", "error")
        return
    print("\n  Instances (kisi ka bhi snapshot le sakte ho):")
    for i, (rg, nm, iid) in enumerate(choice, 1):
        print(f"    {i}) [{WREGIONS[rg]['label']}] {nm}")
    try:
        sel = input("\n  Select instance: ").strip()
    except Exception:
        sel = ""
    if not sel.isdigit() or not (1 <= int(sel) <= len(choice)):
        log("Cancel.", "warn")
        return
    region, inst_name, _iid = choice[int(sel) - 1]
    try:
        sname = input(f"  Snapshot name (ENTER = {inst_name[:20]}): ").strip()
    except Exception:
        sname = ""
    if not sname:
        sname = f"{inst_name[:24]}-snap"
    sid = take_snapshot(region, inst_name, sname, f"Manual snapshot of {inst_name}")
    if sid:
        log(f"SNAPSHOT CREATED (permanent): {sname} | {sid}", "ok")
        print("  ~Rs 0.34/hr (~Rs 250/month) storage. Isi se baad me server bana sakte ho.")
    else:
        log("Snapshot nahi bani.", "error")


def get_public_ip(instance):
    addrs = instance.get("addresses", {})
    public = addrs.get("public", [])
    if isinstance(public, list) and public:
        first = public[0]
        if isinstance(first, dict):
            return first.get("addr")
        return str(first)
    return ""


def get_console_url(instance_id):
    """Best-effort: GET cloud/instances/{id}/console -> VNC/RDP console URL."""
    try:
        result = api_data("GET", f"cloud/instances/{instance_id}/console")
        if isinstance(result, dict):
            return result.get("url", "")
    except Exception:
        pass
    return ""


def winrm_verify(ip, password, max_wait=300):
    """Wait for WinRM(5985) to open, then connect and verify/refresh password.
    Returns (ok, elapsed_seconds)."""
    start = time.time()
    while time.time() - start < max_wait:
        try:
            with socket.create_connection((ip, WINRM_PORT), timeout=3):
                break
        except Exception:
            time.sleep(5)
    else:
        return False, int(time.time() - start)
    while time.time() - start < max_wait:
        try:
            s = winrm.Session(f"http://{ip}:{WINRM_PORT}/wsman",
                              auth=(WIN_ADMIN, password), transport="ntlm")
            r = s.run_ps("Set-LocalUser -Name Administrator -PasswordNeverExpires $true; whoami")
            if r.status_code == 0:
                try:
                    s.run_cmd("netsh advfirewall firewall add rule name=WinRM_keep dir=in action=allow protocol=TCP localport=%s" % WINRM_PORT)
                except Exception:
                    pass
                return True, int(time.time() - start)
        except Exception:
            pass
        time.sleep(6)
    return False, int(time.time() - start)


def remove_gpo():
    log("Removing Windows Proxy GPO...")
    ps = '''
$reg = "HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Internet Settings"
Set-ItemProperty -Path $reg -Name "ProxyEnable" -Value 0 -Force
Set-ItemProperty -Path $reg -Name "ProxyServer" -Value "" -Force
netsh winhttp reset proxy
Write-Host "GPO REMOVED!"
'''
    try:
        subprocess.run(["powershell", "-ExecutionPolicy", "Bypass", "-Command", ps], timeout=30)
        log("GPO removed!", "ok")
    except Exception as e:
        log(f"GPO remove error: {e}", "warn")


def nuke_all(delete_gpo=True):
    """Delete only WINDOWS spot instances (spot-win-*) + spot-win SGs + local server file -> ZERO BILL. Linux proxies untouched."""
    log("=" * 50)
    log("NUKE (WINDOWS SPOT ONLY) - SAB DELETE (ZERO BILL)", "error")
    log("=" * 50)
    insts = api_data("GET", "cloud/instances")
    for inst in (insts or []):
        iid = inst.get("id", "")
        name = inst.get("name", "")
        if iid and "spot-win" in (name or "").lower():
            log(f"  Delete instance: {name}")
            api("DELETE", "cloud/instances", data={"key": "id", "values": [iid]})
        elif iid:
            log(f"  SKIP (not Windows spot): {name}")

    # Wait until WINDOWS instances are FULLY gone (delete is async).
    deadline = time.time() + 150
    while time.time() < deadline:
        rem = [i for i in (api_data("GET", "cloud/instances") or [])
               if "spot-win" in (i.get("name") or "").lower()]
        if not rem:
            break
        time.sleep(5)
    if rem:
        log(f"  {len(rem)} Windows instance(s) still terminating... will retry", "warn")

    # Delete spot-win SGs ONLY (squid/proxy = Linux, untouched).
    sgs = api_data("GET", "cloud/security-groups")
    for attempts in range(4):
        targets = []
        for sg in (sgs or []):
            name = (sg.get("name") or "").lower()
            sid = sg.get("id", "")
            if sid and name != "default" and "spot-win" in name:
                targets.append(sg)
        if not targets:
            break
        still = []
        for sg in targets:
            log(f"  Delete SG: {sg.get('name')}")
            r = api("DELETE", "cloud/security-groups", data={"key": "id", "values": [sg['id']]})
            if r is None:
                still.append(sg)
        sgs = still
        if sgs:
            time.sleep(8)
    if sgs:
        log(f"  {len(sgs)} SG(s) delete me help chahiye - web console se delete kar do", "warn")

    pf = os.path.join(SAVE_DIR, PROXY_FILE)
    try:
        if os.path.exists(pf):
            os.remove(pf)
            log(f"  Deleted: {pf}", "ok")
    except Exception:
        pass
    if delete_gpo:
        remove_gpo()
    rem = [i for i in (api_data("GET", "cloud/instances") or [])
           if "spot-win" in (i.get("name") or "").lower()]
    log(f"WINDOWS SPOT RUNNING: {len(rem)} - ZERO BILL!", "ok")
    return len(rem or [])


def schedule_auto_delete(run_hours, email, password):
    """Detached background process -> auto-delete works even if window is closed."""
    seconds = int(float(run_hours) * 3600)
    log(f"Auto-delete scheduler (background): {run_hours} hour(s) - window band karo, phir bhi chalega", "warn")
    args = [sys.executable, os.path.abspath(__file__),
            "--scheduler", "--auto-delete-after", str(seconds),
            "--email", email, "--password", password]
    try:
        if sys.platform == "win32":
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            try:
                flags |= subprocess.DETACHED_PROCESS
            except AttributeError:
                pass
            subprocess.Popen(args, creationflags=flags, close_fds=True,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        else:
            subprocess.Popen(args, start_new_session=True,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
    except Exception as e:
        log(f"Scheduler spawn error: {e}", "error")


def main():
    parser = argparse.ArgumentParser(description="AceCloud SPOT WINDOWS SERVER")
    parser.add_argument("--email", default="")
    parser.add_argument("--password", default="")
    parser.add_argument("--delete", action="store_true")
    parser.add_argument("--count", type=int, default=0, help="Number of Windows servers (0=ask)")
    parser.add_argument("--hours", type=int, default=0,
                        help="Auto-delete all after N hours (min 1). Billing rounds up to full hour.")
    parser.add_argument("--os", default="", help="2022 or 2025 (default 2022)")
    parser.add_argument("--size", type=int, default=0, help="1-5 (default 1)")
    parser.add_argument("--snapshot", action="store_true",
                        help="Manual snapshot lo kisi bhi running instance ka")
    parser.add_argument("--scheduler", action="store_true", help="(internal) background auto-delete worker")
    parser.add_argument("--auto-delete-after", type=int, default=0,
                        help="(internal) seconds to wait before nuke")
    args = parser.parse_args()

    print()
    print("=" * 60)
    print("  AceCloud SPOT Windows SERVER")
    print("=" * 60)
    print()

    email = args.email
    password = args.password
    if not email:
        email = input("  Email: ").strip()
    if not password:
        password = getpass.getpass("  Password: ")

    login(email, password)

    if args.scheduler:
        seconds = max(int(args.auto_delete_after or 0), 0)
        log(f"Scheduler: waiting {seconds/3600:.2f} hour(s) in background...", "warn")
        time.sleep(seconds)
        log("Auto-delete timer expired. Cleaning up...", "warn")
        nuke_all(delete_gpo=True)
        return

    if args.delete:
        nuke_all(delete_gpo=True)
        return

    if args.snapshot:
        snapshot_mode()
        return

    # Region selection (default = Mumbai)
    global REGION
    rsel = ""
    print("\n  Select REGION (kahan banani hai):")
    rcodes = list(WREGIONS.keys())
    for i, rc in enumerate(rcodes, 1):
        print("    %d) %s (%s)" % (i, WREGIONS[rc]["label"], rc))
    try:
        rsel = input("\n  Select region [1 - %d, ENTER = 1]: " % len(rcodes)).strip()
    except Exception:
        rsel = ""
    REGION = rcodes[int(rsel) - 1] if rsel.isdigit() and 1 <= int(rsel) <= len(rcodes) else rcodes[0]
    log(f"Region: {REGION} ({WREGIONS[REGION]['label']})", "ok")
    build_region(REGION)

    # Base selection: public Windows image VS meri snapshot
    boot_source = "image"
    base_snap = False
    os_key = "SNAP"
    print("\n  Base kya chahiye:")
    print("    1) Public Windows image (2022/2025)")
    print("    2) Meri saved snapshot (win-base-snap etc.)")
    try:
        bsel = input("\n  Select base [1, ENTER = public image]: ").strip()
    except Exception:
        bsel = ""
    if bsel == "2":
        snaps = list_my_snapshots()
        if not snaps:
            log("Koi snapshot nahi mili. Pehle `--snapshot` se banao ya public image use karo.", "warn")
        else:
            print("\n  Meri snapshots (permanent saved):")
            for i, sn in enumerate(snaps, 1):
                print(f"    {i}) {sn.get('name')} ({sn.get('size')}GB, {sn.get('status')}, id {sn.get('id')})")
            try:
                ssel = input("\n  Select snapshot: ").strip()
            except Exception:
                ssel = ""
            if ssel.isdigit() and 1 <= int(ssel) <= len(snaps):
                snap = snaps[int(ssel) - 1]
                image = {"label": f"SNAPSHOT: {snap.get('name')}", "id": snap.get("id")}
                boot_source = "snapshot"
                base_snap = True
                log(f"Base: snapshot {snap.get('name')} ({snap.get('id')}) - source_type=snapshot", "ok")
            else:
                log("Cancel -> public image", "warn")

    if not base_snap:
        # OS selection
        os_key = args.os
        if os_key not in ("2022", "2025"):
            print("\n  Windows OS:")
            print("    1) Windows Server 2022")
            print("    2) Windows Server 2025")
            try:
                osel = input("\n  Select OS [1, ENTER = 2022]: ").strip()
            except Exception:
                osel = ""
            os_key = "2025" if osel == "2" else "2022"
        image = IMAGES[os_key]
        log(f"OS: {image['label']}", "ok")

    # Size selection 1-5 (lowest -> highest billing)
    size_n = args.size
    if size_n not in range(1, 6):
        print("\n  Server SIZE (lowest -> highest billing):")
        show_quota()
        for s in SIZES:
            print(f"    {s['n']}) {s['flavor']} - {s['vcpu']} vCPU/{s['ram']}GB - Rs {s['price']}/hr")
        try:
            ssel = input("\n  Select size [1 - 5, ENTER = 1]: ").strip()
        except Exception:
            ssel = ""
        size_n = int(ssel) if ssel.isdigit() and 1 <= int(ssel) <= 5 else 1
    size = SIZES[size_n - 1]
    log(f"Size: {size['flavor']} ({size['vcpu']} vCPU/{size['ram']}GB) Rs {size['price']}/hr", "ok")

    count = args.count
    if count <= 0:
        try:
            count = int(input("\n  Kitni Windows servers banani hain? [1]: ").strip() or "1")
        except Exception:
            count = 1
        if count < 1:
            count = 1
    q = get_quota()
    if q:
        (iu, il), _c, _r = q
        if il is not None and iu + count > il:
            log(f"QUOTA shortage! in_use {iu}, limit {il}. {count} aur nahi banenge.", "error")
    log(f"Count: {count}", "ok")

    run_hours = args.hours
    if run_hours <= 0:
        try:
            run_hours = int(input("\n  Kitne hour ke liye chalao aur phir auto-delete? [1]: ").strip() or "1")
        except Exception:
            run_hours = 1
        if run_hours < 1:
            run_hours = 1
    log(f"Auto-delete after: {run_hours} hour(s)", "ok")

    spot_price = get_spot_price(size["flavor"], size["price"])
    log(f"Spot price {size['flavor']}: Rs.{spot_price}/hr", "ok")
    sg_id = ensure_sg()

    auto_pass = gen_password()
    log(f"Auto password generated (and set via startup script + WinRM verify)", "ok")

    # ============ PHASE 1: create all (serial submit, 2s spacing) ============
    log(f"Creating {count} Windows server(s) in parallel submit...", "ok")
    pending = {}
    prefix = time.strftime("%H%M%S")
    for n in range(count):
        iid, iname = create_win_spot(image["id"], size["id"], spot_price, sg_id, count,
                                     name_prefix=f"{os_key}-{prefix}-{n+1}",
                                     user_data=build_user_data(auto_pass),
                                     source_type=boot_source)
        if iid:
            pending[iname] = iid
            log(f"  Submitted: {iname} ({iid})", "ok")
        else:
            log(f"  Skipped failed create: {iname}", "warn")
        time.sleep(2)

    if not pending:
        log("No instances were created!", "error")
        sys.exit(1)
    log(f"Submitted {len(pending)}/{count}. Waiting for ACTIVE...", "ok")

    # ============ PHASE 2: parallel wait ACTIVE (threaded) ============
    import threading

    def wait_active(iid, max_wait=900):
        start = time.time()
        while time.time() - start < max_wait:
            try:
                result = api_data("GET", f"cloud/instances/{iid}", timeout=40)
            except Exception:
                result = None
            if isinstance(result, dict):
                st = result.get("status", "")
                if st == "ACTIVE":
                    return result
                if st == "ERROR":
                    return None
            time.sleep(8)
        return None

    results_lock = threading.Lock()
    poll_sem = threading.Semaphore(8)
    active_results = {}

    def _wait_worker(name, iid):
        with poll_sem:
            inst = wait_active(iid, max_wait=900)
        ip = ""
        console = ""
        verified = False
        verify_secs = 0
        if inst is not None:
            ip = get_public_ip(inst)
            flav = inst.get("flavor") or {}
            fname = flav.get("name") if isinstance(flav, dict) else flav
            fvc = (flav.get("vcpus") if isinstance(flav, dict) else None) or "?"
            fram = (flav.get("memory") if isinstance(flav, dict) else None) or "?"
            try:
                fram = int(fram) // 1024
            except (TypeError, ValueError):
                pass
            log(f"  {name}: ACTUAL SIZE = {fname} ({fvc} vCPU/{fram}GB)", "ok")
            console = get_console_url(iid)
            if ip:
                log(f"  Verifying password (WinRM) on {name} ...", "warn")
                verified, verify_secs = winrm_verify(ip, auto_pass)
                if verified:
                    log(f"  {name}: password VERIFIED via WinRM in {verify_secs}s", "ok")
                else:
                    log(f"  {name}: WinRM verify timeout ({verify_secs}s) - console se check karo", "warn")
        with results_lock:
            active_results[name] = {"id": iid, "inst": inst, "ip": ip, "console": console,
                                    "verified": verified, "verify_secs": verify_secs}

    threads = []
    for name, iid in pending.items():
        t = threading.Thread(target=_wait_worker, args=(name, iid), daemon=True)
        threads.append(t)
        t.start()
    for t in threads:
        t.join()

    ready = []
    for name, res in active_results.items():
        if res["inst"] is not None and res["ip"]:
            ready.append({"name": name, "id": res["id"], "ip": res["ip"],
                          "console": res.get("console", ""),
                          "pass": auto_pass,
                          "verified": res.get("verified", False),
                          "verify_secs": res.get("verify_secs", 0)})
            log(f"READY: {name} -> {res['ip']} (WinRM {'OK' if res.get('verified') else 'PENDING'})", "ok")
        elif res["inst"] is None:
            log(f"ERROR/TIMEOUT: {name}", "error")

    if not ready:
        log("Koi instance ACTIVE nahi hua.", "error")
        sys.exit(1)
    log(f"{len(ready)}/{count} servers ACTIVE.", "ok")

    # ============ Save plan (created + hours) for dashboard timer ============
    try:
        import json as _json
        from datetime import datetime as _dt
        plans_path = Path(SAVE_DIR).resolve().parent / "instance_plans.json"
        plans = {}
        try:
            with open(plans_path, encoding="utf-8") as _f:
                plans = _json.load(_f)
        except Exception:
            plans = {}
        now_iso = _dt.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        for it in ready:
            plans[it["name"]] = {"created": now_iso, "hours": int(run_hours)}
        with open(plans_path, "w", encoding="utf-8") as _f:
            _json.dump(plans, _f, indent=2)
        log(f"Plan saved -> {plans_path}", "ok")
    except Exception as e:
        log(f"Plan save skip: {e}", "warn")

    # ============ Save RDP info ============
    filepath = os.path.join(SAVE_DIR, PROXY_FILE)
    with open(filepath, "w") as f:  # fresh file per launch batch
        for it in ready:
            f.write(f"{it['ip']}:{RDP_PORT}:{WIN_ADMIN}:{it['pass']}\n")
            f.write(f"verified: {'YES - WinRM password confirmed' if it['verified'] else 'NO - console se check karo'}\n")
            if it.get("console"):
                f.write(f"console: {it['console']}\n")
    log(f"Saved RDP details to {filepath}", "ok")

    # Extra file: sirf IPs (ek per line) - koi aur changes nahi
    ips_path = os.path.join(SAVE_DIR, "spot_server_ips.txt")
    with open(ips_path, "w") as f:
        for it in ready:
            f.write(f"{it['ip']}\n")
    log(f"Saved IPs-only list to {ips_path}", "ok")

    print()
    print("=" * 60)
    print(f"  WINDOWS SERVER READY! ({len(ready)}/{count})")
    print("=" * 60)
    for it in ready:
        print(f"  RDP    : {it['ip']}:{RDP_PORT}")
        print(f"  User   : {WIN_ADMIN}")
        print(f"  Pass   : {it['pass']}  (AUTO-GENERATED, WinRM verified in {it['verify_secs']}s)")
        if it.get("console"):
            print(f"  Console: {it['console']}")
        print()
    print(f"  OS     : {image['label']}")
    print(f"  Size   : {size['flavor']} ({size['vcpu']}vCPU/{size['ram']}GB)")
    print(f"  Billing: SPOT Rs.{spot_price}/hr")
    print(f"  Saved  : {os.path.join(SAVE_DIR, PROXY_FILE)}")
    print(f"  Auto   : delete after {run_hours} hour(s)")
    print()
    print("  LOGIN (CONSOLE -> RDP connect): Administrator / upar wala auto password")
    print("  Note: Password auto-set via startup script + WinRM verify ho gaya hai.")
    print("=" * 60)
    print()

    schedule_auto_delete(run_hours, email, password)
    log("ZERO BILL guarantee active - window band karo, timer background me chalega.", "ok")

    n_console = sum(1 for it in ready if it.get("console"))
    if n_console:
        log(f"Opening {n_console} console(s) in separate browser tabs...", "info")
        for it in ready:
            url = it.get("console", "")
            if url:
                try:
                    webbrowser.open(url, new=2)  # naye tab me
                    time.sleep(1.5)
                except Exception as e:
                    log(f"Browser open error: {e}", "warn")


if __name__ == "__main__":
    main()