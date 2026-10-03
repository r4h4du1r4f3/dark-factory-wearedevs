"""Tablekeeper Stage 1 — reservations service (stdlib only)."""
from __future__ import annotations

import copy
import json
import os
import re
import secrets
import string
import threading
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from zoneinfo import ZoneInfo

try:
    from passwords import hash_password, verify_password
except ImportError:
    from .passwords import hash_password, verify_password  # type: ignore

LOCK = threading.RLock()
STATE = {
    "users": {},          # user_id -> {id,email,password_hash,display_name}
    "by_email": {},       # lower email -> user
    "tokens": {},         # token -> user_id
    "restaurants": {},    # rid -> restaurant dict
    "restaurant_order": [],
    "reservations": {},   # reference -> reservation full dict
    "idem": {},           # (user_id, method, path, key) -> {"body": canonical, "status": int, "response": dict}
}

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
WEEKDAY_IDX = {d: i for i, d in enumerate(WEEKDAYS)}
BARE_LOCAL = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
HHMM_RE = re.compile(r"^\d{2}:\d{2}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+$")
REF_RE = re.compile(r"^[A-Z0-9]{6,12}$")
DIGITS_RE = re.compile(r"^[0-9]+$")
REF_ALPHABET = string.ascii_uppercase + string.digits


# ---------- time helpers ----------

def parse_bare_local(s):
    if not isinstance(s, str) or not BARE_LOCAL.match(s):
        return None
    try:
        dpart, tpart = s.split("T")
        d = datetime.strptime(dpart, "%Y-%m-%d").date()
        hh, mm = map(int, tpart.split(":"))
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            return None
        return (d, hh, mm)
    except Exception:
        return None


def resolve_first(tzname, d, hh, mm):
    """Resolve wall time to first occurrence aware datetime, or None if gap."""
    try:
        tz = ZoneInfo(tzname)
    except Exception:
        return None
    naive = datetime(d.year, d.month, d.day, hh, mm)
    cands = []
    for fold in (0, 1):
        aware = naive.replace(tzinfo=tz, fold=fold)
        try:
            off = aware.utcoffset()
        except Exception:
            continue
        if off is None:
            continue
        utc_naive = naive - off
        try:
            back = utc_naive.replace(tzinfo=timezone.utc).astimezone(tz)
        except Exception:
            continue
        if (back.year, back.month, back.day, back.hour, back.minute) == (d.year, d.month, d.day, hh, mm):
            cands.append(aware)
    if not cands:
        return None
    cands.sort(key=lambda a: naive - a.utcoffset())
    return cands[0]


def aware_iso(dt):
    return dt.isoformat()


def ends_from_start(start_aware, duration_min):
    tz = start_aware.tzinfo
    try:
        utc = start_aware.astimezone(timezone.utc)
        end_utc = utc + timedelta(minutes=duration_min)
        return end_utc.astimezone(tz)
    except Exception:
        return start_aware + timedelta(minutes=duration_min)


def parse_iso_aware(s):
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            return None
        return dt
    except Exception:
        return None


def utc_now():
    return datetime.now(timezone.utc)


def hhmm_to_min(hhmm):
    h, m = map(int, hhmm.split(":"))
    return h * 60 + m


def valid_hhmm(hhmm):
    if not isinstance(hhmm, str) or not HHMM_RE.match(hhmm):
        return False
    try:
        h, m = map(int, hhmm.split(":"))
        return 0 <= h <= 23 and 0 <= m <= 59
    except Exception:
        return False


def weekday_str(d):
    return WEEKDAYS[d.weekday()]


def get_opening(restaurant, d):
    wd = weekday_str(d)
    for h in restaurant.get("opening_hours", []):
        if h.get("weekday") == wd:
            return h
    return None


def canonical_body(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def new_reference():
    existing = STATE["reservations"]
    for _ in range(100):
        ref = "".join(secrets.choice(REF_ALPHABET) for _ in range(8))
        if ref not in existing:
            return ref
    return "R" + uuid.uuid4().hex[:11].upper().replace("A", "B")[:11]


def reservation_public(r):
    return {
        "reservation_id": r["reservation_id"],
        "reference": r["reference"],
        "restaurant_id": r["restaurant_id"],
        "table_id": r["table_id"],
        "party_size": r["party_size"],
        "status": r["status"],
        "starts_at_local": r["starts_at_local"],
        "starts_at": r["starts_at"],
        "ends_at": r["ends_at"],
        "created_at": r["created_at"],
    }


def intervals_overlap(a_start, a_end, b_start, b_end):
    return a_start < b_end and b_start < a_end


class _HeaderRecorder:
    """Wrap rfile to record raw request bytes consumed during parse_request."""
    def __init__(self, rfile):
        self._rfile = rfile
        self.recorded = bytearray()

    def readline(self, *args, **kwargs):
        data = self._rfile.readline(*args, **kwargs)
        try:
            self.recorded.extend(data)
        except Exception:
            pass
        return data

    def read(self, *args, **kwargs):
        return self._rfile.read(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._rfile, name)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "Tablekeeper/1.0"

    def log_message(self, *args):
        pass

    def parse_request(self):
        orig = self.rfile
        rec = _HeaderRecorder(orig)
        self.rfile = rec
        try:
            ok = BaseHTTPRequestHandler.parse_request(self)
        finally:
            try:
                self.rfile = orig
            except Exception:
                pass
            raw = bytes(rec.recorded)
        # Extract raw Idempotency-Key value (preserve whitespace).
        # Wire format is "Field: value"; clients send "Idempotency-Key: <key>".
        # To distinguish empty ("Idempotency-Key: " -> remainder "") from a
        # single-space key ("Idempotency-Key:   " -> remainder " "), strip
        # exactly one leading SP/HTAB as the delimiter and keep the rest verbatim.
        try:
            self._raw_idem = None
            self._raw_idem_present = False
            if raw:
                parts = raw.split(b"\r\n")
                # parts[0] is request line; headers follow until empty line
                for line in parts[1:]:
                    if line == b"":
                        break
                    if line[:1] in (b" ", b"\t"):
                        continue  # continuation line
                    if b":" not in line:
                        continue
                    name, _, after = line.partition(b":")
                    if name.strip().lower() == b"idempotency-key":
                        try:
                            s = after.decode("iso-8859-1")
                        except Exception:
                            s = ""
                        if s[:1] in (" ", "\t"):
                            s = s[1:]
                        self._raw_idem = s
                        self._raw_idem_present = True
                        break
        except Exception:
            self._raw_idem = None
            self._raw_idem_present = False
        return ok

    # ----- plumbing -----
    def send_json(self, status, payload):
        if status == 204 or payload is None:
            body = b""
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def fail(self, status, code):
        self.send_json(status, {"error": {"code": code, "message": code}})

    def raw_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except Exception:
            length = 0
        if length <= 0:
            return b""
        try:
            return self.rfile.read(length)
        except Exception:
            return b""

    def parse_json_body(self):
        """Returns (ok, value). ok False => unparseable (400). Empty => {}."""
        raw = self.raw_body()
        if not raw:
            return (True, {})
        try:
            v = json.loads(raw)
            return (True, v)
        except Exception:
            return (False, None)

    def auth_user(self):
        h = self.headers.get("Authorization")
        if not h or not h.startswith("Bearer "):
            return None
        tok = h[len("Bearer "):]
        if not tok:
            return None
        with LOCK:
            uid = STATE["tokens"].get(tok)
            if uid is None:
                return None
            return STATE["users"].get(uid)

    def idem_key(self):
        # header name case-insensitive; BaseHTTPRequestHandler headers support get
        k = self.headers.get("Idempotency-Key")
        if k is None:
            # try lowercase variant
            k = self.headers.get("idempotency-key")
        return k

    # ----- routing -----
    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def do_PATCH(self):
        self.route("PATCH")

    def do_PUT(self):
        self.route("PUT")

    def do_DELETE(self):
        self.route("DELETE")

    def route(self, method):
        try:
            parsed = urlparse(self.path)
            path = parsed.path or "/"
            if len(path) > 1 and path.endswith("/"):
                path = path.rstrip("/")
            if path == "":
                path = "/"
            qs = parse_qs(parsed.query, keep_blank_values=True)

            if method == "GET" and path == "/health":
                return self.send_json(200, {"status": "ok"})
            if method == "POST" and path == "/_test/reset":
                return self.handle_reset()
            if method == "GET" and path == "/_test/export":
                return self.handle_export()
            if method == "POST" and path == "/_test/import":
                return self.handle_import()
            if method == "POST" and path == "/auth/signup":
                return self.handle_signup()
            if method == "POST" and path == "/auth/login":
                return self.handle_login()
            if method == "GET" and path == "/restaurants":
                return self.handle_list_restaurants()
            if method == "GET" and path.startswith("/restaurants/"):
                rid = path[len("/restaurants/"):]
                return self.handle_get_restaurant(rid)
            if method == "GET" and path == "/availability":
                return self.handle_availability(qs)
            if method == "POST" and path == "/reservations":
                return self.handle_create_reservation()
            if method == "GET" and path == "/reservations":
                return self.handle_list_reservations()
            if method == "GET" and path.startswith("/reservations/"):
                ref = path[len("/reservations/"):]
                if "/" not in ref:
                    return self.handle_get_reservation(ref)
                return self.fail(404, "not_found")
            if method == "POST" and path.startswith("/reservations/") and path.endswith("/cancel"):
                mid = path[len("/reservations/"):-len("/cancel")]
                if mid.endswith("/"):
                    mid = mid[:-1]
                if "/" in mid or not mid:
                    return self.fail(404, "not_found")
                return self.handle_cancel(mid)
            if method == "PATCH" and path.startswith("/reservations/"):
                ref = path[len("/reservations/"):]
                if "/" in ref or not ref:
                    return self.fail(404, "not_found")
                return self.handle_patch(ref)
            if method == "POST" and path == "/reservation-moves":
                return self.handle_moves()
            return self.fail(404, "not_found")
        except BrokenPipeError:
            pass
        except Exception:
            try:
                self.fail(500, "internal")
            except Exception:
                pass

    # ----- auth -----
    def handle_signup(self):
        ok, data = self.parse_json_body()
        if not ok:
            return self.fail(400, "malformed_request")
        if not isinstance(data, dict):
            return self.fail(400, "malformed_request")
        email = data.get("email")
        password = data.get("password")
        display = data.get("display_name")
        if email is None or password is None or display is None:
            return self.fail(422, "validation_failed")
        if not isinstance(email, str) or not isinstance(password, str) or not isinstance(display, str):
            return self.fail(400, "malformed_request")
        if not EMAIL_RE.match(email):
            return self.fail(422, "validation_failed")
        if len(password) < 8:
            return self.fail(422, "validation_failed")
        if len(email) > 320 or len(display) > 256:
            return self.fail(422, "validation_failed")
        with LOCK:
            if email.lower() in STATE["by_email"]:
                return self.fail(409, "email_taken")
        hashed = hash_password(password)
        with LOCK:
            if email.lower() in STATE["by_email"]:
                return self.fail(409, "email_taken")
            uid = "u_" + uuid.uuid4().hex[:12]
            user = {"id": uid, "email": email, "password": hashed, "display_name": display}
            STATE["users"][uid] = user
            STATE["by_email"][email.lower()] = user
            tok = secrets.token_hex(32)
            STATE["tokens"][tok] = uid
        return self.send_json(201, {"user_id": uid, "display_name": display, "token": tok})

    def handle_login(self):
        ok, data = self.parse_json_body()
        if not ok:
            return self.fail(400, "malformed_request")
        if not isinstance(data, dict):
            return self.fail(400, "malformed_request")
        if "email" not in data or "password" not in data:
            return self.fail(422, "validation_failed")
        email = data.get("email")
        password = data.get("password")
        if not isinstance(email, str) or not isinstance(password, str):
            return self.fail(400, "malformed_request")
        with LOCK:
            user = STATE["by_email"].get(email.lower())
            stored = None if user is None else user["password"]
            uid = None if user is None else user["id"]
            disp = None if user is None else user["display_name"]
        if user is None or not verify_password(password, stored):
            return self.fail(401, "unauthenticated")
        with LOCK:
            # user may have been reset away; re-check
            if uid not in STATE["users"]:
                return self.fail(401, "unauthenticated")
            tok = secrets.token_hex(32)
            STATE["tokens"][tok] = uid
        return self.send_json(200, {"user_id": uid, "display_name": disp, "token": tok})

    # ----- reset -----
    def handle_reset(self):
        ok, data = self.parse_json_body()
        if not ok:
            return self.fail(400, "malformed_request")
        if not isinstance(data, dict):
            return self.fail(422, "validation_failed")
        err = self.validate_and_apply_fixture(data)
        if err is not None:
            return self.fail(422, "validation_failed")
        return self.send_json(204, None)

    def validate_and_apply_fixture(self, fx):
        # returns None on success, "err" on failure (without changing state)
        users = fx.get("users", [])
        restaurants = fx.get("restaurants", [])
        reservations = fx.get("reservations", [])
        if "users" in fx and not isinstance(users, list):
            return "err"
        if "restaurants" in fx and not isinstance(restaurants, list):
            return "err"
        if "reservations" in fx and not isinstance(reservations, list):
            return "err"
        # validate users
        seen_uids = set()
        seen_emails = set()
        for u in users:
            if not isinstance(u, dict):
                return "err"
            for f in ("id", "email", "password", "display_name"):
                if f not in u:
                    return "err"
            uid, email, pw, disp = u["id"], u["email"], u["password"], u["display_name"]
            if not isinstance(uid, str) or not (1 <= len(uid) <= 64):
                return "err"
            if not isinstance(email, str) or not EMAIL_RE.match(email):
                return "err"
            if not isinstance(pw, str):
                return "err"
            if not isinstance(disp, str):
                return "err"
            if uid in seen_uids:
                return "err"
            seen_uids.add(uid)
            el = email.lower()
            if el in seen_emails:
                return "err"
            seen_emails.add(el)
        # validate restaurants
        seen_rids = set()
        rest_map = {}
        for r in restaurants:
            if not isinstance(r, dict):
                return "err"
            for f in ("id", "name", "timezone", "slot_minutes", "reservation_duration_minutes",
                      "cancellation_cutoff_minutes", "opening_hours", "tables"):
                if f not in r:
                    return "err"
            rid = r["id"]
            if not isinstance(rid, str) or not (1 <= len(rid) <= 64):
                return "err"
            if rid in seen_rids:
                return "err"
            seen_rids.add(rid)
            if not isinstance(r["name"], str):
                return "err"
            if not isinstance(r["timezone"], str):
                return "err"
            try:
                ZoneInfo(r["timezone"])
            except Exception:
                return "err"
            for f in ("slot_minutes", "reservation_duration_minutes"):
                v = r[f]
                if type(v) is not int or v <= 0:
                    return "err"
            cc = r["cancellation_cutoff_minutes"]
            if type(cc) is not int or cc < 0:
                return "err"
            oh = r["opening_hours"]
            if not isinstance(oh, list):
                return "err"
            seen_wd = set()
            for h in oh:
                if not isinstance(h, dict):
                    return "err"
                for f in ("weekday", "opens", "closes"):
                    if f not in h:
                        return "err"
                if h["weekday"] not in WEEKDAYS:
                    return "err"
                if h["weekday"] in seen_wd:
                    return "err"
                seen_wd.add(h["weekday"])
                if not valid_hhmm(h["opens"]) or not valid_hhmm(h["closes"]):
                    return "err"
                if hhmm_to_min(h["closes"]) <= hhmm_to_min(h["opens"]):
                    return "err"
            tables = r["tables"]
            if not isinstance(tables, list):
                return "err"
            seen_tids = set()
            for t in tables:
                if not isinstance(t, dict):
                    return "err"
                for f in ("id", "capacity"):
                    if f not in t:
                        return "err"
                tid = t["id"]
                if not isinstance(tid, str) or not (1 <= len(tid) <= 64):
                    return "err"
                if tid in seen_tids:
                    return "err"
                seen_tids.add(tid)
                if type(t["capacity"]) is not int or t["capacity"] < 1:
                    return "err"
            rest_map[rid] = r
        # validate reservations (seeded)
        seen_ids = set()
        seen_refs = set()
        # build user id set
        user_ids = {u["id"] for u in users}
        parsed_seeds = []  # (reservation dict to store, start_aware, end_aware)
        for s in reservations:
            if not isinstance(s, dict):
                return "err"
            for f in ("id", "reference", "user_id", "restaurant_id", "table_id",
                      "starts_at_local", "party_size"):
                if f not in s:
                    return "err"
            sid = s["id"]
            ref = s["reference"]
            if not isinstance(sid, str) or not (1 <= len(sid) <= 64):
                return "err"
            if not isinstance(ref, str) or not REF_RE.match(ref):
                return "err"
            if sid in seen_ids or ref in seen_refs:
                return "err"
            seen_ids.add(sid)
            seen_refs.add(ref)
            if s["user_id"] not in user_ids:
                return "err"
            rid = s["restaurant_id"]
            if rid not in rest_map:
                return "err"
            rest = rest_map[rid]
            tid = s["table_id"]
            if not isinstance(tid, str):
                return "err"
            tmap = {t["id"]: t for t in rest["tables"]}
            if tid not in tmap:
                return "err"
            ps = s["party_size"]
            if type(ps) is not int or ps < 1:
                return "err"
            if ps > tmap[tid]["capacity"]:
                return "err"
            sal = s["starting_at_local"] if "starting_at_local" in s else s.get("starts_at_local")
            # support only starts_at_local
            if not isinstance(sal, str) or parse_bare_local(sal) is None:
                return "err"
            parsed = parse_bare_local(sal)
            d, hh, mm = parsed
            aware = resolve_first(rest["timezone"], d, hh, mm)
            if aware is None:
                return "err"
            # outside/grid checks (wall)
            opening = get_opening(rest, d)
            if opening is None:
                return "err"
            opens_min = hhmm_to_min(opening["opens"])
            closes_min = hhmm_to_min(opening["closes"])
            start_min = hh * 60 + mm
            dur = rest["reservation_duration_minutes"]
            if start_min < opens_min or start_min + dur > closes_min:
                return "err"
            if (start_min - opens_min) % rest["slot_minutes"] != 0:
                return "err"
            end_aware = ends_from_start(aware, dur)
            parsed_seeds.append((s, aware, end_aware))
        # overlap among seeds
        for i in range(len(parsed_seeds)):
            si, ai_s, ai_e = parsed_seeds[i]
            for j in range(i + 1, len(parsed_seeds)):
                sj, aj_s, aj_e = parsed_seeds[j]
                if si["restaurant_id"] == sj["restaurant_id"] and si["table_id"] == sj["table_id"]:
                    if intervals_overlap(ai_s, ai_e, aj_s, aj_e):
                        return "err"
        # all valid — apply under lock
        with LOCK:
            STATE["users"] = {}
            STATE["by_email"] = {}
            STATE["tokens"] = {}
            STATE["restaurants"] = {}
            STATE["restaurant_order"] = []
            STATE["reservations"] = {}
            STATE["idem"] = {}
            for u in users:
                nu = {"id": u["id"], "email": u["email"],
                      "password": hash_password(u["password"]),
                      "display_name": u["display_name"]}
                STATE["users"][nu["id"]] = nu
                STATE["by_email"][nu["email"].lower()] = nu
            for r in restaurants:
                # deep copy fixture shape
                rc = copy.deepcopy(r)
                STATE["restaurants"][rc["id"]] = rc
                STATE["restaurant_order"].append(rc["id"])
            now_s = utc_now().isoformat()
            for (s, aware, end_aware) in parsed_seeds:
                rest = STATE["restaurants"][s["restaurant_id"]]
                full = {
                    "reservation_id": s["id"],
                    "reference": s["reference"],
                    "restaurant_id": s["restaurant_id"],
                    "table_id": s["table_id"],
                    "party_size": s["party_size"],
                    "status": "confirmed",
                    "starts_at_local": s["starts_at_local"],
                    "starts_at": aware_iso(aware),
                    "ends_at": aware_iso(end_aware),
                    "created_at": now_s,
                    "user_id": s["user_id"],
                }
                STATE["reservations"][s["reference"]] = full
        return None

    # ----- export/import -----
    def handle_export(self):
        with LOCK:
            snap = {
                "users": copy.deepcopy(list(STATE["users"].values())),
                "restaurants": copy.deepcopy([STATE["restaurants"][rid] for rid in STATE["restaurant_order"]]),
                "reservations": copy.deepcopy(list(STATE["reservations"].values())),
                "tokens": copy.deepcopy(STATE["tokens"]),
                "idem": copy.deepcopy([
                    {"user_id": k[0], "method": k[1], "path": k[2], "key": k[3],
                     "body": v["body"], "status": v["status"], "response": v["response"]}
                    for k, v in STATE["idem"].items()
                ]),
            }
        return self.send_json(200, {"track": "tablekeeper", "format_version": 1, "state": snap})

    def handle_import(self):
        ok, data = self.parse_json_body()
        if not ok:
            return self.fail(400, "malformed_request")
        if not isinstance(data, dict):
            return self.fail(400, "malformed_request")
        if data.get("track") != "tablekeeper" or data.get("format_version") != 1 or "state" not in data:
            return self.fail(422, "validation_failed")
        st = data.get("state")
        if not isinstance(st, dict):
            return self.fail(422, "validation_failed")
        # validate state shape
        try:
            users = st.get("users")
            restaurants = st.get("restaurants")
            reservations = st.get("reservations")
            tokens = st.get("tokens")
            idem = st.get("idem")
            if not isinstance(users, list) or not isinstance(restaurants, list) \
                    or not isinstance(reservations, list) or not isinstance(tokens, dict) \
                    or not isinstance(idem, list):
                return self.fail(422, "validation_failed")
            # light validation of contents
            for u in users:
                if not isinstance(u, dict):
                    return self.fail(422, "validation_failed")
                for f in ("id", "email", "password", "display_name"):
                    if f not in u:
                        return self.fail(422, "validation_failed")
            for r in restaurants:
                if not isinstance(r, dict) or "id" not in r:
                    return self.fail(422, "validation_failed")
            for rv in reservations:
                if not isinstance(rv, dict):
                    return self.fail(422, "validation_failed")
                for f in ("reservation_id", "reference", "restaurant_id", "table_id",
                          "party_size", "status", "starts_at_local", "starts_at",
                          "ends_at", "created_at", "user_id"):
                    if f not in rv:
                        return self.fail(422, "validation_failed")
            for e in idem:
                if not isinstance(e, dict):
                    return self.fail(422, "validation_failed")
                for f in ("user_id", "method", "path", "key", "body", "status", "response"):
                    if f not in e:
                        return self.fail(422, "validation_failed")
        except Exception:
            return self.fail(422, "validation_failed")
        with LOCK:
            new_users = {u["id"]: copy.deepcopy(u) for u in users}
            new_by = {}
            for u in users:
                el = u["email"].lower() if isinstance(u.get("email"), str) else None
                if el is None:
                    return self.fail(422, "validation_failed")
                new_by[el] = new_users[u["id"]]
            new_rest = {}
            new_order = []
            for r in restaurants:
                new_rest[r["id"]] = copy.deepcopy(r)
                new_order.append(r["id"])
            new_res = {}
            for rv in reservations:
                new_res[rv["reference"]] = copy.deepcopy(rv)
            new_tokens = copy.deepcopy(tokens)
            new_idem = {}
            for e in idem:
                new_idem[(e["user_id"], e["method"], e["path"], e["key"])] = {
                    "body": e["body"], "status": e["status"], "response": copy.deepcopy(e["response"])}
            STATE["users"] = new_users
            STATE["by_email"] = new_by
            STATE["restaurants"] = new_rest
            STATE["restaurant_order"] = new_order
            STATE["reservations"] = new_res
            STATE["tokens"] = new_tokens
            STATE["idem"] = new_idem
        return self.send_json(204, None)

    # ----- restaurants -----
    def handle_list_restaurants(self):
        with LOCK:
            out = [{"id": STATE["restaurants"][rid]["id"],
                    "name": STATE["restaurants"][rid]["name"],
                    "timezone": STATE["restaurants"][rid]["timezone"]}
                   for rid in STATE["restaurant_order"]]
        return self.send_json(200, {"restaurants": out})

    def handle_get_restaurant(self, rid):
        with LOCK:
            r = STATE["restaurants"].get(rid)
            if r is None:
                return self.fail(404, "not_found")
            rc = copy.deepcopy(r)
        return self.send_json(200, rc)

    def handle_availability(self, qs):
        def one(k):
            v = qs.get(k)
            if v is None or len(v) == 0:
                return None
            return v[0]
        rid = one("restaurant_id")
        date_s = one("date")
        party_s = one("party_size")
        if rid is None or date_s is None or party_s is None:
            return self.fail(422, "validation_failed")
        with LOCK:
            rest = STATE["restaurants"].get(rid)
        if rest is None:
            return self.fail(404, "not_found")
        if not isinstance(date_s, str) or not DATE_RE.match(date_s):
            return self.fail(422, "validation_failed")
        try:
            d = datetime.strptime(date_s, "%Y-%m-%d").date()
        except Exception:
            return self.fail(422, "validation_failed")
        if not isinstance(party_s, str) or not DIGITS_RE.match(party_s):
            return self.fail(422, "validation_failed")
        try:
            party = int(party_s)
        except Exception:
            return self.fail(422, "validation_failed")
        if party < 1:
            return self.fail(422, "validation_failed")
        tzname = rest["timezone"]
        opening = get_opening(rest, d)
        if opening is None:
            return self.send_json(200, {"restaurant_id": rid, "date": date_s,
                                       "timezone": tzname, "slots": []})
        opens_min = hhmm_to_min(opening["opens"])
        closes_min = hhmm_to_min(opening["closes"])
        step = rest["slot_minutes"]
        dur = rest["reservation_duration_minutes"]
        tmap_order = [t["id"] for t in rest["tables"]]
        tcap = {t["id"]: t["capacity"] for t in rest["tables"]}
        with LOCK:
            confirmed = [r for r in STATE["reservations"].values()
                         if r["restaurant_id"] == rid and r["status"] == "confirmed"]
        slots = []
        t = opens_min
        while t + dur <= closes_min:
            hh, mm = divmod(t, 60)
            local_s = f"{date_s}T{hh:02d}:{mm:02d}"
            aware = resolve_first(tzname, d, hh, mm)
            if aware is None:
                t += step
                continue
            slot_start = aware
            slot_end = ends_from_start(aware, dur)
            avail = []
            for tid in tmap_order:
                if tcap[tid] < party:
                    continue
                busy = False
                for r in confirmed:
                    if r["table_id"] != tid:
                        continue
                    rs = parse_iso_aware(r["starts_at"])
                    re_ = parse_iso_aware(r["ends_at"])
                    if rs is None or re_ is None:
                        continue
                    if intervals_overlap(slot_start, slot_end, rs, re_):
                        busy = True
                        break
                if not busy:
                    avail.append(tid)
            slots.append({"starts_at_local": local_s,
                          "starts_at": aware_iso(slot_start),
                          "available_table_ids": avail})
            t += step
        return self.send_json(200, {"restaurant_id": rid, "date": date_s,
                                   "timezone": tzname, "slots": slots})

    # ----- reservations -----
    def check_idem(self, user_id, method, path):
        # Prefer raw header value to preserve whitespace-only keys.
        # BaseHTTPRequestHandler strips whitespace-only values to "", so use
        # the raw bytes captured in parse_request: absent -> missing, "" ->
        # missing, 1..255 (including whitespace-only) -> valid, >255 -> invalid.
        if getattr(self, "_raw_idem_present", False):
            k = self._raw_idem
            if k is None or k == "":
                return ("missing", None, None)
            if len(k) > 255:
                return ("invalid", k, None)
            return ("ok", k, None)
        # Fallback when raw capture unavailable (should not happen normally).
        k = self.idem_key()
        if k is None or k == "":
            return ("missing", None, None)
        if len(k) > 255:
            return ("invalid", k, None)
        return ("ok", k, None)

    def handle_create_reservation(self):
        user = self.auth_user()
        if user is None:
            return self.fail(401, "unauthenticated")
        ok, data = self.parse_json_body()
        if not ok:
            return self.fail(400, "malformed_request")
        if not isinstance(data, dict):
            return self.fail(400, "malformed_request")
        ks, key, _ = self.check_idem(user["id"], "POST", "/reservations")
        if ks == "missing":
            return self.fail(400, "missing_idempotency_key")
        if ks == "invalid":
            return self.fail(422, "validation_failed")
        canon = canonical_body(data)
        with LOCK:
            slot = STATE["idem"].get((user["id"], "POST", "/reservations", key))
            if slot is not None:
                if slot["body"] == canon:
                    return self.send_json(200, copy.deepcopy(slot["response"]))
                else:
                    return self.fail(409, "idempotency_key_reuse")
            # validate + create
            res = self.validate_booking_fields(data, None)
            if isinstance(res, tuple) and len(res) == 2 and isinstance(res[0], int):
                # error (status, code)
                status, code = res
                return self.fail(status, code)
            # res is (restaurant, table, d, hh, mm, aware, end_aware, party)
            restaurant, table, d, hh, mm, aware, end_aware, party = res
            # overlap check
            rs = aware
            re_ = end_aware
            for r in STATE["reservations"].values():
                if r["status"] != "confirmed":
                    continue
                if r["restaurant_id"] != restaurant["id"] or r["table_id"] != table["id"]:
                    continue
                es = parse_iso_aware(r["starts_at"])
                ee = parse_iso_aware(r["ends_at"])
                if es is None or ee is None:
                    continue
                if intervals_overlap(rs, re_, es, ee):
                    return self.fail(409, "table_unavailable")
            # create
            rid_out = "res_" + uuid.uuid4().hex[:12]
            ref = new_reference()
            now_s = utc_now().isoformat()
            full = {
                "reservation_id": rid_out,
                "reference": ref,
                "restaurant_id": restaurant["id"],
                "table_id": table["id"],
                "party_size": party,
                "status": "confirmed",
                "starts_at_local": data["starts_at_local"],
                "starts_at": aware_iso(aware),
                "ends_at": aware_iso(end_aware),
                "created_at": now_s,
                "user_id": user["id"],
            }
            STATE["reservations"][ref] = full
            pub = reservation_public(full)
            STATE["idem"][(user["id"], "POST", "/reservations", key)] = {
                "body": canon, "status": 201, "response": copy.deepcopy(pub)}
            return self.send_json(201, pub)

    def validate_booking_fields(self, data, existing_restaurant=None):
        """Validate POST/PATCH resulting fields. Returns (status,code) on error
        or (restaurant, table, d, hh, mm, aware, end_aware, party)."""
        # required
        for f in ("restaurant_id", "table_id", "starts_at_local", "party_size"):
            if f not in data:
                return (422, "validation_failed")
        rid = data.get("restaurant_id")
        tid = data.get("table_id")
        sal = data.get("starts_at_local")
        party = data.get("party_size")
        # types
        if not isinstance(rid, str) or not isinstance(tid, str) or not isinstance(sal, str):
            # party special-cased below; for these three wrong type => 400
            # but need to distinguish party: check party first for special?
            # If rid/tid/sal wrong type => 400 (unless sal string-invalid handled later)
            if not isinstance(rid, str) or not isinstance(tid, str):
                return (400, "malformed_request")
            if not isinstance(sal, str):
                return (400, "malformed_request")
        if len(rid) > 64 or len(tid) > 64:
            return (422, "validation_failed")
        # party: any non-int (incl str/bool/float) => 422
        if type(party) is not int or party < 1:
            return (422, "validation_failed")
        # starts_at_local format: must be bare => 422 (special, takes precedence over 400)
        parsed = parse_bare_local(sal)
        if parsed is None:
            return (422, "validation_failed")
        d, hh, mm = parsed
        restaurant = STATE["restaurants"].get(rid)
        if restaurant is None:
            return (404, "not_found")
        tmap = {t["id"]: t for t in restaurant["tables"]}
        table = tmap.get(tid)
        if table is None:
            return (404, "not_found")
        aware = resolve_first(restaurant["timezone"], d, hh, mm)
        if aware is None:
            return (422, "invalid_local_time")
        opening = get_opening(restaurant, d)
        if opening is None:
            return (422, "outside_opening_hours")
        opens_min = hhmm_to_min(opening["opens"])
        closes_min = hhmm_to_min(opening["closes"])
        start_min = hh * 60 + mm
        dur = restaurant["reservation_duration_minutes"]
        if start_min < opens_min or start_min + dur > closes_min:
            return (422, "outside_opening_hours")
        if (start_min - opens_min) % restaurant["slot_minutes"] != 0:
            return (422, "not_on_slot_grid")
        if party > table["capacity"]:
            return (422, "party_exceeds_capacity")
        end_aware = ends_from_start(aware, dur)
        return (restaurant, table, d, hh, mm, aware, end_aware, party)

    def handle_list_reservations(self):
        user = self.auth_user()
        if user is None:
            return self.fail(401, "unauthenticated")
        with LOCK:
            mine = [r for r in STATE["reservations"].values() if r["user_id"] == user["id"]]
        # sort starts_at descending
        def sk(r):
            dt = parse_iso_aware(r["starts_at"])
            return dt if dt is not None else datetime.min.replace(tzinfo=timezone.utc)
        mine.sort(key=sk, reverse=True)
        return self.send_json(200, {"reservations": [reservation_public(r) for r in mine]})

    def handle_get_reservation(self, ref):
        user = self.auth_user()
        if user is None:
            return self.fail(401, "unauthenticated")
        with LOCK:
            r = STATE["reservations"].get(ref)
            if r is None or r["user_id"] != user["id"]:
                return self.fail(404, "not_found")
            pub = reservation_public(r)
        return self.send_json(200, pub)

    def handle_cancel(self, ref):
        user = self.auth_user()
        if user is None:
            return self.fail(401, "unauthenticated")
        with LOCK:
            r = STATE["reservations"].get(ref)
            if r is None or r["user_id"] != user["id"]:
                return self.fail(404, "not_found")
            if r["status"] == "cancelled":
                return self.send_json(200, reservation_public(r))
            rest = STATE["restaurants"].get(r["restaurant_id"])
            if rest is None:
                return self.fail(404, "not_found")
            start = parse_iso_aware(r["starts_at"])
            if start is None:
                return self.fail(500, "internal")
            now = utc_now()
            cutoff = rest["cancellation_cutoff_minutes"]
            if now >= start - timedelta(minutes=cutoff):
                return self.fail(409, "cutoff_passed")
            r["status"] = "cancelled"
            pub = reservation_public(r)
            return self.send_json(200, pub)

    def handle_patch(self, ref):
        user = self.auth_user()
        if user is None:
            return self.fail(401, "unauthenticated")
        ok, data = self.parse_json_body()
        if not ok:
            return self.fail(400, "malformed_request")
        if not isinstance(data, dict):
            return self.fail(400, "malformed_request")
        with LOCK:
            r = STATE["reservations"].get(ref)
            if r is None or r["user_id"] != user["id"]:
                return self.fail(404, "not_found")
            if r["status"] == "cancelled":
                return self.fail(409, "reservation_cancelled")
            rest = STATE["restaurants"].get(r["restaurant_id"])
            if rest is None:
                return self.fail(404, "not_found")
            cur_start = parse_iso_aware(r["starts_at"])
            if cur_start is None:
                return self.fail(500, "internal")
            if utc_now() >= cur_start - timedelta(minutes=rest["cancellation_cutoff_minutes"]):
                return self.fail(409, "cutoff_passed")
            # build new values
            new_tid = r["table_id"]
            new_sal = r["starts_at_local"]
            new_party = r["party_size"]
            if "table_id" in data:
                v = data["table_id"]
                if not isinstance(v, str):
                    return self.fail(400, "malformed_request")
                if len(v) > 64:
                    return self.fail(422, "validation_failed")
                new_tid = v
            if "starts_at_local" in data:
                v = data["starts_at_local"]
                if not isinstance(v, str):
                    return self.fail(400, "malformed_request")
                # format check -> 422
                if parse_bare_local(v) is None:
                    return self.fail(422, "validation_failed")
                new_sal = v
            if "party_size" in data:
                v = data["party_size"]
                if type(v) is not int or v < 1:
                    return self.fail(422, "validation_failed")
                new_party = v
            # validate resulting (reuse logic but with restaurant fixed)
            tmap = {t["id"]: t for t in rest["tables"]}
            if new_tid not in tmap:
                return self.fail(404, "not_found")
            table = tmap[new_tid]
            parsed = parse_bare_local(new_sal)
            if parsed is None:
                return self.fail(422, "validation_failed")
            d, hh, mm = parsed
            aware = resolve_first(rest["timezone"], d, hh, mm)
            if aware is None:
                return self.fail(422, "invalid_local_time")
            opening = get_opening(rest, d)
            if opening is None:
                return self.fail(422, "outside_opening_hours")
            opens_min = hhmm_to_min(opening["opens"])
            closes_min = hhmm_to_min(opening["closes"])
            start_min = hh * 60 + mm
            dur = rest["reservation_duration_minutes"]
            if start_min < opens_min or start_min + dur > closes_min:
                return self.fail(422, "outside_opening_hours")
            if (start_min - opens_min) % rest["slot_minutes"] != 0:
                return self.fail(422, "not_on_slot_grid")
            if new_party > table["capacity"]:
                return self.fail(422, "party_exceeds_capacity")
            end_aware = ends_from_start(aware, dur)
            # overlap excluding self
            for o in STATE["reservations"].values():
                if o["reference"] == ref or o["status"] != "confirmed":
                    continue
                if o["restaurant_id"] != rest["id"] or o["table_id"] != new_tid:
                    continue
                es = parse_iso_aware(o["starts_at"])
                ee = parse_iso_aware(o["ends_at"])
                if es is None or ee is None:
                    continue
                if intervals_overlap(aware, end_aware, es, ee):
                    return self.fail(409, "table_unavailable")
            r["table_id"] = new_tid
            r["starts_at_local"] = new_sal
            r["party_size"] = new_party
            r["starts_at"] = aware_iso(aware)
            r["ends_at"] = aware_iso(end_aware)
            return self.send_json(200, reservation_public(r))

    # ----- moves -----
    def handle_moves(self):
        user = self.auth_user()
        if user is None:
            return self.fail(401, "unauthenticated")
        ok, data = self.parse_json_body()
        if not ok:
            return self.fail(400, "malformed_request")
        if not isinstance(data, dict):
            return self.fail(422, "validation_failed")
        ks, key, _ = self.check_idem(user["id"], "POST", "/reservation-moves")
        if ks == "missing":
            return self.fail(400, "missing_idempotency_key")
        if ks == "invalid":
            return self.fail(422, "validation_failed")
        canon = canonical_body(data)
        with LOCK:
            slot = STATE["idem"].get((user["id"], "POST", "/reservation-moves", key))
            if slot is not None:
                if slot["body"] == canon:
                    return self.send_json(200, copy.deepcopy(slot["response"]))
                else:
                    return self.fail(409, "idempotency_key_reuse")
            # validate shape
            moves = data.get("moves")
            if not isinstance(moves, list) or not (1 <= len(moves) <= 8):
                return self.fail(422, "validation_failed")
            refs = []
            for m in moves:
                if not isinstance(m, dict):
                    return self.fail(422, "validation_failed")
                ref = m.get("reference")
                if not isinstance(ref, str):
                    return self.fail(422, "validation_failed")
                refs.append(ref)
            if len(set(refs)) != len(refs):
                return self.fail(422, "validation_failed")
            # lookup
            bookings = []
            for ref in refs:
                r = STATE["reservations"].get(ref)
                if r is None or r["user_id"] != user["id"]:
                    return self.fail(404, "not_found")
                bookings.append(r)
            rids = {b["restaurant_id"] for b in bookings}
            if len(rids) > 1:
                return self.fail(422, "validation_failed")
            rid = next(iter(rids))
            rest = STATE["restaurants"].get(rid)
            if rest is None:
                return self.fail(404, "not_found")
            tmap = {t["id"]: t for t in rest["tables"]}
            # per-booking non-occupancy checks in input order
            planned = []  # (booking, new_tid, new_sal, new_party, aware, end_aware)
            for idx, m in enumerate(moves):
                b = bookings[idx]
                if b["status"] == "cancelled":
                    return self.fail(409, "reservation_cancelled")
                cur_start = parse_iso_aware(b["starts_at"])
                if cur_start is None:
                    return self.fail(500, "internal")
                if utc_now() >= cur_start - timedelta(minutes=rest["cancellation_cutoff_minutes"]):
                    return self.fail(409, "cutoff_passed")
                new_tid = b["table_id"]
                new_sal = b["starts_at_local"]
                new_party = b["party_size"]
                if "table_id" in m:
                    v = m["table_id"]
                    if not isinstance(v, str):
                        return self.fail(400, "malformed_request")
                    if len(v) > 64:
                        return self.fail(422, "validation_failed")
                    new_tid = v
                if "starts_at_local" in m:
                    v = m["starts_at_local"]
                    if not isinstance(v, str):
                        return self.fail(400, "malformed_request")
                    if parse_bare_local(v) is None:
                        return self.fail(422, "validation_failed")
                    new_sal = v
                if "party_size" in m:
                    v = m["party_size"]
                    if type(v) is not int or v < 1:
                        return self.fail(422, "validation_failed")
                    new_party = v
                if new_tid not in tmap:
                    return self.fail(404, "not_found")
                table = tmap[new_tid]
                parsed = parse_bare_local(new_sal)
                if parsed is None:
                    return self.fail(422, "validation_failed")
                d, hh, mm = parsed
                aware = resolve_first(rest["timezone"], d, hh, mm)
                if aware is None:
                    return self.fail(422, "invalid_local_time")
                opening = get_opening(rest, d)
                if opening is None:
                    return self.fail(422, "outside_opening_hours")
                opens_min = hhmm_to_min(opening["opens"])
                closes_min = hhmm_to_min(opening["closes"])
                start_min = hh * 60 + mm
                dur = rest["reservation_duration_minutes"]
                if start_min < opens_min or start_min + dur > closes_min:
                    return self.fail(422, "outside_opening_hours")
                if (start_min - opens_min) % rest["slot_minutes"] != 0:
                    return self.fail(422, "not_on_slot_grid")
                if new_party > table["capacity"]:
                    return self.fail(422, "party_exceeds_capacity")
                end_aware = ends_from_start(aware, dur)
                planned.append((b, new_tid, new_sal, new_party, aware, end_aware))
            # occupancy: among resulting + with unlisted
            listed_refs = set(refs)
            # among resulting
            for i in range(len(planned)):
                for j in range(i + 1, len(planned)):
                    if planned[i][1] != planned[j][1]:
                        continue
                    if intervals_overlap(planned[i][4], planned[i][5], planned[j][4], planned[j][5]):
                        return self.fail(409, "table_unavailable")
            # with unlisted
            for (b, nt, ns, np_, aw, ew) in planned:
                for o in STATE["reservations"].values():
                    if o["reference"] in listed_refs:
                        continue
                    if o["status"] != "confirmed":
                        continue
                    if o["restaurant_id"] != rid or o["table_id"] != nt:
                        continue
                    es = parse_iso_aware(o["starts_at"])
                    ee = parse_iso_aware(o["ends_at"])
                    if es is None or ee is None:
                        continue
                    if intervals_overlap(aw, ew, es, ee):
                        return self.fail(409, "table_unavailable")
            # commit
            for (b, nt, ns, np_, aw, ew) in planned:
                b["table_id"] = nt
                b["starts_at_local"] = ns
                b["party_size"] = np_
                b["starts_at"] = aware_iso(aw)
                b["ends_at"] = aware_iso(ew)
            resp = {"reservations": [reservation_public(bookings[i]) for i in range(len(bookings))]}
            # bookings list holds refs to STATE objects; public snapshot is current
            resp_copy = copy.deepcopy(resp)
            STATE["idem"][(user["id"], "POST", "/reservation-moves", key)] = {
                "body": canon, "status": 201, "response": resp_copy}
            return self.send_json(201, resp_copy)


class Server(ThreadingHTTPServer):
    request_queue_size = 256
    daemon_threads = True
    allow_reuse_address = True


def main():
    port = int(os.environ.get("PORT", "8080"))
    print(f"tablekeeper listening on 0.0.0.0:{port}", flush=True)
    Server(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
