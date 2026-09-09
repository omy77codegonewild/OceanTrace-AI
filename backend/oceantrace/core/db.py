"""SQLite persistence (local-first MVP). Schema mirrors 07_api_database.md;
geometries are stored as GeoJSON text and can be migrated to PostGIS 1:1."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from .config import get_settings

_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, status TEXT NOT NULL,
  data_mode TEXT NOT NULL DEFAULT 'imported',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  config_snapshot TEXT NOT NULL, notes TEXT
);
CREATE TABLE IF NOT EXISTS scenes (
  id TEXT PRIMARY KEY, case_id TEXT REFERENCES cases(id), acquisition_time TEXT,
  bounds TEXT, asset_uri TEXT, preview_uri TEXT, metadata TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS slicks (
  id TEXT PRIMARY KEY, case_id TEXT REFERENCES cases(id), scene_id TEXT REFERENCES scenes(id),
  geom TEXT NOT NULL, class TEXT NOT NULL, confidence REAL NOT NULL, properties TEXT NOT NULL,
  review_status TEXT, review_note TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS env_fields (
  id TEXT PRIMARY KEY, case_id TEXT REFERENCES cases(id), provider TEXT NOT NULL,
  data_mode TEXT NOT NULL, coverage TEXT NOT NULL, artifact_uri TEXT, metadata TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hindcast_runs (
  id TEXT PRIMARY KEY, case_id TEXT REFERENCES cases(id), slick_id TEXT REFERENCES slicks(id),
  env_field_id TEXT, origin_geom TEXT, release_start TEXT, release_end TEXT,
  metrics TEXT NOT NULL, config TEXT NOT NULL, artifact_uri TEXT, status TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ais_imports (
  id TEXT PRIMARY KEY, case_id TEXT REFERENCES cases(id), source TEXT NOT NULL,
  data_mode TEXT NOT NULL, filename TEXT, rows_total INTEGER, rows_valid INTEGER,
  rows_quarantined INTEGER, quarantine_sample TEXT, time_start TEXT, time_end TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ais_positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT NOT NULL, import_id TEXT NOT NULL,
  mmsi INTEGER NOT NULL, ts TEXT NOT NULL, ts_epoch REAL NOT NULL, lon REAL NOT NULL, lat REAL NOT NULL,
  sog REAL, cog REAL, heading REAL, vessel_name TEXT, vessel_type TEXT, length_m REAL, source TEXT
);
CREATE INDEX IF NOT EXISTS ais_case_mmsi_ts ON ais_positions(case_id, mmsi, ts_epoch);
CREATE INDEX IF NOT EXISTS ais_case_ts ON ais_positions(case_id, ts_epoch);
CREATE INDEX IF NOT EXISTS ais_case_bbox ON ais_positions(case_id, lon, lat);
CREATE TABLE IF NOT EXISTS attribution_runs (
  id TEXT PRIMARY KEY, case_id TEXT REFERENCES cases(id), hindcast_run_id TEXT,
  weights TEXT NOT NULL, summary TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS vessel_scores (
  id TEXT PRIMARY KEY, attribution_run_id TEXT REFERENCES attribution_runs(id), case_id TEXT,
  mmsi INTEGER, rank INTEGER, score REAL, priority TEXT, factors TEXT, evidence TEXT,
  limitations TEXT, vessel TEXT, track TEXT, review_status TEXT, review_note TEXT
);
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, case_id TEXT, kind TEXT NOT NULL, state TEXT NOT NULL,
  progress REAL NOT NULL DEFAULT 0, message TEXT, result TEXT, error TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT, ts TEXT NOT NULL, actor TEXT NOT NULL,
  action TEXT NOT NULL, detail TEXT
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _connect() -> sqlite3.Connection:
    path = get_settings().resolved_db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


_conn: sqlite3.Connection | None = None


def init_db() -> None:
    global _conn
    with _lock:
        if _conn is None:
            _conn = _connect()
            _conn.executescript(SCHEMA)
            _conn.commit()


@contextmanager
def db() -> Iterator[sqlite3.Connection]:
    init_db()
    assert _conn is not None
    with _lock:
        try:
            yield _conn
            _conn.commit()
        except Exception:
            _conn.rollback()
            raise


def dumps(obj: Any) -> str:
    return json.dumps(obj, default=str, separators=(",", ":"))


def loads(s: str | None, default: Any = None) -> Any:
    if s is None:
        return default
    try:
        return json.loads(s)
    except Exception:
        return default


def row_to_dict(row: sqlite3.Row | None, json_cols: tuple[str, ...] = ()) -> dict[str, Any] | None:
    if row is None:
        return None
    d = dict(row)
    for c in json_cols:
        if c in d:
            d[c] = loads(d[c])
    return d


def audit(case_id: str | None, action: str, detail: Any = None, actor: str = "analyst") -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO audit_log(case_id, ts, actor, action, detail) VALUES (?,?,?,?,?)",
            (case_id, utcnow(), actor, action, dumps(detail) if detail is not None else None),
        )
