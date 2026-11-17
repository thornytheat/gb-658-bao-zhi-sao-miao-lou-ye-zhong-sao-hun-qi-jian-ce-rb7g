import sqlite3
from contextlib import contextmanager

from app.config import DATABASE_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    input_sha256 TEXT NOT NULL,
    accepted INTEGER NOT NULL,
    quarantined INTEGER NOT NULL
);

-- 中心已入账记录。business_key 是稳定记录标识；期版页字段可以随修订变化，
-- 字段值保存在 fields_json 中，以免把“记录身份”和“当期归属”混为一谈。
CREATE TABLE IF NOT EXISTS sync_records (
    business_key TEXT PRIMARY KEY,
    station_id TEXT,
    fields_json TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    head_revision TEXT,
    approved_fields_json TEXT NOT NULL DEFAULT '[]',
    approved_values_json TEXT NOT NULL DEFAULT '{}',
    approved_version INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- 每次入账都留一份不可变快照，三方合并据此找到共同底稿。
CREATE TABLE IF NOT EXISTS sync_record_versions (
    business_key TEXT NOT NULL,
    version INTEGER NOT NULL,
    fields_json TEXT NOT NULL,
    revision_id TEXT,
    approved_fields_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    PRIMARY KEY (business_key, version)
);

CREATE TABLE IF NOT EXISTS sync_revisions (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    revision_id TEXT NOT NULL UNIQUE,
    business_key TEXT NOT NULL,
    station_id TEXT NOT NULL,
    batch_id TEXT,
    parent_revision TEXT,
    base_version INTEGER,
    expected_version INTEGER,
    base_fields_json TEXT,
    mode TEXT NOT NULL DEFAULT 'fields',
    fields_json TEXT,
    changes_json TEXT,
    payload_sha256 TEXT NOT NULL,
    effective_json TEXT,
    status TEXT NOT NULL,
    reason_code TEXT,
    record_version_before INTEGER,
    record_version_after INTEGER,
    evidence_json TEXT,
    submitted_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    booked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_sync_revisions_key ON sync_revisions(business_key);
CREATE INDEX IF NOT EXISTS idx_sync_revisions_parent ON sync_revisions(parent_revision);
CREATE INDEX IF NOT EXISTS idx_sync_revisions_station ON sync_revisions(station_id);
CREATE INDEX IF NOT EXISTS idx_sync_revisions_status ON sync_revisions(status);

CREATE TABLE IF NOT EXISTS sync_conflicts (
    conflict_id TEXT PRIMARY KEY,
    revision_id TEXT NOT NULL,
    business_key TEXT NOT NULL,
    field TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    base_present INTEGER NOT NULL,
    base_value_json TEXT,
    ours_present INTEGER NOT NULL,
    ours_value_json TEXT,
    theirs_present INTEGER NOT NULL,
    theirs_value_json TEXT,
    record_version_at_hold INTEGER NOT NULL,
    status TEXT NOT NULL,
    resolution_source TEXT,
    resolution_value_json TEXT,
    resolved_by TEXT,
    resolved_at TEXT,
    evidence_json TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sync_conflicts_revision ON sync_conflicts(revision_id);
CREATE INDEX IF NOT EXISTS idx_sync_conflicts_status ON sync_conflicts(status);
CREATE INDEX IF NOT EXISTS idx_sync_conflicts_key ON sync_conflicts(business_key);

CREATE TABLE IF NOT EXISTS sync_batches (
    batch_id TEXT PRIMARY KEY,
    station_id TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL,
    summary_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_batch_items (
    batch_id TEXT NOT NULL,
    revision_id TEXT NOT NULL,
    business_key TEXT,
    status TEXT NOT NULL,
    reason_code TEXT,
    message TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (batch_id, revision_id)
);
CREATE INDEX IF NOT EXISTS idx_sync_batch_items_revision ON sync_batch_items(revision_id);

CREATE TABLE IF NOT EXISTS sync_reviews (
    review_id TEXT PRIMARY KEY,
    business_key TEXT NOT NULL,
    expected_version INTEGER,
    result_version INTEGER NOT NULL,
    fields_json TEXT NOT NULL,
    approved_fields_json TEXT NOT NULL,
    operator TEXT,
    evidence_json TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sync_reviews_key ON sync_reviews(business_key);

-- 已入账扫描件。station_id + sha256 + 业务键 是收件幂等键：重试不重复接收，
-- 但同一内容登记到不同页面时仍各留一条，避免丢页码归属。
CREATE TABLE IF NOT EXISTS sync_scans (
    station_id TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    business_key TEXT NOT NULL,
    file_name TEXT,
    fields_json TEXT NOT NULL,
    batch_id TEXT,
    line_no INTEGER,
    received_at TEXT NOT NULL,
    PRIMARY KEY (station_id, sha256, business_key)
);
CREATE INDEX IF NOT EXISTS idx_sync_scans_key ON sync_scans(business_key);
CREATE INDEX IF NOT EXISTS idx_sync_scans_batch ON sync_scans(batch_id);

-- 隔离扫描件保留原始行值和物理行号，不参与业务计算。
CREATE TABLE IF NOT EXISTS sync_scan_quarantine (
    quarantine_id TEXT PRIMARY KEY,
    station_id TEXT NOT NULL,
    batch_id TEXT,
    line_no INTEGER,
    reason_code TEXT NOT NULL,
    raw_value TEXT,
    message TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sync_quarantine_station ON sync_scan_quarantine(station_id);
CREATE INDEX IF NOT EXISTS idx_sync_quarantine_batch ON sync_scan_quarantine(batch_id);

"""


def connect():
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DATABASE_PATH, timeout=5.0)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout = 5000")
    return db


def init_db():
    with connect() as db:
        db.executescript(SCHEMA)


def record_run(manifest: dict) -> bool:
    init_db()
    with connect() as db:
        before = db.total_changes
        db.execute(
            "INSERT OR IGNORE INTO runs VALUES (?, ?, ?, ?)",
            (
                manifest["run_id"],
                manifest["input_sha256"],
                manifest["accepted"],
                manifest["quarantined"],
            ),
        )
        return db.total_changes > before


def run_count() -> int:
    init_db()
    with connect() as db:
        return db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]


@contextmanager
def transaction():
    """A write transaction that serialises concurrent sync/review writes.

    SQLite has a single writer.  BEGIN IMMEDIATE makes the read-check-write
    sequence used for optimistic version checks atomic across processes.
    """

    init_db()
    db = connect()
    try:
        db.execute("BEGIN IMMEDIATE")
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@contextmanager
def read():
    """A read-only connection; does not take the writer lock."""

    init_db()
    db = connect()
    try:
        yield db
    finally:
        db.close()
