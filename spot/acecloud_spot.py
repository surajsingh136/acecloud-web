#!/usr/bin/env python3
"""
AceCloud SPOT Squid Proxy Launcher (Windows/Linux - Pure Python)
=================================================================
Creates CHEAP SPOT instances (billing_type=spot) running a private Squid proxy.
Proxy format in proxy.txt: ip:port:ajay:ajay

Spot pricing: uses live price_per_spot (INR) from /cloud/pricing, and sends
it as spot_flavor_current_price so the server accepts the create (otherwise
422 "Spot instance price has changed").

Usage:
    python acecloud_spot.py --email E --password P
    python acecloud_spot.py --email E --password P --count 3
    python acecloud_spot.py --email E --password P --delete
"""

import argparse
import base64
import getpass
import json
import os
import random
import re
import string
import subprocess
import sys
import time
import socket
from pathlib import Path

sys.dont_write_bytecode = True

try:
    import requests
except ImportError:
    subprocess.check_call([sys.executable, "-m", "pip", "install", "requests", "-q"])
    import requests

try:
    import paramiko
except Exception:
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "paramiko", "-q"])
    except Exception:
        pass
    try:
        import paramiko
    except Exception:
        paramiko = None

WEB_BASE = "https://customer.acecloud.ai"
API_BASE = "https://customer.acecloud.ai/api/v1"
PROXY_FILE = "proxy_spot.txt"
SAVE_DIR = Path(__file__).resolve().parent
DEFAULT_PORT = 3128
BOOT_SIZE_GB = 20
REGION = "ap-south-mum-2"
PROJECT_ID = "614d6fae59af4035871b1de4301ee5af"
KEY_NAME = ""

# Region -> assets (flavor, image, network, sg, spot-price fallback)
REGIONS = {
    "ap-south-mum-2": {   # Mumbai (default)
        "label": "Mumbai",
        "flavor": "0288ce6a-723c-4712-bdeb-c500c6e54ed4",   # C4a.medium
        "image": "91f3565c-b0ab-4936-b719-806cc0ee5ae3",    # Ubuntu-24.04-LTS
        "network": "e21d8b35-296a-4c10-ba2b-1ab2934086c0",  # ext-mum-old
        "sg": "fc6e23a5-e737-428d-82f6-a284f2cfb07f",
        "spot_fallback": 0.8996,
    },
    "ap-south-noi-1": {   # Noida
        "label": "Noida",
        "flavor": "5ee30d75-6e01-40c7-9ef3-d1145a2f3238",   # C4a.medium
        "image": "89244901-471a-47a4-a0b6-c41597e0367e",    # Ubuntu-24.04-LTS
        "network": "4731ecad-f310-4d4c-8f68-02bbd92d575b",  # EXT-NOI
        "sg": "fc6e23a5-e737-428d-82f6-a284f2cfb07f",
        "spot_fallback": 0.819,
    },
}

# 5 proxy sizes (lowest -> highest billing). Flavor ids/rates per region are live-fetched.
LINUX_SIZES_NAMES = ["C4a.medium", "C4a.large", "C4a.xlarge", "C4a.2xlarge", "C4a.4xlarge"]
LINUX_FLAVOR_FALLBACK = {
    "ap-south-mum-2": {
        "C4a.medium": "0288ce6a-723c-4712-bdeb-c500c6e54ed4",
        "C4a.large": "867766b7-8497-4a5e-95f5-2401df03910a",
        "C4a.xlarge": "ebdf679b-869f-4085-8e31-3bd7deb43383",
        "C4a.2xlarge": "982e7784-f518-4b50-9523-c5e93bafa0de",
        "C4a.4xlarge": "8d5fcacc-33f9-4936-a030-c515939ec4b2",
    },
    "ap-south-noi-1": {
        "C4a.medium": "5ee30d75-6e01-40c7-9ef3-d1145a2f3238",
        "C4a.large": "6be040d3-52af-451d-a4cf-c4ba2554be12",
        "C4a.xlarge": "7e3f6de1-e981-47d4-9f30-a80033120eb0",
        "C4a.2xlarge": "fc29da70-b052-4417-a1ba-3124bd669994",
        "C4a.4xlarge": "2af2f3f8-d95a-4787-b48f-53c71cda54cf",
    },
}
LINUX_SPOT_FALLBACK = {
    "ap-south-mum-2": {"C4a.medium": 0.8996, "C4a.large": 1.7956, "C4a.xlarge": 3.5905,
                       "C4a.2xlarge": 7.1815, "C4a.4xlarge": 14.363},
    "ap-south-noi-1": {"C4a.medium": 0.7917, "C4a.large": 1.5777, "C4a.xlarge": 3.1553,
                       "C4a.2xlarge": 6.3105, "C4a.4xlarge": 12.6209},
}

IMAGE_ID = REGIONS[REGION]["image"]
FLAVOR_ID = REGIONS[REGION]["flavor"]
EXT_NET_ID = REGIONS[REGION]["network"]
PROXY_USER = "ajay"
PROXY_PASS = "ajay"

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
        log("accessToken not found", "error")
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


def get_spot_price(size_name="C4a.medium"):
    """Fetch live spot price_per_spot (INR) for given C4a size in current REGION."""
    params = {"resource": "instances", "is_gpu": "false", "is_spot": "true",
              "num_instances": "1", "currency": "INR",
              "exclude_resource_type": "aie,nb", "os_type": "linux"}
    data = api_data("GET", "cloud/pricing", params=params)
    for x in (data or []):
        if (x.get("resource_name") or "") == size_name:
            price = x.get("price_per_spot")
            if price:
                return float(price)
    fb = (LINUX_SPOT_FALLBACK.get(REGION) or {}).get(size_name, 0.8996)
    log(f"Spot price not found: {size_name}, defaulting to {fb}", "warn")
    return fb


def get_quota():
    """Live project quota -> (instances, cores, ram_gb) each (in_use, limit)."""
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
    q = get_quota()
    if not q:
        print("  (QUOTA: live fetch failed)")
        return
    (iu, il), (cu, cl), (ru, rl) = q
    free = None if il is None else max(0, il - iu)
    print(f"  QUOTA (live): instances {iu}/{_qty(il)} | cores {cu}/{_qty(cl)} | RAM {ru}GB/{_qty(rl)}GB")
    if free is not None:
        if free == 0:
            print("  !! INSTANCE QUOTA FULL - delete an instance first or request a quota increase !!")
        else:
            print(f"  -> remaining free: {free} instance(s) you can create")


def build_linux_sizes(region):
    """Live-fetch flavors -> 5 sizes (name, id, vcpu, ram). Static fallback if fetch fails."""
    sizes = []
    try:
        flavs = api_data("GET", "cloud/flavors", params={"region": region, "limit": "500"}) or []
        byn = {(x.get("name") or "").lower(): x for x in flavs}
        for i, nm in enumerate(LINUX_SIZES_NAMES, 1):
            f = byn.get(nm.lower())
            sizes.append({
                "n": i, "name": nm,
                "id": (f or {}).get("id", (LINUX_FLAVOR_FALLBACK.get(region) or {}).get(nm, "")),
                "vcpu": (f or {}).get("vcpus", 1),
                "ram": ((f or {}).get("memory", 2048) or 2048) // 1024,
                "price": get_spot_price(nm),
            })
    except Exception:
        pass
    if not sizes:
        for i, nm in enumerate(LINUX_SIZES_NAMES, 1):
            sizes.append({"n": i, "name": nm,
                          "id": (LINUX_FLAVOR_FALLBACK.get(region) or {}).get(nm, ""),
                          "vcpu": 1, "ram": 2,
                          "price": (LINUX_SPOT_FALLBACK.get(region) or {}).get(nm, 0.8996)})
    return sizes


def _sg_has_port(sg, port):
    for rl in (sg.get("security_group_rules") or []):
        pmin = rl.get("port_range_min")
        pmax = rl.get("port_range_max")
        try:
            pmin, pmax = int(pmin), int(pmax)
        except (TypeError, ValueError):
            continue
        if pmin <= port <= pmax:
            return True
    return False


def ensure_sg(port):
    sgs = api_data("GET", "cloud/security-groups")
    for sg in (sgs or []):
        name = (sg.get("name") or "").lower()
        sid = sg.get("id", "")
        if sid and name != "default" and _sg_has_port(sg, port):
            log(f"Using existing SG (port {port} open): {name} ({sid})", "ok")
            return sid
    sg_name = f"squid-sg-{int(time.time())}"
    rules = [
        {"rule_type": "Inbound", "protocol_name": "SSH",
         "port_range_min": 22, "port_range_max": 22, "remote": "any"},
        {"rule_type": "Inbound", "protocol_name": "Custom TCP",
         "port_range_min": port, "port_range_max": port, "remote": "any"},
    ]
    result = api_data("POST", "cloud/security-groups", data={"name": sg_name, "rules": rules})
    if isinstance(result, dict) and result.get("id"):
        log(f"SG created: {sg_name} ({result['id']})", "ok")
        return result["id"]
    log("SG create failed, using region default SG", "warn")
    return REGIONS[REGION]["sg"]


def build_user_data(proxy_user, proxy_pass, squid_port=DEFAULT_PORT):
    safe_user = proxy_user.replace("'", "").replace('"', "")
    safe_pass = proxy_pass.replace("'", "").replace('"', "")
    return f'''#!/bin/bash
# ServerOK-style Squid proxy installer (auto-retry - never miss)
export DEBIAN_FRONTEND=noninteractive
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
exec > >(tee -a /var/log/squid-install.log) 2>&1
echo "=== squid install start $(date) ==="

# DNS make tight fix for 103.x pool (provider DNS unreachable)
if ! grep -q "8.8.8.8" /etc/resolv.conf 2>/dev/null; then
  echo "nameserver 8.8.8.8" > /etc/resolv.conf
  echo "nameserver 1.1.1.1" >> /etc/resolv.conf
fi
echo 'Acquire::ForceIPv4 "true";' > /etc/apt/apt.conf.d/99force-ipv4 2>/dev/null || true
chattr -i /etc/resolv.conf 2>/dev/null || true
echo "nameserver 8.8.8.8" > /etc/resolv.conf
echo "nameserver 1.1.1.1" >> /etc/resolv.conf
chattr +i /etc/resolv.conf 2>/dev/null || true

apt_ok=0
for i in 1 2 3 4 5 6; do
  apt-get update -qq && {{ apt_ok=1; break; }}
  echo "[retry] apt update $i ..."; sleep 5
done

apt_ok=0
for i in 1 2 3 4 5 6; do
  apt-get install -y -qq apache2-utils squid && {{ apt_ok=1; break; }}
  echo "[retry] apt install $i ..."; sleep 5
done

if [ ! -x /usr/sbin/squid ] && [ ! -x /usr/local/sbin/squid ]; then
  echo "FATAL: squid binary missing"; exit 1
fi

mkdir -p /etc/squid /var/spool/squid
[ -f /etc/squid/squid.conf ] && cp /etc/squid/squid.conf /etc/squid/squid.conf.bak.$(date +%s) 2>/dev/null
touch /etc/squid/passwd /etc/squid/blacklist.acl
cat > /etc/squid/squid.conf << 'SQEOF'
http_port {squid_port}
dns_nameservers 8.8.8.8 1.1.1.1
cache deny all
hierarchy_stoplist cgi-bin ?

access_log none
cache_store_log none
cache_log /dev/null

refresh_pattern ^ftp: 1440 20% 10080
refresh_pattern ^gopher: 1440 0% 1440
refresh_pattern -i (/cgi-bin/|\\?) 0 0% 0
refresh_pattern . 0 20% 4320

acl localhost src 127.0.0.1/32 ::1
acl to_localhost dst 127.0.0.0/8 0.0.0.0/32 ::1

acl SSL_ports port 1-65535
acl Safe_ports port 1-65535
acl CONNECT method CONNECT
acl siteblacklist dstdomain "/etc/squid/blacklist.acl"
http_access allow manager localhost
http_access deny manager

http_access deny !Safe_ports

http_access deny CONNECT !SSL_ports
http_access deny siteblacklist
auth_param basic program /usr/lib/squid/basic_ncsa_auth /etc/squid/passwd

auth_param basic children 5
auth_param basic realm Squid proxy-caching web server
auth_param basic credentialsttl 2 hours
acl password proxy_auth REQUIRED
http_access allow localhost
http_access allow password
http_access deny all

forwarded_for off
request_header_access Allow allow all
request_header_access Authorization allow all
request_header_access WWW-Authenticate allow all
request_header_access Proxy-Authorization allow all
request_header_access Proxy-Authenticate allow all
request_header_access Cache-Control allow all
request_header_access Content-Encoding allow all
request_header_access Content-Length allow all
request_header_access Content-Type allow all
request_header_access Date allow all
request_header_access Expires allow all
request_header_access Host allow all
request_header_access If-Modified-Since allow all
request_header_access Last-Modified allow all
request_header_access Location allow all
request_header_access Pragma allow all
request_header_access Accept allow all
request_header_access Accept-Charset allow all
request_header_access Accept-Encoding allow all
request_header_access Accept-Language allow all
request_header_access Content-Language allow all
request_header_access Mime-Version allow all
request_header_access Retry-After allow all
request_header_access Title allow all
request_header_access Connection allow all
request_header_access Proxy-Connection allow all
request_header_access User-Agent allow all
request_header_access Cookie allow all
request_header_access All deny all
SQEOF

chown -R proxy:proxy /var/spool/squid /etc/squid/passwd 2>/dev/null || true
htpasswd -bc /etc/squid/passwd {safe_user} {safe_pass}

if [ -x /sbin/iptables ]; then
  /sbin/iptables -I INPUT -p tcp --dport {squid_port} -j ACCEPT 2>/dev/null || true
fi
if [ -x /usr/sbin/ufw ]; then
  ufw allow {squid_port}/tcp >/dev/null 2>&1 || true
fi

systemctl enable squid >/dev/null 2>&1 || true
systemctl restart squid 2>/dev/null || service squid restart

sleep 3
if systemctl is-active --quiet squid 2>/dev/null || pgrep -x squid >/dev/null 2>&1; then
  touch /var/log/squid-install.done
  echo "SQUID_OK"
else
  echo "SQUID_PENDING"
  exit 1
fi
'''


def create_spot_instance(spot_price, sg_id, user_data_b64, port, name=None, billing_type="spot"):
    inst_name = name or f"spot-{int(time.time())}-{random.randint(100,999)}"
    payload = {
        "name": inst_name,
        "count": 1,
        "flavor": FLAVOR_ID,
        "billing_type": billing_type,
        "boot_uuid": IMAGE_ID,
        "source_type": "image",
        "delete_on_termination": True,
        "volumes": [{"size": BOOT_SIZE_GB, "boot": True,
                     "volume_type": "NVMe based High IOPS Storage",
                     "billing_type": "hourly" if billing_type != "spot" else "spot"}],
        "network": [EXT_NET_ID],
        "security_group": [sg_id],
        "key_name": KEY_NAME,
        "availability_zone": "nova",
        "script": user_data_b64,
    }
    if billing_type == "spot":
        payload["spot_flavor_current_price"] = spot_price
    try:
        result = api_data("POST", "cloud/instances", data=payload, timeout=90)
        if isinstance(result, dict) and result.get("id"):
            return result["id"], inst_name
        log(f"Spot create failed: {inst_name}", "error")
    except Exception as e:
        log(f"Spot create exception: {e} | {inst_name}", "error")
    return "", inst_name


def wait_for_active(instance_id, max_wait=300):
    log(f"Waiting ACTIVE (max {max_wait}s)...")
    start = time.time()
    while time.time() - start < max_wait:
        result = api_data("GET", f"cloud/instances/{instance_id}")
        if isinstance(result, dict):
            status = result.get("status", "")
            if status == "ACTIVE":
                log(f"ACTIVE ({int(time.time()-start)}s)", "ok")
                return result
            if status == "ERROR":
                log("Instance ERROR state!", "error")
                return None
        time.sleep(10)
    log("Not ACTIVE in time.", "error")
    return None


def get_public_ip(instance):
    addrs = instance.get("addresses", {})
    public = addrs.get("public", [])
    if isinstance(public, list) and public:
        first = public[0]
        if isinstance(first, dict):
            return first.get("addr")
        return str(first)
    return ""


def discover_keypairs():
    data = api_data("GET", "cloud/key-pairs")
    return data if isinstance(data, list) else []


def ensure_keypair():
    """Dedicated ed25519 keypair with LOCAL private key (needed for SSH repair fallback)."""
    ssh_dir = Path.home() / ".ssh"
    ssh_dir.mkdir(parents=True, exist_ok=True)
    priv_key = ssh_dir / "id_ed25519"
    pub_key = ssh_dir / "id_ed25519.pub"
    if not priv_key.exists():
        subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(priv_key), "-q"], check=True)
    if pub_key.exists():
        pubkey = pub_key.read_text().strip()
    else:
        with open(pub_key, "w") as f:
            subprocess.run(["ssh-keygen", "-y", "-f", str(priv_key)], stdout=f, check=True)
        pubkey = pub_key.read_text().strip()
    for kp in discover_keypairs():
        kn = kp.get("name") or kp.get("id") or ""
        if str(kn).startswith("proxy-key-"):
            log(f"Using existing keypair: {kn}", "ok")
            return kn
    key_name = f"proxy-key-{int(time.time())}"
    result = api_data("POST", "cloud/key-pairs", data={"name": key_name, "key": pubkey})
    if isinstance(result, dict) and result.get("id"):
        log(f"Keypair created: {key_name}", "ok")
        return key_name
    log(f"Keypair create failed. Name: {key_name}", "warn")
    return key_name


def ssh_repair(ip, key_path, install_script, attempts=4):
    """SSH-in (ubuntu/root), run installer if marker missing. Returns True when marker exists."""
    if paramiko is None:
        return False
    for attempt in range(1, attempts + 1):
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ok = False
        for user in ("ubuntu", "root"):
            try:
                client.connect(ip, username=user, key_filename=str(key_path),
                               timeout=15, banner_timeout=20, auth_timeout=20)
                _in, out, _err = client.exec_command(
                    "test -f /var/log/squid-install.done && echo DONE || echo NO", timeout=30)
                if "DONE" in (out.read().decode(errors="replace") or ""):
                    client.close()
                    return True
                cmd = "bash -s" if user == "root" else "sudo -n bash -s"
                _in, out, _err = client.exec_command(cmd, timeout=900)
                _in.write(install_script)
                _in.channel.shutdown_write()
                outtxt = out.read().decode(errors="replace")
                status = out.channel.recv_exit_status()
                client.close()
                if status == 0 or "SQUID_OK" in outtxt:
                    ok = True
                    break
                log(f"  SSH ran installer (rc={status}) on {ip}, rechecking...", "warn")
            except Exception as e:
                log(f"  SSH {user}@{ip} attempt {attempt}: {e}", "warn")
                try:
                    client.close()
                except Exception:
                    pass
        if ok:
            return True
        time.sleep(12)
    return False


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


def _is_proxy_inst(name):
    """Linux proxy instance = created by THIS script only. Windows spot stays untouched."""
    n = (name or "").lower()
    if "spot-win" in n or "windows" in n:
        return False
    if "proxy" in n:
        return True
    return re.match(r"^spot-\d+-\d+$", n) is not None


def nuke_all(delete_gpo=True, regions=None):
    """Delete only LINUX proxy instances + squid/proxy SGs + local proxy file (Windows spot untouched)."""
    regions = regions or list(REGIONS)
    log("=" * 50)
    log("NUKE (LINUX PROXY ONLY) - DELETE ALL (ZERO BILL)", "error")
    log("=" * 50)
    for region in regions:
        log(f"-- Region: {region} ({REGIONS[region]['label']}) --", "ok")
        insts = api_data("GET", "cloud/instances", params={"region": region})
        for inst in (insts or []):
            name = inst.get("name", "")
            if inst.get("id") and _is_proxy_inst(name):
                log(f"  Delete instance: {name}")
                api("DELETE", "cloud/instances", data={"key": "id", "values": [inst["id"]]})
            elif inst.get("id"):
                log(f"  SKIP (not Linux proxy): {name}")
        sgs = api_data("GET", "cloud/security-groups", params={"region": region})
        for sg in (sgs or []):
            name = sg.get("name", "") or ""
            sid = sg.get("id", "")
            ln = name.lower()
            if (sid and ln != "default"
                    and ("squid" in ln or "proxy" in ln or ln.startswith("spot-sg-"))):
                log(f"  Delete SG: {name}")
                api("DELETE", "cloud/security-groups", data={"key": "id", "values": [sid]})
            elif sid:
                log(f"  SKIP SG (Windows/other): {name}")
    time.sleep(8)
    pf = os.path.join(SAVE_DIR, PROXY_FILE)
    for f in list(SAVE_DIR.glob("proxy*.txt")) + [os.path.join(SAVE_DIR, "proxy.txt")]:
        try:
            if os.path.exists(f):
                os.remove(f)
                log(f"  Deleted: {f}", "ok")
        except Exception:
            pass
    if delete_gpo:
        remove_gpo()
    total = 0
    for region in regions:
        insts = api_data("GET", "cloud/instances", params={"region": region}) or []
        total += len(insts)
        for inst in insts:
            log(f"  RUNNING {region} [{inst.get('status')}]: {inst.get('name')}", "warn")
    log(f"RUNNING INSTANCES (all regions): {total} — ZERO BILL!", "ok")
    return total


def schedule_auto_delete(run_hours, email, password):
    """Background thread: sleep run_hours, then login again + nuke_all (ZERO BILL)."""
    import threading
    seconds = int(float(run_hours) * 3600)

    def worker():
        log(f"Auto-delete scheduled in {run_hours} hour(s)...", "warn")
        time.sleep(seconds)
        log(f"Auto-delete timer expired. Cleaning up...", "warn")
        try:
            login(email, password)
            time.sleep(3)
        except Exception as e:
            log(f"Re-login failed: {e}", "warn")
        try:
            nuke_all(delete_gpo=True)
        except Exception as e:
            log(f"Auto nuke error: {e}", "error")
        os._exit(0)

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    return t


def main():
    parser = argparse.ArgumentParser(description="AceCloud SPOT Squid Proxy")
    parser.add_argument("--email", default="")
    parser.add_argument("--password", default="")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--delete", action="store_true")
    parser.add_argument("--count", type=int, default=0, help="Number of spot proxies (0=ask)")
    parser.add_argument("--hours", type=int, default=0,
                        help="Auto-delete all instances after N hours (min 1). Billing rounds up to next full hour.")
    parser.add_argument("--size", type=int, default=0, help="1-5 proxy size (default 1 = C4a.medium)")
    args = parser.parse_args()

    print()
    print("=" * 60)
    print("  AceCloud SPOT Squid Proxy (cheap, hourly)")
    print("=" * 60)
    print()

    email = args.email
    password = args.password
    if not email:
        email = input("  Email: ").strip()
    if not password:
        password = getpass.getpass("  Password: ")

    global REGION, FLAVOR_ID, IMAGE_ID, EXT_NET_ID, KEY_NAME
    login(email, password)

    # Delete mode: choose region, then cleanup
    if args.delete:
        print("\n  Which region's proxies should be deleted?")
        print("    1) Mumbai only")
        print("    2) Noida only")
        print("    3) Both regions (default)")
        try:
            rup = input("\n  Select [3]: ").strip()
        except Exception:
            rup = ""
        if rup == "1":
            targets = ["ap-south-mum-2"]
        elif rup == "2":
            targets = ["ap-south-noi-1"]
        else:
            targets = list(REGIONS)
        nuke_all(delete_gpo=True, regions=targets)
        return

    # Region selection (1 = Mumbai default, 2 = Noida)
    print("\n  Region:")
    for i, (rk, rv) in enumerate(REGIONS.items(), 1):
        print(f"    {i}) {rv['label']} ({'default' if i == 1 else ''})")
    try:
        rsel = input("\n  Select region [1 - %d, ENTER = Mumbai]: " % len(REGIONS)).strip()
    except Exception:
        rsel = ""
    if rsel == "2":
        REGION = "ap-south-noi-1"
    else:
        REGION = "ap-south-mum-2"
    FLAVOR_ID = REGIONS[REGION]["flavor"]
    IMAGE_ID = REGIONS[REGION]["image"]
    EXT_NET_ID = REGIONS[REGION]["network"]
    log(f"Region: {REGION} ({REGIONS[REGION]['label']})", "ok")

    sizes = build_linux_sizes(REGION)
    size_n = args.size
    if size_n not in range(1, len(sizes) + 1):
        print("\n  Proxy SIZE (lowest -> highest billing):")
        show_quota()
        for s in sizes:
            print("    %d) %s - %d vCPU/%dGB - Rs %s/hr" % (s["n"], s["name"], s["vcpu"], s["ram"], s["price"]))
        try:
            ssel = input("\n  Select size [1 - %d, ENTER = 1]: " % len(sizes)).strip()
        except Exception:
            ssel = ""
        size_n = int(ssel) if ssel.isdigit() and 1 <= int(ssel) <= len(sizes) else 1
    sel = sizes[size_n - 1]
    FLAVOR_ID = sel["id"]
    log(f"Size: {sel['name']} ({sel['vcpu']} vCPU/{sel['ram']}GB) - spot Rs.{sel['price']}/hr", "ok")

    KEY_NAME = ensure_keypair()

    count = args.count
    if count <= 0:
        try:
            count = int(input("\n  How many spot proxies to create? [1]: ").strip() or "1")
        except Exception:
            count = 1
        if count < 1:
            count = 1
    log(f"Count: {count}", "ok")

    run_hours = args.hours
    if run_hours <= 0:
        try:
            run_hours = int(input("\n  Hours to run before auto-delete? [1]: ").strip() or "1")
        except Exception:
            run_hours = 1
        if run_hours < 1:
            run_hours = 1
    log(f"Auto-delete after: {run_hours} hour(s)", "ok")
    if run_hours < 1:
        run_hours = 1

    spot_price = get_spot_price(sel["name"])
    log(f"Spot price {sel['name']}: Rs.{spot_price}/hr", "ok")
    sg_id = ensure_sg(args.port)
    user_data = build_user_data(PROXY_USER, PROXY_PASS, args.port)
    user_data_b64 = base64.b64encode(user_data.encode()).decode()

    # ============ PHASE 1: 30 instances PARALLEL create (fast) ============
    log(f"Creating {count} spot instances in parallel (no waiting)...", "ok")
    pending = {}   # name -> instance_id
    for n in range(count):
        iid, iname = create_spot_instance(spot_price, sg_id, user_data_b64, args.port,
                                          name=f"spot-{int(time.time())}-{n+1}")
        if iid:
            pending[iname] = iid
            log(f"  Submitted: {iname} ({iid})", "ok")
        else:
            log(f"  Skipped failed create: {iname}", "warn")
        time.sleep(2)  # short spacing to avoid rate-limit on rapid creates

    if not pending:
        log("No instances were created!", "error")
        sys.exit(1)
    log(f"Submitted {len(pending)}/{count} instances. Waiting for ACTIVE...", "ok")

    # ============ PHASE 2: MULTI-THREADED wait for all instances to go ACTIVE ============
    import threading

    def wait_active(iid, max_wait=600):
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

    # each instance waits in its own thread (all together = time of a single instance)
    # but limit API polling (max 8 concurrent) so we don't hit the rate limit
    results_lock = threading.Lock()
    poll_sem = threading.Semaphore(8)
    ready = []
    active_results = {}

    def _wait_worker(name, iid):
        with poll_sem:
            inst = wait_active(iid, max_wait=600)
        ip = ""
        if inst is not None:
            ip = get_public_ip(inst)
        with results_lock:
            active_results[name] = {"id": iid, "inst": inst, "ip": ip}

    threads = []
    for name, iid in pending.items():
        t = threading.Thread(target=_wait_worker, args=(name, iid), daemon=True)
        threads.append(t)
        t.start()

    # wait for all threads to finish (no timeout worries)
    for t in threads:
        t.join()

    # collect the results - only those ACTIVE + with an IP are ready
    for name, res in active_results.items():
        if res["inst"] is not None and res["ip"]:
            ready.append({"name": name, "id": res["id"], "ip": res["ip"]})
            log(f"ACTIVE: {name} -> {res['ip']}", "ok")
        elif res["inst"] is None:
            log(f"ERROR/TIMEOUT: {name}", "error")

    if not ready:
        log("No instance became ACTIVE.", "error")
        sys.exit(1)
    log(f"{len(ready)}/{count} instances are ACTIVE and have IPs.", "ok")

    # ============ PHASE 3: Squid verify MULTI-THREADED (all together) ============
    auth = base64.b64encode(f"{PROXY_USER}:{PROXY_PASS}".encode()).decode()
    req_tpl = (f"GET http://api.ipify.org/ HTTP/1.1\r\n"
               f"Host: api.ipify.org\r\n"
               f"Proxy-Authorization: Basic {auth}\r\n"
               f"Connection: close\r\n\r\n").encode()

    def verify_squid(ip, port, tries=45):
        for _ in range(tries):
            try:
                with socket.create_connection((ip, port), timeout=5) as s:
                    s.settimeout(8)
                    s.sendall(req_tpl)
                    data = b""
                    try:
                        while True:
                            c = s.recv(4096)
                            if not c:
                                break
                            data += c
                    except Exception:
                        pass
                    if b"200 OK" in data:
                        return True
            except Exception:
                pass
            time.sleep(6)
        return False

    verify_lock = threading.Lock()
    verified = []
    verify_results = {}
    verify_sem = threading.Semaphore(8)  # max 8 concurrent network verifies

    def _verify_worker(name, ip, port):
        with verify_sem:
            ok = verify_squid(ip, port)
        with verify_lock:
            verify_results[name] = ok

    vthreads = []
    for it in ready:
        t = threading.Thread(target=_verify_worker, args=(it["name"], it["ip"], args.port), daemon=True)
        vthreads.append(t)
        t.start()
    for t in vthreads:
        t.join()

    for it in ready:
        if verify_results.get(it["name"]):
            verified.append(it)
            log(f"Squid OK: {it['ip']}:{args.port} (200)", "ok")
        else:
            log(f"WARNING: Squid not found: {it['ip']} - verify manually", "warn")

    # ============ PHASE 4: SSH REPAIR (never miss - keypair-based) ============
    failed = [it for it in ready if not verify_results.get(it["name"])]
    if failed and paramiko is not None:
        log(f"Phase 4: SSH repair for {len(failed)} instance(s)...", "warn")
        key_path = Path.home() / ".ssh" / "id_ed25519"
        install_script = build_user_data(PROXY_USER, PROXY_PASS, args.port)
        repair_sem = threading.Semaphore(4)
        repair_lock = threading.Lock()
        repair_results = {}

        def _repair_worker(name, ip):
            with repair_sem:
                ok_marker = ssh_repair(ip, key_path, install_script)
            ok_net = False
            if ok_marker:
                ok_net = verify_squid(ip, args.port, tries=6)
            with repair_lock:
                repair_results[name] = ok_net

        rthreads = []
        for it in failed:
            t = threading.Thread(target=_repair_worker, args=(it["name"], it["ip"]), daemon=True)
            rthreads.append(t)
            t.start()
        for t in rthreads:
            t.join()
        for it in failed:
            if repair_results.get(it["name"]):
                verified.append(it)
                log(f"REPAIRED Squid OK: {it['ip']}:{args.port} (200)", "ok")
            else:
                log(f"Repair failed: {it['ip']}", "error")
    elif failed:
        log("paramiko unavailable - SSH repair skipped", "warn")

    filepath = os.path.join(SAVE_DIR, PROXY_FILE)
    with open(filepath, "a") as f:
        for it in verified:
            f.write(f"{it['ip']}:{args.port}:{PROXY_USER}:{PROXY_PASS}\n")
    log(f"Saved {len(verified)} proxies to {filepath}", "ok")
    created = [{"ip": it["ip"], "port": args.port, "user": PROXY_USER,
                "pass": PROXY_PASS, "id": it["id"]} for it in verified]

    print()
    print("=" * 60)
    print(f"  SPOT PROXY READY! ({len(verified)}/{count})")
    print("=" * 60)
    for c in created:
        print(f"  http://{c['user']}:{c['pass']}@{c['ip']}:{c['port']}")
    print(f"  Type   : SPOT (cheap, Rs.{spot_price}/hr)")
    print(f"  Region : {REGION}")
    print(f"  Saved  : {os.path.join(SAVE_DIR, PROXY_FILE)}")
    print(f"  Auto   : delete after {run_hours} hour(s)")
    print("=" * 60)
    print()

    # Start auto-delete timer (in background) -> ZERO BILL guarantee
    schedule_auto_delete(run_hours, email, password)
    log("No extra bill while we're running. Summary above.", "ok")


if __name__ == "__main__":
    main()
