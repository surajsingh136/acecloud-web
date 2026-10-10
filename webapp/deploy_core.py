"""deploy_core.py - Linux/cloud-safe replacement for the pure functions that
app.py used to import from dashboard.py (which hardcodes Windows paths + tkinter).

Original files are NOT modified.
"""
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import acecloud_spot as ac
import spot_server as sw

SPOT_DIR = Path(__file__).resolve().parent.parent / "spot"
SERVER_DIR = SPOT_DIR / "server"

EMAIL = os.environ.get("ACECLOUD_EMAIL", "")
PASSWD = os.environ.get("ACECLOUD_PASSWD", "")
CREDS_FILE = Path(os.environ.get("ACECLOUD_CREDS_FILE", str(SPOT_DIR / "spot_creds.json")))
PLANS_FILE = SPOT_DIR / "instance_plans.json"

FLV_SPEC = {
    "W.C3a.large": (2, 4), "W.C3a.xlarge": (4, 8), "W.C3a.2xlarge": (8, 16),
    "W.C3a.4xlarge": (16, 32), "W.C3a.8xlarge": (32, 64),
    "C4a.medium": (1, 2), "C4a.large": (2, 4), "C4a.xlarge": (4, 8),
    "C4a.2xlarge": (8, 16), "C4a.4xlarge": (16, 32),
}

REGION_KEYS = {v["label"]: k for k, v in sw.WREGIONS.items()}


def load_plans():
    try:
        with open(PLANS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_plan(names, hours):
    try:
        plans = load_plans()
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for nm in names:
            plans[nm] = {"created": now, "hours": int(hours)}
        with open(PLANS_FILE, "w", encoding="utf-8") as f:
            json.dump(plans, f, indent=2)
    except Exception:
        pass


def plan_timer(plans, name, created_api):
    try:
        pl = plans.get(name) or {}
        created = pl.get("created") or created_api
        if not created:
            return "plan: -"
        dt = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        used = max((now - dt).total_seconds() / 3600.0, 0)
        hours = pl.get("hours")
        if not hours:
            return f"used {used:.1f}h | end ?"
        end = dt + timedelta(hours=float(hours))
        end_loc = end.astimezone().strftime("%H:%M")
        return f"{int(hours)}h | used {used:.1f}h | ~{end_loc}"
    except Exception:
        return "plan: -"


def fetch_ondemand_price(flavor_name):
    try:
        rows = ac.api_data("GET", "cloud/pricing",
                           params={"resource": "instances", "is_gpu": "false",
                                   "is_spot": "false"}) or []
        for x in rows:
            if (x.get("resource_name") or "") == flavor_name:
                return x.get("price_per_hour")
    except Exception:
        pass
    return None


def users_from_txt():
    out = {}
    f = SERVER_DIR / "spot_server.txt"
    if not f.exists():
        return out
    cur = {}
    for line in f.read_text("utf-8", "replace").splitlines():
        line = line.strip()
        m = re.match(r"^([\d.]+):(\d+):([^:]+):(.+)$", line)
        if m:
            ip, port, user, pwd = m.groups()
            cur = {"user": user, "pass": pwd, "port": port, "source": "spot_server.txt"}
            out.setdefault(ip, cur)
        elif "console:" in line and cur:
            cur["console"] = line.split("console:", 1)[1].strip()
        elif "verified:" in line.lower() and cur:
            cur["verified"] = "YES" in line.upper()
    return out
