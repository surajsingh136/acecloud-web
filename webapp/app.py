#!/usr/bin/env python3
"""
AceCloud Web Manager v2
========================
acecloud_gui.py ke SAME features, browser me.
Original scripts (acecloud_spot.py / spot_server.py / dashboard.py) sirf IMPORT
ho rahe hain - unme ek bhi line change nahi hoti.

Run:    python app.py           -> http://127.0.0.1:8787
PIN:    terminal me print hota hai (ya ACEWEB_PIN env set karo)
"""
import base64
import json
import os
import random
import re
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse, parse_qs

WEB_DIR = Path(__file__).resolve().parent
SPOT_DIR = WEB_DIR.parent / "spot"
sys.path.insert(0, str(SPOT_DIR))
sys.path.insert(0, str(SPOT_DIR / "server"))

import acecloud_spot as ac          # original script - untouched
import spot_server as sw            # original script - untouched
from deploy_core import (load_plans, save_plan, plan_timer, fetch_ondemand_price,
                         users_from_txt, FLV_SPEC, REGION_KEYS, EMAIL, PASSWD, CREDS_FILE)

HOST = os.environ.get("ACEWEB_HOST", "0.0.0.0")
PORT = int(os.environ.get("ACEWEB_PORT") or os.environ.get("PORT") or "8787")
PIN = os.environ.get("ACEWEB_PIN", "") or "3315"  # default PIN (env se override ho sakta hai)

MIME = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
        ".js": "application/javascript; charset=utf-8", ".ico": "image/x-icon"}

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

# ============================== app state ==============================
BUSY = False
BUSY_LK = threading.Lock()
STATE = {"logged": False, "balance": None, "instances": [], "creds": {}}


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


# ============================== helpers (GUI jaisa hi) ==============================
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


# ============================== login / state ==============================
def do_login():
    try:
        ac.login(EMAIL, PASSWD)
        sw.login(EMAIL, PASSWD)
        STATE["logged"] = True
        push(f"CONNECTED: {EMAIL}", "ok")
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
                 else (fetch_ondemand_price(size["flavor"]) or size["price"]))
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
            raise ValueError("Base select nahi hua")
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
            push("Kuch nahi bana.", "error")
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
                 else (fetch_ondemand_price(sel["name"]) or sel["price"]))
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
            push("Koi proxy ACTIVE nahi hui.", "error")
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
    push(f"Auto-delete scheduled: {hours}h (server chalta rehna chahiye)", "warn")

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
            push("~Rs 0.34/hr (~Rs 250/month). Create me Base='SNAP:' chuno.", "info")
        else:
            push("Snapshot nahi bani.", "error")
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
            p = s["price"] if is_spot else (fetch_ondemand_price(s["name"]) or s["price"])
            unit = "SPOT" if is_spot else "ON-DEMAND"
            out["sizes"].append(
                f"{s['n']}) {s['name']} - {s['vcpu']}vCPU/{s['ram']}GB - INR {p:,.2f}/hr {unit}")
        out["bases"] = ["Linux (auto)"]
    else:
        sw.REGION = code
        sw.build_region(code)
        is_spot = type_ == "Windows Spot"
        for s in list(sw.SIZES):
            p = s["price"] if is_spot else (fetch_ondemand_price(s["flavor"]) or s["price"])
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

        # static files - no PIN (UI ko load hone do, data API pe protected hai)
        if path == "/":
            self._send((WEB_DIR / "static" / "index.html").read_bytes(), MIME[".html"])
            return
        if path in ("/style.css", "/app.js"):
            fname = path.lstrip("/")
            self._send((WEB_DIR / "static" / fname).read_bytes(), MIME[Path(fname).suffix])
            return

        if not self._authed():
            self._json({"error": "wrong pin"}, 401)
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
        if not self._authed():
            self._json({"error": "wrong pin"}, 401)
            return
        b = self._body()
        path = urlparse(self.path).path

        if path == "/api/create":
            if not STATE["logged"]:
                self._json({"ok": False, "msg": "Login nahi hua"}, 400)
                return
            if not begin():
                self._json({"ok": False, "msg": "Pehla kaam khatam hone do..."}, 409)
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
                # GUI rule: Windows Spot -> spot, Normal Instance -> hourly
                billing = "spot" if t == "Windows Spot" else "hourly"
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
    print(f"  PIN   :  {PIN}   (browser/phone pe pehli baar poochega)")
    print("  Public URL chahiye? tunnel.bat chalao (free Cloudflare)")
    print("=" * 58 + "\n")
    push(f"PIN: {PIN}", "sys")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
