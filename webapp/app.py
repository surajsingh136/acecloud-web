#!/usr/bin/env python3
"""
AceCloud Web Manager v2
========================
Same features as acecloud_gui.py, but in the browser.
Original scripts (acecloud_spot.py / spot_server.py / dashboard.py) are only
IMPORTED - not a single line in them is changed.

Run:    python app.py           -> http://127.0.0.1:8787
PIN:    printed in the terminal (or set the ACEWEB_PIN env var)
"""
import base64
import json
import os
import random
import re
import requests
import socket
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse, parse_qs

WEB_DIR = Path(__file__).resolve().parent
SPOT_DIR = WEB_DIR.parent / "spot"
sys.path.insert(0, str(SPOT_DIR))
sys.path.insert(0, str(SPOT_DIR / "server"))

import acecloud_spot as ac          # original script - untouched
import spot_server as sw            # original script - untouched
import hostzop_core as hz           # new module - hostzop.com web port (own code)
import excloud_core as ex           # new module - excloud.in web port (own code)
from deploy_core import (load_plans, save_plan, plan_timer, fetch_ondemand_price,
                         users_from_txt, FLV_SPEC, REGION_KEYS, EMAIL, PASSWD, CREDS_FILE)

HOST = os.environ.get("ACEWEB_HOST", "0.0.0.0")
PORT = int(os.environ.get("ACEWEB_PORT") or os.environ.get("PORT") or "8787")
PIN = os.environ.get("ACEWEB_PIN", "") or "3315"  # default PIN (can be overridden via env)

MIME = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
        ".js": "application/javascript; charset=utf-8", ".ico": "image/x-icon",
        ".svg": "image/svg+xml"}

STATIC_FILES = {"/": "index.html", "/all": "all.html", "/style.css": "style.css",
                "/app.js": "app.js", "/all.css": "all.css", "/all.js": "all.js",
                "/logo.svg": "logo.svg", "/hostzop": "hostzop.html",
                "/hostzop.js": "hostzop.js", "/hostzop.css": "hostzop.css",
                "/hostzop-logo.svg": "hostzop-logo.svg",
                "/excloud": "excloud.html", "/excloud.js": "excloud.js",
                "/excloud.css": "excloud.css", "/excloud-logo.svg": "excloud-logo.svg"}

ACCOUNTS_FILE = SPOT_DIR / "accounts.json"

# ============================== log buffer ==============================
LOGS = []
LOG_LK = threading.Lock()


def push(msg, kind="info"):
    with LOG_LK:
        LOGS.append({"t": time.strftime("%H:%M:%S"), "m": str(msg), "k": kind})
        if len(LOGS) > 600:
            del LOGS[:300]


def _hook(mod):
    mod.log = lambda msg, level="info": push(msg, level)


_hook(ac)
_hook(sw)
_hook(hz)
_hook(ex)

# ============================== app state ==============================
BUSY = False
BUSY_LK = threading.Lock()
STATE = {"logged": False, "balance": None, "instances": [], "creds": {}}
ACTIVE_ID = "default"


def begin():
    global BUSY
    if not BUSY_LK.acquire(blocking=False):
        return False
    BUSY = True
    return True


def end():
    global BUSY
    BUSY = False
    BUSY_LK.release()


# ============================== helpers (same as GUI) ==============================
def classify(inst):
    name = (inst.get("name") or "").lower()
    bill = str(inst.get("billing_type") or "").lower()
    if ("proxy" in name or re.match(r"^spot-\d+-\d+$", name)) and "win" not in name:
        return "proxy"
    if bill == "spot" or "spot-win" in name:
        return "spot"
    return "ondemand"


def pub_ip(inst):
    pub = (inst.get("addresses") or {}).get("public") or []
    if pub:
        p = pub[0]
        return p.get("addr", "") if isinstance(p, dict) else str(p)
    return ""


def size_txt(inst):
    flavor = inst.get("flavor") or ""
    fname, cpu, mem, prc = "", None, None, 0
    if isinstance(flavor, dict):
        prc = flavor.get("spot_price_per_hour") or 0
        fname = flavor.get("name") or ""
        cpu = flavor.get("vcpus")
        mem = flavor.get("memory") or flavor.get("ram")
    else:
        fname = str(flavor)
    if cpu is None or mem is None:
        e = FLV_SPEC.get(fname)
        if e:
            cpu = cpu if cpu is not None else e[0]
            mem = mem if mem is not None else e[1] * 1024
    ram = "?"
    if mem is not None:
        try:
            ram = int(mem) // 1024
        except (TypeError, ValueError):
            ram = mem
    return (f"{fname} {cpu if cpu is not None else '?'}vCPU/{ram}GB".strip()
            if fname else f"{cpu}vCPU/{ram}GB"), prc


def load_creds():
    creds = {}
    try:
        if CREDS_FILE.exists():
            creds = json.loads(CREDS_FILE.read_text("utf-8")) or {}
    except Exception:
        creds = {}
    for ip, c in users_from_txt().items():
        creds.setdefault(ip, c)
    return creds


def save_creds(creds):
    try:
        CREDS_FILE.write_text(json.dumps(creds, indent=2), encoding="utf-8")
    except Exception:
        pass


def proxy_line_for(ip):
    for f in (SPOT_DIR / ac.PROXY_FILE, SPOT_DIR / "proxy.txt"):
        try:
            for line in f.read_text("utf-8", "replace").splitlines():
                p = line.split(":")
                if len(p) == 4 and p[0] == ip:
                    return line.strip()
        except Exception:
            pass
    return ""


# ============================== accounts (multi-account) ==============================
# The current account comes from env (ACECLOUD_EMAIL / ACECLOUD_PASSWD) and is always
# account id "default" - it is read-only and never modified. Extra accounts can come
# from the ACECLOUD_ACCOUNTS env var (JSON list) or from the file (added via UI).
def _env_accounts():
    out = []
    raw = (os.environ.get("ACECLOUD_ACCOUNTS") or "").strip()
    if not raw:
        return out
    try:
        data = json.loads(raw)
    except Exception:
        push("ACECLOUD_ACCOUNTS is not valid JSON", "warn")
        return out
    for a in (data if isinstance(data, list) else []):
        if isinstance(a, dict) and a.get("email") and a.get("password"):
            out.append({
                "id": str(a.get("id") or ("acc_" + uuid.uuid4().hex[:8])),
                "label": str(a.get("label") or a.get("email")),
                "email": str(a["email"]), "password": str(a["password"]),
                "api_key": str(a.get("api_key") or ""), "note": str(a.get("note") or ""),
                "source": "envlist",
            })
    return out


def load_accounts():
    accts = []
    if EMAIL and PASSWD:
        accts.append({"id": "default", "label": "Current (default)", "email": EMAIL,
                      "password": PASSWD, "api_key": "", "note": "From environment",
                      "source": "env"})
    seen = {a["id"] for a in accts}
    for a in _env_accounts():
        if a["id"] in seen:
            a["id"] = "acc_" + uuid.uuid4().hex[:8]
        seen.add(a["id"])
        accts.append(a)
    try:
        if ACCOUNTS_FILE.exists():
            for a in (json.loads(ACCOUNTS_FILE.read_text("utf-8")) or []):
                if (isinstance(a, dict) and a.get("email") and a.get("password")
                        and a.get("id") not in seen):
                    a.setdefault("source", "file")
                    seen.add(a.get("id"))
                    accts.append(a)
    except Exception:
        pass
    return accts


def save_accounts(accts):
    data = [a for a in accts if a.get("source") == "file"]
    try:
        ACCOUNTS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


def find_account(aid):
    for a in load_accounts():
        if a.get("id") == aid:
            return a
    return None


def active_account():
    return find_account(ACTIVE_ID) or find_account("default")


# ============================== hostzop (multi-account) ==============================
# Hostzop uses an API key (no email/password). The default account comes from
# HOSTZOP_APIKEY env (read-only); extra accounts come from HOSTZOP_ACCOUNTS or the UI.
HZ_FILE = SPOT_DIR / "hostzop.json"
HZ_ACTIVE = "default"


def _env_hz():
    out = []
    key = (os.environ.get("HOSTZOP_APIKEY") or "").strip()
    if key:
        out.append({"id": "default", "label": "Current (default)", "apikey": key,
                    "billing_account_id": (os.environ.get("HOSTZOP_BILLING_ACCOUNT_ID") or "").strip(),
                    "location": (os.environ.get("HOSTZOP_LOCATION") or "").strip(),
                    "note": "From environment", "source": "env"})
    raw = (os.environ.get("HOSTZOP_ACCOUNTS") or "").strip()
    if raw:
        try:
            data = json.loads(raw)
        except Exception:
            push("HOSTZOP_ACCOUNTS is not valid JSON", "warn")
            data = []
        for a in (data if isinstance(data, list) else []):
            if isinstance(a, dict) and a.get("apikey"):
                out.append({"id": str(a.get("id") or ("hz_" + uuid.uuid4().hex[:8])),
                            "label": str(a.get("label") or "Hostzop"),
                            "apikey": str(a["apikey"]),
                            "billing_account_id": str(a.get("billing_account_id") or ""),
                            "location": str(a.get("location") or ""),
                            "note": str(a.get("note") or ""), "source": "envlist"})
    return out


def load_hz_accounts():
    accts, seen = [], set()
    for a in _env_hz():
        if a["id"] in seen:
            a["id"] = "hz_" + uuid.uuid4().hex[:8]
        seen.add(a["id"])
        accts.append(a)
    try:
        if HZ_FILE.exists():
            for a in (json.loads(HZ_FILE.read_text("utf-8")) or []):
                if isinstance(a, dict) and a.get("apikey") and a.get("id") not in seen:
                    a.setdefault("source", "file")
                    seen.add(a.get("id"))
                    accts.append(a)
    except Exception:
        pass
    return accts


def save_hz_accounts(accts):
    data = [a for a in accts if a.get("source") == "file"]
    try:
        HZ_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


def find_hz_account(aid):
    for a in load_hz_accounts():
        if a.get("id") == aid:
            return a
    return None


def active_hz():
    accts = load_hz_accounts()
    return (find_hz_account(HZ_ACTIVE)
            or next((a for a in accts if a.get("source") == "env"), None)
            or (accts[0] if accts else None))


def hz_key():
    return ((active_hz() or {}).get("apikey") or "").strip()


def hz_meta(pool=None):
    key = hz_key()
    a = active_hz() or {}
    if not key:
        return {"ok": False, "msg": "No hostzop account", "locations": [], "pools": [],
                "images": [], "sizes": [], "user": ""}
    info = hz.verify(key)
    pol = hz.pricing(key)
    pool_list = hz.pools(key)
    sel_name = ""
    if pool:
        sel_name = next((p.get("name") for p in pool_list
                         if str(p.get("uuid")) == str(pool)), "") or ""
    rates = hz.class_rates(sel_name) or pol
    sizes = [{"name": n, "cpu": c, "ram": r, "disk": d,
              "rate": hz.hourly_rate(pol, c, r, d, rates=rates),
              "rate_win": hz.hourly_rate(pol, c, r, d, windows=True, rates=rates),
              "class": rates.get("label") if rates is not pol else ""}
             for n, c, r, d in hz.SIZE_CHART]
    pools = [{"name": p.get("name"), "uuid": p.get("uuid"),
              "default": bool(p.get("is_default_designated"))} for p in pool_list]
    flat = [{"name": n, "versions": vs} for n, vs in hz.images(key)]
    return {"ok": True, "user": info.get("user", "") if info.get("ok") else "",
            "locations": info.get("locations", []) if info.get("ok") else [],
            "pools": pools, "images": flat, "sizes": sizes,
            "billing_account_id": a.get("billing_account_id") or ""}


def job_hz_create(body):
    try:
        key = hz_key()
        res = hz.create_proxies(key, body, int(body.get("count", 1) or 1))
        lines = res.get("lines", [])
        creds = res.get("creds", [])
        if creds:
            pw = load_hz_pw()
            for c in creds:
                if c.get("uuid"):
                    pw[c["uuid"]] = c
            save_hz_pw(pw)
        for ln in lines:
            push(f"READY {ln}", "ok")
        if lines:
            out = SPOT_DIR / "hostzop_proxy.txt"
            with open(out, "a") as f:
                for ln in lines:
                    f.write(ln + "\n")
            push(f"Saved {len(lines)} line(s) -> {out}", "ok")
        for e in res.get("errors", []):
            push(e, "warn")
    except BaseException as e:
        push(f"HOSTZOP CREATE ERROR: {e}", "error")
    finally:
        end()


def job_hz_delete(uuids, is_all):
    try:
        key = hz_key()
        if is_all:
            hz.delete_all(key)
        else:
            hz.delete_vms(key, uuids)
    except BaseException as e:
        push(f"HOSTZOP DELETE ERROR: {e}", "error")
    finally:
        end()


HZ_SNAPS_FILE = SPOT_DIR / "hostzop_snaps.json"
HZ_PW_FILE = SPOT_DIR / "hostzop_pw.json"


def load_hz_pw():
    try:
        if HZ_PW_FILE.exists():
            data = json.loads(HZ_PW_FILE.read_text("utf-8"))
            return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}


def save_hz_pw(m):
    try:
        HZ_PW_FILE.write_text(json.dumps(m, indent=2), encoding="utf-8")
    except Exception:
        pass


def load_hz_snaps():
    try:
        if HZ_SNAPS_FILE.exists():
            data = json.loads(HZ_SNAPS_FILE.read_text("utf-8"))
            return data if isinstance(data, list) else []
    except Exception:
        pass
    return []


def save_hz_snaps(snaps):
    try:
        HZ_SNAPS_FILE.write_text(json.dumps(snaps, indent=2), encoding="utf-8")
    except Exception:
        pass


def job_hz_snapshot(vm_uuid, vm_name, name):
    try:
        key = hz_key()
        push(f"Taking snapshot of {vm_name or vm_uuid} ...", "sys")
        res = hz.make_snapshot(key, vm_uuid)
        push(f"create replica: HTTP {res.get('code')}", "info")
        ru = res.get("replica_uuid")
        if not ru:
            push("Replica UUID not returned - snapshot may have failed", "warn")
            return
        snaps = load_hz_snaps()
        snaps.append({"name": name or (str(vm_name or "vm") + " snapshot"),
                      "replica_uuid": ru, "vm_uuid": vm_uuid, "vm_name": vm_name or "",
                      "date": time.strftime("%Y-%m-%d %H:%M:%S")})
        save_hz_snaps(snaps)
        push(f"Snapshot saved: {snaps[-1]['name']}", "ok")
    except BaseException as e:
        push(f"HOSTZOP SNAPSHOT ERROR: {e}", "error")
    finally:
        end()


# ============================== ExCloud (excloud.in) ==============================
# ExCloud uses an API key (sk-excloud-...). The default account comes from the
# EXCLOUD_APIKEY env (read-only); extra accounts come from EXCLOUD_ACCOUNTS or the UI.
EX_FILE = SPOT_DIR / "excloud.json"
EX_ACTIVE = "default"


def _env_ex():
    out = []
    key = (os.environ.get("EXCLOUD_APIKEY") or "").strip()
    if key:
        out.append({"id": "default", "label": "Current (default)", "apikey": key,
                    "zone_id": (os.environ.get("EXCLOUD_ZONE_ID") or "").strip(),
                    "subnet_id": (os.environ.get("EXCLOUD_SUBNET_ID") or "").strip(),
                    "security_group_ids": (os.environ.get("EXCLOUD_SECURITY_GROUP_IDS") or "").strip(),
                    "note": "From environment", "source": "env"})
    raw = (os.environ.get("EXCLOUD_ACCOUNTS") or "").strip()
    if raw:
        try:
            data = json.loads(raw)
        except Exception:
            push("EXCLOUD_ACCOUNTS is not valid JSON", "warn")
            data = []
        for a in (data if isinstance(data, list) else []):
            if isinstance(a, dict) and a.get("apikey"):
                out.append({"id": str(a.get("id") or ("ex_" + uuid.uuid4().hex[:8])),
                            "label": str(a.get("label") or "ExCloud"),
                            "apikey": str(a["apikey"]),
                            "zone_id": str(a.get("zone_id") or ""),
                            "subnet_id": str(a.get("subnet_id") or ""),
                            "security_group_ids": str(a.get("security_group_ids") or ""),
                            "note": str(a.get("note") or ""), "source": "envlist"})
    return out


def load_ex_accounts():
    accts, seen = [], set()
    for a in _env_ex():
        if a["id"] in seen:
            a["id"] = "ex_" + uuid.uuid4().hex[:8]
        seen.add(a["id"])
        accts.append(a)
    try:
        if EX_FILE.exists():
            for a in (json.loads(EX_FILE.read_text("utf-8")) or []):
                if isinstance(a, dict) and a.get("apikey") and a.get("id") not in seen:
                    a.setdefault("source", "file")
                    seen.add(a.get("id"))
                    accts.append(a)
    except Exception:
        pass
    return accts


def save_ex_accounts(accts):
    data = [a for a in accts if a.get("source") == "file"]
    try:
        EX_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


def find_ex_account(aid):
    for a in load_ex_accounts():
        if a.get("id") == aid:
            return a
    return None


def active_ex():
    accts = load_ex_accounts()
    return (find_ex_account(EX_ACTIVE)
            or next((a for a in accts if a.get("source") == "env"), None)
            or (accts[0] if accts else None))


def ex_key():
    return ((active_ex() or {}).get("apikey") or "").strip()


def ex_meta():
    key = ex_key()
    if not key:
        return {"ok": False, "msg": "No excloud account", "images": [], "types": [],
                "subnets": [], "default_subnet_id": "", "user": ""}
    return ex.meta(key)


def job_ex_create(body):
    try:
        key = ex_key()
        res = ex.create_proxies(key, body, int(body.get("count", 1) or 1))
        lines = res.get("lines", [])
        for ln in lines:
            push(f"READY {ln}", "ok")
        if lines:
            out = SPOT_DIR / "excloud_proxy.txt"
            with open(out, "a") as f:
                for ln in lines:
                    f.write(ln + "\n")
            push(f"Saved {len(lines)} line(s) -> {out}", "ok")
        for e in res.get("errors", []):
            push(e, "warn")
    except BaseException as e:
        push(f"EXCLOUD CREATE ERROR: {e}", "error")
    finally:
        end()


def job_ex_delete(ids, is_all):
    try:
        key = ex_key()
        if is_all:
            ex.delete_all(key)
        else:
            ex.delete_vms(key, ids)
    except BaseException as e:
        push(f"EXCLOUD DELETE ERROR: {e}", "error")
    finally:
        end()


EX_SNAPS_FILE = SPOT_DIR / "excloud_snaps.json"


def load_ex_snaps():
    try:
        if EX_SNAPS_FILE.exists():
            data = json.loads(EX_SNAPS_FILE.read_text("utf-8"))
            return data if isinstance(data, list) else []
    except Exception:
        pass
    return []


def save_ex_snaps(snaps):
    try:
        EX_SNAPS_FILE.write_text(json.dumps(snaps, indent=2), encoding="utf-8")
    except Exception:
        pass


def job_ex_snapshot(vm_id, vm_name, name):
    try:
        key = ex_key()
        push(f"Taking snapshot of {vm_name or vm_id} ...", "sys")
        res = ex.make_snapshot(key, vm_id)
        push(f"snapshot create: HTTP {res.get('code')}", "info")
        sid = res.get("snapshot_id")
        if not sid:
            push("Snapshot ID not returned - snapshot may have failed", "warn")
            return
        snaps = load_ex_snaps()
        snaps.append({"name": name or (str(vm_name or "vm") + " snapshot"),
                      "snapshot_id": sid, "vm_id": vm_id, "vm_name": vm_name or "",
                      "date": time.strftime("%Y-%m-%d %H:%M:%S")})
        save_ex_snaps(snaps)
        push(f"Snapshot saved: {snaps[-1]['name']}", "ok")
    except BaseException as e:
        push(f"EXCLOUD SNAPSHOT ERROR: {e}", "error")
    finally:
        end()


def detect_account(email, password):
    """Temporary login in a SEPARATE session -> this account's projects + networks.
    Does NOT touch the active session of the running account."""
    web = "https://customer.acecloud.ai"
    api = f"{web}/api/v1"
    try:
        s = requests.Session()
        csrf = s.get(f"{web}/v1/api/auth/csrf", timeout=20).json().get("csrfToken")
        r2 = s.post(f"{web}/v1/api/auth/callback/credentials",
                    data={"email": email, "password": password, "csrfToken": csrf,
                          "redirect": "false", "json": "true"}, timeout=20)
        if r2.status_code != 200:
            return {"ok": False, "msg": f"Login failed HTTP {r2.status_code}"}
        token = (s.get(f"{web}/v1/api/auth/session", timeout=20).json()
                 .get("user", {}).get("accessToken"))
        if not token:
            return {"ok": False, "msg": "No access token (wrong login?)"}
        h = {"Authorization": f"Bearer {token}"}
        projects = (s.get(f"{api}/projects", headers=h, timeout=30).json() or {}).get("data") or {}
        out_projects, networks = [], {}
        for pid, pv in projects.items():
            regs = [rk for rk, rv in ((pv or {}).get("regions") or {}).items()
                    if isinstance(rv, dict) and rv.get("is_accessible")]
            out_projects.append({"id": pid, "name": (pv or {}).get("name") or pid, "regions": regs})
        for p in out_projects:
            for reg in p["regions"]:
                if reg in networks:
                    continue
                try:
                    vpcs = (s.get(f"{api}/cloud/vpcs", headers=h,
                                  params={"region": reg, "project_id": p["id"]}, timeout=30)
                            .json() or {}).get("data") or []
                except Exception:
                    vpcs = []
                networks[reg] = [{"id": v.get("id"), "name": v.get("name"),
                                  "external": bool(v.get("router_external"))}
                                 for v in vpcs if isinstance(v, dict) and v.get("id")]
        return {"ok": True, "projects": out_projects, "networks": networks}
    except Exception as e:
        return {"ok": False, "msg": str(e)}


def discover_account():
    """After login, map THIS account's project id + external network per region.
    Uses saved overrides if present, otherwise auto-detects. Project/network ids
    are account-specific, unlike the hardcoded defaults."""
    a = active_account() or {}
    pid = (a.get("project_id") or "").strip()
    if not pid:
        try:
            r = sw.SESSION.get(f"{sw.API_BASE}/projects",
                               headers={"Authorization": f"Bearer {sw.AUTH_TOKEN}"}, timeout=30)
            projects = (r.json() or {}).get("data") or {}
        except BaseException as e:
            push(f"Project discovery failed: {e}", "warn")
            projects = {}
        if isinstance(projects, dict) and projects:
            for k, v in projects.items():
                if isinstance(v, dict) and (v.get("name") or "").lower() == "default project":
                    pid = k
                    break
            pid = pid or next(iter(projects.keys()))
    if pid:
        ac.PROJECT_ID = pid
        sw.PROJECT_ID = pid
        push(f"Project: {pid[:8]}...", "ok")
    else:
        push("No project id (set it in account details)", "warn")

    overrides = {"ap-south-mum-2": a.get("net_mumbai"), "ap-south-noi-1": a.get("net_noida")}
    regions = sorted(set(list(sw.WREGIONS.keys()) + list(ac.REGIONS.keys())))
    for code in regions:
        nid = (overrides.get(code) or "").strip()
        if not nid:
            try:
                rr = sw.SESSION.get(f"{sw.API_BASE}/cloud/vpcs",
                                    headers={"Authorization": f"Bearer {sw.AUTH_TOKEN}"},
                                    params={"region": code, "project_id": pid}, timeout=30)
                vpcs = (rr.json() or {}).get("data") or []
            except BaseException:
                vpcs = []
            ext = next((v for v in vpcs if isinstance(v, dict) and v.get("router_external")), None)
            if not ext and vpcs:
                ext = vpcs[0]
            nid = (ext or {}).get("id", "") if isinstance(ext, dict) else ""
        if nid:
            if code in ac.REGIONS:
                ac.REGIONS[code]["network"] = nid
            if code in sw.WREGIONS:
                sw.WREGIONS[code]["net"] = nid
    dreg = (a.get("region") or "").strip()
    if dreg in regions:
        ac.REGION = dreg
        sw.REGION = dreg


# ============================== login / state ==============================
def do_login():
    a = active_account()
    if not a:
        STATE["logged"] = False
        push("No account configured", "error")
        return
    try:
        ac.login(a["email"], a["password"])
        sw.login(a["email"], a["password"])
        STATE["logged"] = True
        discover_account()
        push(f"CONNECTED: {a['email']} [{a.get('label') or ''}]", "ok")
    except BaseException as e:
        STATE["logged"] = False
        push(f"Login fail: {e}", "error")


def refresh_state():
    if not STATE["logged"]:
        return
    try:
        rows = []
        for rid, rinfo in sw.WREGIONS.items():
            data = sw.api_data("GET", "cloud/instances",
                               params={"region": rid, "limit": "500"})
            for inst in (data or []):
                rows.append((rid, rinfo["label"], inst))
        balance = None
        try:
            r = sw.SESSION.get(f"{sw.API_BASE}/wallet",
                               headers={"Authorization": f"Bearer {sw.AUTH_TOKEN}"},
                               timeout=20)
            balance = ((r.json() or {}).get("data") or {}).get("balance")
        except Exception:
            pass
        STATE["instances"] = rows
        STATE["balance"] = balance
        STATE["creds"] = load_creds()
    except BaseException as e:
        push(f"refresh error: {e}", "error")


# ============================== CREATE: Windows / Normal (GUI _job_win) ==============================
def job_create_win(region, base, size_idx, count, hours, billing):
    try:
        code = REGION_KEYS[region]
        sw.REGION = code
        sw.build_region(code)
        size = sw.SIZES[size_idx]
        push(f"CREATE WIN: {region} | {size['flavor']} | x{count} | {hours}h | "
             f"{billing.upper()} | base={base}", "sys")
        price = (sw.get_spot_price(size["flavor"], size["price"]) if billing == "spot"
                 else (fetch_ondemand_price(size["flavor"], "windows") or size["price"]))
        push(f"Price {size['flavor']}: Rs.{price}/hr", "ok")
        sg = sw.ensure_sg()
        auto_pass = sw.gen_password()
        snaps = sw.list_my_snapshots(code) or []
        image_id, source = None, "image"
        if base.startswith("Windows 2022"):
            image_id = sw.IMAGES["2022"]["id"]
        elif base.startswith("Windows 2025"):
            image_id = sw.IMAGES["2025"]["id"]
        elif "SNAP:" in base:
            for s in snaps:
                if f"SNAP: {s.get('name')} ({s.get('size')}GB, {s.get('status')})" == base:
                    image_id = s.get("id")
                    source = "snapshot"
                    break
        if not image_id:
            raise ValueError("Base not selected")
        pending = {}
        for n in range(count):
            iid, iname = sw.create_win_spot(
                image_id, size["id"], price, sg, count,
                name_prefix=f"{region[:3]}-{time.strftime('%H%M%S')}-{n + 1}",
                user_data=sw.build_user_data(auto_pass),
                source_type=source, billing_type=billing)
            if iid:
                pending[iname] = iid
                push(f"Submitted: {iname}", "ok")
            else:
                push(f"Create fail: {iname}", "error")
            time.sleep(2)
        if not pending:
            push("Nothing was created.", "error")
            return
        save_plan(list(pending.keys()), hours)
        creds = load_creds()
        for name, iid in pending.items():
            inst = None
            for _ in range(120):
                try:
                    r = sw.api_data("GET", f"cloud/instances/{iid}", timeout=30)
                except Exception:
                    r = None
                if isinstance(r, dict):
                    st = r.get("status", "")
                    if st == "ACTIVE":
                        inst = r
                        break
                    if st == "ERROR":
                        break
                time.sleep(8)
            ip = sw.get_public_ip(inst) if inst else ""
            console = sw.get_console_url(iid) if inst else ""
            if ip:
                verified = False
                try:
                    verified, _ = sw.winrm_verify(ip, auto_pass, max_wait=180)
                except Exception:
                    verified = False
                creds[ip] = {"user": "Administrator", "pass": auto_pass, "port": "3389",
                             "console": console, "name": name, "region": code,
                             "verified": verified}
                save_creds(creds)
                push(f"READY: {name} -> {ip} (WinRM {'OK' if verified else 'pending'})",
                     "ok" if verified else "warn")
            else:
                push(f"NOT ACTIVE: {name}", "error")
        schedule_timer(hours, "win")
    except BaseException as e:
        push(f"CREATE ERROR: {e}", "error")
    finally:
        end()
        refresh_state()


# ============================== CREATE: Linux Proxy (GUI _job_proxy) ==============================
def job_create_proxy(code, size_idx, count, hours, port, user, pwd, billing_lbl):
    billing = "spot" if billing_lbl.startswith("SPOT") else "hourly"
    try:
        ac.REGION = code
        sizes = ac.build_linux_sizes(code)
        if size_idx < 0 or size_idx >= len(sizes):
            raise ValueError("size invalid")
        sel = sizes[size_idx]
        ac.FLAVOR_ID = sel["id"]
        ac.IMAGE_ID = ac.REGIONS[code]["image"]
        ac.EXT_NET_ID = ac.REGIONS[code]["network"]
        ac.KEY_NAME = ac.ensure_keypair()
        push(f"CREATE PROXY: {ac.REGIONS[code]['label']} | {sel['name']} | "
             f"x{count} | {hours}h | {billing.upper()}", "sys")
        price = (ac.get_spot_price(sel["name"]) if billing == "spot"
                 else (fetch_ondemand_price(sel["name"], "linux") or sel["price"]))
        push(f"Price {sel['name']}: Rs.{price}/hr", "ok")
        sg = ac.ensure_sg(port)
        ud_b64 = base64.b64encode(ac.build_user_data(user, pwd, port).encode()).decode()
        ready = []
        for n in range(count):
            iid, iname = ac.create_spot_instance(
                price, sg, ud_b64, port,
                name=f"spot-{int(time.time())}-{n + 1}", billing_type=billing)
            if not iid:
                push(f"Create fail: {iname}", "error")
                continue
            push(f"Submitted: {iname}", "ok")
            inst = None
            for _ in range(75):
                try:
                    inst = ac.api_data("GET", f"cloud/instances/{iid}", timeout=40)
                except Exception:
                    inst = None
                st = (inst or {}).get("status", "") if isinstance(inst, dict) else ""
                if st == "ACTIVE":
                    break
                if st == "ERROR":
                    inst = None
                    break
                time.sleep(8)
            ip = ac.get_public_ip(inst) if inst else ""
            if ip:
                ready.append({"name": iname, "id": iid, "ip": ip})
                push(f"ACTIVE: {iname} -> {ip}", "ok")
            else:
                push(f"NOT ACTIVE: {iname}", "error")
            time.sleep(2)
        if not ready:
            push("No proxy became ACTIVE.", "error")
            return
        save_plan([it["name"] for it in ready], hours)
        for it in ready:
            push(f"PROXY  http://{user}:{pwd}@{it['ip']}:{port}", "ok")
        pf = SPOT_DIR / ac.PROXY_FILE
        with open(pf, "a") as f:
            for it in ready:
                f.write(f"{it['ip']}:{port}:{user}:{pwd}\n")
        push(f"Saved -> {pf}", "ok")
        schedule_timer(hours, "proxy")
    except BaseException as e:
        push(f"PROXY ERROR: {e}", "error")
    finally:
        end()
        refresh_state()


def schedule_timer(hours, kind):
    push(f"Auto-delete scheduled: {hours}h (server must keep running)", "warn")

    def worker():
        time.sleep(int(float(hours) * 3600))
        push("Auto-delete timer expired. Cleaning up...", "warn")
        try:
            do_login()
            time.sleep(3)
        except BaseException as e:
            push(f"Re-login fail: {e}", "warn")
        try:
            if kind == "win":
                sw.nuke_all(delete_gpo=False)
            else:
                ac.nuke_all(delete_gpo=False)
        except BaseException as e:
            push(f"Auto nuke error: {e}", "error")
        refresh_state()

    threading.Thread(target=worker, daemon=True).start()


# ============================== DELETE / SNAPSHOT / TEST ==============================
def job_delete(items, is_all):
    try:
        for x in items:
            if is_all:
                rid, inst = x[0], x[2]
                name = inst.get("name", "?")
                iid = inst.get("id", "")
            else:
                rid, name, iid = x["region"], x["name"], x["id"]
            ac.REGION = rid
            ac.api("DELETE", "cloud/instances", data={"key": "id", "values": [iid]})
            push(f"Deleted: {name}", "ok")
            time.sleep(1)
        creds = load_creds()
        names = {x[2].get("name") if is_all else x.get("name") for x in items}
        for ip in [ip for ip, c in creds.items() if c.get("name") in names]:
            creds.pop(ip, None)
        save_creds(creds)
    except BaseException as e:
        push(f"DELETE ERROR: {e}", "error")
    finally:
        end()
        refresh_state()


def job_snapshot(region, name, snap_name):
    try:
        push(f"SNAPSHOT: {name} (region {region}) -> {snap_name}", "sys")
        sid = sw.take_snapshot(region, name, snap_name, f"Manual snapshot of {name}")
        if sid:
            push(f"SNAPSHOT CREATED: {snap_name} | {sid}", "ok")
            push("~Rs 0.34/hr (~Rs 250/month). In Create, choose Base='SNAP:'.", "info")
        else:
            push("Snapshot was not created.", "error")
    except BaseException as e:
        push(f"SNAPSHOT ERROR: {e}", "error")
    finally:
        end()
        refresh_state()


def job_proxy_test(line):
    push(f"Testing: {line}", "sys")
    try:
        p = line.split(":")
        if len(p) < 4:
            raise ValueError("format ip:port:user:pass")
        host, port, user, pwd = p[0], int(p[1]), p[2], p[3]
        tok = base64.b64encode(f"{user}:{pwd}".encode()).decode()
        req = ("CONNECT 8.8.8.8:53 HTTP/1.1\r\nHost: 8.8.8.8:53\r\n"
               f"Proxy-Authorization: Basic {tok}\r\n\r\n").encode()
        s = socket.create_connection((host, port), timeout=12)
        s.settimeout(12)
        s.sendall(req)
        head = s.recv(512).decode(errors="replace")
        s.close()
        ok = head.startswith("HTTP/1.1 200") or head.startswith("HTTP/1.0 200")
        push(f"{host}:{port} -> " + ("PROXY OK (200, auth pass)" if ok else
                                     f"FAIL: {head.splitlines()[0] if head else 'no response'}"),
             "ok" if ok else "error")
    except BaseException as e:
        push(f"Test FAIL: {e}", "error")
    finally:
        end()


# ============================== sizes / bases ==============================
def get_sizes(type_, region, billing):
    out = {"sizes": [], "bases": []}
    code = REGION_KEYS.get(region, "ap-south-mum-2")
    if type_ == "Linux Proxy":
        ac.REGION = code
        is_spot = billing.startswith("SPOT")
        for s in ac.build_linux_sizes(code):
            p = s["price"] if is_spot else (fetch_ondemand_price(s["name"], "linux") or s["price"])
            unit = "SPOT" if is_spot else "ON-DEMAND"
            out["sizes"].append(
                f"{s['n']}) {s['name']} - {s['vcpu']}vCPU/{s['ram']}GB - INR {p:,.2f}/hr {unit}")
        out["bases"] = ["Linux (auto)"]
    else:
        sw.REGION = code
        sw.build_region(code)
        is_spot = billing.startswith("SPOT")
        for s in list(sw.SIZES):
            p = s["price"] if is_spot else (fetch_ondemand_price(s["flavor"], "windows") or s["price"])
            unit = "SPOT" if is_spot else "ON-DEMAND"
            out["sizes"].append(
                f"{s['n']}) {s['flavor']} - {s['vcpu']}vCPU/{s['ram']}GB - INR {p:,.2f}/hr {unit}")
        snaps = sw.list_my_snapshots(code) or []
        out["bases"] = ["Windows 2022 (image)", "Windows 2025 (image)"] + [
            f"SNAP: {s.get('name')} ({s.get('size')}GB, {s.get('status')})" for s in snaps]
    return out


# ============================== HTTP ==============================
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    # ---------- plumbing ----------
    def _send(self, body, ctype, code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _json(self, obj, code=200):
        self._send(json.dumps(obj).encode(), "application/json; charset=utf-8", code)

    def _authed(self):
        q = parse_qs(urlparse(self.path).query)
        got = self.headers.get("X-Pin") or (q.get("pin") or [""])[0]
        return got == PIN

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode())
        except Exception:
            return {}

    # ---------- GET ----------
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/ping":
            self._json({"ok": True, "pin": bool(PIN)})
            return

        # static files - no PIN (let the UI load; data API is protected)
        fname = STATIC_FILES.get(path)
        if fname:
            self._send((WEB_DIR / "static" / fname).read_bytes(),
                       MIME.get(Path(fname).suffix, "application/octet-stream"))
            return

        if not self._authed():
            self._json({"error": "wrong pin"}, 401)
            return

        if path == "/api/accounts":
            self._json({"active": ACTIVE_ID, "accounts": [{
                "id": a["id"], "label": a.get("label") or a["email"],
                "email": a["email"], "has_pass": bool(a.get("password")),
                "api_key": a.get("api_key") or "", "note": a.get("note") or "",
                "region": a.get("region") or "",
                "project_id": a.get("project_id") or "",
                "net_mumbai": a.get("net_mumbai") or "",
                "net_noida": a.get("net_noida") or "",
                "source": a.get("source", "file"),
                "editable": a.get("source") == "file",
            } for a in load_accounts()]})
            return

        if path == "/api/hostzop/accounts":
            self._json({"active": HZ_ACTIVE, "accounts": [{
                "id": a["id"], "label": a.get("label") or "Hostzop",
                "apikey": a.get("apikey") or "", "has_key": bool(a.get("apikey")),
                "billing_account_id": a.get("billing_account_id") or "",
                "location": a.get("location") or "", "note": a.get("note") or "",
                "source": a.get("source", "file"),
                "editable": a.get("source") == "file",
            } for a in load_hz_accounts()]})
            return

        if path == "/api/hostzop/meta":
            q = parse_qs(urlparse(self.path).query)
            self._json(hz_meta((q.get("pool") or [""])[0]))
            return

        if path == "/api/hostzop/state":
            st = hz.state(hz_key())
            pw = load_hz_pw()
            for row in st.get("rows", []):
                c = pw.get(row.get("uuid") or "")
                if c:
                    row["root_password"] = c.get("root_password", "")
                    row["proxy_user"] = c.get("proxy_user", "")
                    row["proxy_pass"] = c.get("proxy_pass", "")
            self._json(st)
            return

        if path == "/api/hostzop/balance":
            self._json(hz.balance(hz_key(), (active_hz() or {}).get("billing_account_id") or ""))
            return

        if path == "/api/hostzop/snaps":
            self._json({"snaps": load_hz_snaps()})
            return

        if path == "/api/hostzop/output":
            q = parse_qs(urlparse(self.path).query)
            fn = "windows.txt" if (q.get("file") or ["proxy"])[0] == "windows" else "hostzop_proxy.txt"
            p = SPOT_DIR / fn
            txt = ""
            if p.exists():
                try:
                    txt = p.read_text("utf-8", errors="replace")
                except Exception:
                    txt = ""
            self._json({"file": fn, "text": txt})
            return

        if path == "/api/excloud/accounts":
            self._json({"active": EX_ACTIVE, "accounts": [{
                "id": a["id"], "label": a.get("label") or "ExCloud",
                "apikey": a.get("apikey") or "", "has_key": bool(a.get("apikey")),
                "zone_id": a.get("zone_id") or "", "subnet_id": a.get("subnet_id") or "",
                "note": a.get("note") or "", "source": a.get("source", "file"),
                "editable": a.get("source") == "file",
            } for a in load_ex_accounts()]})
            return

        if path == "/api/excloud/meta":
            self._json(ex_meta())
            return

        if path == "/api/excloud/state":
            self._json(ex.state(ex_key()))
            return

        if path == "/api/excloud/snaps":
            self._json({"snaps": load_ex_snaps()})
            return

        if path == "/api/excloud/output":
            p = SPOT_DIR / "excloud_proxy.txt"
            txt = ""
            if p.exists():
                try:
                    txt = p.read_text("utf-8", errors="replace")
                except Exception:
                    txt = ""
            self._json({"file": "excloud_proxy.txt", "text": txt})
            return

        if path == "/api/state":
            plans = load_plans()
            rows, counts = [], {"spot": 0, "ondemand": 0, "proxy": 0, "active": 0, "total": 0}
            for rid, rlabel, inst in STATE["instances"]:
                cat = classify(inst)
                status = str(inst.get("status") or "UNKNOWN").upper()
                size, prc = size_txt(inst)
                counts["total"] += 1
                counts[cat] = counts.get(cat, 0) + 1
                if status == "ACTIVE":
                    counts["active"] += 1
                pricing = (f"Rs.{float(prc):.2f}/hr" if prc
                           else ("hrly" if cat == "proxy" else "auto"))
                rows.append({
                    "id": inst.get("id", ""), "region": rid, "region_label": rlabel,
                    "status": status, "name": inst.get("name") or "?", "ip": pub_ip(inst),
                    "size": size, "cat": cat,
                    "type": {"spot": "SPOT", "ondemand": "ON-DEMAND", "proxy": "PROXY"}[cat],
                    "pricing": pricing,
                    "plan": plan_timer(plans, inst.get("name") or "?", inst.get("created")),
                })
            self._json({"logged": STATE["logged"], "busy": BUSY,
                        "balance": STATE["balance"], "counts": counts, "instances": rows})
            return

        if path == "/api/logs":
            try:
                since = int((parse_qs(urlparse(self.path).query).get("since") or ["0"])[0])
            except ValueError:
                since = 0
            with LOG_LK:
                self._json({"logs": LOGS[since:], "n": len(LOGS)})
            return

        if path == "/api/sizes":
            q = parse_qs(urlparse(self.path).query)
            try:
                self._json(get_sizes((q.get("type") or ["Windows Spot"])[0],
                                     (q.get("region") or ["Mumbai"])[0],
                                     (q.get("billing") or ["SPOT (cheap)"])[0]))
            except BaseException as e:
                self._json({"error": str(e)}, 500)
            return

        if path == "/api/proxyline":
            q = parse_qs(urlparse(self.path).query)
            self._json({"line": proxy_line_for((q.get("ip") or [""])[0])})
            return

        if path == "/api/creds":
            q = parse_qs(urlparse(self.path).query)
            ip = (q.get("ip") or [""])[0]
            c = load_creds().get(ip) or {}
            self._json({"pass": c.get("pass", ""), "user": c.get("user", ""),
                        "console": c.get("console", "")})
            return

        self._json({"error": "not found"}, 404)

    # ---------- POST ----------
    def do_POST(self):
        global ACTIVE_ID, HZ_ACTIVE, EX_ACTIVE
        if not self._authed():
            self._json({"error": "wrong pin"}, 401)
            return
        b = self._body()
        path = urlparse(self.path).path

        if path == "/api/create":
            if not STATE["logged"]:
                self._json({"ok": False, "msg": "Not logged in"}, 400)
                return
            if not begin():
                self._json({"ok": False, "msg": "Wait for the current task to finish..."}, 409)
                return
            t = b.get("type", "Windows Spot")
            if t == "Linux Proxy":
                threading.Thread(target=job_create_proxy, daemon=True, args=(
                    REGION_KEYS.get(b.get("region", "Mumbai"), "ap-south-mum-2"),
                    int(b.get("size_idx", 0)), int(b.get("count", 1)),
                    int(b.get("hours", 3)), int(b.get("port", 3128)),
                    b.get("user", "ajay") or "ajay", b.get("pass") or "ajay",
                    b.get("billing", "SPOT (cheap)"))).start()
            else:
                billing = "spot" if (b.get("billing") or "").startswith("SPOT") else "hourly"
                threading.Thread(target=job_create_win, daemon=True, args=(
                    b.get("region", "Mumbai"), b.get("base", "Windows 2022 (image)"),
                    int(b.get("size_idx", 0)), int(b.get("count", 1)),
                    int(b.get("hours", 3)), billing)).start()
            self._json({"ok": True})
            return

        if path == "/api/delete":
            if not begin():
                self._json({"ok": False, "msg": "Busy..."}, 409)
                return
            items = list(STATE["instances"]) if b.get("all") else b.get("items", [])
            threading.Thread(target=job_delete, daemon=True,
                             args=(items, bool(b.get("all")))).start()
            self._json({"ok": True})
            return

        if path == "/api/snapshot":
            if not begin():
                self._json({"ok": False, "msg": "Busy..."}, 409)
                return
            name = b.get("name", "")
            snap = (b.get("snap_name") or "").strip() or f"{name[:24]}-snap"
            threading.Thread(target=job_snapshot, daemon=True,
                             args=(b.get("region", ""), name, snap)).start()
            self._json({"ok": True})
            return

        if path == "/api/test":
            if not begin():
                self._json({"ok": False, "msg": "Busy..."}, 409)
                return
            threading.Thread(target=job_proxy_test, daemon=True,
                             args=(b.get("line", ""),)).start()
            self._json({"ok": True})
            return

        if path == "/api/accounts/detect":
            self._json(detect_account((b.get("email") or "").strip(),
                                      b.get("password") or ""))
            return

        if path == "/api/accounts/add":
            label = (b.get("label") or "").strip()
            email = (b.get("email") or "").strip()
            pwd = b.get("password") or ""
            key = (b.get("api_key") or "").strip()
            note = (b.get("note") or "").strip()
            region = (b.get("region") or "").strip()
            project_id = (b.get("project_id") or "").strip()
            net_mumbai = (b.get("net_mumbai") or "").strip()
            net_noida = (b.get("net_noida") or "").strip()
            eid = (b.get("id") or "").strip()
            extra = {"api_key": key, "note": note, "region": region,
                     "project_id": project_id, "net_mumbai": net_mumbai, "net_noida": net_noida}
            accts = load_accounts()
            if eid:
                tgt = next((a for a in accts if a.get("id") == eid), None)
                if not tgt or tgt.get("source") != "file":
                    self._json({"ok": False, "msg": "This account is read-only"}, 400)
                    return
                if label:
                    tgt["label"] = label
                if email:
                    tgt["email"] = email
                if pwd:
                    tgt["password"] = pwd
                tgt.update(extra)
                save_accounts(accts)
                new_id = eid
            else:
                if not email or not pwd:
                    self._json({"ok": False, "msg": "Email and password required"}, 400)
                    return
                new_id = "acc_" + uuid.uuid4().hex[:8]
                rec = {"id": new_id, "label": label or email, "email": email,
                       "password": pwd, "source": "file"}
                rec.update(extra)
                accts.append(rec)
                save_accounts(accts)
            if b.get("use"):
                ACTIVE_ID = new_id
                threading.Thread(target=lambda: (do_login(), refresh_state()),
                                 daemon=True).start()
            self._json({"ok": True, "id": new_id})
            return

        if path == "/api/accounts/delete":
            eid = (b.get("id") or "").strip()
            accts = load_accounts()
            tgt = next((a for a in accts if a.get("id") == eid), None)
            if not tgt or tgt.get("source") != "file":
                self._json({"ok": False, "msg": "Cannot delete this account"}, 400)
                return
            save_accounts([a for a in accts if a.get("id") != eid])
            if ACTIVE_ID == eid:
                ACTIVE_ID = "default"
                threading.Thread(target=lambda: (do_login(), refresh_state()),
                                 daemon=True).start()
            self._json({"ok": True})
            return

        if path == "/api/accounts/activate":
            eid = (b.get("id") or "").strip()
            if not find_account(eid):
                self._json({"ok": False, "msg": "No such account"}, 400)
                return
            ACTIVE_ID = eid
            threading.Thread(target=lambda: (do_login(), refresh_state()), daemon=True).start()
            self._json({"ok": True})
            return

        if path == "/api/hostzop/verify":
            self._json(hz.verify((b.get("apikey") or "").strip()))
            return

        if path == "/api/hostzop/accounts/add":
            label = (b.get("label") or "").strip()
            key = (b.get("apikey") or "").strip()
            bill = str(b.get("billing_account_id") or "").strip()
            loc = (b.get("location") or "").strip()
            note = (b.get("note") or "").strip()
            eid = (b.get("id") or "").strip()
            accts = load_hz_accounts()
            if eid:
                tgt = next((a for a in accts if a.get("id") == eid), None)
                if not tgt or tgt.get("source") != "file":
                    self._json({"ok": False, "msg": "This account is read-only"}, 400)
                    return
                if label:
                    tgt["label"] = label
                if key:
                    tgt["apikey"] = key
                tgt["billing_account_id"] = bill
                tgt["location"] = loc
                tgt["note"] = note
                save_hz_accounts(accts)
                new_id = eid
            else:
                if not key:
                    self._json({"ok": False, "msg": "API key required"}, 400)
                    return
                new_id = "hz_" + uuid.uuid4().hex[:8]
                accts.append({"id": new_id, "label": label or "Hostzop", "apikey": key,
                              "billing_account_id": bill, "location": loc,
                              "note": note, "source": "file"})
                save_hz_accounts(accts)
            if b.get("use"):
                HZ_ACTIVE = new_id
            self._json({"ok": True, "id": new_id})
            return

        if path == "/api/hostzop/accounts/delete":
            eid = (b.get("id") or "").strip()
            accts = load_hz_accounts()
            tgt = next((a for a in accts if a.get("id") == eid), None)
            if not tgt or tgt.get("source") != "file":
                self._json({"ok": False, "msg": "Cannot delete this account"}, 400)
                return
            save_hz_accounts([a for a in accts if a.get("id") != eid])
            if HZ_ACTIVE == eid:
                HZ_ACTIVE = "default"
            self._json({"ok": True})
            return

        if path == "/api/hostzop/accounts/activate":
            eid = (b.get("id") or "").strip()
            if not find_hz_account(eid):
                self._json({"ok": False, "msg": "No such account"}, 400)
                return
            HZ_ACTIVE = eid
            self._json({"ok": True})
            return

        if path == "/api/hostzop/create":
            if not hz_key():
                self._json({"ok": False, "msg": "Add/activate a hostzop account first"}, 400)
                return
            if not begin():
                self._json({"ok": False, "msg": "Busy..."}, 409)
                return
            threading.Thread(target=job_hz_create, daemon=True, args=(b,)).start()
            self._json({"ok": True})
            return

        if path == "/api/hostzop/delete":
            if not hz_key():
                self._json({"ok": False, "msg": "No hostzop account"}, 400)
                return
            if not begin():
                self._json({"ok": False, "msg": "Busy..."}, 409)
                return
            threading.Thread(target=job_hz_delete, daemon=True,
                             args=(b.get("uuids") or [], bool(b.get("all")))).start()
            self._json({"ok": True})
            return

        if path == "/api/hostzop/testproxy":
            self._json(hz.test_proxy(b.get("line") or ""))
            return

        if path == "/api/hostzop/snapshot":
            if not hz_key():
                self._json({"ok": False, "msg": "No hostzop account"}, 400)
                return
            vm_uuid = (b.get("vm_uuid") or "").strip()
            if not vm_uuid:
                self._json({"ok": False, "msg": "vm_uuid required"}, 400)
                return
            if not begin():
                self._json({"ok": False, "msg": "Busy..."}, 409)
                return
            threading.Thread(target=job_hz_snapshot, daemon=True,
                             args=(vm_uuid, (b.get("vm_name") or "").strip(),
                                   (b.get("name") or "").strip())).start()
            self._json({"ok": True})
            return

        if path == "/api/excloud/verify":
            self._json(ex.verify((b.get("apikey") or "").strip()))
            return

        if path == "/api/excloud/accounts/add":
            label = (b.get("label") or "").strip()
            key = (b.get("apikey") or "").strip()
            zone = str(b.get("zone_id") or "").strip()
            sub = str(b.get("subnet_id") or "").strip()
            note = (b.get("note") or "").strip()
            eid = (b.get("id") or "").strip()
            accts = load_ex_accounts()
            if eid:
                tgt = next((a for a in accts if a.get("id") == eid), None)
                if not tgt or tgt.get("source") != "file":
                    self._json({"ok": False, "msg": "This account is read-only"}, 400)
                    return
                if label:
                    tgt["label"] = label
                if key:
                    tgt["apikey"] = key
                tgt["zone_id"] = zone
                tgt["subnet_id"] = sub
                tgt["note"] = note
                save_ex_accounts(accts)
                new_id = eid
            else:
                if not key:
                    self._json({"ok": False, "msg": "API key required"}, 400)
                    return
                new_id = "ex_" + uuid.uuid4().hex[:8]
                accts.append({"id": new_id, "label": label or "ExCloud", "apikey": key,
                              "zone_id": zone, "subnet_id": sub, "note": note, "source": "file"})
                save_ex_accounts(accts)
            if b.get("use"):
                EX_ACTIVE = new_id
            self._json({"ok": True, "id": new_id})
            return

        if path == "/api/excloud/accounts/delete":
            eid = (b.get("id") or "").strip()
            accts = load_ex_accounts()
            tgt = next((a for a in accts if a.get("id") == eid), None)
            if not tgt or tgt.get("source") != "file":
                self._json({"ok": False, "msg": "Cannot delete this account"}, 400)
                return
            save_ex_accounts([a for a in accts if a.get("id") != eid])
            if EX_ACTIVE == eid:
                EX_ACTIVE = "default"
            self._json({"ok": True})
            return

        if path == "/api/excloud/accounts/activate":
            eid = (b.get("id") or "").strip()
            if not find_ex_account(eid):
                self._json({"ok": False, "msg": "No such account"}, 400)
                return
            EX_ACTIVE = eid
            self._json({"ok": True})
            return

        if path == "/api/excloud/create":
            if not ex_key():
                self._json({"ok": False, "msg": "Add/activate an excloud account first"}, 400)
                return
            if not begin():
                self._json({"ok": False, "msg": "Busy..."}, 409)
                return
            threading.Thread(target=job_ex_create, daemon=True, args=(b,)).start()
            self._json({"ok": True})
            return

        if path == "/api/excloud/delete":
            if not ex_key():
                self._json({"ok": False, "msg": "No excloud account"}, 400)
                return
            if not begin():
                self._json({"ok": False, "msg": "Busy..."}, 409)
                return
            threading.Thread(target=job_ex_delete, daemon=True,
                             args=(b.get("ids") or [], bool(b.get("all")))).start()
            self._json({"ok": True})
            return

        if path == "/api/excloud/testproxy":
            self._json(ex.test_proxy(b.get("line") or ""))
            return

        if path == "/api/excloud/snapshot":
            if not ex_key():
                self._json({"ok": False, "msg": "No excloud account"}, 400)
                return
            vm_id = (b.get("vm_id") or "").strip()
            if not vm_id:
                self._json({"ok": False, "msg": "vm_id required"}, 400)
                return
            if not begin():
                self._json({"ok": False, "msg": "Busy..."}, 409)
                return
            threading.Thread(target=job_ex_snapshot, daemon=True,
                             args=(vm_id, (b.get("vm_name") or "").strip(),
                                   (b.get("name") or "").strip())).start()
            self._json({"ok": True})
            return

        if path == "/api/login":
            threading.Thread(target=lambda: (do_login(), refresh_state()), daemon=True).start()
            self._json({"ok": True})
            return

        if path == "/api/refresh":
            threading.Thread(target=refresh_state, daemon=True).start()
            self._json({"ok": True})
            return

        self._json({"error": "not found"}, 404)


def main():
    push("AceCloud Web Manager v2 starting...", "sys")
    threading.Thread(target=lambda: (do_login(), refresh_state()), daemon=True).start()
    print("\n" + "=" * 58)
    print("  AceCloud Web Manager v2")
    print(f"  LOCAL :  http://127.0.0.1:{PORT}")
    print(f"  HUB   :  http://127.0.0.1:{PORT}/all")
    print(f"  PIN   :  {PIN}   (asked once per browser/phone)")
    print("  Need a public URL? Run tunnel.bat (free Cloudflare)")
    print("=" * 58 + "\n")
    push(f"PIN: {PIN}", "sys")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
