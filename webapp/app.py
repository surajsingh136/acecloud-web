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
import gcore_core as gc             # new module - gcore.com web port (own code)
from deploy_core import (plan_timer, plan_deadline, fetch_ondemand_price,
                         users_from_txt, FLV_SPEC, REGION_KEYS, EMAIL, PASSWD)
import store                        # multi-user accounts + sessions + persistence

# ---- per-request user context (ThreadingHTTPServer -> one thread per request) ----
CUR = threading.local()


def cur_uid():
    return getattr(CUR, "uid", None) or "owner"


def set_cur(uid):
    CUR.uid = uid or "owner"


def udir():
    """The current user's private data directory."""
    return store.user_dir(cur_uid())


def is_owner():
    try:
        return cur_uid() == store.owner_uid()
    except Exception:
        return True

HOST = os.environ.get("ACEWEB_HOST", "0.0.0.0")
PORT = int(os.environ.get("ACEWEB_PORT") or os.environ.get("PORT") or "8787")
PIN = os.environ.get("ACEWEB_PIN", "") or "3315"  # default PIN (can be overridden via env)

MIME = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
        ".js": "application/javascript; charset=utf-8", ".ico": "image/x-icon",
        ".svg": "image/svg+xml"}

STATIC_FILES = {"/all": "all.html", "/style.css": "style.css",
                "/app.js": "app.js", "/all.css": "all.css", "/all.js": "all.js",
                "/logo.svg": "logo.svg", "/index.css": "index.css",
                "/acecloud": "index.html",
                "/hostzop": "hostzop.html",
                "/hostzop.js": "hostzop.js", "/hostzop.css": "hostzop.css",
                "/hostzop-logo.svg": "hostzop-logo.svg",
                "/excloud": "excloud.html", "/excloud.js": "excloud.js",
                "/excloud.css": "excloud.css", "/excloud-logo.svg": "excloud-logo.svg",
                "/gcore": "gcore.html", "/gcore.js": "gcore.js",
                "/gcore.css": "gcore.css", "/gcore-logo.svg": "gcore-logo.svg",
                "/login": "login.html", "/login.js": "login.js", "/login.css": "login.css",
                "/admin": "admin.html", "/admin.js": "admin.js", "/admin.css": "admin.css",
                "/help": "help.html", "/help.js": "help.js", "/help.css": "help.css"}

# AceCloud page is only reachable when it is the user's default host (served at "/").
HOST_PAGES = {"acecloud": "index.html", "hostzop": "hostzop.html",
              "excloud": "excloud.html", "gcore": "gcore.html"}
APP_PAGES = {"/all", "/acecloud", "/hostzop", "/excloud", "/gcore"}


# ============================== per-user prefs (active account per provider) ==============================
def load_prefs():
    try:
        f = udir() / "prefs.json"
        if f.exists():
            return json.loads(f.read_text("utf-8")) or {}
    except Exception:
        pass
    return {}


def save_prefs(d):
    try:
        (udir() / "prefs.json").write_text(json.dumps(d, indent=2), encoding="utf-8")
    except Exception:
        pass


def get_pref(kind, default="default"):
    return (load_prefs().get(kind) or default)


def set_pref(kind, val):
    d = load_prefs()
    d[kind] = val
    save_prefs(d)


# ============================== per-user instance plans (auto-delete hours) ==============================
def load_plans():
    try:
        f = udir() / "instance_plans.json"
        if f.exists():
            return json.loads(f.read_text("utf-8")) or {}
    except Exception:
        pass
    return {}


def save_plan(names, hours):
    try:
        plans = load_plans()
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        for nm in names:
            plans[nm] = {"created": now, "hours": int(hours)}
        (udir() / "instance_plans.json").write_text(json.dumps(plans, indent=2), encoding="utf-8")
        store.sync_now()
    except Exception:
        pass


# ============================== auto-delete timers ==============================
# One timer per provider, per user. Fires ~1 min before the full hour so the bill
# stays at exactly the set number of hours (never spills into an extra hour).
def _timer_file():
    return udir() / "auto_timers.json"


def load_timers():
    try:
        f = udir() / "auto_timers.json"
        if f.exists():
            d = json.loads(f.read_text("utf-8"))
            return d if isinstance(d, list) else []
    except Exception:
        pass
    return []


def save_timers(lst):
    try:
        (udir() / "auto_timers.json").write_text(json.dumps(lst, indent=2), encoding="utf-8")
        store.sync_now()
    except Exception:
        pass


def set_timer(kind, hours):
    try:
        hours = float(hours or 0)
    except Exception:
        return
    if hours <= 0:
        return
    deadline = time.time() + max(60, int(hours * 3600) - 60)
    lst = [t for t in load_timers() if t.get("kind") != kind]
    lst.append({"kind": kind, "hours": int(hours), "deadline": deadline})
    save_timers(lst)
    et = time.strftime("%H:%M", time.localtime(deadline))
    push(f"AUTO-DELETE [{kind.upper()}] set: {int(hours)}h -> delete ~{et} "
         f"(1 min before, bill-safe)", "warn")


def timer_deadline(kind):
    for t in load_timers():
        if t.get("kind") == kind:
            return t.get("deadline")
    return None


def all_uids():
    try:
        return [store.owner_uid()] + [u["id"] for u in store.list_public()]
    except Exception:
        return [store.owner_uid()]


def _check_timers():
    now = time.time()
    lst = load_timers()
    due = [t for t in lst if (t.get("deadline") or 0) <= now]
    if not due:
        return
    for t in due:
        kind = t.get("kind")
        push(f"AUTO-DELETE [{str(kind).upper()}] time up -> deleting...", "warn")
        try:
            if kind == "hz" and hz_key():
                hz.delete_all(hz_key())
            elif kind == "ex" and ex_key():
                ex.delete_all(ex_key())
            elif kind == "gc" and gc_cfg().get("apikey"):
                gc.delete_all(gc_key(), gc_cfg())
        except BaseException as e:
            push(f"AUTO-DELETE [{kind}] error: {e}", "error")
    save_timers([t for t in lst if (t.get("deadline") or 0) > now])
    refresh_state()
    store.sync_now()


def timer_loop():
    while True:
        time.sleep(15)
        for uid in all_uids():
            set_cur(uid)
            try:
                _check_timers()
            except BaseException:
                pass


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
_hook(gc)

# ============================== app state ==============================
BUSY = False
BUSY_LK = threading.Lock()
STATES = {}     # uid -> {logged, balance, instances, creds}


def st(uid=None):
    return STATES.setdefault(uid or cur_uid(),
                             {"logged": False, "balance": None, "instances": [], "creds": {}})


def spawn(fn, *args):
    """Run fn in a daemon thread carrying the current user's context."""
    uid = cur_uid()

    def run():
        set_cur(uid)
        try:
            fn(*args)
        except BaseException as e:
            push(f"thread error: {e}", "error")

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


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


def _creds_file():
    return udir() / "spot_creds.json"


def load_creds():
    creds = {}
    try:
        f = _creds_file()
        if f.exists():
            creds = json.loads(f.read_text("utf-8")) or {}
    except Exception:
        creds = {}
    if is_owner():
        for ip, c in users_from_txt().items():
            creds.setdefault(ip, c)
    return creds


def save_creds(creds):
    try:
        _creds_file().write_text(json.dumps(creds, indent=2), encoding="utf-8")
        store.sync_now()
    except Exception:
        pass


def proxy_line_for(ip):
    for f in (udir() / ac.PROXY_FILE, udir() / "proxy.txt"):
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
    if is_owner() and EMAIL and PASSWD:
        accts.append({"id": "default", "label": "Current (default)", "email": EMAIL,
                      "password": PASSWD, "api_key": "", "note": "From environment",
                      "source": "env"})
    seen = {a["id"] for a in accts}
    if is_owner():
        for a in _env_accounts():
            if a["id"] in seen:
                a["id"] = "acc_" + uuid.uuid4().hex[:8]
            seen.add(a["id"])
            accts.append(a)
    try:
        f = udir() / "accounts.json"
        if f.exists():
            for a in (json.loads(f.read_text("utf-8")) or []):
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
        (udir() / "accounts.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
        store.sync_now()
    except Exception:
        pass


def find_account(aid):
    for a in load_accounts():
        if a.get("id") == aid:
            return a
    return None


def active_account():
    aid = load_prefs().get("ace", "default")
    return find_account(aid) or find_account("default")


# ============================== hostzop (multi-account) ==============================
# Hostzop uses an API key (no email/password). The default account comes from
# HOSTZOP_APIKEY env (read-only, owner only); extra accounts come from HOSTZOP_ACCOUNTS or the UI.
def _hz_file():
    return udir() / "hostzop.json"


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
    if is_owner():
        for a in _env_hz():
            if a["id"] in seen:
                a["id"] = "hz_" + uuid.uuid4().hex[:8]
            seen.add(a["id"])
            accts.append(a)
    try:
        f = _hz_file()
        if f.exists():
            for a in (json.loads(f.read_text("utf-8")) or []):
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
        _hz_file().write_text(json.dumps(data, indent=2), encoding="utf-8")
        store.sync_now()
    except Exception:
        pass


def find_hz_account(aid):
    for a in load_hz_accounts():
        if a.get("id") == aid:
            return a
    return None


def active_hz():
    aid = load_prefs().get("hz", "default")
    accts = load_hz_accounts()
    return (find_hz_account(aid)
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
            set_timer("hz", body.get("hours"))
            out = udir() / "hostzop_proxy.txt"
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


def _hz_snaps_file():
    return udir() / "hostzop_snaps.json"


def _hz_pw_file():
    return udir() / "hostzop_pw.json"


def load_hz_pw():
    try:
        f = _hz_pw_file()
        if f.exists():
            data = json.loads(f.read_text("utf-8"))
            return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}


def save_hz_pw(m):
    try:
        _hz_pw_file().write_text(json.dumps(m, indent=2), encoding="utf-8")
        store.sync_now()
    except Exception:
        pass


def load_hz_snaps():
    try:
        f = _hz_snaps_file()
        if f.exists():
            data = json.loads(f.read_text("utf-8"))
            return data if isinstance(data, list) else []
    except Exception:
        pass
    return []


def save_hz_snaps(snaps):
    try:
        _hz_snaps_file().write_text(json.dumps(snaps, indent=2), encoding="utf-8")
        store.sync_now()
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
# EXCLOUD_APIKEY env (read-only, owner only); extra accounts come from EXCLOUD_ACCOUNTS or the UI.
def _ex_file():
    return udir() / "excloud.json"


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
    if is_owner():
        for a in _env_ex():
            if a["id"] in seen:
                a["id"] = "ex_" + uuid.uuid4().hex[:8]
            seen.add(a["id"])
            accts.append(a)
    try:
        if _ex_file().exists():
            for a in (json.loads(_ex_file().read_text("utf-8")) or []):
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
        _ex_file().write_text(json.dumps(data, indent=2), encoding="utf-8")
        store.sync_now()
    except Exception:
        pass


def find_ex_account(aid):
    for a in load_ex_accounts():
        if a.get("id") == aid:
            return a
    return None


def active_ex():
    accts = load_ex_accounts()
    return (find_ex_account(load_prefs().get("ex", "default"))
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
            set_timer("ex", body.get("hours"))
            out = udir() / "excloud_proxy.txt"
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


def _ex_snaps_file():
    return udir() / "excloud_snaps.json"


def load_ex_snaps():
    try:
        f = _ex_snaps_file()
        if f.exists():
            data = json.loads(f.read_text("utf-8"))
            return data if isinstance(data, list) else []
    except Exception:
        pass
    return []


def save_ex_snaps(snaps):
    try:
        _ex_snaps_file().write_text(json.dumps(snaps, indent=2), encoding="utf-8")
        store.sync_now()
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


# ============================== Gcore (gcore.com) ==============================
# Gcore uses an API key. The default account comes from the GCORE_APIKEY env
# (read-only, owner only); extra accounts come from GCORE_ACCOUNTS or the UI.
def _gc_file():
    return udir() / "gcore.json"


def _env_gc():
    out = []
    key = os.environ.get("GCORE_APIKEY", "").strip()
    if key:
        out.append({"id": "default", "label": "Gcore",
                    "apikey": key,
                    "project_id": os.environ.get("GCORE_PROJECT_ID", "").strip(),
                    "region_id": os.environ.get("GCORE_REGION_ID", "").strip() or "108",
                    "note": "from env", "source": "env"})
    raw = os.environ.get("GCORE_ACCOUNTS", "").strip()
    if raw:
        try:
            data = json.loads(raw)
        except Exception:
            data = []
        for a in (data if isinstance(data, list) else []):
            if isinstance(a, dict) and a.get("apikey"):
                out.append({"id": str(a.get("id") or ("gc_" + uuid.uuid4().hex[:8])),
                            "label": str(a.get("label") or "Gcore"),
                            "apikey": str(a["apikey"]),
                            "project_id": str(a.get("project_id") or ""),
                            "region_id": str(a.get("region_id") or "108"),
                            "note": str(a.get("note") or ""), "source": "envlist"})
    return out


def load_gc_accounts():
    accts, seen = [], set()
    if is_owner():
        for a in _env_gc():
            if a["id"] in seen:
                a["id"] = "gc_" + uuid.uuid4().hex[:8]
            seen.add(a["id"])
            accts.append(a)
    try:
        if _gc_file().exists():
            for a in (json.loads(_gc_file().read_text("utf-8")) or []):
                if isinstance(a, dict) and a.get("apikey") and a.get("id") not in seen:
                    a.setdefault("source", "file")
                    seen.add(a.get("id"))
                    accts.append(a)
    except Exception:
        pass
    return accts


def save_gc_accounts(accts):
    data = [a for a in accts if a.get("source") == "file"]
    try:
        _gc_file().write_text(json.dumps(data, indent=2), encoding="utf-8")
        store.sync_now()
    except Exception:
        pass


def find_gc_account(aid):
    for a in load_gc_accounts():
        if a.get("id") == aid:
            return a
    return None


def active_gc():
    accts = load_gc_accounts()
    return (find_gc_account(load_prefs().get("gc", "default"))
            or next((a for a in accts if a.get("source") == "env"), None)
            or (accts[0] if accts else None))


def gc_cfg():
    a = active_gc() or {}
    return {"apikey": (a.get("apikey") or "").strip(),
            "project_id": str(a.get("project_id") or "").strip(),
            "region_id": str(a.get("region_id") or "").strip()}


def gc_key():
    return gc_cfg()["apikey"]


def gc_meta(region_id=""):
    cfg = gc_cfg()
    if not cfg["apikey"]:
        return {"ok": False, "msg": "No gcore account", "regions": [], "images": [],
                "sizes": [], "project_id": "", "region_id": ""}
    return gc.meta(cfg["apikey"], cfg, region_id=region_id)


def job_gc_create(body):
    try:
        res = gc.create_proxies(gc_key(), body, int(body.get("count", 1) or 1))
        lines = res.get("lines", [])
        for ln in lines:
            push(f"READY {ln}", "ok")
        if lines:
            set_timer("gc", body.get("hours"))
            out = udir() / "gcore_proxy.txt"
            with open(out, "a") as f:
                for ln in lines:
                    f.write(ln + "\n")
            push(f"Saved {len(lines)} line(s) -> {out}", "ok")
        for e in res.get("errors", []):
            push(e, "warn")
    except BaseException as e:
        push(f"GCORE CREATE ERROR: {e}", "error")
    finally:
        end()


def job_gc_delete(ids, is_all):
    try:
        cfg = gc_cfg()
        if is_all:
            gc.delete_all(gc_key(), cfg)
        else:
            gc.delete_vms(gc_key(), cfg, ids)
    except BaseException as e:
        push(f"GCORE DELETE ERROR: {e}", "error")
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
        st()["logged"] = False
        push("No account configured", "error")
        return
    try:
        ac.login(a["email"], a["password"])
        sw.login(a["email"], a["password"])
        st()["logged"] = True
        discover_account()
        push(f"CONNECTED: {a['email']} [{a.get('label') or ''}]", "ok")
    except BaseException as e:
        st()["logged"] = False
        push(f"Login fail: {e}", "error")


def refresh_state():
    if not st()["logged"]:
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
        st()["instances"] = rows
        st()["balance"] = balance
        st()["creds"] = load_creds()
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
        pf = udir() / ac.PROXY_FILE
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
    wait = max(60, int(float(hours) * 3600) - 60)
    et = time.strftime("%H:%M", time.localtime(time.time() + wait))
    push(f"AUTO-DELETE [{kind.upper()}] set: {int(float(hours))}h -> delete ~{et} "
         f"(1 min before, bill-safe). Server must keep running.", "warn")

    def worker():
        time.sleep(wait)
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

    spawn(worker)


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
    def _send(self, body, ctype, code=200, cookie=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if cookie is not None:
            self.send_header("Set-Cookie", f"cw_sess={cookie}; Path=/; HttpOnly; SameSite=Lax; Max-Age=1209600")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _json(self, obj, code=200, cookie=None):
        self._send(json.dumps(obj).encode(), "application/json; charset=utf-8", code, cookie)

    def _cookie(self):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.partition("=")
            if k.strip() == "cw_sess":
                return unquote(v)
        return ""

    def _uid(self):
        q = parse_qs(urlparse(self.path).query)
        if (self.headers.get("X-Pin") or (q.get("pin") or [""])[0]) == PIN:
            return store.owner_uid()
        uid = store.parse_session(self._cookie())
        if uid and store.get(uid):
            return uid
        return None

    def _authed(self):
        uid = self._uid()
        if uid:
            set_cur(uid)
        return bool(uid)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode())
        except Exception:
            return {}

    def _serve(self, fname):
        try:
            self._send((WEB_DIR / "static" / fname).read_bytes(),
                       MIME.get(Path(fname).suffix, "application/octet-stream"))
        except Exception:
            self._json({"error": "not found"}, 404)

    # ---------- GET ----------
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/ping":
            self._json({"ok": True, "pin": bool(PIN), "auth": bool(self._uid())})
            return

        # landing page: login for guests, the cloud hub (all four clouds) once signed in
        if path == "/":
            uid = self._uid()
            if not uid:
                self._serve("login.html")
                return
            set_cur(uid)
            self._serve("all.html")
            return

        # static files - no auth (the UI itself loads; data API is protected)
        fname = STATIC_FILES.get(path)
        if fname and path not in APP_PAGES:
            self._serve(fname)
            return

        if path in APP_PAGES:
            if not self._authed():
                self._serve("login.html")
                return
            self._serve(STATIC_FILES.get(path, "all.html"))
            return

        if not self._authed():
            self._json({"error": "login required"}, 401)
            return

        if path == "/api/me":
            u = store.get(cur_uid()) or {}
            self._json({"ok": True, "id": cur_uid(), "email": u.get("email", ""),
                        "name": u.get("name", ""), "role": u.get("role", "user"),
                        "status": u.get("status", ""), "default_host": store.default_host(cur_uid())})
            return

        if path == "/api/admin/users":
            if not is_owner():
                self._json({"ok": False, "msg": "Admins only"}, 403)
                return
            q = parse_qs(urlparse(self.path).query)
            if (q.get("pass") or [""])[0] != store.ADMIN_PASS:
                self._json({"error": "admin password required"}, 401)
                return
            self._json({"ok": True, "users": store.list_public()})
            return

        if path == "/api/accounts":
            self._json({"active": load_prefs().get("ace", "default"), "accounts": [{
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
            self._json({"active": load_prefs().get("hz", "default"), "accounts": [{
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
            hz_st = hz.state(hz_key())
            pw = load_hz_pw()
            dl = timer_deadline("hz")
            for row in hz_st.get("rows", []):
                row["deadline"] = dl
                c = pw.get(row.get("uuid") or "")
                if c:
                    row["root_password"] = c.get("root_password", "")
            hz_st["busy"] = BUSY
            self._json(hz_st)
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
            p = udir() / fn
            txt = ""
            if p.exists():
                try:
                    txt = p.read_text("utf-8", errors="replace")
                except Exception:
                    txt = ""
            self._json({"file": fn, "text": txt})
            return

        if path == "/api/excloud/accounts":
            self._json({"active": load_prefs().get("ex", "default"), "accounts": [{
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
            ex_st = ex.state(ex_key())
            dl = timer_deadline("ex")
            for row in ex_st.get("rows", []):
                row["deadline"] = dl
            ex_st["busy"] = BUSY
            self._json(ex_st)
            return

        if path == "/api/excloud/snaps":
            self._json({"snaps": load_ex_snaps()})
            return

        if path == "/api/excloud/output":
            p = udir() / "excloud_proxy.txt"
            txt = ""
            if p.exists():
                try:
                    txt = p.read_text("utf-8", errors="replace")
                except Exception:
                    txt = ""
            self._json({"file": "excloud_proxy.txt", "text": txt})
            return

        if path == "/api/gcore/accounts":
            self._json({"active": load_prefs().get("gc", "default"), "accounts": [{
                "id": a["id"], "label": a.get("label") or "Gcore",
                "apikey": a.get("apikey") or "", "has_key": bool(a.get("apikey")),
                "project_id": a.get("project_id") or "", "region_id": a.get("region_id") or "",
                "note": a.get("note") or "", "source": a.get("source", "file"),
                "editable": a.get("source") == "file",
            } for a in load_gc_accounts()]})
            return

        if path == "/api/gcore/meta":
            q = parse_qs(urlparse(self.path).query)
            self._json(gc_meta((q.get("region") or [""])[0]))
            return

        if path == "/api/gcore/state":
            gc_st = gc.state(gc_key(), gc_cfg())
            dl = timer_deadline("gc")
            if isinstance(gc_st, dict):
                for row in gc_st.get("rows", []):
                    row["deadline"] = dl
                gc_st["busy"] = BUSY
            self._json(gc_st)
            return

        if path == "/api/gcore/output":
            p = udir() / "gcore_proxy.txt"
            txt = ""
            if p.exists():
                try:
                    txt = p.read_text("utf-8", errors="replace")
                except Exception:
                    txt = ""
            self._json({"file": "gcore_proxy.txt", "text": txt})
            return

        if path == "/api/state":
            plans = load_plans()
            rows, counts = [], {"spot": 0, "ondemand": 0, "proxy": 0, "active": 0, "total": 0}
            for rid, rlabel, inst in st()["instances"]:
                cat = classify(inst)
                status = str(inst.get("status") or "UNKNOWN").upper()
                size, prc = size_txt(inst)
                counts["total"] += 1
                counts[cat] = counts.get(cat, 0) + 1
                if status == "ACTIVE":
                    counts["active"] += 1
                pricing = (f"Rs.{float(prc):.2f}/hr" if prc
                           else ("hrly" if cat == "proxy" else "auto"))
                nm = inst.get("name") or "?"
                rows.append({
                    "id": inst.get("id", ""), "region": rid, "region_label": rlabel,
                    "status": status, "name": nm, "ip": pub_ip(inst),
                    "size": size, "cat": cat,
                    "type": {"spot": "SPOT", "ondemand": "ON-DEMAND", "proxy": "PROXY"}[cat],
                    "pricing": pricing,
                    "plan": plan_timer(plans, nm, inst.get("created")),
                    "deadline": plan_deadline(plans, nm, inst.get("created")),
                })
            self._json({"logged": st()["logged"], "busy": BUSY,
                        "balance": st()["balance"], "counts": counts, "instances": rows})
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
        b = self._body()
        path = urlparse(self.path).path

        # ----- public auth routes (no session needed) -----
        if path == "/api/auth/signup":
            r = store.register((b.get("email") or "").strip(),
                               b.get("password") or "", (b.get("name") or "").strip())
            self._json(r, 200 if r.get("ok") else 400)
            return

        if path == "/api/auth/login":
            r = store.login((b.get("email") or "").strip(), b.get("password") or "")
            if not r.get("ok"):
                self._json(r, 403 if r.get("status") else 401)
                return
            set_cur(r["uid"])
            self._json({"ok": True, "uid": r["uid"], "role": r["role"]},
                       cookie=store.make_session(r["uid"]))
            return

        if path == "/api/auth/logout":
            self._json({"ok": True}, cookie="")
            return

        # ----- admin panel (separate password gate, no session needed) -----
        if path.startswith("/api/admin/"):
            if (b.get("pass") or "") != store.ADMIN_PASS:
                self._json({"error": "admin password required"}, 401)
                return
            if path == "/api/admin/list":
                self._json({"ok": True, "users": store.list_public(),
                            "persist": store.sync_enabled()})
            elif path == "/api/admin/approve":
                self._json(store.set_status(b.get("id", ""), "approved"))
                store.sync_now()
            elif path == "/api/admin/reject":
                self._json(store.set_status(b.get("id", ""), "rejected"))
                store.sync_now()
            elif path == "/api/admin/delete":
                self._json(store.delete(b.get("id", "")))
                store.sync_now()
            elif path == "/api/admin/pass":
                self._json(store.set_password(b.get("id", ""), b.get("password") or ""))
                store.sync_now()
            else:
                self._json({"error": "unknown admin action"}, 404)
            return

        if not self._authed():
            self._json({"error": "wrong pin"}, 401)
            return

        if path == "/api/me":
            u = store.get(cur_uid()) or {}
            self._json({"ok": True, "id": cur_uid(), "email": u.get("email", ""),
                        "name": u.get("name", ""), "role": u.get("role", "user"),
                        "status": u.get("status", ""),
                        "default_host": store.default_host(cur_uid())})
            return

        if path == "/api/set-default":
            host = (b.get("host") or "").strip()
            if host not in HOST_PAGES:
                self._json({"ok": False, "msg": "Unknown host"}, 400)
                return
            store.set_default_host(cur_uid(), host)
            store.sync_now()
            self._json({"ok": True, "host": host})
            return

        if path == "/api/create":
            if not st()["logged"]:
                self._json({"ok": False, "msg": "Not logged in"}, 400)
                return
            if not begin():
                self._json({"ok": False, "msg": "Wait for the current task to finish..."}, 409)
                return
            t = b.get("type", "Windows Spot")
            if t == "Linux Proxy":
                spawn(job_create_proxy,
                      REGION_KEYS.get(b.get("region", "Mumbai"), "ap-south-mum-2"),
                      int(b.get("size_idx", 0)), int(b.get("count", 1)),
                      int(b.get("hours", 3)), int(b.get("port", 3128)),
                      b.get("user", "ajay") or "ajay", b.get("pass") or "ajay",
                      b.get("billing", "SPOT (cheap)"))
            else:
                billing = "spot" if (b.get("billing") or "").startswith("SPOT") else "hourly"
                spawn(job_create_win,
                      b.get("region", "Mumbai"), b.get("base", "Windows 2022 (image)"),
                      int(b.get("size_idx", 0)), int(b.get("count", 1)),
                      int(b.get("hours", 3)), billing)
            self._json({"ok": True})
            return

        if path == "/api/delete":
            if not begin():
                self._json({"ok": False, "msg": "Busy..."}, 409)
                return
            items = list(st()["instances"]) if b.get("all") else b.get("items", [])
            spawn(job_delete, items, bool(b.get("all")))
            self._json({"ok": True})
            return

        if path == "/api/snapshot":
            if not begin():
                self._json({"ok": False, "msg": "Busy..."}, 409)
                return
            name = b.get("name", "")
            snap = (b.get("snap_name") or "").strip() or f"{name[:24]}-snap"
            spawn(job_snapshot, b.get("region", ""), name, snap)
            self._json({"ok": True})
            return

        if path == "/api/test":
            if not begin():
                self._json({"ok": False, "msg": "Busy..."}, 409)
                return
            spawn(job_proxy_test, b.get("line", ""))
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
                set_pref("ace", new_id)
                spawn(lambda: (do_login(), refresh_state()))
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
            if load_prefs().get("ace") == eid:
                set_pref("ace", "default")
                spawn(lambda: (do_login(), refresh_state()))
            self._json({"ok": True})
            return

        if path == "/api/accounts/activate":
            eid = (b.get("id") or "").strip()
            if not find_account(eid):
                self._json({"ok": False, "msg": "No such account"}, 400)
                return
            set_pref("ace", eid)
            spawn(lambda: (do_login(), refresh_state()))
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
                set_pref("hz", new_id)
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
            if load_prefs().get("hz") == eid:
                set_pref("hz", "default")
            self._json({"ok": True})
            return

        if path == "/api/hostzop/accounts/activate":
            eid = (b.get("id") or "").strip()
            if not find_hz_account(eid):
                self._json({"ok": False, "msg": "No such account"}, 400)
                return
            set_pref("hz", eid)
            self._json({"ok": True})
            return

        if path == "/api/hostzop/create":
            if not hz_key():
                self._json({"ok": False, "msg": "Add/activate a hostzop account first"}, 400)
                return
            if not begin():
                self._json({"ok": False, "msg": "Busy..."}, 409)
                return
            spawn(job_hz_create, b)
            self._json({"ok": True})
            return

        if path == "/api/hostzop/delete":
            if not hz_key():
                self._json({"ok": False, "msg": "No hostzop account"}, 400)
                return
            if not begin():
                self._json({"ok": False, "msg": "Busy..."}, 409)
                return
            spawn(job_hz_delete, b.get("uuids") or [], bool(b.get("all")))
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
            spawn(job_hz_snapshot, vm_uuid, (b.get("vm_name") or "").strip(),
                  (b.get("name") or "").strip())
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
                set_pref("ex", new_id)
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
            if load_prefs().get("ex") == eid:
                set_pref("ex", "default")
            self._json({"ok": True})
            return

        if path == "/api/excloud/accounts/activate":
            eid = (b.get("id") or "").strip()
            if not find_ex_account(eid):
                self._json({"ok": False, "msg": "No such account"}, 400)
                return
            set_pref("ex", eid)
            self._json({"ok": True})
            return

        if path == "/api/excloud/create":
            if not ex_key():
                self._json({"ok": False, "msg": "Add/activate an excloud account first"}, 400)
                return
            if not begin():
                self._json({"ok": False, "msg": "Busy..."}, 409)
                return
            spawn(job_ex_create, b)
            self._json({"ok": True})
            return

        if path == "/api/excloud/delete":
            if not ex_key():
                self._json({"ok": False, "msg": "No excloud account"}, 400)
                return
            if not begin():
                self._json({"ok": False, "msg": "Busy..."}, 409)
                return
            spawn(job_ex_delete, b.get("ids") or [], bool(b.get("all")))
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
            spawn(job_ex_snapshot, vm_id, (b.get("vm_name") or "").strip(),
                  (b.get("name") or "").strip())
            self._json({"ok": True})
            return

        if path == "/api/gcore/accounts/add":
            label = (b.get("label") or "").strip()
            key = (b.get("apikey") or "").strip()
            pid = str(b.get("project_id") or "").strip()
            rid = str(b.get("region_id") or "").strip()
            note = (b.get("note") or "").strip()
            eid = (b.get("id") or "").strip()
            accts = load_gc_accounts()
            if eid:
                tgt = next((a for a in accts if a.get("id") == eid), None)
                if not tgt or tgt.get("source") != "file":
                    self._json({"ok": False, "msg": "This account is read-only"}, 400)
                    return
                if label:
                    tgt["label"] = label
                if key:
                    tgt["apikey"] = key
                tgt["project_id"] = pid
                tgt["region_id"] = rid
                tgt["note"] = note
                save_gc_accounts(accts)
                new_id = eid
            else:
                if not key:
                    self._json({"ok": False, "msg": "API key required"}, 400)
                    return
                new_id = "gc_" + uuid.uuid4().hex[:8]
                accts.append({"id": new_id, "label": label or "Gcore", "apikey": key,
                              "project_id": pid, "region_id": rid, "note": note,
                              "source": "file"})
                save_gc_accounts(accts)
            if b.get("use"):
                set_pref("gc", new_id)
            self._json({"ok": True, "id": new_id})
            return

        if path == "/api/gcore/accounts/delete":
            eid = (b.get("id") or "").strip()
            accts = load_gc_accounts()
            tgt = next((a for a in accts if a.get("id") == eid), None)
            if not tgt or tgt.get("source") != "file":
                self._json({"ok": False, "msg": "Cannot delete this account"}, 400)
                return
            save_gc_accounts([a for a in accts if a.get("id") != eid])
            if load_prefs().get("gc") == eid:
                set_pref("gc", "default")
            self._json({"ok": True})
            return

        if path == "/api/gcore/accounts/activate":
            eid = (b.get("id") or "").strip()
            if not find_gc_account(eid):
                self._json({"ok": False, "msg": "No such account"}, 400)
                return
            set_pref("gc", eid)
            self._json({"ok": True})
            return

        if path == "/api/gcore/verify":
            self._json(gc.verify((b.get("apikey") or "").strip()))
            return

        if path == "/api/gcore/create":
            if not gc_key():
                self._json({"ok": False, "msg": "Add/activate a gcore account first"}, 400)
                return
            if not begin():
                self._json({"ok": False, "msg": "Busy..."}, 409)
                return
            spawn(job_gc_create, b)
            self._json({"ok": True})
            return

        if path == "/api/gcore/delete":
            if not gc_key():
                self._json({"ok": False, "msg": "No gcore account"}, 400)
                return
            if not begin():
                self._json({"ok": False, "msg": "Busy..."}, 409)
                return
            spawn(job_gc_delete, b.get("ids") or [], bool(b.get("all")))
            self._json({"ok": True})
            return

        if path == "/api/gcore/testproxy":
            self._json(gc.test_proxy(b.get("line") or ""))
            return

        if path == "/api/login":
            spawn(lambda: (do_login(), refresh_state()))
            self._json({"ok": True})
            return

        if path == "/api/refresh":
            spawn(refresh_state)
            self._json({"ok": True})
            return

        self._json({"error": "not found"}, 404)


def main():
    push("CloudWeb Web Manager v2 starting...", "sys")
    try:
        owner = store.boot()
        set_cur(owner)
        push(f"Store ready (persist={'github' if store.sync_enabled() else 'local'}), "
             f"{len(store.list_public())} user(s)", "sys")
    except BaseException as e:
        push(f"store boot error: {e}", "error")
    set_cur(store.owner_uid())
    spawn(lambda: (do_login(), refresh_state()))
    threading.Thread(target=timer_loop, daemon=True).start()
    print("\n" + "=" * 58)
    print("  CloudWeb Web Manager v2")
    print(f"  LOCAL :  http://127.0.0.1:{PORT}")
    print(f"  LOGIN :  http://127.0.0.1:{PORT}/")
    print(f"  ADMIN :  http://127.0.0.1:{PORT}/admin   (password: {store.ADMIN_PASS})")
    print(f"  MASTER PIN:  {PIN}")
    print("=" * 58 + "\n")
    push(f"Admin password: {store.ADMIN_PASS}", "sys")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
