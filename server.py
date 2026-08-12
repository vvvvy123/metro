#!/usr/bin/env python3
"""
Metro Transfer — backend API (stdlib only, no pip install).

    python import_city.py --all --reset      # build metro.db first
    python server.py                          # http://localhost:8000

Zero third-party dependencies: http.server + sqlite3 + json. Runs on any
machine with Python 3.8+. CORS is wide open so the static frontend
(web/index.html, even opened from file://) can call it directly.

No login: identity is an anonymous per-device id sent in the `X-Device-Id`
header (or `device_id` in the body). Each device maps to one `user` row
(email column stores `device:<id>`), which keeps "one answer / one vote per
user per transfer" working without accounts.
"""
import json
import re
import uuid
import math
import os
import sys
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import db as dbx
from db import fold, like_pattern, search_key

ROOT = os.path.dirname(os.path.abspath(__file__))
DB_PATH = dbx.DB_PATH
# Localhost only. This was "0.0.0.0", which exposed the dev backend — and the
# real metro.db behind it — to every device on the LAN, with no authentication
# on any write endpoint. Making HOST/PORT/DB_PATH configurable via env is still
# on the pre-deploy list; this is just the safe default.
HOST, PORT = "127.0.0.1", 8000


# fold() / like_pattern() / search_key() moved to db.py: the stored search key must
# be computed identically by this file and by import_city.py. The JS twin of fold()
# is still in web/index.html - change one, change the other.
#
# create_function("fold", 1, fold) went with them: Postgres has no equivalent, so
# searchable rows carry a pre-folded search_fold column instead, which also removes
# 600-8500 Python callbacks per keystroke. The unused like() helper is dropped too.


def db():
    return dbx.connect()


# --------------------------------------------------------------------------
# Ranking  (PRD 十二)  Score = likeRate × heat × timeDecay
# --------------------------------------------------------------------------
def _days_since(iso):
    try:
        y, m, d = map(int, iso[:10].split("-"))
        return max(0, (date.today() - date(y, m, d)).days)
    except Exception:
        return 0


def _time_decay(days):
    if days <= 7:   return 1.00
    if days <= 30:  return 0.98
    if days <= 90:  return 0.95
    if days <= 180: return 0.90
    if days <= 365: return 0.80
    if days <= 730: return 0.60
    return 0.50


def score(likes, dislikes, updated_at):
    like_rate = likes / (likes + dislikes + 5)
    heat = math.log(likes + dislikes + 2)
    return like_rate * heat * _time_decay(_days_since(updated_at))


def sort_answers(answers):
    answers.sort(key=lambda a: a["score"], reverse=True)
    out = answers[:]
    i = 0
    while i < len(out) - 1:
        a, b = out[i], out[i + 1]
        hi = max(a["score"], b["score"]) or 1
        if abs(a["score"] - b["score"]) / hi < 0.03 and \
                _days_since(a["updated_at"]) > _days_since(b["updated_at"]):
            out[i], out[i + 1] = b, a
            i = max(0, i - 1)
        else:
            i += 1
    return out


def lines_by_station(conn, station_ids):
    """station_id -> [line dicts], in ONE query.

    This used to be a per-station query inside station_json(): listing Paris ran
    552 statements for 551 stations. On local SQLite that is only ~11 ms, but the
    deployed shape is a Vercel function talking to Postgres over the network, where
    551 sequential round trips is seconds, not milliseconds.

    Scoped to the matched station ids rather than the whole city on purpose: a
    narrow search matching one station should not drag in every line link in
    Beijing (which measured slower than listing all of Paris).
    """
    ids = list(station_ids or [])
    if not ids:
        return {}
    ph = ",".join(["?"] * len(ids))
    rows = conn.execute(
        f"""SELECT sl.station_id AS sid, l.id, l.name, l.color, l.name_en
              FROM line l JOIN station_line sl ON sl.line_id = l.id
             WHERE sl.station_id IN ({ph})
             ORDER BY l.name COLLATE BINARY """, ids).fetchall()
    out = {}
    for r in rows:
        out.setdefault(r["sid"], []).append(
            {"id": r["id"], "name": r["name"], "color": r["color"], "name_en": r["name_en"]})
    return out


def station_json(r, lines):
    return {"id": r["id"], "name_cn": r["name_cn"], "name_en": r["name_en"],
            "alias": json.loads(r["alias"] or "[]"),
            "lines": lines or []}


def slug(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


# --------------------------------------------------------------------------
# Input limits (every write endpoint is public and unauthenticated)
# --------------------------------------------------------------------------
class HttpError(Exception):
    """A failure whose message is safe to show the client. Anything else becomes a
    generic 500 — the old blanket `f"server error: {e}"` handed out table names,
    column names and constraint names to anyone who sent a malformed body."""

    def __init__(self, msg, code=400):
        super().__init__(msg)
        self.msg, self.code = msg, code


MAX_BODY = 64 * 1024          # the largest legitimate payload here is a few KB
MAX_LIST = 40                 # directions per line, lines per station, per request

# Per-field character limits. Unbounded text was storable, and a single 5 MB
# description made the read endpoint for that transfer return 7 MB to EVERY visitor.
LIMITS = {"name": 60, "name_cn": 60, "name_en": 120, "nickname": 24,
          "description": 1000, "custom_text": 60, "content": 500,
          "country_id": 32, "timezone": 64, "line_id": 64, "alias_item": 60}

_COLOR_OK = re.compile(r"^#[0-9a-fA-F]{3,8}$")


def clean_color(v):
    """Allowlist, not sanitisation. `color` is interpolated raw into a style="..."
    attribute in three places in the frontend, so an unvalidated value here is
    stored XSS reachable by one unauthenticated POST. Anything unexpected becomes
    the default rather than an error, so ordinary clients never break."""
    v = (v or "").strip()
    return v if _COLOR_OK.match(v) else "#4b5563"


def clean_text(body, key, required=False, default="", limit=None):
    v = body.get(key, default)
    if v is None:
        v = default
    if not isinstance(v, str):
        raise HttpError(f"{key} must be a string")
    v = v.strip()
    # SQLite stores an embedded NUL happily; psycopg rejects it outright, so a row
    # written before the port would become unmigratable.
    if "\x00" in v:
        raise HttpError(f"{key} contains a NUL byte")
    cap = limit or LIMITS.get(key, 200)
    if len(v) > cap:
        raise HttpError(f"{key} too long (max {cap} characters)")
    if required and not v:
        raise HttpError(f"{key} is required")
    return v


def clean_alias(body):
    v = body.get("alias", [])
    if v in (None, ""):
        return []
    if not isinstance(v, list):
        raise HttpError("alias must be a list")
    if len(v) > MAX_LIST:
        raise HttpError(f"too many alias entries (max {MAX_LIST})")
    out = []
    for item in v:
        if not isinstance(item, str):
            raise HttpError("alias entries must be strings")
        item = item.strip()
        if "\x00" in item:
            raise HttpError("alias contains a NUL byte")
        if len(item) > LIMITS["alias_item"]:
            raise HttpError(f"alias entry too long (max {LIMITS['alias_item']})")
        if item:
            out.append(item)
    return out


def clean_list(body, key, limit=None):
    v = body.get(key, [])
    if v in (None, ""):
        return []
    if not isinstance(v, list):
        raise HttpError(f"{key} must be a list")
    if len(v) > MAX_LIST:
        raise HttpError(f"too many {key} entries (max {MAX_LIST})")
    out = []
    for item in v:
        if not isinstance(item, str):
            raise HttpError(f"{key} entries must be strings")
        item = item.strip()
        if "\x00" in item:
            raise HttpError(f"{key} contains a NUL byte")
        if len(item) > (limit or LIMITS.get(key, 64)):
            raise HttpError(f"{key} entry too long")
        if item:
            out.append(item)
    return out


# Per-device, per-day write caps. NOT authentication — the device id is
# self-asserted and free to rotate — just a speed bump that makes scripted abuse
# cost something. Recorded in write_log because a serverless instance cannot hold
# a counter in memory.
QUOTA = {"answer": 30, "vote": 300, "comment": 60,
         "city": 3, "station": 30, "line": 15, "direction": 30, "link": 60}


class Handler(BaseHTTPRequestHandler):
    server_version = "MetroTransfer/2.1"
    # Without this, a client that opens a socket and dribbles (or never sends the
    # body it promised) pins a thread indefinitely — ThreadingHTTPServer spawns
    # them without limit.
    timeout = 15

    def _fail(self, e):
        """Known failures keep their message; unknown ones are logged server-side
        and reported generically, so internals never reach the client."""
        if isinstance(e, HttpError):
            return self._err(e.msg, e.code)
        print(f"[error] {self.command} {self.path}: {e!r}", file=sys.stderr, flush=True)
        self._err("server error", 500)

    # ---- low level ----
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,DELETE,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type,X-Device-Id")

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _err(self, msg, code=400):
        self._json({"error": msg}, code)

    def _body(self):
        raw = self.headers.get("Content-Length")
        try:
            n = int(raw or 0)
        except (TypeError, ValueError):
            raise HttpError("bad Content-Length")
        if n < 0:
            # read(-1) used to mean "read until EOF", so one socket held a thread
            # open for as long as the client cared to keep it.
            raise HttpError("bad Content-Length")
        if n > MAX_BODY:
            raise HttpError(f"request body too large (max {MAX_BODY} bytes)", 413)
        if not n:
            return {}
        # Read in chunks and never preallocate from a client-supplied number: a
        # header-only request claiming 3 GB used to make rfile.read(n) reserve 2 GB
        # of RSS before a single body byte arrived.
        buf = bytearray()
        while len(buf) < n:
            chunk = self.rfile.read(min(n - len(buf), 32768))
            if not chunk:
                break
            buf += chunk
        try:
            body = json.loads(bytes(buf).decode("utf-8"))
        except Exception:
            raise HttpError("body is not valid JSON")
        if not isinstance(body, dict):
            raise HttpError("body must be a JSON object")
        return body

    def _quota_check(self, conn, kind, body=None):
        """Require a device id, and refuse past the daily cap. Returns the device."""
        dev = self._device(body)
        if not dev:
            raise HttpError("missing device id (X-Device-Id)")
        if len(dev) > 128:
            raise HttpError("device id too long")
        used = conn.execute(
            "SELECT COUNT(*) c FROM write_log WHERE device=? AND day=? AND kind=?",
            (dev, date.today().isoformat(), kind)).fetchone()["c"]
        if used >= QUOTA.get(kind, 50):
            raise HttpError(f"daily limit reached for {kind} ({QUOTA.get(kind, 50)})", 429)
        return dev

    def _quota_used(self, conn, dev, kind):
        conn.execute("INSERT INTO write_log (device, kind, day) VALUES (?,?,?)",
                     (dev, kind, date.today().isoformat()))

    def _device(self, body=None):
        return self.headers.get("X-Device-Id") or (body or {}).get("device_id")

    def _uid_lookup(self, conn, body=None):
        """Existing user id for this device, or None (read-only, no insert)."""
        dev = self._device(body)
        if not dev:
            return None
        row = conn.execute("SELECT id FROM app_user WHERE email=?", ("device:" + dev,)).fetchone()
        return row["id"] if row else None

    def _uid_ensure(self, conn, body=None):
        """User id for this device, creating the row if needed. None if no device."""
        dev = self._device(body)
        if not dev:
            return None
        email = "device:" + dev
        nick = (body or {}).get("nickname") or "匿名用户"
        row = conn.execute("SELECT id FROM app_user WHERE email=?", (email,)).fetchone()
        if row:
            if (body or {}).get("nickname"):
                conn.execute("UPDATE app_user SET nickname=? WHERE id=?", (nick, row["id"]))
                conn.commit()
            return row["id"]
        uid = dbx.insert_id(conn, "INSERT INTO app_user (email, nickname) VALUES (?,?)", (email, nick))
        conn.commit()
        return uid

    def log_message(self, *a):
        pass

    def _db(self):
        """One connection per request, always closed by the do_* handlers below.

        Nothing used to close or roll back: an exception part-way through a write
        left an open transaction on a connection that only CPython's cyclic GC
        reclaimed (exception -> traceback -> frame -> connection), and until it
        did, EVERY other write failed with 'database is locked' after burning the
        full 5s busy timeout. It did not recover on a timer.
        """
        if getattr(self, "_conn", None) is None:
            self._conn = db()
        return self._conn

    def _close_db(self):
        conn = getattr(self, "_conn", None)
        self._conn = None
        dbx.finish(conn)   # rolls back; closes on SQLite, keeps the socket on Postgres

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        u = urlparse(self.path)
        try:
            self.route_get(u.path.rstrip("/"), parse_qs(u.query))
        except Exception as e:
            self._fail(e)
        finally:
            self._close_db()

    def do_POST(self):
        p = urlparse(self.path).path.rstrip("/")
        try:
            self.route_post(p, self._body())
        except Exception as e:
            self._fail(e)
        finally:
            self._close_db()

    def do_DELETE(self):
        # This had no try/except: an exception escaped into socketserver and the
        # client got NO response at all — indistinguishable from a dropped
        # connection, so the UI could not tell "failed" from "deleted".
        try:
            self.route_delete(urlparse(self.path).path.rstrip("/"))
        except Exception as e:
            self._fail(e)
        finally:
            self._close_db()

    def route_delete(self, p):
        m = re.match(r"^/api/answers/(\d+)$", p)
        if not m:
            return self._err("not found", 404)
        conn = self._db()
        # int(), not the raw regex string: answer.id is an INTEGER column, and
        # Postgres rejects `WHERE id = <text>` outright ("operator does not exist:
        # integer = text") where SQLite quietly coerced it. The sibling routes
        # already did int(); only this one was left.
        aid = int(m.group(1))
        uid = self._uid_lookup(conn)
        row = conn.execute("SELECT user_id FROM answer WHERE id=?", (aid,)).fetchone()
        if not row:
            return self._err("not found", 404)
        if uid is None or row["user_id"] != uid:
            return self._err("not your answer", 403)
        conn.execute("UPDATE answer SET is_deleted=1 WHERE id=?", (aid,))
        conn.commit()
        self._json({"ok": True})

    # ---- GET ----
    def route_get(self, p, q):
        conn = self._db()

        if p == "/api/health":
            n = conn.execute("SELECT COUNT(*) c FROM city").fetchone()["c"]
            return self._json({"ok": True, "cities": n})

        # COLLATE BINARY pins byte order on both engines. SQLite orders TEXT by UTF-8
        # bytes; Postgres would use its own collation and silently reorder CJK.
        if p == "/api/cities":
            qq = like_pattern(q.get("q", [""])[0])
            rows = conn.execute(
                """SELECT id, name_cn, name_en, alias FROM city
                   WHERE search_fold LIKE ? ESCAPE '\\' ORDER BY name_cn COLLATE BINARY """,
                (qq,)).fetchall()
            return self._json([{"id": r["id"], "name_cn": r["name_cn"], "name_en": r["name_en"],
                                "alias": json.loads(r["alias"] or "[]")} for r in rows])

        m = re.match(r"^/api/cities/([^/]+)/stations$", p)
        if m:
            qq = like_pattern(q.get("q", [""])[0])
            rows = conn.execute(
                """SELECT id, name_cn, name_en, alias FROM station
                   WHERE city_id=? AND search_fold LIKE ? ESCAPE '\\'
                   ORDER BY name_cn COLLATE BINARY """, (m.group(1), qq)).fetchall()
            by_st = lines_by_station(conn, [r["id"] for r in rows])
            return self._json([station_json(r, by_st.get(r["id"])) for r in rows])

        m = re.match(r"^/api/cities/([^/]+)/popular$", p)
        if m:
            return self._popular(conn, m.group(1), q)

        # Explicit projections, not `dict(row)` over `SELECT *`: that shipped
        # city_id/system_id/created_at to the client, and a timestamp column would
        # make json.dumps raise. The frontend only reads id/name/color.
        m = re.match(r"^/api/cities/([^/]+)/lines$", p)
        if m:
            rows = conn.execute(
                """SELECT id, city_id, system_id, name, name_en, color FROM line
                   WHERE city_id=? ORDER BY name COLLATE BINARY """, (m.group(1),)).fetchall()
            return self._json([dict(r) for r in rows])

        m = re.match(r"^/api/stations/([^/]+)/lines$", p)
        if m:
            rows = conn.execute(
                """SELECT l.id, l.city_id, l.system_id, l.name, l.name_en, l.color
                   FROM line l JOIN station_line sl ON sl.line_id=l.id
                   WHERE sl.station_id=? ORDER BY l.name COLLATE BINARY """, (m.group(1),)).fetchall()
            return self._json([dict(r) for r in rows])

        m = re.match(r"^/api/lines/([^/]+)/directions$", p)
        if m:
            rows = conn.execute("SELECT id, name FROM direction WHERE line_id=? ORDER BY ordinal", (m.group(1),)).fetchall()
            return self._json([{"id": r["id"], "name": r["name"]} for r in rows])

        if p == "/api/answers":
            return self._answers(conn, q)

        self._err("not found", 404)

    def _popular(self, conn, cid, q):
        """热门换乘站 — community heat first, interchange degree as filler.

        1. stations that actually have experiences, ranked by
           (answers, likes, comments) desc — real community signal;
        2. if fewer than `limit`, top up with the city's biggest interchanges
           (>=2 lines) by line count desc, so the section is never sparse.
        Read-only; every station keeps the /stations payload shape plus counters.
        """
        try:
            limit = max(1, min(50, int(q.get("limit", ["6"])[0])))
        except (TypeError, ValueError):
            limit = 6

        hot = conn.execute(
            """SELECT s.id AS sid,
                      COUNT(DISTINCT a.id) AS answers,
                      COUNT(DISTINCT CASE WHEN v.type='LIKE' THEN v.id END) AS likes,
                      COUNT(DISTINCT c.id) AS comments
                 FROM station s
                 JOIN transfer t ON t.station_id = s.id
                 JOIN answer a ON a.transfer_id = t.id AND a.is_deleted = 0
                 LEFT JOIN vote v ON v.answer_id = a.id
                 LEFT JOIN comment c ON c.answer_id = a.id
                WHERE s.city_id = ?
                GROUP BY s.id
                ORDER BY answers DESC, likes DESC, comments DESC, s.name_cn COLLATE BINARY
                LIMIT ?""", (cid, limit)).fetchall()
        picked = [dict(r) for r in hot]
        seen = {r["sid"] for r in picked}

        if len(picked) < limit:
            fill = conn.execute(
                """SELECT s.id AS sid, COUNT(sl.line_id) AS n
                     FROM station s JOIN station_line sl ON sl.station_id = s.id
                    WHERE s.city_id = ?
                    GROUP BY s.id HAVING COUNT(sl.line_id) >= 2
                    ORDER BY n DESC, s.name_cn COLLATE BINARY
                    LIMIT ?""", (cid, limit * 4)).fetchall()
            # HAVING repeats COUNT(...) instead of the select alias `n`: SQLite
            # accepts the alias, Postgres does not (HAVING is evaluated before the
            # select list). And this branch only runs when a city has fewer
            # answered stations than `limit` — i.e. on a fresh DB it is the normal
            # path, while a well-seeded dev DB never reaches it.
            for r in fill:
                if r["sid"] in seen:
                    continue
                picked.append({"sid": r["sid"], "answers": 0, "likes": 0, "comments": 0})
                seen.add(r["sid"])
                if len(picked) >= limit:
                    break

        ids = [r["sid"] for r in picked]
        if not ids:
            return self._json([])
        ph = ",".join(["?"] * len(ids))
        srows = {r["id"]: r for r in conn.execute(
            f"SELECT id, name_cn, name_en, alias FROM station WHERE id IN ({ph})", ids).fetchall()}
        by_st = lines_by_station(conn, ids)
        out = []
        for r in picked:
            row = srows.get(r["sid"])
            if not row:
                continue
            item = station_json(row, by_st.get(r["sid"]))
            item.update(answers=r["answers"], likes=r["likes"], comments=r["comments"])
            out.append(item)
        self._json(out)

    def _answers(self, conn, q):
        g = lambda k: q.get(k, [""])[0]
        row = conn.execute(
            """SELECT id FROM transfer WHERE station_id=? AND from_line_id=?
               AND from_dir_id=? AND to_line_id=? AND to_dir_id=?""",
            (g("station"), g("from_line"), g("from_dir"), g("to_line"), g("to_dir"))).fetchone()
        if not row:
            return self._json([])
        uid = self._uid_lookup(conn)
        arows = conn.execute(
            """SELECT id, user_id, position_type, car_number, custom_text, description,
                      is_anon, version, updated_at
                 FROM answer WHERE transfer_id=? AND is_deleted=0""", (row["id"],)).fetchall()
        if not arows:
            return self._json([])

        # Batched instead of 5 queries per answer.
        aids = [a["id"] for a in arows]
        ph = ",".join(["?"] * len(aids))
        tally = {}
        for r in conn.execute(
                f"SELECT answer_id, type, COUNT(*) c FROM vote WHERE answer_id IN ({ph}) "
                f"GROUP BY answer_id, type", aids).fetchall():
            tally.setdefault(r["answer_id"], {})[r["type"]] = r["c"]
        nicks = {r["id"]: r["nickname"] for r in conn.execute(
            f"SELECT id, nickname FROM app_user WHERE id IN "
            f"({','.join(['?'] * len(set(a['user_id'] for a in arows)))})",
            list({a["user_id"] for a in arows})).fetchall()}
        cby = {}
        for c in conn.execute(
                f"""SELECT c.answer_id, c.content, c.created_at, u.nickname FROM comment c
                    JOIN app_user u ON u.id=c.user_id WHERE c.answer_id IN ({ph})
                    ORDER BY c.created_at DESC""", aids).fetchall():
            cby.setdefault(c["answer_id"], []).append(c)
        mine = {}
        if uid:
            mine = {r["answer_id"]: r["type"] for r in conn.execute(
                f"SELECT answer_id, type FROM vote WHERE user_id=? AND answer_id IN ({ph})",
                [uid] + aids).fetchall()}

        answers = []
        for a in arows:
            likes = tally.get(a["id"], {}).get("LIKE", 0)
            dislikes = tally.get(a["id"], {}).get("DISLIKE", 0)
            user = {"nickname": nicks.get(a["user_id"])} if a["user_id"] in nicks else None
            comments = cby.get(a["id"], [])
            my = mine.get(a["id"])
            answers.append({
                "id": a["id"], "position_type": a["position_type"], "car_number": a["car_number"],
                "custom_text": a["custom_text"], "description": a["description"], "is_anon": bool(a["is_anon"]),
                # Anonymity has to hold in the payload, not just in the UI: this
                # used to ship the real nickname alongside is_anon=true, so
                # anyone reading the API could de-anonymise every contributor.
                "author": ("匿名" if a["is_anon"] else (user["nickname"] if user else "匿名")),
                "is_mine": uid == a["user_id"],
                "version": a["version"], "updated_at": a["updated_at"][:10],
                "likes": likes, "dislikes": dislikes, "my_vote": my,
                "score": round(score(likes, dislikes, a["updated_at"]), 4),
                "comments": [{"user": c["nickname"], "time": c["created_at"][:10], "text": c["content"]} for c in comments],
            })
        self._json(sort_answers(answers))

    # ---- POST ----
    def route_post(self, p, b):
        conn = self._db()

        if p == "/api/cities":
            dev = self._quota_check(conn, "city", b)
            cn = clean_text(b, "name_cn", required=True)
            en = clean_text(b, "name_en")
            # de-dupe by CN or EN (case-insensitive) so 巴黎 / Paris never double up
            dup = conn.execute(
                """SELECT id, name_cn, name_en FROM city
                   WHERE lower(name_cn)=? OR (name_en!='' AND lower(name_en)=?)""",
                (cn.lower(), en.lower())).fetchone()
            if dup:
                return self._json({"id": dup["id"], "name_cn": dup["name_cn"], "name_en": dup["name_en"]}, 200)
            cid = slug("city")
            alias = json.dumps(clean_alias(b), ensure_ascii=False)
            # search_fold must be written by every insert path, or the new row is
            # created unsearchable (and, if the column were nullable, invisible).
            conn.execute(
                """INSERT INTO city (id, country_id, name_cn, name_en, alias, timezone, search_fold)
                   VALUES (?,?,?,?,?,?,?)""",
                (cid, clean_text(b, "country_id") or None, cn, en, alias,
                 clean_text(b, "timezone", default="UTC") or "UTC",
                 search_key(cid, cn, en, alias)))
            self._quota_used(conn, dev, "city")
            conn.commit()
            return self._json({"id": cid, "name_cn": cn, "name_en": en}, 201)

        m = re.match(r"^/api/cities/([^/]+)/stations$", p)
        if m:
            dev = self._quota_check(conn, "station", b)
            cn = clean_text(b, "name_cn", required=True)
            en = clean_text(b, "name_en")
            sid = slug("st")
            alias = json.dumps(clean_alias(b), ensure_ascii=False)
            conn.execute(
                """INSERT INTO station (id, city_id, name_cn, name_en, alias, search_fold)
                   VALUES (?,?,?,?,?,?)""",
                (sid, m.group(1), cn, en, alias, search_key(cn, en, alias)))
            for lid in clean_list(b, "lines", limit=64):
                dbx.insert_ignore(conn, "station_line", {"station_id": sid, "line_id": lid},
                                  conflict=("station_id", "line_id"))
            self._quota_used(conn, dev, "station")
            conn.commit()
            row = conn.execute(
                "SELECT id, name_cn, name_en, alias FROM station WHERE id=?", (sid,)).fetchone()
            return self._json(station_json(row, lines_by_station(conn, [sid]).get(sid)), 201)

        m = re.match(r"^/api/cities/([^/]+)/lines$", p)
        if m:
            dev = self._quota_check(conn, "line", b)
            lid = slug("line")
            conn.execute("INSERT INTO line (id, city_id, name, name_en, color) VALUES (?,?,?,?,?)",
                         (lid, m.group(1), clean_text(b, "name", required=True),
                          clean_text(b, "name_en"), clean_color(b.get("color"))))
            for i, dname in enumerate(clean_list(b, "directions", limit=LIMITS["name"])):
                conn.execute("INSERT INTO direction (id, line_id, name, ordinal) VALUES (?,?,?,?)",
                             (slug("dir"), lid, dname, i))
            self._quota_used(conn, dev, "line")
            conn.commit()
            return self._json(dict(conn.execute(
                "SELECT id, city_id, system_id, name, name_en, color FROM line WHERE id=?",
                (lid,)).fetchone()), 201)

        m = re.match(r"^/api/stations/([^/]+)/lines$", p)
        if m:
            dev = self._quota_check(conn, "link", b)
            dbx.insert_ignore(conn, "station_line",
                              {"station_id": m.group(1),
                               "line_id": clean_text(b, "line_id", required=True)},
                              conflict=("station_id", "line_id"))
            self._quota_used(conn, dev, "link")
            conn.commit()
            return self._json({"ok": True}, 201)

        m = re.match(r"^/api/lines/([^/]+)/directions$", p)
        if m:
            dev = self._quota_check(conn, "direction", b)
            name = clean_text(b, "name", required=True)
            did = slug("dir")
            n = conn.execute("SELECT COUNT(*) c FROM direction WHERE line_id=?", (m.group(1),)).fetchone()["c"]
            conn.execute("INSERT INTO direction (id, line_id, name, ordinal) VALUES (?,?,?,?)",
                         (did, m.group(1), name, n))
            self._quota_used(conn, dev, "direction")
            conn.commit()
            return self._json({"id": did, "name": name}, 201)

        if p == "/api/answers":
            return self._post_answer(conn, b)

        m = re.match(r"^/api/answers/(\d+)/vote$", p)
        if m:
            return self._vote(conn, int(m.group(1)), b)

        m = re.match(r"^/api/answers/(\d+)/comments$", p)
        if m:
            aid = int(m.group(1))
            dev = self._quota_check(conn, "comment", b)
            # existence check: commenting on a missing or deleted answer used to hit
            # a FK violation and surface as a 500 instead of a 404
            if not conn.execute("SELECT 1 FROM answer WHERE id=? AND is_deleted=0",
                                (aid,)).fetchone():
                raise HttpError("answer not found", 404)
            uid = self._uid_ensure(conn, b)
            if uid is None:
                raise HttpError("missing device id")
            txt = clean_text(b, "content", required=True)
            conn.execute("INSERT INTO comment (answer_id, user_id, content) VALUES (?,?,?)",
                         (aid, uid, txt))
            self._quota_used(conn, dev, "comment")
            conn.commit()
            return self._json({"ok": True}, 201)

        self._err("not found", 404)

    def _post_answer(self, conn, b):
        dev = self._quota_check(conn, "answer", b)
        uid = self._uid_ensure(conn, b)
        if uid is None:
            raise HttpError("missing device id")
        keys = ("station", "from_line", "from_dir", "to_line", "to_dir")
        ref = {k: clean_text(b, k, required=True, limit=64) for k in keys}
        # The 5 ids must actually exist and belong together, otherwise the insert
        # below trips a foreign key and surfaces as an opaque 500.
        if not conn.execute("SELECT 1 FROM station WHERE id=?", (ref["station"],)).fetchone():
            raise HttpError("unknown station", 404)
        for lk, dk in (("from_line", "from_dir"), ("to_line", "to_dir")):
            if not conn.execute(
                    "SELECT 1 FROM station_line WHERE station_id=? AND line_id=?",
                    (ref["station"], ref[lk])).fetchone():
                raise HttpError(f"{ref[lk]} is not a line of that station", 400)
            if not conn.execute("SELECT 1 FROM direction WHERE id=? AND line_id=?",
                                (ref[dk], ref[lk])).fetchone():
                raise HttpError(f"{ref[dk]} is not a direction of {ref[lk]}", 400)

        row = conn.execute(
            """SELECT id FROM transfer WHERE station_id=? AND from_line_id=?
               AND from_dir_id=? AND to_line_id=? AND to_dir_id=?""",
            tuple(ref[k] for k in keys)).fetchone()
        if row:
            tid = row["id"]
        else:
            tid = dbx.insert_id(
                conn,
                """INSERT INTO transfer (station_id, from_line_id, from_dir_id, to_line_id, to_dir_id)
                   VALUES (?,?,?,?,?)""", tuple(ref[k] for k in keys))

        ptype = b.get("position_type", "car")
        if ptype not in ("car", "custom"):
            raise HttpError("position_type must be 'car' or 'custom'")
        car = b.get("car_number")
        if car is not None:
            if isinstance(car, bool) or not isinstance(car, int):
                raise HttpError("car_number must be an integer or null")
            if not 1 <= car <= 40:
                raise HttpError("car_number out of range (1-40)")
        if ptype == "car" and car is None:
            raise HttpError("car_number is required for position_type 'car'")
        custom = clean_text(b, "custom_text")
        if ptype == "custom" and not custom:
            raise HttpError("custom_text is required for position_type 'custom'")
        desc = clean_text(b, "description")
        anon = 1 if b.get("is_anon", True) else 0
        today = date.today().isoformat()

        existing = conn.execute(
            "SELECT id, version, is_deleted FROM answer WHERE transfer_id=? AND user_id=?",
            (tid, uid)).fetchone()
        if existing:
            ver = existing["version"] + 1
            # is_deleted=0 matters: UNIQUE(transfer_id, user_id) means a user who
            # deleted their answer can never insert a new row for that transfer,
            # so without resetting the flag here their next post updated a row
            # that /api/answers filters out — reported as success, invisible
            # forever. Reviving it also reads as a fresh post, hence `created`.
            conn.execute(
                """UPDATE answer SET position_type=?, car_number=?, custom_text=?, description=?,
                   is_anon=?, version=?, updated_at=?, is_deleted=0 WHERE id=?""",
                (ptype, car, custom, desc, anon, ver, today, existing["id"]))
            ans_id, is_new = existing["id"], bool(existing["is_deleted"])
        else:
            ans_id = dbx.insert_id(
                conn,
                """INSERT INTO answer (transfer_id, user_id, position_type, car_number, custom_text,
                   description, is_anon, version, created_at, updated_at) VALUES (?,?,?,?,?,?,?,1,?,?)""",
                (tid, uid, ptype, car, custom, desc, anon, today, today))
            ver, is_new = 1, True

        conn.execute(
            """INSERT INTO answer_version (answer_id, version, position_type, car_number, custom_text, description)
               VALUES (?,?,?,?,?,?)""", (ans_id, ver, ptype, car, custom, desc))
        self._quota_used(conn, dev, "answer")
        conn.commit()
        self._json({"id": ans_id, "version": ver, "created": is_new}, 201)

    def _vote(self, conn, ans_id, b):
        dev = self._quota_check(conn, "vote", b)
        vtype = b.get("type")
        if vtype not in ("LIKE", "DISLIKE"):
            raise HttpError("bad vote type")
        # existence check: voting on a missing answer used to hit a FK violation and
        # come back as a 500 that leaked the constraint name
        if not conn.execute("SELECT 1 FROM answer WHERE id=? AND is_deleted=0",
                            (ans_id,)).fetchone():
            raise HttpError("answer not found", 404)
        uid = self._uid_ensure(conn, b)
        if uid is None:
            raise HttpError("missing device id")
        cur = conn.execute("SELECT type FROM vote WHERE answer_id=? AND user_id=?", (ans_id, uid)).fetchone()
        if cur and cur["type"] == vtype:
            conn.execute("DELETE FROM vote WHERE answer_id=? AND user_id=?", (ans_id, uid))
        elif cur:
            conn.execute("UPDATE vote SET type=? WHERE answer_id=? AND user_id=?", (vtype, ans_id, uid))
        else:
            conn.execute("INSERT INTO vote (answer_id, user_id, type) VALUES (?,?,?)", (ans_id, uid, vtype))
        self._quota_used(conn, dev, "vote")
        conn.commit()
        likes = conn.execute("SELECT COUNT(*) c FROM vote WHERE answer_id=? AND type='LIKE'", (ans_id,)).fetchone()["c"]
        dislikes = conn.execute("SELECT COUNT(*) c FROM vote WHERE answer_id=? AND type='DISLIKE'", (ans_id,)).fetchone()["c"]
        self._json({"likes": likes, "dislikes": dislikes})


def main():
    if not os.path.exists(DB_PATH):
        print("metro.db not found. Run:  python import_city.py --all --reset")
        return
    print(f"Metro Transfer API on http://localhost:{PORT}  (no login — X-Device-Id identity)")
    print("  GET  /api/health | /api/cities?q= | /api/cities/<id>/stations?q=")
    print("  GET  /api/answers?station=&from_line=&from_dir=&to_line=&to_dir=")
    print("  POST /api/answers | /api/answers/<id>/vote | /api/answers/<id>/comments")
    print("Press Ctrl+C to stop.")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
