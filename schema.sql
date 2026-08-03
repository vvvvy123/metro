-- =====================================================================
-- Metro Transfer — SQLite schema (V2, corrected relational model)
--
-- Fixes the core V1 bug: lines / directions were NOT bound to a city,
-- so "北京 西直门 · 2号线" could show Shanghai directions (往浦东).
--
-- Correct ownership chain (PRD 六 数据库重构):
--   Country -> City -> MetroSystem -> Line -> Direction
--                              \-> Station (Station<->Line via station_line)
--   Transfer(station, from_line, from_dir, to_line, to_dir) -> Answer -> Version/Vote/Comment
--
-- Because Line.city_id is enforced and Direction.line_id is enforced,
-- a station's lines and a line's directions are always city-consistent.
-- Run with import_city.py (it executes this file first).
-- =====================================================================

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------
-- Geography
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS country (
    id          TEXT PRIMARY KEY,          -- 'cn', 'jp'
    name_cn     TEXT NOT NULL,
    name_en     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS city (
    id          TEXT PRIMARY KEY,          -- 'beijing'
    country_id  TEXT REFERENCES country(id),
    name_cn     TEXT NOT NULL,             -- 北京
    name_en     TEXT NOT NULL,             -- Beijing
    alias       TEXT DEFAULT '',           -- JSON array string, searchable: "BJ","Peking","beijing","bj"
    timezone    TEXT DEFAULT 'UTC',
    created_at  TEXT DEFAULT (datetime('now'))
);

-- A city can host more than one operator/system; optional but future-proof.
CREATE TABLE IF NOT EXISTS metro_system (
    id          TEXT PRIMARY KEY,          -- 'beijing-subway'
    city_id     TEXT NOT NULL REFERENCES city(id),
    name_cn     TEXT NOT NULL,
    name_en     TEXT NOT NULL
);

-- ---------------------------------------------------------------------
-- Network — every row is bound to a city (this is the fix)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS line (
    id          TEXT PRIMARY KEY,          -- 'bj-l2'
    city_id     TEXT NOT NULL REFERENCES city(id),
    system_id   TEXT REFERENCES metro_system(id),
    name        TEXT NOT NULL,             -- 2号线   (unique only within a city)
    name_en     TEXT DEFAULT '',
    color       TEXT DEFAULT '#4b5563',
    created_at  TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_line_city ON line(city_id);

CREATE TABLE IF NOT EXISTS direction (
    id          TEXT PRIMARY KEY,          -- 'bj-l2-inner'
    line_id     TEXT NOT NULL REFERENCES line(id),
    name        TEXT NOT NULL,             -- 内环 / 往东直门
    ordinal     INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_direction_line ON direction(line_id);

CREATE TABLE IF NOT EXISTS station (
    id          TEXT PRIMARY KEY,          -- 'bj-xizhimen'
    city_id     TEXT NOT NULL REFERENCES city(id),
    name_cn     TEXT NOT NULL,             -- 西直门
    name_en     TEXT DEFAULT '',           -- Xizhimen
    alias       TEXT DEFAULT '',           -- JSON array string: pinyin / initials / en
    created_at  TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_station_city ON station(city_id);

-- Station <-> Line many-to-many. A station only links lines from its own city.
CREATE TABLE IF NOT EXISTS station_line (
    station_id  TEXT NOT NULL REFERENCES station(id),
    line_id     TEXT NOT NULL REFERENCES line(id),
    PRIMARY KEY (station_id, line_id)
);

-- ---------------------------------------------------------------------
-- Transfer relation — the unique key users query against
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS transfer (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    station_id     TEXT NOT NULL REFERENCES station(id),
    from_line_id   TEXT NOT NULL REFERENCES line(id),
    from_dir_id    TEXT NOT NULL REFERENCES direction(id),
    to_line_id     TEXT NOT NULL REFERENCES line(id),
    to_dir_id      TEXT NOT NULL REFERENCES direction(id),
    UNIQUE (station_id, from_line_id, from_dir_id, to_line_id, to_dir_id)
);

-- ---------------------------------------------------------------------
-- Accounts — email + verification code (no password, PRD 五 登录)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS user (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    email       TEXT UNIQUE NOT NULL,      -- one account per email
    nickname    TEXT NOT NULL,
    created_at  TEXT DEFAULT (datetime('now'))
);

-- ---------------------------------------------------------------------
-- Community content
-- ---------------------------------------------------------------------
-- One live answer per (transfer, user). Re-publishing => new version.
CREATE TABLE IF NOT EXISTS answer (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    transfer_id   INTEGER NOT NULL REFERENCES transfer(id),
    user_id       INTEGER NOT NULL REFERENCES user(id),
    position_type TEXT NOT NULL,           -- 'car' | 'custom'
    car_number    INTEGER,                 -- when position_type='car'
    custom_text   TEXT DEFAULT '',         -- when position_type='custom'
    description   TEXT DEFAULT '',
    is_anon       INTEGER DEFAULT 1,
    version       INTEGER DEFAULT 1,
    is_deleted    INTEGER DEFAULT 0,
    created_at    TEXT DEFAULT (datetime('now')),
    updated_at    TEXT DEFAULT (datetime('now')),
    UNIQUE (transfer_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_answer_transfer ON answer(transfer_id);

-- History is kept even after edit/delete (PRD 十 版本机制 / 十一 删除机制).
CREATE TABLE IF NOT EXISTS answer_version (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    answer_id     INTEGER NOT NULL REFERENCES answer(id),
    version       INTEGER NOT NULL,
    position_type TEXT NOT NULL,
    car_number    INTEGER,
    custom_text   TEXT DEFAULT '',
    description   TEXT DEFAULT '',
    created_at    TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_version_answer ON answer_version(answer_id);

-- One vote per (answer, user); can cancel / switch (PRD 八 点赞系统).
CREATE TABLE IF NOT EXISTS vote (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    answer_id   INTEGER NOT NULL REFERENCES answer(id),
    user_id     INTEGER NOT NULL REFERENCES user(id),
    type        TEXT NOT NULL CHECK (type IN ('LIKE','DISLIKE')),
    created_at  TEXT DEFAULT (datetime('now')),
    UNIQUE (answer_id, user_id)
);

-- Flat comments only (PRD 七 评论系统).
CREATE TABLE IF NOT EXISTS comment (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    answer_id   INTEGER NOT NULL REFERENCES answer(id),
    user_id     INTEGER NOT NULL REFERENCES user(id),
    content     TEXT NOT NULL,
    created_at  TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_comment_answer ON comment(answer_id);
