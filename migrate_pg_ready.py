#!/usr/bin/env python3
"""
One-off migration: bring an existing metro.db up to the Postgres-ready schema
WITHOUT losing runtime data (device identities, votes, comments, answers).

    python migrate_pg_ready.py --check     # report only, touch nothing
    python migrate_pg_ready.py             # migrate (asks for nothing else)

Why not just re-import: `import_city.py --all --reset` rebuilds the structural
tables but destroys the 300 app_user / 263 vote / 11 comment / 11 answer rows.

What it does
  1. `user` -> `app_user`   (`user` is a reserved word in Postgres)
  2. `city.search_fold`, `station.search_fold`  (replace the fold() SQL callback)
  3. backfills both columns with db.search_key()
  4. verifies, and only then commits

Safety notes learned the hard way:
  * Python's sqlite3 does NOT open a transaction for DDL, so `rollback()` will
    not undo an ALTER TABLE. We issue an explicit BEGIN IMMEDIATE, which makes
    the whole migration atomic (SQLite DDL itself is transactional).
  * `PRAGMA foreign_keys` is a no-op inside a transaction, so it is set first.
  * `legacy_alter_table` must be OFF or RENAME will not rewrite the three
    child-table `REFERENCES user(id)` clauses, silently orphaning 285 rows.
    OFF is the default, but `foreign_keys` defaults to OFF too, and the
    combination fk=OFF + legacy=ON is the one that breaks — so set both.
  * Stop the backend first. Since connections are per-request there is usually
    no lock, which means this would otherwise *succeed* under a running server
    and leave it 500-ing on `no such table: user`.
"""
import argparse
import os
import re
import sqlite3
import sys

import db

DB_PATH = db.DB_PATH
SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")

# Rename map, not a hardcoded pair: if another table has to be renamed later
# (e.g. `line`, which collides with a Postgres built-in type name), add it here
# rather than writing a second migration against a live database.
RENAMES = {"user": "app_user"}

# table -> (new column, source columns for search_key)
FOLD_COLUMNS = {
    "city": ("search_fold", ("id", "name_cn", "name_en", "alias")),
    # deliberately no id for station: every station id contains '-', which fold()
    # strips, so folding ids would make ?q=bj match all 423 Beijing stations
    "station": ("search_fold", ("name_cn", "name_en", "alias")),
}

TABLES = ["country", "city", "metro_system", "line", "direction", "station",
          "station_line", "transfer", "answer", "answer_version", "vote", "comment"]


def table_names(con):
    return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def columns(con, table):
    return [r[1] for r in con.execute(f"PRAGMA table_info({table})")]


def counts(con, user_table):
    out = {}
    for t in TABLES + [user_table]:
        try:
            out[t] = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except sqlite3.OperationalError:
            out[t] = None
    return out


def schema_tables():
    """Table names declared in schema.sql, so a newly added table counts as pending."""
    with open(SCHEMA_PATH, encoding="utf-8") as f:
        return set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", f.read()))


def pending_work(con):
    names = table_names(con)
    return ([o for o in RENAMES if o in names]
            + [f"{t}.{col}" for t, (col, _) in FOLD_COLUMNS.items()
               if col not in columns(con, t)]
            + [f"table {t}" for t in sorted(schema_tables() - names)])


def report(con):
    names = table_names(con)
    old = [o for o in RENAMES if o in names]
    new = [n for n in RENAMES.values() if n in names]
    absent = sorted(schema_tables() - names)
    if absent:
        print(f"  missing tbl : {absent}")
    user_table = "app_user" if "app_user" in names else "user"
    print(f"  sqlite      : {sqlite3.sqlite_version}")
    print(f"  db          : {DB_PATH} ({os.path.getsize(DB_PATH)} bytes)")
    print(f"  renames     : pending={old} done={new}")
    for t, (col, _) in FOLD_COLUMNS.items():
        cols = columns(con, t)
        if col not in cols:
            print(f"  {t}.{col}: MISSING")
        else:
            n = con.execute(f"SELECT COUNT(*) FROM {t} WHERE {col}=''").fetchone()[0]
            print(f"  {t}.{col}: present, {n} row(s) still blank")
    print(f"  counts      : {counts(con, user_table)}")
    print(f"  fk_check    : {con.execute('PRAGMA foreign_key_check').fetchall()}")
    return old, new


def verify(con):
    """Every check must pass before we commit. `PRAGMA foreign_key_check` alone is
    NOT sufficient — it skips FKs whose parent table does not exist, so it would
    happily pass a schema that still says REFERENCES user(id)."""
    problems = []
    names = table_names(con)

    for old, new in RENAMES.items():
        if old in names:
            problems.append(f"old table `{old}` still exists")
        if new not in names:
            problems.append(f"new table `{new}` missing")
        stale = con.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE sql LIKE ?",
            (f"%REFERENCES {old}(%",)).fetchone()[0]
        if stale:
            problems.append(f"{stale} schema object(s) still REFERENCES {old}(")

    fk = con.execute("PRAGMA foreign_key_check").fetchall()
    if fk:
        problems.append(f"foreign_key_check reported {len(fk)}: {fk[:4]}")

    # orphan check per child table, independent of foreign_key_check
    for child in ("vote", "comment", "answer"):
        n = con.execute(f"""SELECT COUNT(*) FROM {child} c
                            LEFT JOIN app_user u ON u.id = c.user_id
                            WHERE u.id IS NULL""").fetchone()[0]
        if n:
            problems.append(f"{n} orphan row(s) in {child}")

    for t, (col, srcs) in FOLD_COLUMNS.items():
        if col not in columns(con, t):
            problems.append(f"{t}.{col} missing")
            continue
        blank = con.execute(f"SELECT COUNT(*) FROM {t} WHERE {col}=''").fetchone()[0]
        if blank:
            problems.append(f"{t}.{col} blank on {blank} row(s)")
        want = len(srcs) - 1
        bad = con.execute(
            f"""SELECT COUNT(*) FROM {t}
                WHERE length({col}) - length(replace({col}, char(31), '')) <> ?""",
            (want,)).fetchone()[0]
        if bad:
            problems.append(f"{t}.{col} has the wrong separator count on {bad} row(s) "
                            f"(expected {want}; a fold-then-join bug looks exactly like this)")
    return problems


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--check", action="store_true", help="report only, write nothing")
    args = ap.parse_args()

    if not os.path.exists(DB_PATH):
        sys.exit(f"no database at {DB_PATH} — nothing to migrate "
                 f"(a fresh DB gets the new schema straight from schema.sql)")
    if db.IS_PG:
        sys.exit("DATABASE_URL is set; this script only migrates a local SQLite file")

    con = sqlite3.connect(DB_PATH)
    # Must precede BEGIN: PRAGMA foreign_keys is a no-op inside a transaction.
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA legacy_alter_table = OFF")

    print("before:")
    old, new = report(con)

    pending = pending_work(con)

    if args.check:
        print("\n" + (f"PENDING: {', '.join(pending)}" if pending else "nothing pending"))
        con.close()
        sys.exit(1 if pending else 0)

    if not pending:
        print("\nalready migrated — nothing to do")
        con.close()
        return
    if new and old:
        con.close()
        sys.exit(f"refusing: BOTH {old} and {new} exist. That is the half-migrated state "
                 f"that forks identities — restore the snapshot and retry.")

    before = counts(con, "app_user" if "app_user" in table_names(con) else "user")

    print("\nmigrating…")
    con.execute("BEGIN IMMEDIATE")     # explicit: sqlite3 will not wrap DDL for us
    try:
        for o, n in RENAMES.items():
            if o in table_names(con):
                con.execute(f"ALTER TABLE {o} RENAME TO {n}")
                print(f"  renamed {o} -> {n}")

        # Pick up any table/index added to schema.sql since this DB was built
        # (every statement there is CREATE ... IF NOT EXISTS, so this is a no-op
        # for anything that already exists and never touches existing rows).
        before_tables = table_names(con)
        with open(SCHEMA_PATH, encoding="utf-8") as f:
            for stmt in f.read().split(";"):
                # Drop leading `--` comment lines first: each statement in
                # schema.sql is preceded by its own comment block, so testing
                # startswith("CREATE") on the raw chunk skips the CREATE TABLE and
                # then runs the CREATE INDEX that depends on it.
                s = "\n".join(ln for ln in stmt.splitlines()
                              if not ln.strip().startswith("--")).strip()
                if s.upper().startswith("CREATE"):
                    con.execute(s)
        added = table_names(con) - before_tables
        if added:
            print(f"  created missing table(s): {sorted(added)}")

        for t, (col, srcs) in FOLD_COLUMNS.items():
            if col not in columns(con, t):
                # NOT NULL DEFAULT '' on purpose: the list-everything path searches
                # with '%%', and NULL LIKE '%%' is NULL, which would delete the row
                # from the UI rather than merely making it unsearchable.
                con.execute(f"ALTER TABLE {t} ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
                print(f"  added {t}.{col}")
            rows = con.execute(f"SELECT id, {', '.join(srcs)} FROM {t}").fetchall()
            for r in rows:
                con.execute(f"UPDATE {t} SET {col}=? WHERE id=?",
                            (db.search_key(*[x if x is not None else "" for x in r[1:]]), r[0]))
            print(f"  backfilled {t}.{col} for {len(rows)} rows")

        after = counts(con, "app_user")
        moved = {k: (before.get(k), after.get(k)) for k in set(before) | set(after)}
        lost = {k: v for k, v in moved.items()
                if k not in RENAMES and k not in RENAMES.values()
                and v[0] is not None and v[0] != v[1]}
        if lost:
            raise RuntimeError(f"row counts changed: {lost}")
        if before.get("user") is not None and after.get("app_user") != before["user"]:
            raise RuntimeError(f"user {before['user']} -> app_user {after.get('app_user')}")

        problems = verify(con)
        if problems:
            raise RuntimeError("verification failed:\n   - " + "\n   - ".join(problems))
    except Exception as e:
        con.execute("ROLLBACK")
        con.close()
        sys.exit(f"\nMIGRATION ABORTED, database unchanged:\n{e}")

    con.execute("COMMIT")
    print("\nafter:")
    report(con)
    print(f"\nseq: {dict(con.execute('SELECT name, seq FROM sqlite_sequence'))}")
    con.close()
    print("\nmigration committed. Note the new size/mtime as the baseline — the old "
          "'782,336 bytes / mtime 08-07' assertion in HANDOFF is now retired.")


if __name__ == "__main__":
    main()
