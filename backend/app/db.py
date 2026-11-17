from __future__ import annotations
import sqlite3
from app.config import DATA_DIR, DB_PATH


def connect(*, immediate: bool = False) -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, isolation_level=None if not immediate else "EXCLUSIVE", timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    if immediate:
        conn.execute("BEGIN IMMEDIATE")
    return conn
