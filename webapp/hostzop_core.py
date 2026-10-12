#!/usr/bin/env python3
"""
Hostzop Web Core
================
Web-friendly port of the hostzop.com proxy-maker logic.

This is a NEW, standalone module. The original user scripts
(hostzop_proxy.py / hostzop_gui.py / hostzop_find.py / hostzop_snap.py)
are NOT modified and NOT imported - the logic here is a clean re-implementation
for browser use.

Auth: every call sends an `apikey` header to https://api.hostzop.com
"""
import base64
import json
import time
from concurrent.futures import ThreadPoolExecutor

import requests

BASE = "https://api.hostzop.com"

# log() is replaced by the web app so progress shows up in the UI.
log = lambda msg, level="info": None  # noqa: E731

SIZE_CHART = [
    ("CPU-1x-4GB", 1, 4096, 30),
    ("CPU-2x-8GB", 2, 8192, 50),
    ("CPU-4x-16GB", 4, 16384, 100),
    ("CPU-8x-32GB", 8, 32768, 200),
    ("CPU-16x-64GB", 16, 65536, 400),
]

SQUID_SETUP = r"""#!/bin/bash
LOG=/root/proxy_setup.log
exec >>$LOG 2>&1
echo "=== start $(date) ==="
export DEBIAN_FRONTEND=noninteractive
echo 'root:__ROOT_PASSWORD__' | chpasswd
sed -i 's/#PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config
sed -i 's/#PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config
echo 'PermitRootLogin yes' >> /etc/ssh/sshd_config
echo 'PasswordAuthentication yes' >> /etc/ssh/sshd_config
systemctl restart ssh || systemctl restart sshd
apt-get update -y >/tmp/apt-update.log 2>&1
apt-get install -y squid apache2-utils >/tmp/apt-squid.log 2>&1
AUTH_HELPER=$(ls /usr/lib/squid/basic_ncsa_auth /usr/lib/squid3/basic_ncsa_auth /usr/lib/x86_64-linux-gnu/squid/basic_ncsa_auth 2>/dev/null | head -n1)
if [ -z "$AUTH_HELPER" ]; then AUTH_HELPER=$(find /usr/lib -name basic_ncsa_auth 2>/dev/null | head -n1); fi
htpasswd -bc /etc/squid/htpasswd __PROXY_USER__ __PROXY_PASS__
cat > /etc/squid/squid.conf <<'SQUIDEOF'
auth_param basic program __AUTH_HELPER__ /etc/squid/htpasswd
auth_param basic realm Hostzop Proxy
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
systemctl restart squid
""".strip()


# ============================== low level ==============================
def api(method, path, key, **kwargs):
    headers = kwargs.pop("headers", {})
    headers["apikey"] = key
    try:
        r = requests.request(method, BASE + path, headers=headers, timeout=90, **kwargs)
    except Exception as e:
        return 0, {"error": str(e)}
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, r.text


def _list(method, path, key, **kwargs):
    code, data = api(method, path, key, **kwargs)
    if code == 200 and isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    return []


# ============================== read APIs ==============================
def user_info(key):
    code, data = api("GET", "/v1/user-resource/user", key)
    if code == 200 and isinstance(data, dict):
        return data
    return {}


def locations(key):
    return _list("GET", "/v1/config/locations", key)


def pools(key):
    return _list("GET", "/v1/user-resource/host_pool/list", key)


def images(key):
    """Return sorted list of [os_name, [versions...]] from the plain_os catalog."""
    data = _list("GET", "/v1/config/vm_images/plain_os", key)
    seen = {}
    for img in data:
        name = img.get("os_name")
        for v in (img.get("versions") or []):
            if isinstance(v, dict) and v.get("published"):
                seen.setdefault(name, set()).add(v.get("os_version"))
    return sorted((n, sorted(vs)) for n, vs in seen.items())


def pricing(key):
    code, data = api("GET", "/v1/pricing/policy", key)
    policy = {}
    if code == 200 and isinstance(data, dict):
        for p in (data.get("policy") or []):
            if not isinstance(p, dict):
                continue
            rt = p.get("resourceType")
            if rt == "CPU":
                policy["cpu"] = p.get("pricePerUnit")
            elif rt == "RAM":
                policy["ram"] = p.get("pricePerUnit")
            elif rt == "STORAGE":
                policy["storage"] = p.get("pricePerUnit")
    return policy


def hourly_rate(policy, cpu, ram, disks):
    if not policy:
        return None
    return (cpu * policy.get("cpu", 0) + (ram / 1024.0) * policy.get("ram", 0)
            + disks * policy.get("storage", 0))


def _slugs(key):
    return [l.get("slug") for l in locations(key) if l.get("slug")]


def _pref(slug):
    return f"/v1/{slug}" if slug else "/v1"


def list_vms(key, slug=None):
    """VMs across the default location + every location slug (deduped)."""
    seen = {}
    prefs = [slug] if slug else [None] + _slugs(key)
    for s in prefs:
        for v in _list("GET", f"{_pref(s)}/user-resource/vm/list", key):
            seen.setdefault(v.get("uuid"), v)
    return list(seen.values())


def list_ips(key, slug=None):
    seen = {}
    prefs = [slug] if slug else [None] + _slugs(key)
    for s in prefs:
        for i in _list("GET", f"{_pref(s)}/network/ip_addresses", key):
            seen.setdefault(i.get("address"), i)
    return list(seen.values())


def get_vm(key, uuid, slug=None):
    prefs = [slug] if slug else [None] + _slugs(key)
    for s in prefs:
        code, data = api("GET", f"{_pref(s)}/user-resource/vm", key, params={"uuid": uuid})
        if code == 200 and isinstance(data, dict) and data:
            return data
    return {}


def networks(key, slug=None):
    return _list("GET", f"{_pref(slug)}/network/networks", key)


def verify(key):
    """Validate the apikey and gather what the UI needs. Never raises."""
    key = (key or "").strip()
    if not key:
        return {"ok": False, "msg": "API key required"}
    code, data = api("GET", "/v1/user-resource/user", key)
    if code != 200 or not isinstance(data, dict):
        msg = data.get("detail") if isinstance(data, dict) else None
        return {"ok": False, "msg": msg or f"Invalid API key (HTTP {code})"}
    profile = data.get("profile_data") or {}
    email = profile.get("email") or data.get("name") or ""
    locs = [{"slug": l.get("slug"), "name": l.get("display_name") or l.get("slug"),
             "is_default": bool(l.get("is_default"))}
            for l in locations(key) if l.get("slug")]
    return {"ok": True, "user": data.get("name") or email, "email": email,
            "locations": locs}


def state(key):
    """Rows for the servers table: name, ip, status, os, size, proxy, uuid."""
    key = (key or "").strip()
    if not key:
        return {"ok": False, "msg": "no key", "rows": []}
    vms = list_vms(key)
    ips = list_ips(key)
    rows = []
    for v in vms:
        uuid = v.get("uuid") or ""
        on = str(v.get("os_name") or "")
        ver = str(v.get("os_version") or "")
        ip = ""
        for i in ips:
            if i.get("assigned_to") == uuid and i.get("enabled"):
                ip = i.get("address") or ""
                break
        ram_gb = (v.get("ram") or 0) // 1024
        size = f"{v.get('vcpu') or '?'}C/{ram_gb}G"
        os_label = (on + (f" {ver}" if ver else "")) if on else "?"
        rows.append({
            "name": str(v.get("name") or uuid or "?"),
            "ip": ip, "status": str(v.get("status") or "?").title(),
            "os": os_label, "size": size, "uuid": uuid,
            "created": str(v.get("created_at") or "")[:16],
            "windows": on.lower().startswith("win"),
        })
    return {"ok": True, "rows": rows}


# ============================== write APIs ==============================
def _build_cloud_init(cfg):
    script = (SQUID_SETUP.replace("__ROOT_PASSWORD__", str(cfg.get("root_password") or ""))
              .replace("__PROXY_USER__", str(cfg.get("proxy_user") or ""))
              .replace("__PROXY_PASS__", str(cfg.get("proxy_pass") or "")))
    return {
        "write_files": [{
            "path": "/opt/hz_setup.sh",
            "content": base64.b64encode(script.encode()).decode(),
            "encoding": "b64",
            "permissions": "0755",
        }],
        "runcmd": ["/opt/hz_setup.sh"],
    }


def ensure_network(key, name, slug=None):
    name = (name or "My Network").strip() or "My Network"
    for n in networks(key, slug):
        if n.get("name") == name or n.get("is_default"):
            return n
    api("POST", f"{_pref(slug)}/network/network", key, params={"name": name})
    for n in networks(key, slug):
        if n.get("name") == name or n.get("is_default"):
            return n
    return None


def _create_one(key, cfg, name, pool_uuid, os_name, os_version, network_uuid, slug=None):
    is_win = os_name == "windows"
    data = {
        "name": name,
        "os_name": os_name,
        "os_version": os_version,
        "disks": cfg["disks"],
        "vcpu": cfg["vcpu"],
        "ram": cfg["ram"],
        "username": "Administrator" if is_win else "root",
        "password": cfg["root_password"],
        "network_uuid": network_uuid,
        "reserve_public_ip": "True" if cfg.get("reserve_public_ip", True) else "False",
    }
    if not is_win:
        data["cloud_init"] = json.dumps(_build_cloud_init(cfg))
    if pool_uuid:
        data["designated_pool_uuid"] = pool_uuid
    if cfg.get("billing_account_id"):
        data["billing_account_id"] = cfg["billing_account_id"]
    return api("POST", f"{_pref(slug)}/user-resource/vm", key, data=data)


def find_or_assign_ip(key, cfg, vm_uuid, slug=None):
    for ip in list_ips(key, slug):
        if ip.get("assigned_to") == vm_uuid and ip.get("enabled"):
            return ip.get("address")
    body = {"name": str(cfg.get("instance_name") or "hz") + "-ip"}
    if cfg.get("billing_account_id"):
        body["billing_account_id"] = cfg["billing_account_id"]
    c1, r1 = api("POST", f"{_pref(slug)}/network/ip_addresses", key, json=body)
    address = r1.get("address") if isinstance(r1, dict) else None
    if not address:
        for ip in list_ips(key, slug):
            address = ip.get("address")
            if address:
                break
    if not address:
        return None
    api("POST", f"{_pref(slug)}/network/ip_addresses/{address}/assign", key,
        json={"assigned_to": vm_uuid, "assigned_to_resource_type": "virtual_machine"})
    return address


def create_proxies(key, cfg, count):
    """Full flow: ensure network -> create N VMs -> wait running -> assign IPs.
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
    cfg.setdefault("instance_name", "hzproxy")
    slug = (cfg.get("location") or "").strip() or None

    me = user_info(key)
    log(f"Hostzop user: {me.get('name', '?')}", "sys")

    os_name = "windows" if str(cfg.get("os_name", "")).lower().startswith("win") else "ubuntu"
    os_version = str(cfg.get("os_version") or ("2022" if os_name == "windows" else "22.04"))
    if os_name == "ubuntu" and os_version and "." not in os_version:
        os_version = os_version + ".04"

    pool_uuid = cfg.get("designated_pool_uuid") or ""
    if not pool_uuid:
        for p in pools(key):
            if p.get("is_default_designated"):
                pool_uuid = p.get("uuid")
                break

    net = ensure_network(key, cfg.get("network_name"), slug)
    if not net:
        return {"lines": [], "errors": ["Network not found/created"]}
    net_uuid = net.get("uuid")
    log(f"Location: {slug or 'default'} | Network: {net.get('name')} ({str(net_uuid)[:12]})", "info")

    errors = []
    created = []
    with ThreadPoolExecutor(max_workers=min(count, 10)) as pool:
        futs = {}
        for i in range(count):
            name = cfg["instance_name"] if count == 1 else f"{cfg['instance_name']}-{i + 1}"
            futs[pool.submit(_create_one, key, cfg, name, pool_uuid, os_name, os_version,
                             net_uuid, slug)] = name
        for fut, name in futs.items():
            try:
                code, resp = fut.result()
            except Exception as e:
                code, resp = 0, {"error": str(e)}
            if code == 200 and isinstance(resp, dict) and resp.get("uuid"):
                created.append((name, resp["uuid"]))
                log(f"Submitted: {name}", "ok")
            else:
                errors.append(f"{name}: HTTP {code} {str(resp)[:160]}")
                log(f"Create failed: {name} ({code})", "error")

    if not created:
        return {"lines": [], "errors": errors or ["No VM created"]}

    log(f"Waiting for {len(created)} VM(s) to start ...", "sys")
    pending = dict(created)
    deadline = time.time() + 1800
    while pending and time.time() < deadline:
        time.sleep(10)
        for name, uuid in list(pending.items()):
            v = get_vm(key, uuid, slug)
            s = str(v.get("status", "")).lower()
            if s in ("running", "active", "ready", "available"):
                del pending[name]
                log(f"Running: {name}", "ok")
            elif s in ("failed", "error"):
                errors.append(f"{name}: {s}")
                del pending[name]
                log(f"Failed: {name}", "error")
    for name in pending:
        errors.append(f"{name}: timeout waiting for running")
        log(f"Timeout: {name}", "warn")

    win = os_name == "windows"
    lines = []
    with ThreadPoolExecutor(max_workers=min(len(created), 10)) as pool:
        futs = {pool.submit(find_or_assign_ip, key, cfg, uuid, slug): name for name, uuid in created}
        for fut, name in futs.items():
            try:
                ip = fut.result()
            except Exception as e:
                ip = None
                errors.append(f"{name}: ip error {e}")
            if not ip:
                errors.append(f"{name}: no public IP")
                log(f"No IP: {name}", "warn")
                continue
            if win:
                lines.append(f"{ip}:3389:Administrator:{cfg['root_password']}")
            else:
                lines.append(f"{ip}:{cfg['proxy_port']}:{cfg['proxy_user']}:{cfg['proxy_pass']}")
    return {"lines": lines, "errors": errors}


def delete_vms(key, uuids, slug=None):
    key = (key or "").strip()
    done, errors = 0, []
    slugs = [slug] if slug else [None] + _slugs(key)
    for uuid in (uuids or []):
        code, resp = 500, ""
        for s in slugs:
            code, resp = api("DELETE", f"{_pref(s)}/user-resource/vm", key, data={"uuid": uuid})
            if code == 200:
                break
        if code == 200:
            done += 1
            log(f"Deleted VM {str(uuid)[:12]}", "ok")
        else:
            errors.append(f"{str(uuid)[:12]}: HTTP {code} {str(resp)[:120]}")
            log(f"Delete failed {str(uuid)[:12]} ({code})", "error")
    return {"ok": True, "deleted": done, "errors": errors}


def delete_all(key):
    """Delete every VM + stale (unassigned) IPs. Best-effort, returns counts."""
    key = (key or "").strip()
    vms = list_vms(key)
    res = delete_vms(key, [v.get("uuid") for v in vms if v.get("uuid")])
    known = {v.get("uuid") for v in list_vms(key)}
    freed = 0
    for ip in list_ips(key):
        if ip.get("assigned_to") and ip["assigned_to"] not in known:
            addr = ip.get("address")
            if addr:
                for s in [None] + _slugs(key):
                    c, _ = api("DELETE", f"{_pref(s)}/network/ip_addresses/{addr}", key)
                    if c == 200:
                        break
                freed += 1
    log(f"Cleanup done: {res['deleted']} VM(s), {freed} stale IP(s)", "ok")
    return {"ok": True, "deleted": res["deleted"], "ips": freed}
