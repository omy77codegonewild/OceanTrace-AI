"""Live AIS recorder adapter (aisstream.io). Records real-time position reports
for a bounding box into the case's AIS store for a bounded duration.

Why: free AIS APIs give *live* data only. Recording the corridor continuously
builds the historical archive later needed for attribution. The key comes from
the environment (OT_AISSTREAM_API_KEY); it is never sent to the browser.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from ..core.config import get_settings
from ..core.db import db, dumps, utcnow

log = logging.getLogger("oceantrace.ais.live")

_TYPE_MAP = {
    30: "fishing", 31: "tug", 32: "tug", 52: "tug", 36: "sailing", 37: "pleasure", 60: "passenger",
    **{i: "passenger" for i in range(61, 70)}, 70: "cargo", **{i: "cargo" for i in range(71, 80)},
    80: "tanker", **{i: "tanker" for i in range(81, 90)}, 90: "other",
}


def ship_type_label(code: int | None) -> str | None:
    if code is None:
        return None
    return _TYPE_MAP.get(int(code), "other" if code else None)


def record_aisstream(case_id: str, bbox: list[float], minutes: float, progress: Callable[[float, str], None] | None = None) -> dict[str, Any]:
    settings = get_settings()
    key = settings.aisstream_api_key
    if not key:
        raise RuntimeError("OT_AISSTREAM_API_KEY is not configured (.env). Live AIS recording is unavailable; import a CSV instead.")
    try:
        import websockets  # noqa: WPS433
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("websockets package not installed") from e

    import_id = f"ais_{uuid.uuid4().hex[:10]}"
    min_lon, min_lat, max_lon, max_lat = bbox
    static: dict[int, dict[str, Any]] = {}
    buffered: list[tuple[Any, ...]] = []
    stats = {"positions": 0, "static": 0, "vessels": set()}
    url = "wss://stream.aisstream.io/v0/stream"

    async def _run() -> None:
        deadline = asyncio.get_event_loop().time() + minutes * 60
        async with websockets.connect(url, ping_interval=20, max_size=None) as ws:
            sub = {"APIKey": key, "BoundingBoxes": [[[min_lat, min_lon], [max_lat, max_lon]]],
                   "FilterMessageTypes": ["PositionReport", "ShipStaticData"]}
            await ws.send(json.dumps(sub))
            last_flush = asyncio.get_event_loop().time()
            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    break
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=min(remaining, 15))
                except asyncio.TimeoutError:
                    continue
                msg = json.loads(raw)
                if "error" in msg:
                    raise RuntimeError(f"aisstream error: {msg['error']}")
                mt = msg.get("MessageType")
                meta = msg.get("MetaData", {})
                mmsi = int(meta.get("MMSI", 0) or 0)
                if not mmsi:
                    continue
                if mt == "ShipStaticData":
                    sd = msg["Message"]["ShipStaticData"]
                    dim = sd.get("Dimension", {}) or {}
                    static[mmsi] = {"name": (sd.get("Name") or meta.get("ShipName") or "").strip() or None, "type": ship_type_label(sd.get("Type")),
                                    "length": (dim.get("A", 0) or 0) + (dim.get("B", 0) or 0) or None}
                    stats["static"] += 1
                    continue
                if mt == "PositionReport":
                    pr = msg["Message"]["PositionReport"]
                    ts_raw = str(meta.get("time_utc", ""))[:19].replace(" ", "T")
                    try:
                        ts = datetime.fromisoformat(ts_raw).replace(tzinfo=timezone.utc)
                    except ValueError:
                        ts = datetime.now(timezone.utc)
                    lat, lon = float(pr.get("Latitude", 0)), float(pr.get("Longitude", 0))
                    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
                        continue
                    sog = pr.get("Sog")
                    cog = pr.get("Cog")
                    hdg = pr.get("TrueHeading")
                    st = static.get(mmsi, {})
                    buffered.append((case_id, import_id, mmsi, ts.strftime("%Y-%m-%dT%H:%M:%SZ"), ts.timestamp(), lon, lat,
                                     None if sog is None or sog >= 102.3 else float(sog), None if cog is None or cog >= 360 else float(cog),
                                     None if hdg is None or hdg >= 360 else float(hdg), st.get("name") or (meta.get("ShipName") or "").strip() or None,
                                     st.get("type"), st.get("length"), "aisstream.io"))
                    stats["positions"] += 1
                    stats["vessels"].add(mmsi)
                now = asyncio.get_event_loop().time()
                if now - last_flush > 5 and buffered:
                    _flush()
                    last_flush = now
                    if progress:
                        done = 1 - remaining / (minutes * 60)
                        progress(0.05 + 0.9 * done, f"recording live AIS: {stats['positions']} positions, {len(stats['vessels'])} vessels")
        _flush()

    def _flush() -> None:
        if not buffered:
            return
        with db() as conn:
            conn.executemany(
                "INSERT INTO ais_positions(case_id, import_id, mmsi, ts, ts_epoch, lon, lat, sog, cog, heading, vessel_name, vessel_type, length_m, source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                buffered,
            )
        buffered.clear()

    started = utcnow()
    asyncio.run(_run())
    # back-fill static names for positions recorded before static data arrived
    with db() as conn:
        for mmsi, st in static.items():
            conn.execute("UPDATE ais_positions SET vessel_name=COALESCE(vessel_name, ?), vessel_type=COALESCE(vessel_type, ?), length_m=COALESCE(length_m, ?) WHERE case_id=? AND import_id=? AND mmsi=?",
                         (st.get("name"), st.get("type"), st.get("length"), case_id, import_id, mmsi))
        rng = conn.execute("SELECT MIN(ts), MAX(ts) FROM ais_positions WHERE import_id=?", (import_id,)).fetchone()
        conn.execute(
            "INSERT INTO ais_imports(id, case_id, source, data_mode, filename, rows_total, rows_valid, rows_quarantined, quarantine_sample, time_start, time_end, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (import_id, case_id, "aisstream.io live", "real", f"live-record-{started}", stats["positions"], stats["positions"], 0, dumps([]), rng[0], rng[1], utcnow()),
        )
    return {"import_id": import_id, "positions": stats["positions"], "vessels": len(stats["vessels"]), "static_messages": stats["static"], "bbox": bbox, "minutes": minutes, "data_mode": "real", "source": "aisstream.io"}
