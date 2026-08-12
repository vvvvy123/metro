#!/usr/bin/env python3
"""
Metro Transfer — city data importer (stdlib only, no pip install needed).

Reads data/<city>.json (see data_format.md), validates it, and writes into
metro.db (SQLite) using schema.sql. Idempotent: re-running re-imports the
same city cleanly (dimension data is upserted by id).

Usage:
    python import_city.py --city beijing
    python import_city.py --all
    python import_city.py --all --reset      # drop & recreate the DB first

Design notes:
    * Zero third-party dependencies — runs on any machine with Python 3.8+.
    * Validation is the point: it refuses dirty city<->line<->direction links,
      which is exactly the V1 bug this project set out to fix.
"""
import argparse
import json
import os
import sys
from datetime import date, timedelta

import db
from db import upsert

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "data")
DB_PATH = db.DB_PATH   # honours $DB_PATH — a local copy of this constant would make
                       # --reset delete the default file while writes went elsewhere
SCHEMA_PATH = os.path.join(ROOT, "schema.sql")
SCHEMA_PG_PATH = os.path.join(ROOT, "schema_pg.sql")


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------
class DataError(Exception):
    pass


def validate(doc, city_id):
    if doc["city"]["id"] != city_id:
        raise DataError(f"city.id '{doc['city']['id']}' != filename '{city_id}'")

    line_ids = {l["id"] for l in doc["lines"]}
    if len(line_ids) != len(doc["lines"]):
        raise DataError("duplicate line id within city")

    # direction lookup: line_id -> set(direction names)
    dirs = {l["id"]: set(l.get("directions", [])) for l in doc["lines"]}

    station_ids = set()
    station_lines = {}
    for st in doc["stations"]:
        if st["id"] in station_ids:
            raise DataError(f"duplicate station id {st['id']}")
        station_ids.add(st["id"])
        for lid in st["lines"]:
            if lid not in line_ids:
                raise DataError(
                    f"station {st['id']} links line '{lid}' not defined in this city "
                    f"(this is the exact V1 bug — refusing dirty link)")
        station_lines[st["id"]] = set(st["lines"])

    for a in doc.get("seed_answers", []):
        s = a["station"]
        if s not in station_ids:
            raise DataError(f"seed_answer references unknown station {s}")
        for lk, dk in (("from_line", "from_dir"), ("to_line", "to_dir")):
            if a[lk] not in station_lines[s]:
                raise DataError(f"seed_answer: {a[lk]} is not a line of station {s}")
            if a[dk] not in dirs[a[lk]]:
                raise DataError(
                    f"seed_answer: direction '{a[dk]}' not valid for line {a[lk]}")


# --------------------------------------------------------------------------
# DB helpers
# --------------------------------------------------------------------------
def connect():
    return db.connect()


def ensure_schema(conn):
    # Guard against the half-migrated state. Running this schema against a DB that
    # still has the pre-rename `user` table would create an EMPTY `app_user`
    # alongside it, leaving vote/comment/answer FKs pointed at `user`: every device
    # identity silently resets, and new answers written with app_user.id=N pass the
    # FK against the *old* user #N by coincidence, so they get attributed to a
    # stranger. Reachable exactly via the documented `import_city.py --all`.
    if not conn.is_pg:
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('user','app_user')")}
        if "user" in names:
            raise DataError(
                "this database still has the pre-rename `user` table — run "
                "migrate_pg_ready.py first (it preserves your runtime data); "
                "importing now would fork identities")
    path = SCHEMA_PG_PATH if conn.is_pg else SCHEMA_PATH
    with open(path, encoding="utf-8") as f:
        conn.executescript(f.read())
    conn.commit()


def get_or_create_user(conn, email, nickname):
    cur = conn.execute("SELECT id FROM app_user WHERE email = ?", (email,))
    r = cur.fetchone()
    if r:
        return r["id"]
    return db.insert_id(
        conn, "INSERT INTO app_user (email, nickname) VALUES (?, ?)", (email, nickname))


def get_or_create_transfer(conn, s, fl, fd, tl, td):
    cur = conn.execute(
        """SELECT id FROM transfer WHERE station_id=? AND from_line_id=?
           AND from_dir_id=? AND to_line_id=? AND to_dir_id=?""",
        (s, fl, fd, tl, td))
    r = cur.fetchone()
    if r:
        return r["id"]
    return db.insert_id(
        conn,
        """INSERT INTO transfer
           (station_id, from_line_id, from_dir_id, to_line_id, to_dir_id)
           VALUES (?, ?, ?, ?, ?)""", (s, fl, fd, tl, td))


def dir_id(line_id, name):
    """Stable synthetic direction id from line + direction name."""
    slug = "".join(ch if ch.isalnum() else "-" for ch in name).strip("-").lower()
    return f"{line_id}-{slug or 'd'}"


# --------------------------------------------------------------------------
# Import
# --------------------------------------------------------------------------
def import_city(conn, city_id, verbose=True):
    path = os.path.join(DATA_DIR, f"{city_id}.json")
    if not os.path.exists(path):
        raise DataError(f"missing data file: {path}")
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)

    validate(doc, city_id)
    c = doc["city"]

    # country
    upsert(conn, "country", {
        "id": c["country_id"], "name_cn": c.get("country_cn", c["country_id"]),
        "name_en": c.get("country_en", c["country_id"])})

    # city
    # search_fold MUST be listed here: on SQLite `upsert` is INSERT OR REPLACE,
    # which is DELETE+INSERT, so any column omitted falls back to its default —
    # a routine `import_city.py --all` would otherwise blank the search key for
    # all 10 cities and 3353 stations, and the search would silently die.
    city_alias = json.dumps(c.get("alias", []), ensure_ascii=False)
    upsert(conn, "city", {
        "id": c["id"], "country_id": c["country_id"],
        "name_cn": c["name_cn"], "name_en": c["name_en"],
        "alias": city_alias,
        "timezone": c.get("timezone", "UTC"),
        "search_fold": db.search_key(c["id"], c["name_cn"], c["name_en"], city_alias)})

    # system
    sys_id = None
    if "system" in doc:
        sys_id = doc["system"]["id"]
        upsert(conn, "metro_system", {
            "id": sys_id, "city_id": c["id"],
            "name_cn": doc["system"]["name_cn"], "name_en": doc["system"]["name_en"]})

    # lines + directions
    n_dir = 0
    for l in doc["lines"]:
        upsert(conn, "line", {
            "id": l["id"], "city_id": c["id"], "system_id": sys_id,
            "name": l["name"], "name_en": l.get("name_en", ""),
            "color": l.get("color", "#4b5563")})
        for i, dname in enumerate(l.get("directions", [])):
            upsert(conn, "direction", {
                "id": dir_id(l["id"], dname), "line_id": l["id"],
                "name": dname, "ordinal": i})
            n_dir += 1

    # stations + station_line
    for st in doc["stations"]:
        st_alias = json.dumps(st.get("alias", []), ensure_ascii=False)
        upsert(conn, "station", {
            "id": st["id"], "city_id": c["id"],
            "name_cn": st["name_cn"], "name_en": st.get("name_en", ""),
            "alias": st_alias,
            # no id here — see station.search_fold in schema.sql
            "search_fold": db.search_key(st["name_cn"], st.get("name_en", ""), st_alias)})
        for lid in st["lines"]:
            db.insert_ignore(conn, "station_line",
                             {"station_id": st["id"], "line_id": lid},
                             conflict=("station_id", "line_id"))

    # seed answers (optional)
    today = date.today()
    n_ans = 0
    for a in doc.get("seed_answers", []):
        uid = get_or_create_user(conn, a["author_email"], a["author_nick"])
        fd = dir_id(a["from_line"], a["from_dir"])
        td = dir_id(a["to_line"], a["to_dir"])
        tid = get_or_create_transfer(
            conn, a["station"], a["from_line"], fd, a["to_line"], td)

        updated = (today - timedelta(days=a.get("days_ago", 0))).isoformat()
        ver = a.get("version", 1)
        vals = (a["position_type"], a.get("car_number"), a.get("custom_text", ""),
                a.get("description", ""), 1 if a.get("anon", True) else 0, ver,
                updated, updated)
        # Update-or-insert, NOT delete-then-insert. Deleting the parent answer
        # violates the FKs from answer_version/vote/comment — Postgres always
        # enforces those, and so does SQLite once foreign_keys=ON — and it also
        # leaked the old answer_version rows.
        existing = conn.execute(
            "SELECT id FROM answer WHERE transfer_id=? AND user_id=?", (tid, uid)).fetchone()
        if existing:
            ans_id = existing["id"]
            conn.execute(
                """UPDATE answer SET position_type=?, car_number=?, custom_text=?,
                   description=?, is_anon=?, version=?, created_at=?, updated_at=?,
                   is_deleted=0 WHERE id=?""", vals + (ans_id,))
        else:
            ans_id = db.insert_id(
                conn,
                """INSERT INTO answer
                   (position_type, car_number, custom_text, description, is_anon,
                    version, created_at, updated_at, transfer_id, user_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""", vals + (tid, uid))

        # Everything below is insert-if-absent rather than delete-and-recreate:
        # a real user may have commented on (or re-versioned) a seed answer, and
        # re-importing must never destroy their rows.
        if not conn.execute("SELECT 1 FROM answer_version WHERE answer_id=? AND version=?",
                            (ans_id, ver)).fetchone():
            conn.execute(
                """INSERT INTO answer_version
                   (answer_id, version, position_type, car_number, custom_text,
                    description, created_at) VALUES (?,?,?,?,?,?,?)""",
                (ans_id, ver, a["position_type"], a.get("car_number"),
                 a.get("custom_text", ""), a.get("description", ""), updated))

        # votes: synthesize distinct voter accounts to hit the like/dislike counts
        for kind, n in (("LIKE", a.get("likes", 0)), ("DISLIKE", a.get("dislikes", 0))):
            for i in range(n):
                vemail = f"seed+{ans_id}-{kind}-{i}@example.com"
                vuid = get_or_create_user(conn, vemail, "匿名")
                db.insert_ignore(conn, "vote",
                                 {"answer_id": ans_id, "user_id": vuid, "type": kind},
                                 conflict=("answer_id", "user_id"))

        for cm in a.get("comments", []):
            cuid = get_or_create_user(
                conn, f"cmt+{ans_id}-{cm['text'][:6]}@example.com", cm.get("nick", "匿名"))
            ct = (today - timedelta(days=cm.get("days_ago", 0))).isoformat()
            if not conn.execute(
                    "SELECT 1 FROM comment WHERE answer_id=? AND user_id=? AND content=?",
                    (ans_id, cuid, cm["text"])).fetchone():
                conn.execute(
                    "INSERT INTO comment (answer_id, user_id, content, created_at) VALUES (?,?,?,?)",
                    (ans_id, cuid, cm["text"], ct))
        n_ans += 1

    conn.commit()
    if verbose:
        print(f"  [{c['name_cn']}] {len(doc['lines'])} lines, {n_dir} directions, "
              f"{len(doc['stations'])} stations, {n_ans} seed answers")


def list_cities():
    return sorted(
        f[:-5] for f in os.listdir(DATA_DIR)
        if f.endswith(".json"))


def main():
    ap = argparse.ArgumentParser(description="Import metro city data into SQLite")
    ap.add_argument("--city", help="city id, e.g. beijing")
    ap.add_argument("--all", action="store_true", help="import every data/*.json")
    ap.add_argument("--reset", action="store_true", help="delete metro.db first")
    args = ap.parse_args()

    if args.reset:
        if db.IS_PG:
            sys.exit("--reset is refused against Postgres (DATABASE_URL is set); "
                     "drop and recreate the schema deliberately instead")
        if os.path.exists(DB_PATH):
            os.remove(DB_PATH)
            print("• removed existing metro.db")

    conn = connect()
    ensure_schema(conn)

    if args.all:
        cities = list_cities()
    elif args.city:
        cities = [args.city]
    else:
        print("nothing to do — pass --city <id> or --all")
        print("available:", ", ".join(list_cities()))
        return

    print(f"importing into {DB_PATH}")
    for cid in cities:
        try:
            import_city(conn, cid)
        except DataError as e:
            print(f"  ✗ {cid}: {e}", file=sys.stderr)
            sys.exit(1)
    conn.close()
    print("done.")


if __name__ == "__main__":
    main()
