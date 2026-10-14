#!/usr/bin/env python3
"""
Gcore Web Core
==============
Web-friendly port of the gcore.com proxy-maker logic.

This is a NEW, standalone module. The original user scripts
(gcore_proxy.py / gcore_gui.py / gcore_find.py / check_instances.py)
are NOT modified and NOT imported - the logic here is a clean
re-implementation for browser use.

Auth: every call sends an `Authorization: APIKey <apikey>` header to
      https://api.gcore.com/cloud
"""
import base64
import time
from concurrent.futures import ThreadPoolExecutor

import requests

BASE = "https://api.gcore.com/cloud"

# EUR -> INR used for the desktop GUI's price chart.
EUR_INR = 110.0

# log() is replaced by the web app so progress shows up in the UI.
log = lambda msg, level="info": None  # noqa: E731

DEFAULT_REGION_ID = "108"   # Mumbai

SQUID_SETUP = r"""#!/bin/bash
LOG=/root/gc_setup.log
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
auth_param basic realm Gcore Proxy
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
def api(method, path, key, retries=4, **kwargs):
    headers = kwargs.pop("headers", {})
    headers["Authorization"] = "APIKey " + (key or "")
    headers["Content-Type"] = "application/json"
    url = BASE + path
    last = None
    for attempt in range(retries):
        try:
            r = requests.request(method, url, headers=headers, timeout=60, **kwargs)
            try:
                return r.status_code, r.json()
            except Exception:
                return r.status_code, r.text
        except Exception as e:
            last = e
            if attempt < retries - 1:
                time.sleep(2 + attempt * 2)
    return 0, {"error": str(last)}


def _results(code, data):
    if code == 200 and isinstance(data, dict):
        return [x for x in (data.get("results") or []) if isinstance(x, dict)]
    return []


# ============================== read APIs ==============================
def projects(key):
    code, data = api("GET", "/v1/projects", key)
    return _results(code, data)


def regions(key):
    code, data = api("GET", "/v1/regions", key)
    return [r for r in _results(code, data) if r.get("state") == "ACTIVE"]


def resolve_project(key, cfg=None):
    cfg = cfg or {}
    pid = cfg.get("project_id")
    if pid:
        return str(pid)
    ps = projects(key)
    if ps:
        return str(ps[0].get("id"))
    return None


def resolve_region(key, cfg=None):
    cfg = cfg or {}
    rid = cfg.get("region_id")
    if rid:
        return str(rid)
    rs = regions(key)
    for r in rs:
        if str(r.get("id")) == DEFAULT_REGION_ID:
            return DEFAULT_REGION_ID
    basic = [r for r in rs if r.get("has_basic_vm")]
    pick = (basic or rs)
    if pick:
        return str(pick[0].get("id"))
    return None


def images(key, pid, rid):
    code, data = api("GET", "/v1/images/{0}/{1}".format(pid, rid), key)
    return _results(code, data)


def flavors(key, pid, rid, include_prices=True):
    q = "?include_prices=true" if include_prices else ""
    code, data = api("GET", "/v1/flavors/{0}/{1}{2}".format(pid, rid, q), key)
    return _results(code, data)


def instances(key, pid, rid):
    code, data = api("GET", "/v1/instances/{0}/{1}".format(pid, rid), key)
    return _results(code, data)


def instance(key, pid, rid, iid):
    code, data = api("GET", "/v1/instances/{0}/{1}/{2}".format(pid, rid, iid), key)
    if code == 200 and isinstance(data, dict):
        return data
    return None


def task(key, tid):
    code, data = api("GET", "/v1/tasks/{0}".format(tid), key)
    if code == 200 and isinstance(data, dict):
        return data
    return None


def wait_task(key, tid, timeout=900):
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(5)
        t = task(key, tid)
        if not t:
            continue
        st = str(t.get("state", "")).upper()
        if st == "FINISHED":
            return t
        if st in ("FAILED", "ERROR"):
            return None
    return None


def public_ip(inst):
    if not isinstance(inst, dict):
        return None
    for _net, addrs in (inst.get("addresses") or {}).items():
        for addr in (addrs or []):
            if addr.get("type") in ("fixed", "floating"):
                return addr.get("addr")
    return None


def security_groups(key, pid, rid):
    code, data = api("GET", "/v1/securitygroups/{0}/{1}".format(pid, rid), key)
    return _results(code, data)


def external_subnets(key, pid, rid):
    """List of (subnet_id, name, cidr) for the region's external network(s)."""
    code, data = api("GET", "/v1/networks/{0}/{1}?owned_by=any".format(pid, rid), key)
    nets = _results(code, data)
    out = []
    for net in nets:
        if not net.get("external"):
            continue
        for sid in (net.get("subnets") or []):
            c2, d2 = api("GET", "/v1/subnets/{0}/{1}/{2}".format(pid, rid, sid), key)
            if c2 == 200 and isinstance(d2, dict) and d2.get("ip_version") == 4:
                out.append({"id": sid, "name": d2.get("name") or "",
                            "cidr": str(d2.get("cidr") or "")})
    return out


def ensure_sg_rule(key, pid, rid, port):
    """Open the proxy port ingress in every security group named 'default'."""
    port = int(port or 3128)
    sgs = security_groups(key, pid, rid)
    targets = [s for s in sgs if str(s.get("name")) == "default"] or sgs[:1]
    for sg in targets:
        sg_id = sg.get("id")
        rules = sg.get("security_group_rules") or []
        exists = any(
            r.get("direction") == "ingress"
            and r.get("protocol") == "tcp"
            and r.get("port_range_min") == port
            and (r.get("port_range_max") or r.get("port_range_min")) == port
            and r.get("remote_ip_prefix") == "0.0.0.0/0"
            for r in rules
        )
        if not exists:
            body = {"direction": "ingress", "ethertype": "IPv4", "protocol": "tcp",
                    "port_range_min": port, "port_range_max": port,
                    "remote_ip_prefix": "0.0.0.0/0", "description": "squid proxy"}
            rc, _ = api("POST", "/v1/securitygroups/{0}/{1}/{2}/rules".format(pid, rid, sg_id),
                        key, json=body)
            log("SG {0}: open tcp/{1} -> {2}".format(str(sg_id)[:8], port, rc), "info")
    return True


# ============================== write APIs ==============================
def build_user_data(cfg):
    return (SQUID_SETUP.replace("__ROOT_PASSWORD__", str(cfg.get("root_password") or ""))
            .replace("__PROXY_USER__", str(cfg.get("proxy_user") or ""))
            .replace("__PROXY_PASS__", str(cfg.get("proxy_pass") or "")))


def create_instance(key, pid, rid, cfg, name, image_id, flavor_id, subnet_id=None):
    interface = {"type": "external"}
    if subnet_id:
        interface["subnet_id"] = subnet_id
    body = {
        "name": name,
        "flavor": flavor_id,
        "image_id": image_id,
        "interfaces": [interface],
        "volumes": [{
            "source": "image",
            "image_id": image_id,
            "size": int(cfg.get("volume_size") or 10),
            "boot_index": 0,
        }],
    }
    if not cfg.get("windows"):
        body["user_data"] = base64.b64encode(build_user_data(cfg).encode()).decode()
    return api("POST", "/v2/instances/{0}/{1}".format(pid, rid), key, json=body)


def delete_instance(key, pid, rid, iid):
    return api("DELETE", "/v1/instances/{0}/{1}/{2}".format(pid, rid, iid), key)


def test_proxy(line):
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
            results.append({"name": name, "ok": False,
                            "msg": "{0} {1}".format(type(e).__name__, str(e)[:80])})
    return {"ok": True, "results": results}


# ============================== aggregated helpers ==============================
def _pick_image(key, pid, rid, cfg):
    imgs = images(key, pid, rid)
    want = str(cfg.get("os_image") or "").strip().lower()
    ubu = [i for i in imgs if "ubuntu" in str(i.get("name", "")).lower()
           and "arm" not in str(i.get("name", "")).lower()]
    ubu.sort(key=lambda i: str(i.get("os_version") or ""))
    if want:
        for i in ubu:
            if str(i.get("name", "")).lower() == want:
                return i
    for i in ubu:
        if "22.04" in str(i.get("name", "")):
            return i
    return ubu[0] if ubu else (imgs[0] if imgs else None)


def verify(key):
    key = (key or "").strip()
    if not key:
        return {"ok": False, "msg": "API key required"}
    code, data = api("GET", "/v1/projects", key)
    if code != 200 or not isinstance(data, dict):
        return {"ok": False, "msg": "Invalid API key (HTTP {0})".format(code)}
    ps = data.get("results") or []
    name = ps[0].get("name") if ps else ""
    return {"ok": True, "user": name, "project_id": str(ps[0].get("id")) if ps else ""}


def meta(key, cfg=None, region_id=None):
    """Regions + (for one region) images & flavors. Never raises."""
    cfg = cfg or {}
    key = (key or "").strip()
    if not key:
        return {"ok": False, "msg": "No gcore account", "regions": [], "images": [],
                "sizes": [], "project_id": "", "region_id": ""}
    info = verify(key)
    if not info.get("ok"):
        return {"ok": False, "msg": info.get("msg"), "regions": [], "images": [],
                "sizes": [], "project_id": "", "region_id": ""}
    pid = resolve_project(key, cfg)
    rid = str(region_id or cfg.get("region_id") or "") or resolve_region(key, cfg)
    regs = [{"id": str(r.get("id")), "name": r.get("display_name") or r.get("slug") or "?",
             "zone": r.get("zone") or "", "country": r.get("country") or "",
             "basic": bool(r.get("has_basic_vm"))} for r in regions(key)]

    imgs = []
    sizes = []
    if pid and rid:
        for im in images(key, pid, rid):
            name = str(im.get("name") or "")
            ost = str(im.get("os_type") or "").lower()
            imgs.append({"id": im.get("id"), "name": name,
                         "windows": ost.startswith("win") or "win" in name.lower().split("-")[0],
                         "arch": "arm64" if "arm" in name.lower() else "x64"})
        fl = [f for f in flavors(key, pid, rid, True)
              if "standard" in str(f.get("flavor_id", "")).lower()
              and "w-" not in str(f.get("flavor_id", "")).lower()
              and "shared" not in str(f.get("flavor_id", "")).lower()
              and not f.get("disabled")]
        fl.sort(key=lambda f: ((f.get("price_per_hour") or 0), f.get("vcpus", 0), f.get("ram", 0)))
        for f in fl:
            price = f.get("price_per_hour")
            rate = round(price * EUR_INR, 4) if price else None
            sizes.append({"name": f.get("flavor_id"), "cpu": f.get("vcpus", 0),
                          "ram": (f.get("ram") or 0) // 1024, "rate": rate})
    return {"ok": True, "user": info.get("user", ""), "project_id": pid or "",
            "region_id": rid or "", "regions": regs, "images": imgs, "sizes": sizes}


def state(key, cfg=None):
    cfg = cfg or {}
    key = (key or "").strip()
    if not key:
        return {"ok": False, "msg": "no key", "rows": []}
    pid = resolve_project(key, cfg)
    rid = resolve_region(key, cfg)
    if not pid or not rid:
        return {"ok": False, "msg": "project/region not resolved", "rows": []}
    rows = []
    for it in instances(key, pid, rid):
        iid = it.get("id") or ""
        fl = it.get("flavor") or {}
        vcpu = fl.get("vcpus") or it.get("vcpus") or 0
        ram = (fl.get("ram") or it.get("ram") or 0) // 1024
        img = it.get("image") or {}
        on = str(it.get("image_name") or img.get("name") or "?")
        rows.append({
            "name": str(it.get("name") or iid[-6:] or "?"),
            "ip": public_ip(it) or "",
            "status": str(it.get("status") or "?").title(),
            "os": on, "size": "{0}C/{1}G".format(vcpu, ram),
            "vm_id": iid, "uuid": iid, "region_id": rid,
            "created": str(it.get("created_at") or "")[:16],
            "windows": "win" in on.lower(),
        })
    return {"ok": True, "rows": rows}


def create_proxies(key, cfg, count):
    """Create N VMs -> wait -> collect public IPs. Returns {'lines','errors'}."""
    key = (key or "").strip()
    if not key:
        return {"lines": [], "errors": ["API key required"]}
    count = max(1, int(count or 1))
    cfg = dict(cfg)
    cfg.setdefault("proxy_port", 3128)
    cfg.setdefault("proxy_user", "ajay")
    cfg.setdefault("proxy_pass", "ajay")
    cfg.setdefault("root_password", "")
    cfg.setdefault("instance_name", "gcoreproxy")
    cfg.setdefault("volume_size", 10)

    pid = resolve_project(key, cfg)
    rid = str(cfg.get("region_id") or "") or resolve_region(key, cfg)
    if not pid or not rid:
        return {"lines": [], "errors": ["project/region not resolved"]}

    info = verify(key)
    log("Gcore project: {0} ({1})".format(info.get("user", "?"), pid), "sys")

    ensure_sg_rule(key, pid, rid, cfg.get("proxy_port"))

    image_id = cfg.get("image_id") or ""
    if not image_id:
        img = _pick_image(key, pid, rid, cfg)
        if not img:
            return {"lines": [], "errors": ["No image found"]}
        image_id = img.get("id")
        log("Image: {0}".format(img.get("name")), "info")
    flavor_id = cfg.get("flavor") or "g2-standard-1-2"
    subnet_id = cfg.get("subnet_id") or None

    created = []
    with ThreadPoolExecutor(max_workers=min(count, 10)) as pool:
        futs = {}
        for i in range(count):
            name = cfg["instance_name"] if count == 1 else "{0}-{1}".format(cfg["instance_name"], i + 1)
            futs[pool.submit(create_instance, key, pid, rid, cfg, name, image_id,
                             flavor_id, subnet_id)] = name
        for fut, name in futs.items():
            try:
                code, resp = fut.result()
            except Exception as e:
                code, resp = 0, {"error": str(e)}
            if code == 200 and isinstance(resp, dict) and resp.get("tasks"):
                tid = resp["tasks"][0]
                t = wait_task(key, tid, timeout=900)
                iid = None
                if t:
                    iid = (t.get("created_resources", {}).get("instances") or [None])[0]
                if iid:
                    created.append((name, iid))
                    log("Submitted: {0}".format(name), "ok")
                else:
                    log("Task failed for {0}".format(name), "error")
            else:
                log("Create failed: {0} (HTTP {1}) {2}".format(name, code, str(resp)[:160]), "error")

    if not created:
        return {"lines": [], "errors": ["No VM created"]}

    log("Waiting for {0} VM(s) to become ACTIVE ...".format(len(created)), "sys")
    errors = []
    for name, iid in created:
        deadline = time.time() + 600
        while time.time() < deadline:
            time.sleep(8)
            inst = instance(key, pid, rid, iid)
            if not inst:
                break
            st = str(inst.get("status", "")).upper()
            if st == "ACTIVE":
                log("Running: {0}".format(name), "ok")
                break
            if st in ("FAILED", "ERROR"):
                errors.append("{0}: {1}".format(name, st))
                log("Failed: {0} {1}".format(name, st), "error")
                break

    win = bool(cfg.get("windows"))
    lines = []
    for name, iid in created:
        inst = instance(key, pid, rid, iid)
        ip = public_ip(inst) if inst else None
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


def _scan_all_instances(key, pid):
    regs = regions(key)
    found = []

    def scan(r):
        rid = r.get("id")
        try:
            out = []
            for it in instances(key, pid, rid):
                out.append((rid, r.get("display_name") or rid, it))
            return out
        except Exception:
            return []

    with ThreadPoolExecutor(max_workers=6) as pool:
        for res in pool.map(scan, regs):
            found.extend(res or [])
    return found


def delete_vms(key, cfg, ids):
    key = (key or "").strip()
    cfg = cfg or {}
    pid = resolve_project(key, cfg)
    ids = set(str(x) for x in (ids or []))
    if not pid or not ids:
        return {"ok": True, "deleted": 0}
    done = 0
    for rid, rname, it in _scan_all_instances(key, pid):
        if str(it.get("id")) in ids:
            code, resp = delete_instance(key, pid, rid, it.get("id"))
            if code == 200:
                done += 1
                log("Deleted {0} ({1})".format(it.get("name"), rname), "ok")
            else:
                log("Delete failed {0} HTTP {1}".format(it.get("name"), code), "error")
    return {"ok": True, "deleted": done}


def delete_all(key, cfg=None):
    key = (key or "").strip()
    cfg = cfg or {}
    pid = resolve_project(key, cfg)
    if not pid:
        return {"ok": False}
    found = _scan_all_instances(key, pid)
    log("Found {0} instance(s) to delete".format(len(found)), "sys")
    for rid, rname, it in found:
        delete_instance(key, pid, rid, it.get("id"))
    deadline = time.time() + 600
    while time.time() < deadline:
        left = _scan_all_instances(key, pid)
        if not left:
            break
        log("Waiting for removal: {0}".format(len(left)), "info")
        time.sleep(12)
    log("Cleanup done", "ok")
    return {"ok": True, "deleted": len(found)}
