"""SQLite storage. Raw tables are append-only (INSERT OR IGNORE only) so the
raw record stays auditable; all transformation happens downstream in memory.
"""
import json
import os
import sqlite3
import time

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS announcements (
    article_id   INTEGER PRIMARY KEY,
    code         TEXT,
    title        TEXT NOT NULL,
    release_time INTEGER NOT NULL,      -- ms epoch UTC
    catalog_id   INTEGER NOT NULL,
    raw_json     TEXT NOT NULL,
    fetched_at   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS symbols (
    symbol        TEXT PRIMARY KEY,
    base_asset    TEXT,
    quote_asset   TEXT,
    status        TEXT,                 -- TRADING / BREAK / ARCHIVE_ONLY (delisted)
    in_exchange_info INTEGER NOT NULL,  -- 1 if present in current exchangeInfo
    in_archive    INTEGER NOT NULL,     -- 1 if present in data.binance.vision
    first_kline_ms INTEGER,             -- exact first 1m kline open time (REST/archive)
    fetched_at    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    symbol             TEXT PRIMARY KEY, -- primary (USDT) pair
    base_asset         TEXT NOT NULL,
    quote_asset        TEXT NOT NULL,
    first_trade_time   INTEGER NOT NULL, -- ms epoch UTC, first 1m kline open
    announcement_time  INTEGER,          -- ms epoch UTC, NULL if unavailable
    announcement_title TEXT,
    announcement_type  TEXT,             -- will_list / launchpool / hodler_airdrop / NULL
    listing_status     TEXT NOT NULL,    -- TRADING or DELISTED (survivorship marker)
    included           INTEGER NOT NULL, -- 1 = qualifies for H-B
    exclude_reason     TEXT,             -- why not, if included=0 (nothing silently dropped)
    created_at         INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS klines (
    symbol       TEXT NOT NULL,
    interval     TEXT NOT NULL,
    open_time    INTEGER NOT NULL,
    open         REAL NOT NULL,
    high         REAL NOT NULL,
    low          REAL NOT NULL,
    close        REAL NOT NULL,
    volume       REAL NOT NULL,
    close_time   INTEGER NOT NULL,
    quote_volume REAL NOT NULL,
    n_trades     INTEGER NOT NULL,
    taker_buy_base  REAL NOT NULL,
    taker_buy_quote REAL NOT NULL,
    source       TEXT NOT NULL,          -- rest / archive_zip
    PRIMARY KEY (symbol, interval, open_time)
);

CREATE TABLE IF NOT EXISTS ingest_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        INTEGER NOT NULL,
    step      TEXT NOT NULL,
    detail    TEXT
);
"""


def connect(db_path: str = None) -> sqlite3.Connection:
    path = db_path or config.DB_PATH
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def log_step(conn, step: str, detail: str = ""):
    conn.execute(
        "INSERT INTO ingest_log (ts, step, detail) VALUES (?,?,?)",
        (int(time.time() * 1000), step, detail),
    )
    conn.commit()


def insert_announcements(conn, articles, catalog_id):
    now = int(time.time() * 1000)
    rows = [
        (
            a["id"], a.get("code"), a["title"], int(a["releaseDate"]),
            catalog_id, json.dumps(a, separators=(",", ":")), now,
        )
        for a in articles
    ]
    conn.executemany(
        "INSERT OR IGNORE INTO announcements "
        "(article_id, code, title, release_time, catalog_id, raw_json, fetched_at) "
        "VALUES (?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    return len(rows)


def upsert_symbol(conn, symbol, base, quote, status, in_ei, in_archive,
                  first_kline_ms):
    # symbols is a derived cache (not raw market data); refreshing it keeps
    # first_kline_ms current without violating raw append-only tables.
    conn.execute(
        "INSERT INTO symbols (symbol, base_asset, quote_asset, status, "
        "in_exchange_info, in_archive, first_kline_ms, fetched_at) "
        "VALUES (?,?,?,?,?,?,?,?) "
        "ON CONFLICT(symbol) DO UPDATE SET base_asset=excluded.base_asset, "
        "quote_asset=excluded.quote_asset, status=excluded.status, "
        "in_exchange_info=excluded.in_exchange_info, in_archive=excluded.in_archive, "
        "first_kline_ms=COALESCE(excluded.first_kline_ms, symbols.first_kline_ms), "
        "fetched_at=excluded.fetched_at",
        (symbol, base, quote, status, in_ei, in_archive, first_kline_ms,
         int(time.time() * 1000)),
    )


def insert_klines(conn, symbol, interval, klines, source):
    rows = [
        (
            symbol, interval, int(k[0]), float(k[1]), float(k[2]), float(k[3]),
            float(k[4]), float(k[5]), int(k[6]), float(k[7]), int(k[8]),
            float(k[9]), float(k[10]), source,
        )
        for k in klines
    ]
    conn.executemany(
        "INSERT OR IGNORE INTO klines VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    return len(rows)


def insert_event(conn, ev: dict):
    conn.execute(
        "INSERT OR REPLACE INTO events (symbol, base_asset, quote_asset, "
        "first_trade_time, announcement_time, announcement_title, "
        "announcement_type, listing_status, included, exclude_reason, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            ev["symbol"], ev["base_asset"], ev["quote_asset"],
            ev["first_trade_time"], ev.get("announcement_time"),
            ev.get("announcement_title"), ev.get("announcement_type"),
            ev["listing_status"], ev["included"], ev.get("exclude_reason"),
            int(time.time() * 1000),
        ),
    )
