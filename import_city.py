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
import sqlite3
import sys
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "data")
DB_PATH = os.path.join(ROOT, "metro.db")
SCHEMA_PATH = os.path.join(ROOT, "schema.sql")


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
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def ensure_schema(conn):
    with open(SCHEMA_PATH, encoding="utf-8") as f:
        conn.executescript(f.read())
    conn.commit()


def upsert(conn, table, row):
    cols = ", ".join(row.keys())
    ph = ", ".join(["?"] * len(row))
    conn.execute(
        f"INSERT OR REPLACE INTO {table} ({cols}) VALUES ({ph})",
        list(row.values()))


def get_or_create_user(conn, email, nickname):
    cur = conn.execute("SELECT id FROM user WHERE email = ?", (email,))
    r = cur.fetchone()
    if r:
        return r[0]
    cur = conn.execute(
        "INSERT INTO user (email, nickname) VALUES (?, ?)", (email, nickname))
    return cur.lastrowid


def get_or_create_transfer(conn, s, fl, fd, tl, td):
    cur = conn.execute(
        """SELECT id FROM transfer WHERE station_id=? AND from_line_id=?
           AND from_dir_id=? AND to_line_id=? AND to_dir_id=?""",
        (s, fl, fd, tl, td))
    r = cur.fetchone()
    if r:
        return r[0]
    cur = conn.execute(
        """INSERT INTO transfer
           (station_id, from_line_id, from_dir_id, to_line_id, to_dir_id)
           VALUES (?, ?, ?, ?, ?)""", (s, fl, fd, tl, td))
    return cur.lastrowid


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
    upsert(conn, "city", {
        "id": c["id"], "country_id": c["country_id"],
        "name_cn": c["name_cn"], "name_en": c["name_en"],
        "alias": json.dumps(c.get("alias", []), ensure_ascii=False),
        "timezone": c.get("timezone", "UTC")})

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
        upsert(conn, "station", {
            "id": st["id"], "city_id": c["id"],
            "name_cn": st["name_cn"], "name_en": st.get("name_en", ""),
            "alias": json.dumps(st.get("alias", []), ensure_ascii=False)})
        for lid in st["lines"]:
            conn.execute(
                "INSERT OR IGNORE INTO station_line (station_id, line_id) VALUES (?, ?)",
                (st["id"], lid))

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
        # replace any existing (transfer,user) answer to keep import idempotent
        conn.execute(
            "DELETE FROM answer WHERE transfer_id=? AND user_id=?", (tid, uid))
        cur = conn.execute(
            """INSERT INTO answer
               (transfer_id, user_id, position_type, car_number, custom_text,
                description, is_anon, version, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (tid, uid, a["position_type"], a.get("car_number"),
             a.get("custom_text", ""), a.get("description", ""),
             1 if a.get("anon", True) else 0, a.get("version", 1),
             updated, updated))
        ans_id = cur.lastrowid
        conn.execute(
            """INSERT INTO answer_version
               (answer_id, version, position_type, car_number, custom_text,
                description, created_at) VALUES (?,?,?,?,?,?,?)""",
            (ans_id, a.get("version", 1), a["position_type"], a.get("car_number"),
             a.get("custom_text", ""), a.get("description", ""), updated))

        # votes: synthesize distinct voter accounts to hit the like/dislike counts
        for kind, n in (("LIKE", a.get("likes", 0)), ("DISLIKE", a.get("dislikes", 0))):
            for i in range(n):
                vemail = f"seed+{ans_id}-{kind}-{i}@example.com"
                vuid = get_or_create_user(conn, vemail, "匿名")
                conn.execute(
                    "INSERT OR IGNORE INTO vote (answer_id, user_id, type) VALUES (?,?,?)",
                    (ans_id, vuid, kind))

        for cm in a.get("comments", []):
            cuid = get_or_create_user(
                conn, f"cmt+{ans_id}-{cm['text'][:6]}@example.com", cm.get("nick", "匿名"))
            ct = (today - timedelta(days=cm.get("days_ago", 0))).isoformat()
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

    if args.reset and os.path.exists(DB_PATH):
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
