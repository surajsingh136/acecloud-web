#!/usr/bin/env python3
"""
ExCloud Web Core
================
Web-friendly port of the excloud.com proxy-maker logic.

This is a NEW, standalone module. The original user scripts
(excloud_lib.py / excloud_gui.py / excloud_proxy.py / excloud_find.py /
excloud_snap.py) are NOT modified and NOT imported - the logic here is a
clean re-implementation for browser use.

Auth: every call sends an `Authorization: Bearer <apikey>` header.
  compute: https://compute.excloud.in
  iam:     https://iam.excloud.in
"""
import base64
import json
import time
from concurrent.futures import ThreadPoolExecutor

import requests

BASE_COMPUTE = "https://compute.excloud.in"
BASE_IAM = "https://iam.excloud.in"

# log() is replaced by the web app so progress shows up in the UI.
log = lambda msg, level="info": None  # noqa: E731

TERMINAL_RUNNING = ("running", "active", "ready", "available", "started")
TERMINAL_FAILED = ("failed", "error", "terminated", "deleted")

SQUID_SETUP = r"""#!/bin/bash
LOG=/root/ex_setup.log
exec >>$LOG 2>&1
echo "=== start $(date) ==="
export DEBIAN_FRONTEND=noninteractive
echo 'root:__ROOT_PASSWORD__' | chpasswd
sed -i 's/#PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config
sed -i 's/#PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config
echo 'PermitRootLogin yes' >> /etc/ssh/sshd_config
echo 'PasswordAuthentication yes' >> /etc/ssh/sshd_config
systemctl restart ssh 2>/dev/null || systemctl restart sshd 2>/dev/null || true
iptables -I INPUT -p tcp --dport 3128 -j ACCEPT 2>/dev/null || true
iptables -I INPUT -p tcp --dport 22 -j ACCEPT 2>/dev/null || true
iptables -A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT 2>/dev/null || true
apt-get update -y >/tmp/apt-update.log 2>&1
apt-get install -y squid apache2-utils >/tmp/apt-squid.log 2>&1
AUTH_HELPER=$(ls /usr/lib/squid/basic_ncsa_auth /usr/lib/squid3/basic_ncsa_auth /usr/lib/x86_64-linux-gnu/squid/basic_ncsa_auth 2>/dev/null | head -n1)
if [ -z "$AUTH_HELPER" ]; then AUTH_HELPER=$(find /usr/lib -name basic_ncsa_auth 2>/dev/null | head -n1); fi
htpasswd -bc /etc/squid/htpasswd __PROXY_USER__ __PROXY_PASS__
cat > /etc/squid/squid.conf <<'SQUIDEOF'
auth_param basic program __AUTH_HELPER__ /etc/squid/htpasswd
auth_param basic realm Excloud Proxy
auth_param basic credentialsttl 2 hours
acl proxy_users proxy_auth REQUIRED
acl SSL_ports port 443
acl Safe_ports port 80
acl Safe_ports port 443
acl Safe_ports port 21
acl Safe_ports port 70
acl Safe_ports port 210
acl Safe_ports port 1025-65535
acl Safe_ports port 280
acl Safe_ports port 488
acl Safe_ports port 591
acl Safe_ports port 777
acl CONNECT method CONNECT
http_access deny !Safe_ports
http_access deny CONNECT !SSL_ports
http_access allow proxy_users
http_access deny all
http_port 3128
coredump_dir /var/spool/squid
refresh_pattern ^ftp:           1440    20%     10080
refresh_pattern ^gopher:        1440    0%      1440
refresh_pattern -i (/cgi-bin/|\?) 0     0%      0
refresh_pattern .               0       20%     4320
SQUIDEOF
sed -i "s|__AUTH_HELPER__|$AUTH_HELPER|g" /etc/squid/squid.conf
systemctl enable squid
systemctl restart squid || true
echo "=== done $(date) ==="
""".strip()


# ============================== low level ==============================
def api(method, path, key, base=BASE_COMPUTE, **kwargs):
    headers = kwargs.pop("headers", {})
    headers["Authorization"] = "Bearer " + key
    try:
        r = requests.request(method, base + path, headers=headers, timeout=90, **kwargs)
    except Exception as e:
        return 0, {"error": str(e)}
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, r.text


def _list(method, path, key, base=BASE_COMPUTE, **kwargs):
    code, data = api(method, path, key, base, **kwargs)
    if code == 200 and isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    return []


def _items(method, path, key, base=BASE_COMPUTE, **kwargs):
    code, data = api(method, path, key, base, **kwargs)
    if code == 200 and isinstance(data, dict):
        return [x for x in (data.get("items") or []) if isinstance(x, dict)]
    return []


# ============================== read APIs ==============================
def user_info(key):
    code, data = api("GET", "/me", key, BASE_IAM)
    if code == 200 and isinstance(data, dict):
        return data
    return {}


def quotas(key):
    code, data = api("GET", "/quotas", key, BASE_IAM)
    if code == 200 and isinstance(data, dict):
        return data.get("limits") or {}
    return {}


def images(key):
    return _list("GET", "/image/list", key)


def instance_types(key, provisionable_only=True):
    out = _list("GET", "/instancetype/list", key)
    if provisionable_only:
        out = [t for t in out if t.get("provisionable")]
    return out


def subnets(key):
    return _list("GET", "/subnet/list", key)


def security_groups(key):
    return _list("GET", "/securitygroup/list", key)


def pick_security_group(key):
    sgs = security_groups(key)
    if not sgs:
        return []
    for s in sgs:
        name = str(s.get("name") or "").lower()
        if "allow-all" in name or "allow all" in name or name == "allowall":
            return [_num(s.get("id"))]
        if "proxy" in name or "squid" in name:
            return [_num(s.get("id"))]
    # prefer id 3776 if present (common in this account)
    for s in sgs:
        if s.get("id") == 3776:
            return [3776]
    # fallback first
    return [_num(sgs[0].get("id"))] if sgs else []


def ssh_keys(key):
    return _list("GET", "/sshpubkey/list", key)


def list_vms(key):
    return _list("GET", "/compute/list", key)


def list_instances(key):
    return _items("GET", "/compute/instances", key)


def get_vm(key, vm_id):
    code, data = api("GET", "/compute/{0}".format(vm_id), key)
    if code == 200 and isinstance(data, dict):
        return data
    return None


def list_ips(key):
    return _items("GET", "/compute/publicipv4/list", key)


def list_volumes(key):
    return _list("GET", "/volume/list", key)


def list_snapshots(key):
    return _list("GET", "/snapshot/list", key)


def find_default_subnet(key):
    subs = subnets(key)
    for s in subs:
        name = str(s.get("name", "")).lower()
        if name in ("default", "default subnet") or name.startswith("default"):
            return s
    return subs[0] if subs else None


def is_windows_name(name):
    n = str(name or "").lower()
    return n.startswith("windows") or n.startswith("win-") or n.startswith("win_") or " windows" in n


def is_windows_image(img):
    name = str((img or {}).get("name") or "").lower()
    user = str((img or {}).get("default_user") or "").lower()
    return "win" in name.split("-")[0] or user == "administrator"


# ============================== write APIs ==============================
def build_user_data(cfg):
    return (SQUID_SETUP.replace("__ROOT_PASSWORD__", str(cfg.get("root_password") or ""))
            .replace("__PROXY_USER__", str(cfg.get("proxy_user") or ""))
            .replace("__PROXY_PASS__", str(cfg.get("proxy_pass") or "")))


def _num(v):
    """Best-effort cast to int (the API rejects numeric fields sent as strings)."""
    if v is None or str(v).strip() == "":
        return None
    try:
        return int(str(v).strip())
    except Exception:
        return v


def create_vm(key, cfg, name, image_id, instance_type, disk_gib, subnet_id, zone_id,
              template=None, windows=False):
    data = {
        "name": name,
        "zone_id": _num(zone_id),
        "allocate_public_ipv4": True,
        "image_id": _num(image_id),
        "instance_type": instance_type,
        "root_volume": {"name": name + "-root", "size_gib": _num(disk_gib), "zone_id": _num(zone_id)},
        "root_password": cfg.get("root_password", ""),
    }
    sub = _num(subnet_id)
    if sub is not None:
        data["subnet_id"] = sub
    if template and template.get("snapshot_id"):
        data["root_volume"]["source_snapshot_id"] = _num(template["snapshot_id"])
    if not windows:
        data["user_data"] = build_user_data(cfg)
    if cfg.get("ssh_pubkey"):
        data["ssh_pubkey"] = cfg["ssh_pubkey"]
    if cfg.get("security_group_ids"):
        data["security_group_ids"] = [_num(x) for x in cfg["security_group_ids"]]
    return api("POST", "/compute/create", key, json=data)


def terminate_vm(key, vm_id, delete_root=True):
    return api("POST", "/compute/terminate", key,
               json={"vm_id": int(vm_id), "delete_root_volume": bool(delete_root)})


def wait_vm_running(key, vm_id, timeout_sec=1800):
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        time.sleep(12)
        v = get_vm(key, vm_id)
        if not v:
            return None
        state = str(v.get("state", "")).lower()
        if state in TERMINAL_RUNNING:
            return v
        if state in TERMINAL_FAILED:
            return None
    return None


def wait_vm_gone(key, vm_id, timeout_sec=600):
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        time.sleep(10)
        v = get_vm(key, vm_id)
        if not v:
            return True
        state = str(v.get("state", "")).lower()
        if state in ("terminated", "deleted", "error"):
            return True
    return False


def test_proxy(line):
    """Try the proxy against Google + IRCTC (server side, like the desktop)."""
    line = (line or "").strip()
    try:
        host, port, user, pw = line.split(":", 3)
        port = int(port)
    except Exception:
        return {"ok": False, "msg": "Format: ip:port:user:pass", "results": []}
    px = "http://{0}:{1}@{2}:{3}".format(user, pw, host, port)
    proxies = {"http": px, "https": px}
    results = []
    for name, url in [("IRCTC", "https://www.irctc.co.in"), ("Google", "https://www.google.com")]:
        try:
            r = requests.get(url, proxies=proxies, timeout=25, allow_redirects=True)
            results.append({"name": name, "ok": True, "code": r.status_code,
                            "ms": int(r.elapsed.total_seconds() * 1000)})
        except Exception as e:
            results.append({"name": name, "ok": False, "msg": "{0} {1}".format(type(e).__name__, str(e)[:80])})
    return {"ok": True, "results": results}


# ============================== aggregated helpers ==============================
def verify(key):
    """Validate the apikey and gather what the UI needs. Never raises."""
    key = (key or "").strip()
    if not key:
        return {"ok": False, "msg": "API key required"}
    code, data = api("GET", "/me", key, BASE_IAM)
    if code != 200 or not isinstance(data, dict):
        msg = data.get("detail") if isinstance(data, dict) else None
        return {"ok": False, "msg": msg or "Invalid API key (HTTP {0})".format(code)}
    return {"ok": True, "user": data.get("org_name") or data.get("email") or "",
            "email": data.get("email") or "", "org": data.get("org_name") or ""}


def meta(key):
    key = (key or "").strip()
    if not key:
        return {"ok": False, "msg": "No excloud account", "images": [], "types": [],
                "subnets": [], "default_subnet_id": ""}
    imgs = []
    for im in images(key):
        imgs.append({"id": im.get("id"), "name": im.get("name"),
                     "windows": is_windows_image(im),
                     "default_user": im.get("default_user") or ""})
    types = []
    for t in instance_types(key):
        cpu = (t.get("cpu") or 0) / 1000.0
        cpu = int(cpu) if cpu == int(cpu) else cpu
        types.append({"name": t.get("name"), "cpu": cpu,
                      "ram": (t.get("memory") or 0) // 1024,
                      "disk": (t.get("disk") or 0) // 1024,
                      "rate": t.get("on_demand_price_inr_per_hour")})
    subs = [{"id": s.get("id"), "name": s.get("name"),
             "cidr": s.get("ipv4_cidr") or ""} for s in subnets(key)]
    dsub = find_default_subnet(key) or {}
    info = verify(key)
    return {"ok": bool(info.get("ok")), "user": info.get("user", ""),
            "email": info.get("email", ""), "images": imgs, "types": types,
            "subnets": subs, "default_subnet_id": dsub.get("id") or "",
            "security_groups": [{"id": s.get("id"), "name": s.get("name"),
                                 "description": s.get("description") or ""}
                                for s in security_groups(key)]}


def state(key):
    """Rows for the servers table."""
    key = (key or "").strip()
    if not key:
        return {"ok": False, "msg": "no key", "rows": []}
    items = list_instances(key)
    ips = {i.get("id"): i for i in list_ips(key) if isinstance(i, dict)}
    rows = []
    for it in items:
        st = str(it.get("state") or "").lower()
        if st in ("terminated", "deleted"):
            continue
        vm_id = it.get("vm_id") or ""
        ip = it.get("public_ipv4") or ""
        on = str(it.get("image_name") or "?")
        mem = (it.get("memory_mib") or 0) // 1024
        cpu = it.get("vcpu") or 0
        if cpu and cpu >= 1000 and cpu % 1000 == 0:
            cpu = cpu // 1000
        size = "{0}C/{1}G".format(cpu, mem)
        rows.append({
            "name": str(it.get("name") or vm_id or "?"), "ip": ip,
            "status": str(it.get("state") or "?").title(), "os": on, "size": size,
            "vm_id": vm_id, "uuid": vm_id,
            "created": str(it.get("created_at") or "")[:16],
            "windows": is_windows_name(on),
        })
    return {"ok": True, "rows": rows}


def create_proxies(key, cfg, count):
    """Full flow: create N VMs -> wait running -> collect public IPs.
    Returns {'lines': [...], 'errors': [...]}. Progress is emitted via log()."""
    key = (key or "").strip()
    if not key:
        return {"lines": [], "errors": ["API key required"]}
    count = max(1, int(count or 1))
    cfg = dict(cfg)
    cfg.setdefault("proxy_port", 3128)
    cfg.setdefault("proxy_user", "ajay")
    cfg.setdefault("proxy_pass", "ajay")
    cfg.setdefault("root_password", "")
    cfg.setdefault("instance_name", "exproxy")
    cfg.setdefault("root_disk_gib", 25)
    # auto-pick SG if not provided
    if not cfg.get("security_group_ids"):
        try:
            sg = pick_security_group(key)
            if sg:
                cfg["security_group_ids"] = sg
        except Exception:
            pass

    me = user_info(key)
    log("Excloud user: {0} ({1})".format(me.get("org_name", "?"), me.get("email", "?")), "sys")

    img = cfg.get("image") or {}
    image_id = img.get("id") or cfg.get("image_id")
    instype = cfg.get("instance_type") or ""
    subnet_id = cfg.get("subnet_id")
    zone_id = cfg.get("zone_id", 1)
    disk = int(cfg.get("root_disk_gib") or 25)
    win = bool(img.get("windows"))

    template = None
    tpl = cfg.get("template")
    if tpl and tpl.get("snapshot_id"):
        template = {"snapshot_id": tpl.get("snapshot_id")}

    created = []
    with ThreadPoolExecutor(max_workers=min(count, 10)) as pool:
        futs = {}
        for i in range(count):
            name = cfg["instance_name"] if count == 1 else "{0}-{1}".format(cfg["instance_name"], i + 1)
            c2 = dict(cfg)
            futs[pool.submit(create_vm, key, c2, name, image_id, instype, disk,
                             subnet_id, zone_id, template, win)] = name
        for fut, name in futs.items():
            try:
                code, resp = fut.result()
            except Exception as e:
                code, resp = 0, {"error": str(e)}
            if code == 200 and isinstance(resp, dict) and resp.get("vm_id"):
                created.append((name, resp["vm_id"]))
                log("Submitted: {0}".format(name), "ok")
            else:
                log("Create failed: {0} (HTTP {1}) {2}".format(name, code, str(resp)[:160]), "error")

    if not created:
        return {"lines": [], "errors": ["No VM created"]}

    log("Waiting for {0} VM(s) to start ...".format(len(created)), "sys")
    pending = dict(created)
    deadline = time.time() + 1800
    while pending and time.time() < deadline:
        time.sleep(10)
        for name, vm_id in list(pending.items()):
            v = get_vm(key, vm_id)
            if not v:
                del pending[name]
                continue
            s = str(v.get("state", "")).lower()
            if s in TERMINAL_RUNNING:
                del pending[name]
                log("Running: {0}".format(name), "ok")
            elif s in TERMINAL_FAILED:
                del pending[name]
                log("Failed: {0} {1}".format(name, s), "error")

    errors = []
    for name in pending:
        errors.append("{0}: timeout waiting for running".format(name))
        log("Timeout: {0}".format(name), "warn")

    lines = []
    for name, vm_id in created:
        v = get_vm(key, vm_id)
        ip = v.get("public_ipv4") if isinstance(v, dict) else None
        if not ip:
            errors.append("{0}: no public IP".format(name))
            log("No IP: {0}".format(name), "warn")
            continue
        if win:
            lines.append("{0}:3389:Administrator:{1}".format(ip, cfg.get("root_password", "")))
        else:
            lines.append("{0}:{1}:{2}:{3}".format(ip, cfg.get("proxy_port"),
                                                  cfg.get("proxy_user"), cfg.get("proxy_pass")))
    return {"lines": lines, "errors": errors}


def delete_vms(key, vm_ids):
    key = (key or "").strip()
    done, errors = 0, []
    for vm_id in (vm_ids or []):
        code, resp = terminate_vm(key, vm_id, delete_root=True)
        if code == 200:
            done += 1
            log("Deleted VM {0}".format(vm_id), "ok")
        else:
            errors.append("{0}: HTTP {1} {2}".format(vm_id, code, str(resp)[:120]))
            log("Delete failed {0} ({1})".format(vm_id, code), "error")
    return {"ok": True, "deleted": done, "errors": errors}


def delete_all(key):
    """Delete every VM + root volumes + reserved IPs. Best-effort."""
    key = (key or "").strip()
    for vm in list_vms(key):
        terminate_vm(key, vm.get("vm_id"), delete_root=True)
    for it in list_instances(key):
        if str(it.get("state", "")).lower() not in ("terminated", "deleted"):
            terminate_vm(key, it.get("vm_id"), delete_root=True)

    deadline = time.time() + 900
    while time.time() < deadline:
        left = [v for v in list_instances(key)
                if str(v.get("state", "")).lower() not in ("terminated", "deleted")]
        if not left:
            break
        log("Waiting for VM removal: {0}".format(len(left)), "info")
        time.sleep(15)

    for v in list_volumes(key):
        if isinstance(v, dict) and v.get("id"):
            api("POST", "/volume/delete", key, json={"volume_id": int(v["id"])})
            log("Deleted volume {0}".format(v["id"]), "ok")

    freed = 0
    for i in list_ips(key):
        if not isinstance(i, dict):
            continue
        if str(i.get("allocation_type", "")).upper() == "RESERVED" and i.get("id"):
            api("POST", "/compute/publicipv4/release", key,
                json={"reservation_id": int(i["id"])})
            freed += 1
    log("Cleanup done: {0} IP(s) released".format(freed), "ok")
    return {"ok": True, "ips": freed}


def make_snapshot(key, vm_id):
    vol_id = None
    v = get_vm(key, vm_id)
    if isinstance(v, dict):
        vol_id = v.get("root_volume_id")
    if not vol_id:
        return {"code": 0, "resp": "no root volume", "snapshot_id": None}
    code, resp = api("POST", "/snapshot/create", key, json={"volume_id": int(vol_id)})
    snap_id = resp.get("snapshot_id") if isinstance(resp, dict) else None
    return {"code": code, "resp": resp, "snapshot_id": snap_id}
