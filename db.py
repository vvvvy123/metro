#!/usr/bin/env python3
"""
Metro Transfer — thin DB adapter.

Local development stays on SQLite with **zero third-party dependencies** (just
`python server.py`); deployment runs on Postgres by setting `DATABASE_URL`.

This is deliberately NOT an ORM. It does exactly five things:

  1. picks the backend from the environment                       -> connect()
  2. rewrites `?` placeholders to `%s` for Postgres               -> Conn.execute
  3. returns an inserted integer id                               -> insert_id()
     (psycopg has no `lastrowid`; Postgres needs `RETURNING id`)
  4. emits dialect-correct upserts                                -> upsert() / insert_ignore()
  5. ends a request without leaking an open transaction           -> finish()

SQL is written ONCE, in SQLite spelling with `?`, and every call site keeps
using `conn.execute(sql, params)` unchanged — that is what `Conn` is for.

Also holds the search-key helpers, because the search key must be computed
byte-identically by both server.py and import_city.py.
"""
import os
import re
import sqlite3
import unicodedata

ROOT = os.path.dirname(os.path.abspath(__file__))


def _load_dotenv(path):
    """Minimal .env reader — stdlib only, so `python server.py` still needs no
    pip install. Real environment variables always win, which is what makes this a
    no-op on a PaaS (Vercel injects them directly). Not a dotenv clone: no export
    keyword, no interpolation, no multi-line values."""
    try:
        # utf-8-sig, not utf-8: PowerShell's `Out-File -Encoding utf8` and Notepad
        # both write a BOM, which would otherwise make the first key parse as
        # "﻿DATABASE_URL" and be silently ignored.
        with open(path, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                os.environ.setdefault(k, v)
    except FileNotFoundError:
        pass


_load_dotenv(os.path.join(ROOT, ".env"))

DB_PATH = os.environ.get("DB_PATH") or os.path.join(ROOT, "metro.db")
DSN = (os.environ.get("DATABASE_URL") or "").strip()
IS_PG = bool(DSN)


# --------------------------------------------------------------------------
# Search key
#
# Replaces the old `conn.create_function("fold", ...)`, which has no Postgres
# equivalent. Each searchable row stores ONE pre-folded `search_fold` column, so
# both engines search identically by construction and no Python callback runs
# per row (it used to be 600-8500 calls per keystroke).
# --------------------------------------------------------------------------

# whitespace + every hyphen/dash variant (ASCII -, U+2010–2015 hyphen..horiz bar,
# U+2212 minus) so 'Saint-Germain-en-Laye' == 'saintgermainenlaye'.
# Built programmatically so the dashes cannot accidentally form a character range
# (writing them literally inside the class is a syntax error: "bad character
# range"). Equivalent to the original r"[\s-‐-―−]" — fold()
# must stay byte-identical to what the old server.py produced, since the whole
# port is verified by diffing against pre-change golden output.
_DASHES = "".join(chr(c) for c in (0x2D, 0x2010, 0x2011, 0x2012, 0x2013, 0x2014, 0x2015, 0x2212))
_FOLD_DROP = re.compile("[\\s" + re.escape(_DASHES) + "]")

# U+001F UNIT SEPARATOR. Chosen because fold() can never *emit* it (NFKD and
# str.lower() never produce C0 controls) AND fold() actively strips it (Python's
# \s matches U+001C-001F), so a folded query can never contain one -> a query can
# never match across a field boundary, and a hostile field value can't inject one.
SEP = "\x1f"


def fold(s):
    """Search key insensitive to case, whitespace, diacritics (accents) and
    hyphens/dashes. NFKD-decompose, drop combining marks, lowercase, then strip
    whitespace + dashes, so 'La Défense' == 'la defense' and
    'Châtelet–Les Halles' == 'chateletleshalles'. CJK names are unaffected.

    The JS twin lives in web/index.html — change one, change the other."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return _FOLD_DROP.sub("", s.lower())


def search_key(*fields):
    """Build a row's stored `search_fold` value.

    MUST fold each field and THEN join. Folding the joined string instead would
    delete every separator (see SEP above: fold strips U+001F), degenerating to
    bare concatenation — which produces false hits straddling the boundary, e.g.
    'men[' matching 西直门 because name_en/alias are '' and '[]' for most rows.
    """
    return SEP.join(fold(f) for f in fields)


def like_pattern(q):
    """`%…%` LIKE pattern for a user query, with metacharacters escaped.

    Escaping is required for correctness, not just tidiness: an unescaped `_`
    matches SEP and would let a query span two fields (`门_x` would hit 西直门,
    which the old per-column OR never did). Postgres also treats `\\` as LIKE's
    escape character by default, so an unescaped trailing backslash is an error
    there. Pair every use with `LIKE ? ESCAPE '\\'` (valid in both engines).

    Visible consequence, intended: `?q=%` no longer matches every row.
    """
    f = fold(q)
    for ch in ("\\", "%", "_"):
        f = f.replace(ch, "\\" + ch)
    return f"%{f}%"


# --------------------------------------------------------------------------
# Connection
# --------------------------------------------------------------------------
def _to_pg(sql):
    """SQLite spelling -> Postgres.

    * `COLLATE BINARY` -> `COLLATE "C"`. Both mean "order by bytes", but the name
      differs per engine and each rejects the other's. Byte order is what we want
      pinned: SQLite sorts TEXT by UTF-8 bytes, while Postgres would apply its own
      collation and silently reorder CJK — which would change the 热门换乘站 top-6.
    * literal `%` must be doubled BEFORE `?` becomes `%s`, or psycopg's own
      %-interpolation chokes on it.
    """
    sql = sql.replace("COLLATE BINARY", 'COLLATE "C"')
    return sql.replace("%", "%%").replace("?", "%s")


class Conn:
    """Wraps a raw DBAPI connection so call sites keep using `conn.execute(sql,
    params)` with `?` placeholders and name-based row access on both engines."""

    def __init__(self, raw, is_pg):
        self._raw = raw
        self.is_pg = is_pg

    def execute(self, sql, params=()):
        cur = self._raw.cursor()
        cur.execute(_to_pg(sql) if self.is_pg else sql, params)
        return cur

    def executescript(self, sql):
        if self.is_pg:
            cur = self._raw.cursor()
            cur.execute(sql)          # psycopg accepts multi-statement text
            self._raw.commit()
        else:
            self._raw.executescript(sql)

    def commit(self):
        self._raw.commit()

    def rollback(self):
        self._raw.rollback()

    def close(self):
        self._raw.close()

    @property
    def raw(self):
        return self._raw


def connect():
    if IS_PG:
        import psycopg                       # only needed on the deployed path
        from psycopg.rows import dict_row
        raw = psycopg.connect(DSN, row_factory=dict_row, autocommit=False)
        return Conn(raw, True)
    raw = sqlite3.connect(DB_PATH)
    raw.row_factory = sqlite3.Row
    raw.execute("PRAGMA foreign_keys = ON")
    return Conn(raw, False)


def finish(conn):
    """End-of-request teardown. Always roll back first: an exception mid-write
    used to leave an open transaction on a leaked connection, and every later
    write then failed with 'database is locked' until the GC caught up.

    SQLite closes (a file handle per request is free). Postgres keeps the
    connection for reuse — reconnecting per request would pay TCP+TLS each time.
    """
    if conn is None:
        return
    try:
        conn.rollback()
    except Exception:
        pass
    if not conn.is_pg:
        conn.close()


# --------------------------------------------------------------------------
# Writes that differ per dialect
# --------------------------------------------------------------------------
def insert_id(conn, sql, params=()):
    """INSERT and return the generated integer id.

    Must be read BEFORE commit on Postgres (`RETURNING id`); sqlite3's
    `lastrowid` survives a commit but psycopg's is always None.
    """
    if conn.is_pg:
        return conn.execute(sql.rstrip().rstrip(";") + " RETURNING id", params).fetchone()["id"]
    return conn.execute(sql, params).lastrowid


def insert_ignore(conn, table, row, conflict):
    """INSERT OR IGNORE -> ON CONFLICT (...) DO NOTHING."""
    cols = ", ".join(row)
    ph = ", ".join(["?"] * len(row))
    tgt = ", ".join(conflict)
    if conn.is_pg:
        sql = f"INSERT INTO {table} ({cols}) VALUES ({ph}) ON CONFLICT ({tgt}) DO NOTHING"
    else:
        sql = f"INSERT OR IGNORE INTO {table} ({cols}) VALUES ({ph})"
    return conn.execute(sql, list(row.values()))


def upsert(conn, table, row, conflict=("id",)):
    """INSERT OR REPLACE -> ON CONFLICT (...) DO UPDATE.

    Note the two are NOT equivalent and the Postgres form is the one we want:
    REPLACE is DELETE+INSERT, so it resets every omitted column to its default
    (that is how re-importing silently wiped `created_at`, and would wipe
    `search_fold`) and it deletes a parent row that children reference — which
    Postgres would refuse outright.
    """
    cols = list(row)
    ph = ", ".join(["?"] * len(cols))
    collist = ", ".join(cols)
    if conn.is_pg:
        tgt = ", ".join(conflict)
        sets = ", ".join(f"{c}=EXCLUDED.{c}" for c in cols if c not in conflict)
        sql = (f"INSERT INTO {table} ({collist}) VALUES ({ph}) "
               f"ON CONFLICT ({tgt}) DO UPDATE SET {sets}")
    else:
        sql = f"INSERT OR REPLACE INTO {table} ({collist}) VALUES ({ph})"
    return conn.execute(sql, list(row.values()))
