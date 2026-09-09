"""AIS candidate search, evidence features and explainable scoring (FR-6/FR-7).

S = 100 · D · (wP·P + wT·T + wH·H + wL·L + wG·G + wV·V)

All features are in [0, 1] and every score persists the raw feature values plus
human-readable evidence strings. Output is an *investigation priority*, never a
finding of responsibility."""
from __future__ import annotations

import math
import uuid
from datetime import datetime, timezone
from typing import Any

import numpy as np
from shapely import contains_xy
from shapely.geometry import shape

from ..core.db import db
from ..geo.utils import angular_diff_deg, bearing_deg, buffer_km, haversine_km, haversine_km_vec

PRIORITY_LABELS = {"high": "High investigation priority", "medium": "Medium investigation priority", "low": "Low investigation priority", "insufficient": "Insufficient correlation"}


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _epoch(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def _local_frame(lat0: float) -> tuple[float, float]:
    lat_r = math.radians(lat0)
    m_lat = 111132.954 - 559.822 * math.cos(2 * lat_r) + 1.175 * math.cos(4 * lat_r)
    m_lon = 111412.84 * math.cos(lat_r) - 93.5 * math.cos(3 * lat_r)
    return m_lon, m_lat


def _min_distance_km_to_polygon(lons: np.ndarray, lats: np.ndarray, poly) -> tuple[np.ndarray, np.ndarray]:
    """Vectorised approx distance (km) from points to polygon boundary/interior
    using a local equirectangular frame (adequate for < ~200 km)."""
    c = poly.centroid
    m_lon, m_lat = _local_frame(c.y)
    inside = contains_xy(poly, lons, lats)
    # boundary distance: sample polygon boundary
    from shapely.geometry import LineString

    boundary = poly.boundary
    length = boundary.length
    n_s = int(min(400, max(50, length / 0.002)))
    lines = list(boundary.geoms) if hasattr(boundary, "geoms") else [boundary]
    pts = []
    for ln in lines:
        if not isinstance(ln, LineString) or ln.length == 0:
            continue
        k = max(2, int(n_s * ln.length / max(length, 1e-9)))
        pts.extend(ln.interpolate(f, normalized=True).coords[0] for f in np.linspace(0, 1, k))
    if not pts:
        pts = [(c.x, c.y)]
    B = np.array(pts)
    dx = (lons[:, None] - B[None, :, 0]) * m_lon
    dy = (lats[:, None] - B[None, :, 1]) * m_lat
    d = np.sqrt(dx * dx + dy * dy).min(axis=1) / 1000.0
    d[inside] = 0.0
    return d, inside


def load_tracks(case_id: str, bbox: list[float], t0: float, t1: float) -> dict[int, dict[str, Any]]:
    with db() as conn:
        rows = conn.execute(
            "SELECT mmsi, ts_epoch, lon, lat, sog, cog, heading, vessel_name, vessel_type, length_m, source FROM ais_positions "
            "WHERE case_id=? AND ts_epoch BETWEEN ? AND ? AND lon BETWEEN ? AND ? AND lat BETWEEN ? AND ? ORDER BY mmsi, ts_epoch",
            (case_id, t0, t1, bbox[0], bbox[2], bbox[1], bbox[3]),
        ).fetchall()
    tracks: dict[int, dict[str, Any]] = {}
    for r in rows:
        t = tracks.setdefault(int(r["mmsi"]), {"t": [], "lon": [], "lat": [], "sog": [], "cog": [], "hdg": [], "name": None, "type": None, "length": None, "source": None})
        t["t"].append(float(r["ts_epoch"]))
        t["lon"].append(float(r["lon"]))
        t["lat"].append(float(r["lat"]))
        t["sog"].append(np.nan if r["sog"] is None else float(r["sog"]))
        t["cog"].append(np.nan if r["cog"] is None else float(r["cog"]))
        t["hdg"].append(np.nan if r["heading"] is None else float(r["heading"]))
        t["name"] = t["name"] or r["vessel_name"]
        t["type"] = t["type"] or r["vessel_type"]
        t["length"] = t["length"] or r["length_m"]
        t["source"] = t["source"] or r["source"]
    for t in tracks.values():
        for k in ("t", "lon", "lat", "sog", "cog", "hdg"):
            t[k] = np.asarray(t[k], dtype=float)
    return tracks


def _expected_interval_s(all_tracks: dict[int, dict[str, Any]]) -> float:
    """Dataset-level expected reporting interval = median of per-vessel median gaps."""
    meds = []
    for t in all_tracks.values():
        if t["t"].size > 3:
            d = np.diff(t["t"])
            d = d[d > 0]
            if d.size:
                meds.append(np.median(d))
    return float(np.median(meds)) if meds else 300.0


def _type_plausibility(vtype: str | None, table: dict[str, float]) -> tuple[float, str]:
    if not vtype:
        return float(table.get("unknown", 0.5)), "unknown"
    v = vtype.lower().strip()
    if v in table:
        return float(table[v]), v
    for key, val in table.items():
        if key in v:
            return float(val), key
    if v.isdigit():
        code = int(v)
        if 80 <= code <= 89:
            return float(table.get("tanker", 1.0)), "tanker"
        if 70 <= code <= 79:
            return float(table.get("cargo", 0.7)), "cargo"
        if code == 30:
            return float(table.get("fishing", 0.4)), "fishing"
    return float(table.get("unknown", 0.5)), v


def attribute(case_id: str, hindcast: dict[str, Any], slick_centroid: list[float], acquisition_epoch: float, cfg_ais: dict[str, Any], cfg_attr: dict[str, Any],
              weights: dict[str, float] | None = None, excluded_mmsi: list[int] | None = None) -> dict[str, Any]:
    w = dict(cfg_attr.get("weights", {}))
    if weights:
        w.update({k: float(v) for k, v in weights.items() if k in w})
    tot = sum(w.values())
    if tot <= 0:
        raise ValueError("weights must sum to a positive value")
    w = {k: v / tot for k, v in w.items()}

    origin = shape(hindcast["origin_geometry"])
    if origin.is_empty:
        raise ValueError("hindcast origin geometry is empty")
    regions = hindcast.get("origin_regions", {})
    core = shape(regions["50"]["geometry"]) if "50" in regions else origin
    outer = shape(regions["90"]["geometry"]) if "90" in regions else origin
    rs, re_ = _epoch(hindcast["release_time_start_utc"]), _epoch(hindcast["release_time_end_utc"])
    search = cfg_ais.get("candidate_search", {})
    feat_cfg = cfg_ais.get("features", {})
    buf_km = float(search.get("origin_buffer_km", 25))
    pad_h = float(search.get("time_pad_hours", 6))
    search_geom = buffer_km(outer, buf_km)
    bbox = list(search_geom.bounds)
    t0, t1 = rs - pad_h * 3600, min(re_ + pad_h * 3600, acquisition_epoch + 3600)

    with db() as conn:
        n_all = conn.execute("SELECT COUNT(DISTINCT mmsi) FROM ais_positions WHERE case_id=?", (case_id,)).fetchone()[0]
        n_time = conn.execute("SELECT COUNT(DISTINCT mmsi) FROM ais_positions WHERE case_id=? AND ts_epoch BETWEEN ? AND ?", (case_id, t0, t1)).fetchone()[0]
    tracks = load_tracks(case_id, bbox, t0, t1)
    expected_s = _expected_interval_s(tracks)
    gap_mult = float(feat_cfg.get("gap_multiple_of_expected", 4.0))
    min_gap_s = float(feat_cfg.get("min_gap_minutes", 20)) * 60
    loiter_kn = float(feat_cfg.get("loiter_speed_knots", 1.5))
    loiter_min_s = float(feat_cfg.get("loiter_min_minutes", 30)) * 60
    prox_scale = float(feat_cfg.get("proximity_scale_km", 15))
    type_table = cfg_attr.get("vessel_type_plausibility", {})
    bands = cfg_attr.get("priority_bands", {"high": 80, "medium": 55, "low": 30})
    excluded = set(excluded_mmsi or [])

    o_c = origin.centroid
    slick_lon, slick_lat = slick_centroid
    source_to_slick = bearing_deg(o_c.x, o_c.y, slick_lon, slick_lat)

    # Space-time backtrack cloud (times, lon[T,K], lat[T,K]) — lets us ask "how close was the
    # vessel to where the oil WAS at that same moment", which is far more discriminating than a
    # static polygon when the window is wide.
    stc = hindcast.get("spacetime_cloud")
    st_times = st_lon = st_lat = None
    if stc and stc.get("times"):
        st_times = np.asarray(stc["times"], dtype=float)
        st_lon = np.asarray(stc["lon"], dtype=float)
        st_lat = np.asarray(stc["lat"], dtype=float)
        order = np.argsort(st_times)
        st_times, st_lon, st_lat = st_times[order], st_lon[order], st_lat[order]
    st_scale_km = float(feat_cfg.get("spacetime_scale_km", 5.0))

    filters: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    for mmsi, tr in tracks.items():
        if mmsi in excluded:
            filters.append({"mmsi": mmsi, "decision": "excluded_by_analyst"})
            continue
        n = tr["t"].size
        if n < 2:
            filters.append({"mmsi": mmsi, "decision": "dropped", "reason": "fewer than 2 positions in search window"})
            continue
        d_core, in_core = _min_distance_km_to_polygon(tr["lon"], tr["lat"], core)
        d_org, in_org = _min_distance_km_to_polygon(tr["lon"], tr["lat"], origin)
        d_out, in_out = _min_distance_km_to_polygon(tr["lon"], tr["lat"], outer)
        # ---- space-time distance: for each AIS fix inside the backtrack horizon, distance to the
        #      nearest particle of the cloud at that time (linear interpolation between steps) ----
        st_d = np.full(n, np.nan)
        if st_times is not None and st_times.size > 1:
            inside_h = (tr["t"] >= st_times[0]) & (tr["t"] <= st_times[-1])
            if inside_h.any():
                m_lon_, m_lat_ = _local_frame(float(np.mean(tr["lat"])))
                for k in np.nonzero(inside_h)[0]:
                    j = int(np.searchsorted(st_times, tr["t"][k]))
                    j0, j1 = max(0, j - 1), min(st_times.size - 1, j)
                    w1 = 0.0 if j1 == j0 else (tr["t"][k] - st_times[j0]) / max(st_times[j1] - st_times[j0], 1e-6)
                    plon = st_lon[j0] * (1 - w1) + st_lon[j1] * w1
                    plat = st_lat[j0] * (1 - w1) + st_lat[j1] * w1
                    dd = np.sqrt(((plon - tr["lon"][k]) * m_lon_) ** 2 + ((plat - tr["lat"][k]) * m_lat_) ** 2) / 1000.0
                    # distance to the 10th percentile of the particle cloud = "was the vessel where a
                    # plausible share of the oil was", robust to outlier particles
                    st_d[k] = float(np.percentile(dd, 10))
        has_st = bool(np.isfinite(st_d).any())
        # ---- P: proximity — static distance to the origin region, sharpened by the space-time match ----
        d_min = float(d_org.min())
        i_min = int(d_org.argmin())
        P_static = float(math.exp(-d_min / prox_scale))
        if has_st:
            st_min = float(np.nanmin(st_d))
            i_st = int(np.nanargmin(st_d))
            P_st = float(math.exp(-st_min / st_scale_km))
            P = 0.4 * P_static + 0.6 * P_st
            if st_min < d_min:
                i_min = i_st
        else:
            st_min, P_st = None, None
            P = P_static
        # ---- T: temporal overlap — fraction of the release window during which the vessel is inside the 90% region (interpolated) ----
        in_window = (tr["t"] >= rs) & (tr["t"] <= re_)
        near_in_window = in_window & (d_out <= 0.0)
        # time inside outer region during window (approximate by sample dwell)
        if in_window.sum() >= 1:
            dwell = 0.0
            for k in np.nonzero(near_in_window)[0]:
                prev = tr["t"][k - 1] if k > 0 else tr["t"][k]
                nxt = tr["t"][k + 1] if k + 1 < n else tr["t"][k]
                dwell += 0.5 * (min(nxt, re_) - max(prev, rs))
            dwell = max(0.0, min(dwell, re_ - rs))
            T = dwell / max(re_ - rs, 1)
            # vessels present in window but only near (not inside) the region get partial credit by distance
            if T == 0:
                dmin_w = float(d_out[in_window].min())
                T = 0.5 * math.exp(-dmin_w / prox_scale)
            # space-time: fraction of window fixes within st_scale of the oil's position at that time
            if has_st:
                stw = st_d[in_window]
                if np.isfinite(stw).any():
                    near_frac = float(np.mean(np.nan_to_num(stw, nan=1e9) <= 2 * st_scale_km))
                    T = max(T, 0.5 * near_frac + 0.5 * float(math.exp(-np.nanmin(stw) / st_scale_km)))
        else:
            dwell = 0.0
            # no report in window: nearest report time distance
            dt_h = float(min(abs(tr["t"] - rs).min(), abs(tr["t"] - re_).min())) / 3600
            T = 0.3 * math.exp(-dt_h / max(pad_h, 1))
        # ---- H: heading compatibility — track direction near closest approach vs source→slick bearing ----
        k0, k1 = max(0, i_min - 2), min(n - 1, i_min + 2)
        if k1 > k0 and haversine_km(tr["lon"][k0], tr["lat"][k0], tr["lon"][k1], tr["lat"][k1]) > 0.05:
            trk_brg = bearing_deg(tr["lon"][k0], tr["lat"][k0], tr["lon"][k1], tr["lat"][k1])
        elif not np.isnan(tr["cog"][i_min]):
            trk_brg = float(tr["cog"][i_min])
        else:
            trk_brg = None
        if trk_brg is None:
            H, h_txt = 0.5, "heading unavailable"
        else:
            diff = angular_diff_deg(trk_brg, source_to_slick)
            # a vessel steaming along or opposite the source→slick axis is compatible with a trailing discharge
            axis_diff = min(diff, 180 - diff)
            H = float(max(0.0, 1.0 - axis_diff / 90.0))
            h_txt = f"track bearing {trk_brg:.0f}° vs source→slick axis {source_to_slick:.0f}° (Δ {axis_diff:.0f}°)"
        # ---- L: loitering — slow speed dwell inside the 90% region ----
        slow = (np.nan_to_num(tr["sog"], nan=99) <= loiter_kn) & (d_out <= 2.0)
        loiter_s = 0.0
        if slow.any():
            dts = np.diff(tr["t"])
            loiter_s = float(np.sum(np.minimum(dts, 3600) * (slow[:-1] & slow[1:])))
        L = float(min(1.0, loiter_s / max(loiter_min_s * 3, 1))) if loiter_s >= loiter_min_s else float(0.3 * loiter_s / max(loiter_min_s, 1))
        # ---- G: AIS gap relevance — gaps exceeding expected interval that overlap the window while near the region ----
        dts = np.diff(tr["t"])
        gap_idx = np.nonzero(dts > max(gap_mult * expected_s, min_gap_s))[0]
        gaps: list[dict[str, Any]] = []
        G = 0.0
        for gi in gap_idx:
            ga, gb = tr["t"][gi], tr["t"][gi + 1]
            overlap = max(0.0, min(gb, re_) - max(ga, rs))
            near = min(d_out[gi], d_out[gi + 1]) <= buf_km
            gaps.append({"start_utc": _iso(ga), "end_utc": _iso(gb), "minutes": round((gb - ga) / 60, 1), "overlaps_window_minutes": round(overlap / 60, 1), "near_origin": bool(near),
                         "from": [round(float(tr["lon"][gi]), 5), round(float(tr["lat"][gi]), 5)], "to": [round(float(tr["lon"][gi + 1]), 5), round(float(tr["lat"][gi + 1]), 5)]})
            if near and overlap > 0:
                G = max(G, min(1.0, overlap / max(re_ - rs, 1)) * 0.7 + 0.3)
            elif near:
                G = max(G, 0.25)
        # ---- V: vessel type plausibility ----
        V, vtype_norm = _type_plausibility(tr["type"], type_table)
        # ---- D: data quality ----
        span_h = (tr["t"][-1] - tr["t"][0]) / 3600
        density = n / max(span_h, 0.25)
        has_meta = 0.5 * (tr["name"] is not None) + 0.5 * (tr["type"] is not None)
        D = float(np.clip(0.5 + 0.3 * min(1.0, density / 6.0) + 0.2 * has_meta, 0.3, 1.0))

        S = 100.0 * D * (w["proximity"] * P + w["temporal_overlap"] * T + w["heading_compatibility"] * H + w["loitering"] * L + w["ais_gap_relevance"] * G + w["vessel_type"] * V)
        S = float(round(S, 1))
        pr = "high" if S >= bands["high"] else "medium" if S >= bands["medium"] else "low" if S >= bands["low"] else "insufficient"
        evidence = [
            f"minimum distance to {int(hindcast.get('confidence_level', 0.7) * 100)}% origin region: {d_min:.1f} km",
            (f"space-time match: {st_min:.1f} km from the backtracked oil position at {_iso(tr['t'][i_st])}" if has_st else "space-time match unavailable (no AIS fixes inside backtrack horizon)"),
            f"inside 90% origin region for {dwell / 60:.0f} min of the {((re_ - rs) / 60):.0f}-min release window" if dwell > 0 else "not inside the 90% origin region during the release window",
            h_txt,
            f"slow-speed dwell (≤{loiter_kn} kn) near origin: {loiter_s / 60:.0f} min",
            (f"{len(gaps)} AIS gap(s) > {max(gap_mult * expected_s, min_gap_s) / 60:.0f} min; max overlap with window {max((g['overlaps_window_minutes'] for g in gaps), default=0):.0f} min" if gaps else f"no AIS gaps above {max(gap_mult * expected_s, min_gap_s) / 60:.0f} min (expected interval {expected_s / 60:.1f} min)"),
            f"vessel type '{tr['type'] or 'unknown'}' plausibility {V:.2f}",
            f"{n} positions over {span_h:.1f} h ({density:.1f}/h); metadata completeness {has_meta:.1f}",
        ]
        limitations = ["Investigation priority only — not evidence of responsibility.", "Origin region and release window are model estimates with stated uncertainty."]
        if tr["type"] is None:
            limitations.append("Vessel type unknown; type plausibility uses the neutral prior.")
        if n < 5:
            limitations.append("Sparse track: fewer than 5 AIS positions in the search window.")
        step = max(1, n // 400)
        results.append({
            "mmsi": mmsi, "vessel": {"name": tr["name"], "type": tr["type"], "type_normalized": vtype_norm, "length_m": tr["length"], "source": tr["source"]},
            "score": S, "priority": pr, "priority_label": PRIORITY_LABELS[pr],
            "factors": {"proximity": round(P, 3), "temporal_overlap": round(T, 3), "heading_compatibility": round(H, 3), "loitering": round(L, 3), "ais_gap_relevance": round(G, 3), "vessel_type": round(V, 3), "data_quality": round(D, 3)},
            "raw": {"min_distance_km": round(d_min, 2), "spacetime_min_km": None if not has_st else round(st_min, 2), "closest_time_utc": _iso(tr["t"][i_min]), "closest_point": [round(float(tr["lon"][i_min]), 5), round(float(tr["lat"][i_min]), 5)], "dwell_minutes": round(dwell / 60, 1),
                    "loiter_minutes": round(loiter_s / 60, 1), "gaps": gaps, "positions": int(n), "span_hours": round(span_h, 2), "track_bearing_deg": None if trk_brg is None else round(trk_brg, 1), "source_to_slick_bearing_deg": round(source_to_slick, 1),
                    "in_core_region": bool(in_core.any()), "in_origin_region": bool(in_org.any())},
            "evidence": evidence, "limitations": limitations,
            "track": {"type": "Feature", "geometry": {"type": "LineString", "coordinates": [[round(float(x), 5), round(float(y), 5)] for x, y in zip(tr["lon"][::step], tr["lat"][::step])]},
                       "properties": {"mmsi": mmsi, "times": [_iso(t) for t in tr["t"][::step]], "sog": [None if np.isnan(v) else round(float(v), 1) for v in tr["sog"][::step]]}},
        })
        filters.append({"mmsi": mmsi, "decision": "scored", "score": S})

    results.sort(key=lambda r: -r["score"])
    for i, r in enumerate(results, start=1):
        r["rank"] = i
    top_n = int(cfg_attr.get("top_n", 10))
    summary = {
        "vessels_in_dataset": int(n_all), "vessels_in_time_window": int(n_time), "vessels_in_search_region": len(tracks), "vessels_scored": len(results),
        "search_bbox": [round(v, 4) for v in bbox], "search_buffer_km": buf_km, "search_time_start_utc": _iso(t0), "search_time_end_utc": _iso(t1),
        "release_window": [hindcast["release_time_start_utc"], hindcast["release_time_end_utc"]], "expected_report_interval_min": round(expected_s / 60, 1),
        "weights": w, "search_geometry": search_geom.__geo_interface__, "dark_source_possible": len(results) == 0 or (results and results[0]["score"] < bands["medium"]),
        "ais_data_modes": _ais_data_modes(case_id),
        "note": "Ranking is an investigation priority under model assumptions; it is not a legal finding.",
    }
    return {"candidates": results[:top_n], "all_scored": len(results), "filters": filters, "summary": summary}


def _ais_data_modes(case_id: str) -> list[str]:
    """Distinct provenance labels of the AIS imports this ranking was computed from (persisted
    with the run so the export stays honest even if imports are later deleted)."""
    with db() as conn:
        rows = conn.execute("SELECT DISTINCT data_mode FROM ais_imports WHERE case_id=? ORDER BY data_mode", (case_id,)).fetchall()
    return [r["data_mode"] for r in rows]


def persist_attribution(case_id: str, hindcast_run_id: str, result: dict[str, Any]) -> str:
    from ..core.db import dumps, utcnow

    run_id = f"attr_{uuid.uuid4().hex[:10]}"
    with db() as conn:
        conn.execute("INSERT INTO attribution_runs(id, case_id, hindcast_run_id, weights, summary, created_at) VALUES (?,?,?,?,?,?)",
                     (run_id, case_id, hindcast_run_id, dumps(result["summary"]["weights"]), dumps({k: v for k, v in result["summary"].items() if k != "search_geometry"} | {"filters": result["filters"][:500]}), utcnow()))
        for c in result["candidates"]:
            conn.execute("INSERT INTO vessel_scores(id, attribution_run_id, case_id, mmsi, rank, score, priority, factors, evidence, limitations, vessel, track) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                         (f"vs_{uuid.uuid4().hex[:10]}", run_id, case_id, c["mmsi"], c["rank"], c["score"], c["priority"], dumps(c["factors"] | {"raw": c["raw"]}), dumps(c["evidence"]), dumps(c["limitations"]), dumps(c["vessel"]), dumps(c["track"])))
    return run_id
