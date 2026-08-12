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
import sqlite3
import uuid
import math
import os
import unicodedata
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

ROOT = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(ROOT, "metro.db")
# Localhost only. This was "0.0.0.0", which exposed the dev backend — and the
# real metro.db behind it — to every device on the LAN, with no authentication
# on any write endpoint. Making HOST/PORT/DB_PATH configurable via env is still
# on the pre-deploy list; this is just the safe default.
HOST, PORT = "127.0.0.1", 8000


# whitespace + every hyphen/dash variant (ASCII -, U+2010–2015 hyphen..horiz bar,
# U+2212 minus) so 'Saint-Germain-en-Laye' == 'saintgermainenlaye'.
_FOLD_DROP = re.compile(r"[\s\u002d\u2010-\u2015\u2212]")


def fold(s):
    """Search key insensitive to case, whitespace, diacritics (accents) and
    hyphens/dashes. NFKD-decompose, drop combining marks, lowercase, then strip
    whitespace + dashes, so 'La Défense' == 'la defense' and
    'Châtelet–Les Halles' == 'chateletleshalles'. CJK names are unaffected."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return _FOLD_DROP.sub("", s.lower())


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.create_function("fold", 1, fold)   # usable inside SQL: fold(name_cn)
    return conn


def like(q):
    return f"%{(q or '').strip().lower()}%"


def like_fold(q):
    return f"%{fold(q)}%"


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


def station_json(conn, r):
    lines = conn.execute(
        """SELECT l.id, l.name, l.color, l.name_en FROM line l
           JOIN station_line sl ON sl.line_id = l.id
           WHERE sl.station_id = ? ORDER BY l.name""", (r["id"],)).fetchall()
    return {"id": r["id"], "name_cn": r["name_cn"], "name_en": r["name_en"],
            "alias": json.loads(r["alias"] or "[]"),
            "lines": [dict(x) for x in lines]}


def slug(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


class Handler(BaseHTTPRequestHandler):
    server_version = "MetroTransfer/2.1"

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
        n = int(self.headers.get("Content-Length", 0) or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def _device(self, body=None):
        return self.headers.get("X-Device-Id") or (body or {}).get("device_id")

    def _uid_lookup(self, conn, body=None):
        """Existing user id for this device, or None (read-only, no insert)."""
        dev = self._device(body)
        if not dev:
            return None
        row = conn.execute("SELECT id FROM user WHERE email=?", ("device:" + dev,)).fetchone()
        return row["id"] if row else None

    def _uid_ensure(self, conn, body=None):
        """User id for this device, creating the row if needed. None if no device."""
        dev = self._device(body)
        if not dev:
            return None
        email = "device:" + dev
        nick = (body or {}).get("nickname") or "匿名用户"
        row = conn.execute("SELECT id FROM user WHERE email=?", (email,)).fetchone()
        if row:
            if (body or {}).get("nickname"):
                conn.execute("UPDATE user SET nickname=? WHERE id=?", (nick, row["id"]))
                conn.commit()
            return row["id"]
        cur = conn.execute("INSERT INTO user (email, nickname) VALUES (?,?)", (email, nick))
        conn.commit()
        return cur.lastrowid

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
        if conn is not None:
            try:
                conn.rollback()      # discard anything not explicitly committed
            except Exception:
                pass
            conn.close()

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        u = urlparse(self.path)
        try:
            self.route_get(u.path.rstrip("/"), parse_qs(u.query))
        except Exception as e:
            self._err(f"server error: {e}", 500)
        finally:
            self._close_db()

    def do_POST(self):
        p = urlparse(self.path).path.rstrip("/")
        try:
            self.route_post(p, self._body())
        except Exception as e:
            self._err(f"server error: {e}", 500)
        finally:
            self._close_db()

    def do_DELETE(self):
        # This had no try/except: an exception escaped into socketserver and the
        # client got NO response at all — indistinguishable from a dropped
        # connection, so the UI could not tell "failed" from "deleted".
        try:
            self.route_delete(urlparse(self.path).path.rstrip("/"))
        except Exception as e:
            self._err(f"server error: {e}", 500)
        finally:
            self._close_db()

    def route_delete(self, p):
        m = re.match(r"^/api/answers/(\d+)$", p)
        if not m:
            return self._err("not found", 404)
        conn = self._db()
        uid = self._uid_lookup(conn)
        row = conn.execute("SELECT user_id FROM answer WHERE id=?", (m.group(1),)).fetchone()
        if not row:
            return self._err("not found", 404)
        if uid is None or row["user_id"] != uid:
            return self._err("not your answer", 403)
        conn.execute("UPDATE answer SET is_deleted=1 WHERE id=?", (m.group(1),))
        conn.commit()
        self._json({"ok": True})

    # ---- GET ----
    def route_get(self, p, q):
        conn = self._db()

        if p == "/api/health":
            n = conn.execute("SELECT COUNT(*) c FROM city").fetchone()["c"]
            return self._json({"ok": True, "cities": n})

        if p == "/api/cities":
            qq = like_fold(q.get("q", [""])[0])
            rows = conn.execute(
                """SELECT * FROM city WHERE fold(id) LIKE ? OR fold(name_cn) LIKE ?
                   OR fold(name_en) LIKE ? OR fold(alias) LIKE ? ORDER BY name_cn""",
                (qq, qq, qq, qq)).fetchall()
            return self._json([{"id": r["id"], "name_cn": r["name_cn"], "name_en": r["name_en"],
                                "alias": json.loads(r["alias"] or "[]")} for r in rows])

        m = re.match(r"^/api/cities/([^/]+)/stations$", p)
        if m:
            qq = like_fold(q.get("q", [""])[0])
            rows = conn.execute(
                """SELECT * FROM station WHERE city_id=? AND
                   (fold(name_cn) LIKE ? OR fold(name_en) LIKE ? OR fold(alias) LIKE ?)
                   ORDER BY name_cn""", (m.group(1), qq, qq, qq)).fetchall()
            return self._json([station_json(conn, r) for r in rows])

        m = re.match(r"^/api/cities/([^/]+)/popular$", p)
        if m:
            return self._popular(conn, m.group(1), q)

        m = re.match(r"^/api/cities/([^/]+)/lines$", p)
        if m:
            rows = conn.execute("SELECT * FROM line WHERE city_id=? ORDER BY name", (m.group(1),)).fetchall()
            return self._json([dict(r) for r in rows])

        m = re.match(r"^/api/stations/([^/]+)/lines$", p)
        if m:
            rows = conn.execute(
                """SELECT l.* FROM line l JOIN station_line sl ON sl.line_id=l.id
                   WHERE sl.station_id=? ORDER BY l.name""", (m.group(1),)).fetchall()
            return self._json([dict(r) for r in rows])

        m = re.match(r"^/api/lines/([^/]+)/directions$", p)
        if m:
            rows = conn.execute("SELECT * FROM direction WHERE line_id=? ORDER BY ordinal", (m.group(1),)).fetchall()
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
                ORDER BY answers DESC, likes DESC, comments DESC, s.name_cn
                LIMIT ?""", (cid, limit)).fetchall()
        picked = [dict(r) for r in hot]
        seen = {r["sid"] for r in picked}

        if len(picked) < limit:
            fill = conn.execute(
                """SELECT s.id AS sid, COUNT(sl.line_id) AS n
                     FROM station s JOIN station_line sl ON sl.station_id = s.id
                    WHERE s.city_id = ?
                    GROUP BY s.id HAVING n >= 2
                    ORDER BY n DESC, s.name_cn
                    LIMIT ?""", (cid, limit * 4)).fetchall()
            for r in fill:
                if r["sid"] in seen:
                    continue
                picked.append({"sid": r["sid"], "answers": 0, "likes": 0, "comments": 0})
                seen.add(r["sid"])
                if len(picked) >= limit:
                    break

        out = []
        for r in picked:
            row = conn.execute("SELECT * FROM station WHERE id=?", (r["sid"],)).fetchone()
            if not row:
                continue
            item = station_json(conn, row)
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
        answers = []
        for a in conn.execute("SELECT * FROM answer WHERE transfer_id=? AND is_deleted=0", (row["id"],)).fetchall():
            likes = conn.execute("SELECT COUNT(*) c FROM vote WHERE answer_id=? AND type='LIKE'", (a["id"],)).fetchone()["c"]
            dislikes = conn.execute("SELECT COUNT(*) c FROM vote WHERE answer_id=? AND type='DISLIKE'", (a["id"],)).fetchone()["c"]
            user = conn.execute("SELECT nickname FROM user WHERE id=?", (a["user_id"],)).fetchone()
            comments = conn.execute(
                """SELECT c.content, c.created_at, u.nickname FROM comment c
                   JOIN user u ON u.id=c.user_id WHERE c.answer_id=? ORDER BY c.created_at DESC""",
                (a["id"],)).fetchall()
            my = None
            if uid:
                v = conn.execute("SELECT type FROM vote WHERE answer_id=? AND user_id=?", (a["id"], uid)).fetchone()
                my = v["type"] if v else None
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
            cn = (b["name_cn"] or "").strip()
            en = (b.get("name_en", "") or "").strip()
            # de-dupe by CN or EN (case-insensitive) so 巴黎 / Paris never double up
            dup = conn.execute(
                "SELECT * FROM city WHERE lower(name_cn)=? OR (name_en!='' AND lower(name_en)=?)",
                (cn.lower(), en.lower())).fetchone()
            if dup:
                return self._json({"id": dup["id"], "name_cn": dup["name_cn"], "name_en": dup["name_en"]}, 200)
            cid = slug("city")
            conn.execute(
                "INSERT INTO city (id, country_id, name_cn, name_en, alias, timezone) VALUES (?,?,?,?,?,?)",
                (cid, b.get("country_id"), cn, en,
                 json.dumps(b.get("alias", []), ensure_ascii=False), b.get("timezone", "UTC")))
            conn.commit()
            return self._json({"id": cid, "name_cn": cn, "name_en": en}, 201)

        m = re.match(r"^/api/cities/([^/]+)/stations$", p)
        if m:
            sid = slug("st")
            conn.execute("INSERT INTO station (id, city_id, name_cn, name_en, alias) VALUES (?,?,?,?,?)",
                         (sid, m.group(1), b["name_cn"], b.get("name_en", ""),
                          json.dumps(b.get("alias", []), ensure_ascii=False)))
            for lid in b.get("lines", []):
                conn.execute("INSERT OR IGNORE INTO station_line (station_id, line_id) VALUES (?,?)", (sid, lid))
            conn.commit()
            return self._json(station_json(conn, conn.execute("SELECT * FROM station WHERE id=?", (sid,)).fetchone()), 201)

        m = re.match(r"^/api/cities/([^/]+)/lines$", p)
        if m:
            lid = slug("line")
            conn.execute("INSERT INTO line (id, city_id, name, name_en, color) VALUES (?,?,?,?,?)",
                         (lid, m.group(1), b["name"], b.get("name_en", ""), b.get("color", "#4b5563")))
            for i, dname in enumerate(b.get("directions", [])):
                conn.execute("INSERT INTO direction (id, line_id, name, ordinal) VALUES (?,?,?,?)",
                             (slug("dir"), lid, dname, i))
            conn.commit()
            return self._json(dict(conn.execute("SELECT * FROM line WHERE id=?", (lid,)).fetchone()), 201)

        m = re.match(r"^/api/stations/([^/]+)/lines$", p)
        if m:
            conn.execute("INSERT OR IGNORE INTO station_line (station_id, line_id) VALUES (?,?)", (m.group(1), b["line_id"]))
            conn.commit()
            return self._json({"ok": True}, 201)

        m = re.match(r"^/api/lines/([^/]+)/directions$", p)
        if m:
            did = slug("dir")
            n = conn.execute("SELECT COUNT(*) c FROM direction WHERE line_id=?", (m.group(1),)).fetchone()["c"]
            conn.execute("INSERT INTO direction (id, line_id, name, ordinal) VALUES (?,?,?,?)", (did, m.group(1), b["name"], n))
            conn.commit()
            return self._json({"id": did, "name": b["name"]}, 201)

        if p == "/api/answers":
            return self._post_answer(conn, b)

        m = re.match(r"^/api/answers/(\d+)/vote$", p)
        if m:
            return self._vote(conn, int(m.group(1)), b)

        m = re.match(r"^/api/answers/(\d+)/comments$", p)
        if m:
            uid = self._uid_ensure(conn, b)
            if uid is None:
                return self._err("missing device id", 400)
            txt = (b.get("content") or "").strip()
            if not txt:
                return self._err("empty comment")
            conn.execute("INSERT INTO comment (answer_id, user_id, content) VALUES (?,?,?)", (int(m.group(1)), uid, txt))
            conn.commit()
            return self._json({"ok": True}, 201)

        self._err("not found", 404)

    def _post_answer(self, conn, b):
        uid = self._uid_ensure(conn, b)
        if uid is None:
            return self._err("missing device id", 400)
        keys = ("station", "from_line", "from_dir", "to_line", "to_dir")
        if not all(b.get(k) for k in keys):
            return self._err("incomplete transfer")
        row = conn.execute(
            """SELECT id FROM transfer WHERE station_id=? AND from_line_id=?
               AND from_dir_id=? AND to_line_id=? AND to_dir_id=?""", tuple(b[k] for k in keys)).fetchone()
        if row:
            tid = row["id"]
        else:
            cur = conn.execute(
                """INSERT INTO transfer (station_id, from_line_id, from_dir_id, to_line_id, to_dir_id)
                   VALUES (?,?,?,?,?)""", tuple(b[k] for k in keys))
            tid = cur.lastrowid

        ptype = b.get("position_type", "car")
        car = b.get("car_number")
        custom = b.get("custom_text", "")
        desc = b.get("description", "")
        anon = 1 if b.get("is_anon", True) else 0
        today = date.today().isoformat()

        existing = conn.execute("SELECT * FROM answer WHERE transfer_id=? AND user_id=?", (tid, uid)).fetchone()
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
            cur = conn.execute(
                """INSERT INTO answer (transfer_id, user_id, position_type, car_number, custom_text,
                   description, is_anon, version, created_at, updated_at) VALUES (?,?,?,?,?,?,?,1,?,?)""",
                (tid, uid, ptype, car, custom, desc, anon, today, today))
            ans_id, ver, is_new = cur.lastrowid, 1, True

        conn.execute(
            """INSERT INTO answer_version (answer_id, version, position_type, car_number, custom_text, description)
               VALUES (?,?,?,?,?,?)""", (ans_id, ver, ptype, car, custom, desc))
        conn.commit()
        self._json({"id": ans_id, "version": ver, "created": is_new}, 201)

    def _vote(self, conn, ans_id, b):
        uid = self._uid_ensure(conn, b)
        if uid is None:
            return self._err("missing device id", 400)
        vtype = b.get("type")
        if vtype not in ("LIKE", "DISLIKE"):
            return self._err("bad vote type")
        cur = conn.execute("SELECT type FROM vote WHERE answer_id=? AND user_id=?", (ans_id, uid)).fetchone()
        if cur and cur["type"] == vtype:
            conn.execute("DELETE FROM vote WHERE answer_id=? AND user_id=?", (ans_id, uid))
        elif cur:
            conn.execute("UPDATE vote SET type=? WHERE answer_id=? AND user_id=?", (vtype, ans_id, uid))
        else:
            conn.execute("INSERT INTO vote (answer_id, user_id, type) VALUES (?,?,?)", (ans_id, uid, vtype))
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
