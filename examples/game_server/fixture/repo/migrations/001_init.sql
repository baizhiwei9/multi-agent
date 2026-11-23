-- 001_init.sql
BEGIN;
CREATE TABLE player (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    legacy_score INTEGER DEFAULT 0
);
CREATE TABLE item (
    id   TEXT PRIMARY KEY,
    name TEXT NOT NULL
);
COMMIT;