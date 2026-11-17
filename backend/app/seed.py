from __future__ import annotations
import sqlite3

from app.db import connect

from app.variance_policy import FINAL_DISPOSITIONS

_TABLES = '''
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tills (
  id INTEGER PRIMARY KEY,
  till_code TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cash_lines (
  id INTEGER PRIMARY KEY,
  line_no TEXT NOT NULL UNIQUE,
  till_code TEXT NOT NULL,
  business_day TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  amount REAL NOT NULL,
  pay_method TEXT NOT NULL,
  note TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS close_batches (
  id INTEGER PRIMARY KEY,
  batch_no TEXT NOT NULL UNIQUE,
  business_day TEXT NOT NULL,
  system_total REAL NOT NULL,
  declared_cash REAL NOT NULL,
  variance REAL NOT NULL,
  line_count INTEGER NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('draft', 'sealed')),
  sealed_at TEXT NOT NULL,
  note TEXT
);
CREATE TABLE IF NOT EXISTS variance_rows (
  id INTEGER PRIMARY KEY,
  batch_id INTEGER NOT NULL REFERENCES close_batches(id),
  business_day TEXT NOT NULL,
  variance REAL NOT NULL,
  disposition TEXT NOT NULL CHECK (disposition IN ({dispositions})),
  note TEXT,
  created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_variance_batch ON variance_rows(batch_id);
CREATE TABLE IF NOT EXISTS variance_holds (
  id INTEGER PRIMARY KEY,
  business_day TEXT NOT NULL,
  reason TEXT
);
CREATE TABLE IF NOT EXISTS reopen_requests (
  id INTEGER PRIMARY KEY,
  business_day TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'none'
);
'''.replace("{dispositions}", ",".join(f"'{d}'" for d in FINAL_DISPOSITIONS))

# 一个营业日只允许一个 sealed 批次；老库若已有重复数据则跳过建索引并交由自检暴露。
_SEALED_DAY_INDEX = '''
CREATE UNIQUE INDEX IF NOT EXISTS ux_sealed_day ON close_batches(business_day) WHERE status='sealed';
'''


def _migrate_variance_rows_note(conn) -> None:
    """老库补齐 variance_rows.note（处置记录说明），不改写既有数据。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(variance_rows)").fetchall()}
    if "note" not in cols:
        conn.execute("ALTER TABLE variance_rows ADD COLUMN note TEXT")


# 同源约束：已 sealed 的批次必须已有最终处置的长短款行；
# 已写入处置行的批次不得停留在 draft 以外的不一致状态。
_GUARD = f'''
CREATE TRIGGER IF NOT EXISTS trg_sealed_requires_variance
BEFORE UPDATE OF status ON close_batches
WHEN NEW.status = 'sealed'
BEGIN
  SELECT RAISE(ABORT, 'sealed batch requires final variance disposition')
  WHERE NOT EXISTS (
    SELECT 1 FROM variance_rows v
    WHERE v.batch_id = NEW.id
      AND v.disposition IN ({",".join(f"'{d}'" for d in FINAL_DISPOSITIONS)})
  );
END;

CREATE TRIGGER IF NOT EXISTS trg_sealed_insert_requires_variance
BEFORE INSERT ON close_batches
WHEN NEW.status = 'sealed'
BEGIN
  SELECT RAISE(ABORT, 'sealed batch requires final variance disposition');
END;

CREATE TRIGGER IF NOT EXISTS trg_variance_requires_batch
BEFORE INSERT ON variance_rows
WHEN NOT EXISTS (SELECT 1 FROM close_batches WHERE id = NEW.batch_id)
BEGIN
  SELECT RAISE(ABORT, 'variance row requires close batch');
END;

CREATE TRIGGER IF NOT EXISTS trg_variance_insert_locks_batch
BEFORE INSERT ON variance_rows
WHEN (SELECT status FROM close_batches WHERE id = NEW.batch_id) <> 'draft'
BEGIN
  SELECT RAISE(ABORT, 'variance row can only be recorded on draft batch');
END;

CREATE TRIGGER IF NOT EXISTS trg_variance_disposition_final
BEFORE INSERT ON variance_rows
WHEN NEW.disposition NOT IN ({",".join(f"'{d}'" for d in FINAL_DISPOSITIONS)})
BEGIN
  SELECT RAISE(ABORT, 'variance disposition must be final (balanced/loss/gain)');
END;

CREATE TRIGGER IF NOT EXISTS trg_variance_disposition_direction
BEFORE INSERT ON variance_rows
WHEN (ABS(NEW.variance) < 1e-9 AND NEW.disposition <> 'balanced')
  OR (NEW.variance < -1e-9 AND NEW.disposition <> 'loss')
  OR (NEW.variance > 1e-9 AND NEW.disposition <> 'gain')
BEGIN
  SELECT RAISE(ABORT, 'variance sign and disposition disagree');
END;

-- 金额同源：长短款行金额必须等于其批次差额（插入与改写都拦截）。
CREATE TRIGGER IF NOT EXISTS trg_variance_amount_matches_batch
BEFORE INSERT ON variance_rows
WHEN ABS(NEW.variance - (SELECT variance FROM close_batches WHERE id = NEW.batch_id)) > 1e-9
BEGIN
  SELECT RAISE(ABORT, 'variance amount must equal batch variance');
END;

CREATE TRIGGER IF NOT EXISTS trg_variance_update_amount_matches_batch
BEFORE UPDATE OF variance, batch_id ON variance_rows
WHEN ABS(NEW.variance - (SELECT variance FROM close_batches WHERE id = NEW.batch_id)) > 1e-9
BEGIN
  SELECT RAISE(ABORT, 'variance amount must equal batch variance');
END;

-- 终态与方向在改写时同样不可倒退（pending 不得经 UPDATE 复活）。
CREATE TRIGGER IF NOT EXISTS trg_variance_update_disposition_final
BEFORE UPDATE OF disposition ON variance_rows
WHEN NEW.disposition NOT IN ({",".join(f"'{d}'" for d in FINAL_DISPOSITIONS)})
BEGIN
  SELECT RAISE(ABORT, 'variance disposition must be final (balanced/loss/gain)');
END;

CREATE TRIGGER IF NOT EXISTS trg_variance_update_direction
BEFORE UPDATE OF variance, disposition ON variance_rows
WHEN (ABS(NEW.variance) < 1e-9 AND NEW.disposition <> 'balanced')
  OR (NEW.variance < -1e-9 AND NEW.disposition <> 'loss')
  OR (NEW.variance > 1e-9 AND NEW.disposition <> 'gain')
BEGIN
  SELECT RAISE(ABORT, 'variance sign and disposition disagree');
END;

-- 已 sealed 批次的长短款行不可删除（防「已 sealed 无长短款行」）。
CREATE TRIGGER IF NOT EXISTS trg_variance_delete_sealed_batch
BEFORE DELETE ON variance_rows
WHEN EXISTS (SELECT 1 FROM close_batches WHERE id = OLD.batch_id AND status = 'sealed')
BEGIN
  SELECT RAISE(ABORT, 'cannot delete variance row of sealed batch');
END;
'''


def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(_TABLES)
        _migrate_variance_rows_note(conn)
        try:
            conn.execute(_SEALED_DAY_INDEX)
        except sqlite3.IntegrityError:
            # 老库已有重复 sealed 批次：不建索引，由 /api/close/integrity 暴露。
            pass
        conn.executescript(_GUARD)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def seed_if_empty() -> None:
    init_db()
    conn = connect()
    try:
        n = conn.execute("SELECT COUNT(*) c FROM tills").fetchone()["c"]
        if n:
            return
        conn.execute("INSERT INTO settings(key, value) VALUES ('business_day', ?)", ("2026-11-16",))
        tills = [
            ("TILL-A", "一号收银"),
            ("TILL-B", "二号收银"),
            ("TILL-C", "三号收银"),
        ]
        for code, title in tills:
            conn.execute("INSERT INTO tills(till_code, title) VALUES (?,?)", (code, title))
        lines = [
            ("CL-0001", "TILL-A", "2026-11-16", "2026-11-16T09:12:00Z", 35.5, "现金", "早市"),
            ("CL-0002", "TILL-A", "2026-11-16", "2026-11-16T10:05:00Z", 128.0, "微信", None),
            ("CL-0003", "TILL-B", "2026-11-16", "2026-11-16T11:20:00Z", 66.0, "现金", None),
            ("CL-0004", "TILL-B", "2026-11-16", "2026-11-16T14:02:00Z", 210.0, "支付宝", None),
            ("CL-0005", "TILL-C", "2026-11-16", "2026-11-16T16:40:00Z", 49.9, "现金", None),
            ("CL-0006", "TILL-A", "2026-11-15", "2026-11-15T18:10:00Z", 88.0, "现金", "昨日"),
        ]
        for row in lines:
            conn.execute(
                '''INSERT INTO cash_lines(line_no, till_code, business_day, occurred_at, amount, pay_method, note, created_at)
                   VALUES (?,?,?,?,?,?,?,?)''',
                (*row, row[3]),
            )
        conn.commit()
    finally:
        conn.close()
