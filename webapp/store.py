"""store.py - multi-user accounts, sessions and persistence.

Users and every user's data live under DATA_ROOT. On Render the disk is
ephemeral, so when GITHUB_TOKEN is set the whole DATA_ROOT tree is mirrored to
a private GitHub repo as one JSON blob (pull on boot, debounced push on change).

Nothing here touches the original scripts; app.py calls into it.
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # repo root
SPOT_DIR = ROOT / "spot"
DATA_ROOT = Path(os.environ.get("ACEWEB_DATA") or (SPOT_DIR / "udata"))
USERS_FILE = DATA_ROOT / "users.json"

PIN = os.environ.get("ACEWEB_PIN", "") or "3315"
ADMIN_PASS = os.environ.get("ADMIN_PASS") or "9794743687"
SECRET = (os.environ.get("ACEWEB_SECRET") or f"{PIN}|{ADMIN_PASS}|cloudweb").encode()

GITHUB_TOKEN = (os.environ.get("GITHUB_TOKEN") or "").strip()
GITHUB_REPO = (os.environ.get("GITHUB_DATA_REPO") or "surajsingh136/cloudweb-data").strip()
GITHUB_PATH = (os.environ.get("GITHUB_DATA_PATH") or "state.json").strip()
GITHUB_BRANCH = (os.environ.get("GITHUB_DATA_BRANCH") or "main").strip()

SESSION_TTL = 60 * 60 * 24 * 14          # 14 days
_LK = threading.RLock()


# ---------------------------------- text helpers ----------------------------------
def _push_console(_):
    pass


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------- password hashing ----------------------------------
def hash_pw(pw, salt=None, iters=120000):
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", (pw or "").encode(), bytes.fromhex(salt), iters)
    return f"pbkdf2${iters}${salt}${dk.hex()}"


def verify_pw(pw, stored):
    try:
        algo, iters, salt, _ = (stored or "").split("$")
        if algo != "pbkdf2":
            return False
        return hmac.compare_digest(stored, hash_pw(pw, salt, int(iters)))
    except Exception:
        return False


# ---------------------------------- sessions (signed cookie) ----------------------------------
def _sign(body):
    return hmac.new(SECRET, body.encode(), hashlib.sha256).hexdigest()


def make_session(uid, ttl=SESSION_TTL):
    exp = int(time.time()) + int(ttl)
    body = f"{uid}.{exp}"
    return f"{body}.{_sign(body)}"


def parse_session(tok):
    try:
        uid, exp, sig = (tok or "").rsplit(".", 2)
        if not hmac.compare_digest(sig, _sign(f"{uid}.{exp}")):
            return None
        if int(exp) < int(time.time()):
            return None
        return uid
    except Exception:
        return None


# ---------------------------------- user records ----------------------------------
def _owner_email():
    return (os.environ.get("OWNER_EMAIL")
            or os.environ.get("ACECLOUD_EMAIL")
            or "owner@cloudweb").strip()


def _blank():
    return {"users": [], "seq": 0}


def load_users():
    with _LK:
        try:
            if USERS_FILE.exists():
                d = json.loads(USERS_FILE.read_text("utf-8"))
                if isinstance(d, dict) and "users" in d:
                    return d
                if isinstance(d, list):          # very old shape
                    return {"users": d, "seq": 0}
        except Exception:
            pass
        return _blank()


def save_users(d):
    with _LK:
        DATA_ROOT.mkdir(parents=True, exist_ok=True)
        USERS_FILE.write_text(json.dumps(d, indent=2), encoding="utf-8")
    _debounced()


def _next_id(d):
    d["seq"] = int(d.get("seq", 0)) + 1
    return f"u{d['seq']:04d}"


def find_user(uid=None, email=None):
    d = load_users()
    for u in d["users"]:
        if uid and u.get("id") == uid:
            return d, u
        if email and (u.get("email") or "").lower() == email.lower():
            return d, u
    return d, None


def ensure_owner():
    """Ensure the owner/admin account exists (keeps the operator's own logins)."""
    d = load_users()
    own = next((u for u in d["users"] if u.get("role") == "admin"), None)
    if not owner_exists(d):
        uid = "owner"
        if any(u.get("id") == uid for u in d["users"]):
            uid = "owner2"
        d["users"].append({
            "id": uid, "email": _owner_email(), "name": "Owner",
            "pw": hash_pw(ADMIN_PASS), "status": "approved", "role": "admin",
            "default_host": "acecloud", "created": _now(),
        })
        save_users(d)
        return uid
    return own["id"] if own else "owner"


def owner_exists(d=None):
    d = d or load_users()
    return any(u.get("role") == "admin" for u in d.get("users", []))


def owner_uid():
    for u in load_users()["users"]:
        if u.get("role") == "admin":
            return u["id"]
    return "owner"


def list_public():
    out = []
    for u in load_users()["users"]:
        out.append({"id": u["id"], "email": u.get("email", ""), "name": u.get("name", ""),
                    "status": u.get("status", "pending"), "role": u.get("role", "user"),
                    "created": u.get("created", ""), "last_login": u.get("last_login", "")})
    return out


def find_by_email(email):
    email = (email or "").strip().lower()
    for u in load_users()["users"]:
        if (u.get("email") or "").strip().lower() == email:
            return u
    return None


def get(uid):
    for u in load_users()["users"]:
        if u.get("id") == uid:
            return u
    return None


def register(email, password, name=""):
    email = (email or "").strip()
    if not email or "@" not in email:
        return {"ok": False, "msg": "Valid email required"}
    if len(password or "") < 6:
        return {"ok": False, "msg": "Password must be at least 6 characters"}
    with _LK:
        d = load_users()
        if any((u.get("email") or "").lower() == email.lower() for u in d["users"]):
            return {"ok": False, "msg": "Email already registered"}
        d["seq"] = int(d.get("seq", 0)) + 1
        uid = f"u{d['seq']:04d}"
        d["users"].append({"id": uid, "email": email, "name": (name or email).strip(),
                           "pw": hash_pw(password), "status": "pending", "role": "user",
                           "default_host": "acecloud", "created": _now()})
        save_users(d)
    sync_now()
    return {"ok": True, "id": uid, "status": "pending"}


def set_status(uid, status):
    with _LK:
        d = load_users()
        for u in d["users"]:
            if u.get("id") == uid:
                u["status"] = status
                save_users(d)
                return {"ok": True}
    return {"ok": False, "msg": "not found"}


def set_password(uid, password):
    if len(password or "") < 6:
        return {"ok": False, "msg": "Password too short"}
    with _LK:
        d = load_users()
        for u in d["users"]:
            if u.get("id") == uid:
                u["pw"] = hash_pw(password)
                save_users(d)
                return {"ok": True}
    return {"ok": False, "msg": "not found"}


def set_default_host(uid, host):
    with _LK:
        d = load_users()
        for u in d["users"]:
            if u.get("id") == uid:
                u["default_host"] = (host or "acecloud").strip()
                save_users(d)
                return {"ok": True, "host": u["default_host"]}
    return {"ok": False}


def default_host(uid):
    u = get(uid) or {}
    return (u.get("default_host") or "acecloud").strip()


def delete(uid):
    if uid == owner_uid():
        return {"ok": False, "msg": "Cannot delete the owner"}
    with _LK:
        d = load_users()
        d["users"] = [u for u in d["users"] if u.get("id") != uid]
        save_users(d)
    return {"ok": True}


def login(email, password):
    """Return {ok, uid, role} or {ok:False, msg}. Admin master password logs in
    the owner even if the email is left blank."""
    email = (email or "").strip()
    if password and password == ADMIN_PASS:
        return {"ok": True, "uid": owner_uid(), "role": "admin"}
    u = find_by_email(email) if email else None
    if not u:
        return {"ok": False, "msg": "No account found for that email"}
    if not verify_pw(password, u.get("pw")):
        return {"ok": False, "msg": "Wrong password"}
    if u.get("status") != "approved":
        return {"ok": False, "msg": "Your account is waiting for admin approval", "status": u.get("status")}
    with _LK:
        d = load_users()
        for x in d["users"]:
            if x.get("id") == u["id"]:
                x["last_login"] = _now()
        save_users(d)
    return {"ok": True, "uid": u["id"], "role": u.get("role", "user")}


# ---------------------------------- per-user data dirs ----------------------------------
def user_dir(uid):
    d = DATA_ROOT / (uid or "_anon")
    d.mkdir(parents=True, exist_ok=True)
    return d


def migrate_legacy(uid):
    """Copy the owner's pre-existing single-user files into their user dir once."""
    dst = user_dir(uid)
    if (dst / ".migrated").exists():
        return
    names = ["accounts.json", "hostzop.json", "excloud.json", "gcore.json",
             "hostzop_snaps.json", "excloud_snaps.json", "gcore_snaps.json",
             "hostzop_pw.json", "auto_timers.json", "spot_creds.json",
             "instance_plans.json", "proxy_spot.txt", "proxy.txt", "windows.txt",
             "hostzop_proxy.txt", "excloud_proxy.txt", "gcore_proxy.txt"]
    for n in names:
        src = SPOT_DIR / n
        if src.exists() and not (dst / n).exists():
            try:
                (dst / n).write_bytes(src.read_bytes())
            except Exception:
                pass
    try:
        (dst / ".migrated").write_text("1", encoding="utf-8")
    except Exception:
        pass


# ---------------------------------- GitHub persistence ----------------------------------
_sync_lock = threading.Lock()
_sync_pending = False
_sync_timer = None


def sync_enabled():
    return bool(GITHUB_TOKEN)


def _collect():
    """DATA_ROOT tree -> {relpath: text} (text only; users.json included)."""
    out = {}
    if not DATA_ROOT.exists():
        return out
    for p in DATA_ROOT.rglob("*"):
        if p.is_file() and p.name != ".migrated":
            try:
                out[str(p.relative_to(DATA_ROOT)).replace("\\", "/")] = p.read_text("utf-8")
            except Exception:
                pass
    return out


def _restore(blob):
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    for rel, text in (blob or {}).items():
        rel = rel.strip("/").replace("\\", "/")
        if ".." in rel:
            continue
        p = DATA_ROOT / rel
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
        except Exception:
            pass


def _gh_headers():
    return {"Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "cloudweb-store"}


def pull():
    """Load remote state into DATA_ROOT. Returns True if the remote existed."""
    if not sync_enabled():
        return False
    import urllib.request
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_PATH}?ref={GITHUB_BRANCH}"
    req = urllib.request.Request(url, headers=_gh_headers())
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode())
        content = base64.b64decode(data.get("content") or "").decode("utf-8")
        _restore(json.loads(content))
        return True
    except Exception as e:
        print(f"[store] pull skipped: {e}")
        return False


def _gh_put(text, message):
    import urllib.request
    base = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_PATH}"
    sha = None
    try:
        req = urllib.request.Request(f"{base}?ref={GITHUB_BRANCH}", headers=_gh_headers())
        with urllib.request.urlopen(req, timeout=20) as r:
            sha = json.loads(r.read().decode()).get("sha")
    except Exception:
        sha = None
    body = {"message": message or f"cloudweb state {_now()}",
            "content": base64.b64encode(text.encode("utf-8")).decode(),
            "branch": GITHUB_BRANCH}
    if sha:
        body["sha"] = sha
    req = urllib.request.Request(base, data=json.dumps(body).encode(),
                                 headers={**_gh_headers(), "Content-Type": "application/json"},
                                 method="PUT")
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status in (200, 201)


def push(message="cloudweb state"):
    if not sync_enabled():
        return False
    with _sync_lock:
        text = json.dumps(_collect(), separators=(",", ":"))
    try:
        return _gh_put(text, message)
    except Exception as e:
        print(f"[store] push failed: {e}")
        return False


def _debounced():
    global _sync_pending, _sync_timer
    with _sync_lock:
        _sync_pending = True
        if _sync_timer:
            return
        _sync_timer = threading.Timer(3.0, _flush)
        _sync_timer.daemon = True
        _sync_timer.start()


def _flush():
    global _sync_timer, _sync_pending
    with _sync_lock:
        _sync_timer = None
        if not _sync_pending:
            return
        _sync_pending = False
    push()


def sync_now():
    """Push immediately (used after a mutating action)."""
    if sync_enabled():
        push()


def boot():
    """Called once at startup: pull remote (or seed it), ensure owner exists."""
    if sync_enabled():
        got = pull()
        if not got:
            push("cloudweb initial state")
    owner = owner_exists()
    uid = ensure_owner()
    migrate_legacy(uid)
    if not owner:
        push("cloudweb: owner seeded")
    return uid
