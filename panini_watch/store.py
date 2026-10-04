"""SQLite state: what we have seen, what we watch, and what we have already told you about."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Iterable

from .models import Product

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    site TEXT NOT NULL,
    kind TEXT NOT NULL,              -- category | search | feed
    value TEXT NOT NULL,             -- URL (category/feed) or search term
    name TEXT NOT NULL DEFAULT '',
    mode TEXT NOT NULL DEFAULT 'loose',   -- strict | loose | none  (football filter strength)
    origin TEXT NOT NULL DEFAULT 'auto',  -- auto | user | config
    enabled INTEGER NOT NULL DEFAULT 1,
    baselined INTEGER NOT NULL DEFAULT 0,
    last_scan REAL, last_count INTEGER, last_error TEXT DEFAULT '',
    UNIQUE(site, kind, value)
);
CREATE TABLE IF NOT EXISTS products (
    site TEXT NOT NULL, pid TEXT NOT NULL,
    name TEXT, url TEXT, price REAL, old_price REAL, currency TEXT,
    in_stock INTEGER, image TEXT, sku TEXT, labels TEXT, source_name TEXT,
    first_seen REAL, last_seen REAL, last_change REAL,
    silent INTEGER NOT NULL DEFAULT 0,   -- 1 = found during baseline (not a "new release")
    PRIMARY KEY (site, pid)
);
CREATE INDEX IF NOT EXISTS ix_products_first ON products(first_seen);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, site TEXT, pid TEXT, type TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS tr_cache (src TEXT NOT NULL, lang TEXT NOT NULL, result TEXT NOT NULL,
                                     PRIMARY KEY (src, lang));
"""


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._db.executescript(SCHEMA)
            cols = {r["name"] for r in self._db.execute("PRAGMA table_info(products)")}
            if "name_orig" not in cols:
                self._db.execute("ALTER TABLE products ADD COLUMN name_orig TEXT")
            self._db.commit()

    # -- translation cache --------------------------------------------------------------------
    def tr_get(self, text: str, lang: str) -> str | None:
        with self._lock:
            r = self._db.execute("SELECT result FROM tr_cache WHERE src=? AND lang=?", (text, lang)).fetchone()
        return r["result"] if r else None

    def tr_set(self, text: str, lang: str, result: str) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO tr_cache(src,lang,result) VALUES(?,?,?)", (text, lang, result))
            self._db.commit()

    def untranslated(self, non_english_sites: list[str], limit: int = 400) -> list[sqlite3.Row]:
        if not non_english_sites:
            return []
        q = ",".join("?" * len(non_english_sites))
        with self._lock:
            return self._db.execute(
                f"SELECT site,pid,name FROM products WHERE name_orig IS NULL AND site IN ({q}) LIMIT ?",
                (*non_english_sites, limit)).fetchall()

    def set_names(self, rows: list[tuple[str, str, str, str]]) -> None:
        """rows: (site, pid, original, english)"""
        with self._lock:
            self._db.executemany("UPDATE products SET name_orig=?, name=? WHERE site=? AND pid=?",
                                 [(o, en, s, p) for s, p, o, en in rows])
            self._db.commit()

    # -- key/value settings -------------------------------------------------------------------
    def get(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            r = self._db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default

    def set(self, key: str, value: str) -> None:
        with self._lock:
            self._db.execute("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                             (key, value))
            self._db.commit()

    def get_json(self, key: str, default=None):
        v = self.get(key)
        return json.loads(v) if v else default

    def set_json(self, key: str, value) -> None:
        self.set(key, json.dumps(value))

    # -- sources ------------------------------------------------------------------------------
    def add_source(self, site: str, kind: str, value: str, name: str = "", mode: str = "loose",
                   origin: str = "auto", enabled: bool = True) -> bool:
        with self._lock:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO sources(site,kind,value,name,mode,origin,enabled) VALUES(?,?,?,?,?,?,?)",
                (site, kind, value, name or value, mode, origin, int(enabled)))
            self._db.commit()
            return cur.rowcount > 0

    def sources(self, site: str | None = None, enabled_only: bool = True) -> list[sqlite3.Row]:
        q, args = "SELECT * FROM sources WHERE 1=1", []
        if site:
            q += " AND site=?"; args.append(site)
        if enabled_only:
            q += " AND enabled=1"
        q += " ORDER BY site, id"
        with self._lock:
            return self._db.execute(q, args).fetchall()

    def update_source(self, sid: int, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self._db.execute(f"UPDATE sources SET {cols} WHERE id=?", (*fields.values(), sid))
            self._db.commit()

    def delete_sources(self, site: str, origin: str = "auto") -> None:
        with self._lock:
            self._db.execute("DELETE FROM sources WHERE site=? AND origin=?", (site, origin))
            self._db.commit()

    def remove_source(self, sid: int) -> None:
        with self._lock:
            self._db.execute("DELETE FROM sources WHERE id=?", (sid,))
            self._db.commit()

    # -- products -----------------------------------------------------------------------------
    def product(self, site: str, pid: str) -> sqlite3.Row | None:
        with self._lock:
            return self._db.execute("SELECT * FROM products WHERE site=? AND pid=?", (site, pid)).fetchone()

    def products_for_site(self, site: str) -> dict[str, sqlite3.Row]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM products WHERE site=?", (site,)).fetchall()
        return {r["pid"]: r for r in rows}

    def upsert_products(self, items: Iterable[tuple[Product, str, bool]]) -> None:
        """items: (product, source_name, silent). Keeps first_seen; updates last_change on change."""
        now = time.time()
        with self._lock:
            for p, src_name, silent in items:
                cur = self._db.execute("SELECT price, in_stock FROM products WHERE site=? AND pid=?",
                                       (p.site, p.pid)).fetchone()
                labels = json.dumps(p.labels, ensure_ascii=False)
                if cur is None:
                    self._db.execute(
                        "INSERT INTO products(site,pid,name,url,price,old_price,currency,in_stock,image,sku,labels,"
                        "source_name,first_seen,last_seen,last_change,silent) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (p.site, p.pid, p.name, p.url, p.price, p.old_price, p.currency, int(p.in_stock), p.image,
                         p.sku, labels, src_name, now, now, now, int(silent)))
                else:
                    changed = (cur["price"] != p.price) or (bool(cur["in_stock"]) != p.in_stock)
                    self._db.execute(
                        "UPDATE products SET name=?,url=?,price=?,old_price=?,currency=?,in_stock=?,image=?,sku=?,"
                        "labels=?,source_name=?,last_seen=?,last_change=CASE WHEN ? THEN ? ELSE last_change END,"
                        "name_orig=COALESCE(?, name_orig) WHERE site=? AND pid=?",
                        (p.name, p.url, p.price, p.old_price, p.currency, int(p.in_stock), p.image or None, p.sku,
                         labels, src_name, now, int(changed), now, p.orig_name, p.site, p.pid))
            self._db.commit()

    def recent_new(self, site: str | None, since: float, limit: int = 40) -> list[sqlite3.Row]:
        q = "SELECT * FROM products WHERE silent=0 AND first_seen>=?"
        args: list = [since]
        if site:
            q += " AND site=?"; args.append(site)
        q += " ORDER BY first_seen DESC LIMIT ?"; args.append(limit)
        with self._lock:
            return self._db.execute(q, args).fetchall()

    def deals(self, site: str | None = None, limit: int = 400) -> list[sqlite3.Row]:
        q = ("SELECT * FROM products WHERE old_price IS NOT NULL AND price IS NOT NULL AND old_price > price "
             "AND last_seen >= ?")
        args: list = [time.time() - 3 * 86400]
        if site:
            q += " AND site=?"; args.append(site)
        q += " ORDER BY (old_price - price) * 1.0 / old_price DESC LIMIT ?"; args.append(limit)
        with self._lock:
            return self._db.execute(q, args).fetchall()

    def search_local(self, term: str, site: str | None = None, limit: int = 60) -> list[sqlite3.Row]:
        q = "SELECT * FROM products WHERE name LIKE ?"
        args: list = [f"%{term}%"]
        if site:
            q += " AND site=?"; args.append(site)
        q += " ORDER BY last_seen DESC LIMIT ?"; args.append(limit)
        with self._lock:
            return self._db.execute(q, args).fetchall()

    def counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._db.execute("SELECT site, COUNT(*) c FROM products GROUP BY site").fetchall()
        return {r["site"]: r["c"] for r in rows}

    # -- events -------------------------------------------------------------------------------
    def log_event(self, site: str, pid: str, type_: str, detail: str = "") -> None:
        with self._lock:
            self._db.execute("INSERT INTO events(ts,site,pid,type,detail) VALUES(?,?,?,?,?)",
                             (time.time(), site, pid, type_, detail))
            self._db.commit()

    def recent_events(self, limit: int = 20) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
