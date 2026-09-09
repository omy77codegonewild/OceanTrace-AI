"""AIS import (FR-6): strict CSV validator/normaliser. Canonical columns:
mmsi,timestamp_utc,longitude,latitude,sog_knots,cog_deg,heading_deg,vessel_name,vessel_type,length_m,source

Common provider column aliases are recognised; rows failing validation are
quarantined with a reason (never silently dropped)."""
from __future__ import annotations

import csv
import io
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

from ..core.db import db, dumps, utcnow

log = logging.getLogger("oceantrace.ais")

ALIASES: dict[str, tuple[str, ...]] = {
    "mmsi": ("mmsi", "MMSI", "userid", "UserID"),
    "timestamp_utc": ("timestamp_utc", "timestamp", "BaseDateTime", "basedatetime", "time", "datetime", "time_utc", "TIMESTAMP UTC", "# Timestamp", "ts"),
    "longitude": ("longitude", "lon", "LON", "Longitude", "long", "x"),
    "latitude": ("latitude", "lat", "LAT", "Latitude", "y"),
    "sog_knots": ("sog_knots", "sog", "SOG", "speed", "speed_knots", "Speed"),
    "cog_deg": ("cog_deg", "cog", "COG", "course", "Course"),
    "heading_deg": ("heading_deg", "heading", "Heading", "true_heading", "TrueHeading"),
    "vessel_name": ("vessel_name", "name", "VesselName", "shipname", "ship_name", "Name"),
    "vessel_type": ("vessel_type", "type", "VesselType", "shiptype", "ship_type", "Ship type"),
    "length_m": ("length_m", "length", "Length", "loa"),
    "source": ("source", "Source", "provider"),
}


def _find_col(columns: list[str], canonical: str) -> str | None:
    lower = {c.lower().strip(): c for c in columns}
    for alias in ALIASES[canonical]:
        if alias.lower() in lower:
            return lower[alias.lower()]
    return None


def _parse_ts(series: pd.Series) -> pd.Series:
    s = series.astype(str).str.strip()
    # epoch seconds / ms
    num = pd.to_numeric(s, errors="coerce")
    if num.notna().mean() > 0.9:
        unit = "ms" if num.dropna().median() > 1e11 else "s"
        return pd.to_datetime(num, unit=unit, utc=True, errors="coerce")
    out = pd.to_datetime(s, utc=True, errors="coerce", format="ISO8601")
    if out.isna().mean() > 0.5:
        out = pd.to_datetime(s, utc=True, errors="coerce", dayfirst=False)
    return out


def import_ais_csv(case_id: str, path: Path, *, source_label: str, data_mode: str, max_rows: int = 2_000_000, filename: str | None = None) -> dict[str, Any]:
    raw = pd.read_csv(path, dtype=str, keep_default_na=False, nrows=max_rows + 1, sep=None, engine="python")
    if len(raw) > max_rows:
        raise ValueError(f"CSV exceeds max_rows={max_rows}")
    cols = list(raw.columns)
    mapping = {c: _find_col(cols, c) for c in ALIASES}
    missing = [c for c in ("mmsi", "timestamp_utc", "longitude", "latitude") if mapping[c] is None]
    if missing:
        raise ValueError(f"CSV missing required columns {missing}; found {cols[:20]}")

    df = pd.DataFrame({k: (raw[v] if v else "") for k, v in mapping.items()})
    n_total = len(df)
    reasons = pd.Series([""] * n_total, index=df.index, dtype=object)

    mmsi = pd.to_numeric(df["mmsi"].str.strip(), errors="coerce")
    bad = mmsi.isna() | (mmsi < 1_000_000) | (mmsi > 999_999_999)
    reasons[bad & (reasons == "")] = "invalid_mmsi"
    ts = _parse_ts(df["timestamp_utc"])
    bad_ts = ts.isna()
    reasons[bad_ts & (reasons == "")] = "unparseable_timestamp"
    lon = pd.to_numeric(df["longitude"], errors="coerce")
    lat = pd.to_numeric(df["latitude"], errors="coerce")
    bad_pos = lon.isna() | lat.isna() | (lon < -180) | (lon > 180) | (lat < -90) | (lat > 90) | ((lon == 0) & (lat == 0))
    reasons[bad_pos & (reasons == "")] = "invalid_position"
    sog = pd.to_numeric(df["sog_knots"], errors="coerce")
    sog = sog.where(~((sog < 0) | (sog > 102.2)), np.nan)  # 102.3 = AIS "not available"
    cog = pd.to_numeric(df["cog_deg"], errors="coerce")
    cog = cog.where(~((cog < 0) | (cog >= 360)), np.nan)  # 360 = not available
    hdg = pd.to_numeric(df["heading_deg"], errors="coerce")
    hdg = hdg.where(~((hdg < 0) | (hdg >= 360)), np.nan)  # 511 = not available
    length = pd.to_numeric(df["length_m"], errors="coerce")

    valid = reasons == ""
    n_valid = int(valid.sum())
    if n_valid == 0:
        raise ValueError("no valid AIS rows after validation (check timestamp/position columns)")
    quarantine_sample = [
        {"row": int(i) + 2, "reason": reasons[i], "mmsi": df["mmsi"][i], "timestamp": df["timestamp_utc"][i]}
        for i in reasons[~valid].index[:25]
    ]
    import_id = f"ais_{uuid.uuid4().hex[:10]}"
    tsv = ts[valid]
    epoch = pd.Series([float(t.timestamp()) for t in tsv], index=tsv.index, dtype=float)
    vt = df["vessel_type"].str.strip().str.lower().replace({"": None})
    vn = df["vessel_name"].str.strip().replace({"": None})
    src = df["source"].str.strip().replace({"": None}).fillna(source_label)
    # Non-negotiable (doc 05): synthetic AIS can never be re-labelled as real/imported. If the file
    # itself declares synthetic rows, or the case is a synthetic case, the import is forced to
    # data_mode="synthetic" and the override is recorded in the summary.
    forced_reason = None
    if data_mode != "synthetic":
        if src[valid].str.lower().str.contains("synth").any():
            forced_reason = "source column declares synthetic rows"
        else:
            with db() as conn:
                row = conn.execute("SELECT data_mode FROM cases WHERE id=?", (case_id,)).fetchone()
            if row and row["data_mode"] == "synthetic":
                forced_reason = "case is a synthetic case"
    if forced_reason:
        log.warning("AIS import for %s forced to data_mode=synthetic (%s)", case_id, forced_reason)
        data_mode = "synthetic"

    rows: Iterable[tuple[Any, ...]] = zip(
        [case_id] * n_valid, [import_id] * n_valid, mmsi[valid].astype(int).tolist(),
        tsv.dt.strftime("%Y-%m-%dT%H:%M:%SZ").tolist(), epoch.tolist(), lon[valid].tolist(), lat[valid].tolist(),
        [None if pd.isna(x) else float(x) for x in sog[valid]], [None if pd.isna(x) else float(x) for x in cog[valid]],
        [None if pd.isna(x) else float(x) for x in hdg[valid]], vn[valid].tolist(), vt[valid].tolist(),
        [None if pd.isna(x) else float(x) for x in length[valid]], src[valid].tolist(),
    )
    with db() as conn:
        conn.executemany(
            "INSERT INTO ais_positions(case_id, import_id, mmsi, ts, ts_epoch, lon, lat, sog, cog, heading, vessel_name, vessel_type, length_m, source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        conn.execute(
            "INSERT INTO ais_imports(id, case_id, source, data_mode, filename, rows_total, rows_valid, rows_quarantined, quarantine_sample, time_start, time_end, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (import_id, case_id, source_label, data_mode, filename or path.name, n_total, n_valid, n_total - n_valid, dumps(quarantine_sample),
             tsv.min().strftime("%Y-%m-%dT%H:%M:%SZ"), tsv.max().strftime("%Y-%m-%dT%H:%M:%SZ"), utcnow()),
        )
    n_vessels = int(mmsi[valid].nunique())
    summary = {
        "import_id": import_id, "case_id": case_id, "source": source_label, "data_mode": data_mode, "filename": filename or path.name,
        "rows_total": n_total, "rows_valid": n_valid, "rows_quarantined": n_total - n_valid, "vessels": n_vessels,
        "time_start_utc": tsv.min().strftime("%Y-%m-%dT%H:%M:%SZ"), "time_end_utc": tsv.max().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "bbox": [float(lon[valid].min()), float(lat[valid].min()), float(lon[valid].max()), float(lat[valid].max())],
        "quarantine_reasons": reasons[~valid].value_counts().to_dict(), "quarantine_sample": quarantine_sample, "column_mapping": mapping,
        "data_mode_forced_reason": forced_reason,
    }
    log.info("AIS import %s: %d/%d rows valid, %d vessels", import_id, n_valid, n_total, n_vessels)
    return summary


def list_imports(case_id: str) -> list[dict[str, Any]]:
    with db() as conn:
        rows = conn.execute("SELECT * FROM ais_imports WHERE case_id=? ORDER BY created_at", (case_id,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["quarantine_sample"] = None
        out.append(d)
    return out


def delete_import(case_id: str, import_id: str) -> None:
    with db() as conn:
        conn.execute("DELETE FROM ais_positions WHERE case_id=? AND import_id=?", (case_id, import_id))
        conn.execute("DELETE FROM ais_imports WHERE case_id=? AND id=?", (case_id, import_id))


def write_positions_csv(rows: list[dict[str, Any]]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=["mmsi", "timestamp_utc", "longitude", "latitude", "sog_knots", "cog_deg", "heading_deg", "vessel_name", "vessel_type", "length_m", "source"])
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return buf.getvalue()
