"""Case service: orchestrates modules and persists artifacts. Routers stay thin."""
from __future__ import annotations

import json
import logging
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
from shapely.geometry import shape

from ..ais.importer import import_ais_csv, list_imports
from ..attribution.scoring import attribute, persist_attribution
from ..core.config import get_algo_config, get_settings
from ..core.db import audit, db, dumps, loads, row_to_dict, utcnow
from ..core.jobs import JobContext, submit
from ..detection.pipeline import adapter_status, run_detection
from ..environment.fields import EnvField, EnvironmentUnavailable, build_provider
from ..geo.utils import geodesic_area_km2, shape_metrics
from ..scenes.ingest import SceneValidationError, ingest_scene
from ..trajectory.lagrangian import analyse, forecast_summary, integrate, validate_params

log = logging.getLogger("oceantrace.service")


class NotFound(LookupError):
    pass


def _case_dir(case_id: str) -> Path:
    d = get_settings().data_dir / "cases" / case_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _epoch(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# cases
# ---------------------------------------------------------------------------
def create_case(name: str, data_mode: str, notes: str | None = None) -> dict[str, Any]:
    case_id = f"case_{uuid.uuid4().hex[:10]}"
    snap = get_algo_config().snapshot()
    now = utcnow()
    with db() as conn:
        conn.execute("INSERT INTO cases(id, name, status, data_mode, created_at, updated_at, config_snapshot, notes) VALUES (?,?,?,?,?,?,?,?)",
                     (case_id, name, "created", data_mode, now, now, dumps(snap), notes))
    audit(case_id, "case.create", {"name": name, "data_mode": data_mode, "config_hash": snap["config_hash"]})
    return get_case(case_id)


def list_cases() -> list[dict[str, Any]]:
    with db() as conn:
        rows = conn.execute("SELECT id, name, status, data_mode, created_at, updated_at FROM cases ORDER BY created_at DESC").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            sc = conn.execute("SELECT id, acquisition_time FROM scenes WHERE case_id=? ORDER BY created_at DESC LIMIT 1", (d["id"],)).fetchone()
            d["scene_id"] = sc["id"] if sc else None
            d["acquisition_time_utc"] = sc["acquisition_time"] if sc else None
            d["n_slicks"] = conn.execute("SELECT COUNT(*) FROM slicks WHERE case_id=?", (d["id"],)).fetchone()[0]
            out.append(d)
    return out


def _set_status(case_id: str, status: str) -> None:
    with db() as conn:
        conn.execute("UPDATE cases SET status=?, updated_at=? WHERE id=?", (status, utcnow(), case_id))


def get_case(case_id: str) -> dict[str, Any]:
    with db() as conn:
        row = conn.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
        if not row:
            raise NotFound(f"case {case_id} not found")
        case = row_to_dict(row, ("config_snapshot",))
        scene = conn.execute("SELECT * FROM scenes WHERE case_id=? ORDER BY created_at DESC LIMIT 1", (case_id,)).fetchone()
        case["scene"] = _scene_dict(scene)
        slicks = conn.execute("SELECT * FROM slicks WHERE case_id=? ORDER BY json_extract(properties,'$.rank')", (case_id,)).fetchall()
        case["slicks"] = {"type": "FeatureCollection", "features": [_slick_feature(s) for s in slicks]}
        env = conn.execute("SELECT * FROM env_fields WHERE case_id=? ORDER BY created_at DESC LIMIT 1", (case_id,)).fetchone()
        case["environment"] = row_to_dict(env, ("coverage", "metadata"))
        hc = conn.execute("SELECT * FROM hindcast_runs WHERE case_id=? AND status='completed' ORDER BY created_at DESC LIMIT 1", (case_id,)).fetchone()
        case["hindcast"] = _hindcast_dict(hc, full=False)
        runs = []
        for r in conn.execute("SELECT id, slick_id, release_start, release_end, status, created_at, json_extract(config,'$.hindcast_hours') AS hindcast_hours, json_extract(config,'$.ensemble_members') AS ensemble_members, json_extract(config,'$.particle_count') AS particle_count, json_extract(config,'$.windage_range') AS windage_range, json_extract(config,'$.current_scale_range') AS current_scale_range, json_extract(metrics,'$.origin_areas_km2') AS origin_areas_km2, json_extract(metrics,'$.environment') AS environment, json_extract(metrics,'$.window_basis') AS window_basis FROM hindcast_runs WHERE case_id=? ORDER BY created_at DESC", (case_id,)).fetchall():
            d = dict(r)
            for k in ("windage_range", "current_scale_range", "origin_areas_km2", "environment"):
                if isinstance(d.get(k), str):
                    d[k] = json.loads(d[k])
            runs.append(d)
        case["hindcast_runs"] = runs
        at = conn.execute("SELECT * FROM attribution_runs WHERE case_id=? ORDER BY created_at DESC LIMIT 1", (case_id,)).fetchone()
        case["attribution"] = _attribution_dict(conn, at)
        case["attribution_runs"] = [dict(r) for r in conn.execute("SELECT id, hindcast_run_id, weights, created_at FROM attribution_runs WHERE case_id=? ORDER BY created_at DESC", (case_id,)).fetchall()]
        case["ais_imports"] = list_imports(case_id)
        n_pos = conn.execute("SELECT COUNT(*), COUNT(DISTINCT mmsi), MIN(ts), MAX(ts) FROM ais_positions WHERE case_id=?", (case_id,)).fetchone()
        case["ais_summary"] = {"positions": n_pos[0], "vessels": n_pos[1], "time_start_utc": n_pos[2], "time_end_utc": n_pos[3]}
        case["detector"] = adapter_status()
        case["config_hash"] = case["config_snapshot"].get("config_hash") if case.get("config_snapshot") else None
        case.pop("config_snapshot", None)
    return case


def _scene_dict(row) -> dict[str, Any] | None:
    if not row:
        return None
    d = row_to_dict(row, ("bounds", "metadata"))
    d["preview_url"] = f"/api/v1/scenes/{d['id']}/preview.png"
    return d


def _slick_feature(row) -> dict[str, Any]:
    props = loads(row["properties"], {})
    props.update({"class": row["class"], "confidence": row["confidence"], "review_status": row["review_status"], "review_note": row["review_note"]})
    return {"type": "Feature", "id": row["id"], "geometry": loads(row["geom"]), "properties": props}


def _env_vectors(env_path: Path, times_utc: list[str], bbox: list[float], nx: int = 9, ny: int = 7) -> dict[str, Any] | None:
    """Current/wind vectors resampled on a regular grid over `bbox` for every replay step
    (compact arrays; the UI turns them into arrows). Values are straight from the stored
    forcing field used by the hindcast — no smoothing beyond the field's own interpolation."""
    try:
        if not env_path.exists() or not times_utc:
            return None
        field = EnvField.load(env_path)
        lons = np.linspace(bbox[0], bbox[2], nx)
        lats = np.linspace(bbox[1], bbox[3], ny)
        LON, LAT = np.meshgrid(lons, lats)
        flat_lon, flat_lat = LON.ravel(), LAT.ravel()
        steps = []
        for t in times_utc:
            epoch = datetime.strptime(t, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
            uc, vc, uw, vw = field.sample(flat_lon, flat_lat, epoch)
            arr = np.stack([uc, vc, uw, vw], axis=1)
            arr = np.where(np.isfinite(arr), arr, 0.0)
            steps.append(np.round(arr, 3).tolist())
        return {"grid": [[round(float(x), 4), round(float(y), 4)] for x, y in zip(flat_lon, flat_lat)], "times_utc": times_utc, "steps": steps,
                "columns": ["u_current_ms", "v_current_ms", "u_wind10_ms", "v_wind10_ms"], "provider": field.provider, "data_mode": field.data_mode}
    except Exception as e:  # pragma: no cover - diagnostics only
        log.warning("env vectors unavailable: %s", e)
        return None


def _hindcast_dict(row, full: bool, include_spacetime: bool = False) -> dict[str, Any] | None:
    if not row:
        return None
    d = row_to_dict(row, ("origin_geom", "metrics", "config"))
    d["origin_geometry"] = d.pop("origin_geom")
    if full and d.get("artifact_uri") and Path(d["artifact_uri"]).exists():
        d["detail"] = json.loads(Path(d["artifact_uri"]).read_text())
        if not include_spacetime:
            d["detail"].pop("spacetime_cloud", None)
        # met-ocean vectors for every replay step over the backtrack corridor (for the map arrows)
        env = d["detail"].get("environment") or {}
        env_id = env.get("env_id") or d.get("env_field_id")
        cloud = d["detail"].get("cloud") or []
        if env_id and cloud:
            pts = np.array([p for step in cloud for p in step], dtype="float64")
            if pts.size:
                w = max(float(pts[:, 0].max() - pts[:, 0].min()), 0.05)
                h = max(float(pts[:, 1].max() - pts[:, 1].min()), 0.05)
                bbox = [float(pts[:, 0].min() - 0.35 * w), float(pts[:, 1].min() - 0.35 * h), float(pts[:, 0].max() + 0.35 * w), float(pts[:, 1].max() + 0.35 * h)]
                env_path = _case_dir(d["case_id"]) / "env" / f"{env_id}.npz"
                d["detail"]["env_vectors"] = _env_vectors(env_path, d["detail"].get("cloud_steps") or [], bbox)
    return d


def _attribution_dict(conn, row) -> dict[str, Any] | None:
    if not row:
        return None
    d = row_to_dict(row, ("weights", "summary"))
    cands = conn.execute("SELECT * FROM vessel_scores WHERE attribution_run_id=? ORDER BY rank", (row["id"],)).fetchall()
    out = []
    for c in cands:
        cd = row_to_dict(c, ("factors", "evidence", "limitations", "vessel", "track"))
        cd["raw"] = cd["factors"].pop("raw", {}) if cd.get("factors") else {}
        out.append(cd)
    d["candidates"] = out
    return d


# ---------------------------------------------------------------------------
# scene + detection
# ---------------------------------------------------------------------------
def ingest_scene_job(case_id: str, upload_path: Path, original_name: str, manual_bounds: list[float] | None, acquisition_time_utc: str | None, run_detect: bool, detect_overrides: dict[str, Any] | None) -> str:
    get_case(case_id)
    cfg = get_algo_config()

    def _job(ctx: JobContext) -> dict[str, Any]:
        ctx.progress(0.05, "validating raster and georeference")
        scene_id = f"scene_{uuid.uuid4().hex[:10]}"
        scene_dir = _case_dir(case_id) / scene_id
        scene_dir.mkdir(parents=True, exist_ok=True)
        dest = scene_dir / ("original" + Path(original_name).suffix.lower())
        shutil.move(str(upload_path), dest)
        try:
            meta = ingest_scene(dest, scene_dir, original_name=original_name, manual_bounds=manual_bounds, acquisition_time_utc=acquisition_time_utc,
                                preview_max_px=int(cfg.get("scene.preview_max_px", 2048)), working_crs=str(cfg.get("scene.working_crs", "EPSG:4326")),
                                max_analysis_px=int(cfg.get("scene.max_analysis_px", 4000)))
        except SceneValidationError as e:
            raise ValueError(f"scene validation failed: {e}") from e
        meta["original_filename"] = original_name
        if meta.get("sensor") == "unknown":
            t, src = None, None
            from ..scenes.ingest import parse_time_from_name
            t, src = parse_time_from_name(original_name)
            if src == "sentinel1_filename":
                meta["sensor"] = "Sentinel-1"
        with db() as conn:
            conn.execute("DELETE FROM slicks WHERE case_id=?", (case_id,))
            conn.execute("INSERT INTO scenes(id, case_id, acquisition_time, bounds, asset_uri, preview_uri, metadata, created_at) VALUES (?,?,?,?,?,?,?,?)",
                         (scene_id, case_id, meta["acquisition_time_utc"], dumps(meta["bounds"]), str(scene_dir / "analysis.tif"), str(scene_dir / "preview.png"), dumps(meta), utcnow()))
        _set_status(case_id, "scene_ready")
        audit(case_id, "scene.ingest", {"scene_id": scene_id, "file": original_name, "acquisition_time_utc": meta["acquisition_time_utc"], "georef_source": meta["georef_source"]})
        ctx.progress(0.1, "scene ready")
        result: dict[str, Any] = {"scene_id": scene_id, "scene": meta}
        if run_detect:
            result["detection"] = _detect(case_id, scene_id, scene_dir, detect_overrides, ctx)
        return result

    return submit("scene_ingest" + ("+detect" if run_detect else ""), case_id, _job)


def _detect(case_id: str, scene_id: str, scene_dir: Path, overrides: dict[str, Any] | None, ctx: JobContext) -> dict[str, Any]:
    ctx.progress(0.12, "running slick detection")
    fc = run_detection(scene_dir / "analysis.tif", scene_dir / "detection", scene_id, overrides=overrides, progress=ctx.progress)
    with db() as conn:
        conn.execute("DELETE FROM slicks WHERE case_id=?", (case_id,))
        for f in fc["features"]:
            p = f["properties"]
            conn.execute("INSERT INTO slicks(id, case_id, scene_id, geom, class, confidence, properties, created_at) VALUES (?,?,?,?,?,?,?,?)",
                         (f["id"], case_id, scene_id, dumps(f["geometry"]), p["class"], p["confidence"], dumps(p), utcnow()))
        conn.execute("UPDATE scenes SET metadata=json_set(metadata, '$.detection_provenance', json(?)) WHERE id=?", (dumps({k: v for k, v in fc["provenance"].items() if k != "tiles_manifest"}), scene_id))
    (scene_dir / "detection" / "slicks.geojson").write_text(dumps(fc))
    _set_status(case_id, "detected" if fc["features"] else "no_slick")
    audit(case_id, "detection.run", {"scene_id": scene_id, "n_features": len(fc["features"]), "adapter": fc["provenance"]["adapter"], "model_version": fc["provenance"]["model_version"]})
    return {"n_features": len(fc["features"]), "provenance": {k: v for k, v in fc["provenance"].items() if k != "tiles_manifest"}}


def catalog_fetch_job(case_id: str, item_id: str, bbox: list[float], polarization: str, run_detect: bool, detect_overrides: dict[str, Any] | None) -> str:
    """Pull a real Sentinel-1 GRD subset from the Planetary Computer catalog and ingest it."""
    from ..scenes.catalog import fetch_subset

    get_case(case_id)
    cfg = get_algo_config()

    def _job(ctx: JobContext) -> dict[str, Any]:
        ctx.progress(0.03, f"fetching Sentinel-1 subset {item_id} via HTTP range reads")
        incoming = get_settings().data_dir / "scenes" / "_incoming" / f"{uuid.uuid4().hex}_{item_id}_{polarization}.tif"
        info = fetch_subset(item_id, bbox, incoming, polarization=polarization, max_px=int(cfg.get("scene.max_analysis_px", 4000)))
        ctx.progress(0.2, "subset ready; validating and building preview")
        scene_id = f"scene_{uuid.uuid4().hex[:10]}"
        scene_dir = _case_dir(case_id) / scene_id
        scene_dir.mkdir(parents=True, exist_ok=True)
        dest = scene_dir / "original.tif"
        shutil.move(str(incoming), dest)
        fname = f"{item_id}_{polarization.upper()}_subset.tif"
        meta = ingest_scene(dest, scene_dir, original_name=fname, preview_max_px=int(cfg.get("scene.preview_max_px", 2048)), working_crs=str(cfg.get("scene.working_crs", "EPSG:4326")),
                            max_analysis_px=int(cfg.get("scene.max_analysis_px", 4000)))
        meta.update({"source_type": "stac", "sensor": "Sentinel-1", "stac_item_id": item_id, "catalog": info, "original_filename": fname, "polarization": polarization.upper()})
        with db() as conn:
            conn.execute("DELETE FROM slicks WHERE case_id=?", (case_id,))
            conn.execute("INSERT INTO scenes(id, case_id, acquisition_time, bounds, asset_uri, preview_uri, metadata, created_at) VALUES (?,?,?,?,?,?,?,?)",
                         (scene_id, case_id, meta["acquisition_time_utc"], dumps(meta["bounds"]), str(scene_dir / "analysis.tif"), str(scene_dir / "preview.png"), dumps(meta), utcnow()))
        _set_status(case_id, "scene_ready")
        audit(case_id, "scene.catalog_fetch", {"scene_id": scene_id, "stac_item_id": item_id, "bbox": bbox, "acquisition_time_utc": meta["acquisition_time_utc"]})
        result: dict[str, Any] = {"scene_id": scene_id, "scene": meta}
        if run_detect:
            result["detection"] = _detect(case_id, scene_id, scene_dir, detect_overrides, ctx)
        return result

    return submit("catalog_fetch" + ("+detect" if run_detect else ""), case_id, _job)


def detect_job(case_id: str, overrides: dict[str, Any] | None) -> str:
    case = get_case(case_id)
    if not case["scene"]:
        raise ValueError("case has no scene; upload a SAR image first")
    scene_id = case["scene"]["id"]
    scene_dir = Path(case["scene"]["asset_uri"]).parent
    return submit("detect", case_id, lambda ctx: _detect(case_id, scene_id, scene_dir, overrides, ctx))


# ---------------------------------------------------------------------------
# environment + hindcast
# ---------------------------------------------------------------------------
def _load_env(case_id: str, bounds: list[float], t_start: datetime, t_end: datetime, provider_name: str, netcdf_path: Path | None, ctx: JobContext | None) -> tuple[EnvField, str]:
    cfg = get_algo_config().section("environment")
    provider = build_provider(provider_name, cfg, netcdf_path)
    if ctx:
        ctx.progress(0.12, f"fetching wind/current fields from {provider_name}")
    field = provider.fetch(bounds, t_start, t_end)
    ok, problems = field.covers(t_start.timestamp(), t_end.timestamp(), bounds)
    env_id = f"env_{uuid.uuid4().hex[:10]}"
    path = _case_dir(case_id) / "env" / f"{env_id}.npz"
    checksum = field.save(path)
    coverage = {"adequate": ok, "problems": problems, "requested_time_start_utc": t_start.strftime("%Y-%m-%dT%H:%M:%SZ"), "requested_time_end_utc": t_end.strftime("%Y-%m-%dT%H:%M:%SZ"), "requested_bounds": bounds}
    with db() as conn:
        conn.execute("INSERT INTO env_fields(id, case_id, provider, data_mode, coverage, artifact_uri, metadata, created_at) VALUES (?,?,?,?,?,?,?,?)",
                     (env_id, case_id, field.provider, field.data_mode, dumps(coverage), str(path), dumps(field.summary() | {"checksum": checksum}), utcnow()))
    audit(case_id, "environment.fetch", {"env_id": env_id, "provider": field.provider, "data_mode": field.data_mode, "coverage_ok": ok})
    return field, env_id


def hindcast_job(case_id: str, slick_id: str, params: dict[str, Any], environment_source: str, netcdf_path: Path | None) -> str:
    case = get_case(case_id)
    feat = next((f for f in case["slicks"]["features"] if f["id"] == slick_id), None)
    if not feat:
        raise NotFound(f"slick {slick_id} not found in case {case_id}")
    cfg = get_algo_config().section("hindcast")
    hp = validate_params(params, cfg["defaults"], cfg["limits"], direction="backward")
    acq = datetime.fromisoformat(case["scene"]["acquisition_time"].replace("Z", "+00:00"))
    geom = shape(feat["geometry"])
    area = feat["properties"].get("area_km2") or geodesic_area_km2(geom)
    run_id = f"hc_{uuid.uuid4().hex[:10]}"

    def _job(ctx: JobContext) -> dict[str, Any]:
        with db() as conn:
            conn.execute("INSERT INTO hindcast_runs(id, case_id, slick_id, env_field_id, origin_geom, release_start, release_end, metrics, config, artifact_uri, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                         (run_id, case_id, slick_id, None, None, None, None, dumps({}), dumps(hp.to_dict() | {"environment_source": environment_source}), None, "running", utcnow()))
        t_start = acq - timedelta(hours=hp.hindcast_hours + 1)
        # env AOI: slick bounds padded by plausible drift distance (1 m/s * hours)
        b = list(geom.bounds)
        pad = min(3.0, 0.02 + hp.hindcast_hours * 3600 * 1.0 / 111000.0)
        aoi = [b[0] - pad, b[1] - pad, b[2] + pad, b[3] + pad]
        try:
            field, env_id = _load_env(case_id, aoi, t_start, acq + timedelta(hours=1), environment_source, netcdf_path, ctx)
        except EnvironmentUnavailable as e:
            with db() as conn:
                conn.execute("UPDATE hindcast_runs SET status='failed', metrics=? WHERE id=?", (dumps({"error": str(e)}), run_id))
            raise
        ctx.progress(0.25, f"integrating {hp.ensemble_members}×{hp.particle_count} particles backward {hp.hindcast_hours} h ({hp.integrator})")
        res = integrate(geom, field, acq.timestamp(), hp, progress=ctx.progress)
        ctx.progress(0.78, "computing convergence metrics and origin window")
        out = analyse(res, float(area), cfg.get("origin_window", {}), progress=ctx.progress)
        cov_ok, cov_problems = field.covers(t_start.timestamp(), acq.timestamp(), aoi)
        if not cov_ok:
            out["limitations"].extend([f"Environmental coverage: {p}" for p in cov_problems])
        out["limitations"].append(f"Environmental data: {field.summary().get('source', field.provider)} [{field.data_mode.upper()}]")
        detail = {"run_id": run_id, "slick_id": slick_id, "params": hp.to_dict(), "environment": field.summary() | {"env_id": env_id}, **out}
        artifact = _case_dir(case_id) / "hindcast" / f"{run_id}.json"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(dumps(detail))
        metrics = {"window_basis": out["window_basis"], "window_hours_back": out["window_hours_back"], "peak_hours_back": out["peak_hours_back"], "origin_area_km2": out["origin_regions"][str(int(round(hp.probability_mass * 100)))]["area_km2"],
                   "origin_areas_km2": {k: v["area_km2"] for k, v in out["origin_regions"].items()}, "stranded_fraction": out["stranded_fraction"], "ensemble_members": hp.ensemble_members, "particle_count": hp.particle_count,
                   "confidence_level": hp.probability_mass, "limitations": out["limitations"], "environment": {"provider": field.provider, "data_mode": field.data_mode, "env_id": env_id, "source": field.summary().get("source")}}
        with db() as conn:
            conn.execute("UPDATE hindcast_runs SET env_field_id=?, origin_geom=?, release_start=?, release_end=?, metrics=?, artifact_uri=?, status='completed' WHERE id=?",
                         (env_id, dumps(out["origin_geometry"]), out["release_time_start_utc"], out["release_time_end_utc"], dumps(metrics), str(artifact), run_id))
        _set_status(case_id, "hindcast_ready")
        audit(case_id, "hindcast.run", {"run_id": run_id, "slick_id": slick_id, "params": hp.to_dict(), "release_window": [out["release_time_start_utc"], out["release_time_end_utc"]], "env_id": env_id})
        return {"hindcast_run_id": run_id, "release_time_start_utc": out["release_time_start_utc"], "release_time_end_utc": out["release_time_end_utc"], "window_basis": out["window_basis"], "origin_area_km2": metrics["origin_area_km2"], "environment": metrics["environment"]}

    return submit("hindcast", case_id, _job)


def forecast_job(case_id: str, slick_id: str, params: dict[str, Any], environment_source: str) -> str:
    case = get_case(case_id)
    feat = next((f for f in case["slicks"]["features"] if f["id"] == slick_id), None)
    if not feat:
        raise NotFound(f"slick {slick_id} not found")
    cfg = get_algo_config().section("hindcast")
    p = dict(params)
    p.setdefault("hindcast_hours", cfg.get("forecast", {}).get("hours", 24))
    hp = validate_params(p, cfg["defaults"], cfg["limits"], direction="forward")
    acq = datetime.fromisoformat(case["scene"]["acquisition_time"].replace("Z", "+00:00"))
    geom = shape(feat["geometry"])

    def _job(ctx: JobContext) -> dict[str, Any]:
        b = list(geom.bounds)
        pad = min(3.0, 0.02 + hp.hindcast_hours * 3600 / 111000.0)
        aoi = [b[0] - pad, b[1] - pad, b[2] + pad, b[3] + pad]
        field, env_id = _load_env(case_id, aoi, acq - timedelta(hours=1), acq + timedelta(hours=hp.hindcast_hours + 1), environment_source, None, ctx)
        ctx.progress(0.25, f"integrating forward {hp.hindcast_hours} h")
        res = integrate(geom, field, acq.timestamp(), hp, progress=ctx.progress)
        out = forecast_summary(res, cfg.get("origin_window", {}))
        out["params"] = hp.to_dict()
        out["hours"] = hp.hindcast_hours
        out["created_at"] = utcnow()
        out["environment"] = field.summary() | {"env_id": env_id}
        out["slick_id"] = slick_id
        out["limitations"] = ["Forward drift under model assumptions using forecast/analysis winds and currents; weathering, spreading and evaporation are not modelled."]
        path = _case_dir(case_id) / "forecast" / f"{slick_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(dumps(out))
        audit(case_id, "forecast.run", {"slick_id": slick_id, "hours": hp.hindcast_hours, "env_id": env_id})
        return {"slick_id": slick_id, "hours": hp.hindcast_hours, "envelope_area_km2": out["envelope_area_km2"], "coastal_impact_risk": out["coastal_impact_risk"]}

    return submit("forecast", case_id, _job)


def get_forecast(case_id: str, slick_id: str) -> dict[str, Any] | None:
    get_case(case_id)  # NotFound for unknown case
    path = _case_dir(case_id) / "forecast" / f"{slick_id}.json"
    return json.loads(path.read_text()) if path.exists() else None


def get_hindcast(case_id: str, run_id: str | None = None, include_spacetime: bool = False) -> dict[str, Any] | None:
    with db() as conn:
        if run_id:
            row = conn.execute("SELECT * FROM hindcast_runs WHERE case_id=? AND id=?", (case_id, run_id)).fetchone()
        else:
            row = conn.execute("SELECT * FROM hindcast_runs WHERE case_id=? AND status='completed' ORDER BY created_at DESC LIMIT 1", (case_id,)).fetchone()
    return _hindcast_dict(row, full=True, include_spacetime=include_spacetime)


# ---------------------------------------------------------------------------
# AIS + attribution
# ---------------------------------------------------------------------------
def ais_import_job(case_id: str, path: Path, filename: str, source_label: str, data_mode: str) -> str:
    get_case(case_id)
    max_rows = int(get_algo_config().get("ais.csv.max_rows", 2_000_000))

    def _job(ctx: JobContext) -> dict[str, Any]:
        ctx.progress(0.1, "validating AIS CSV")
        summary = import_ais_csv(case_id, path, source_label=source_label, data_mode=data_mode, max_rows=max_rows, filename=filename)
        _set_status(case_id, "ais_loaded")
        audit(case_id, "ais.import", {k: summary[k] for k in ("import_id", "rows_valid", "rows_quarantined", "vessels", "data_mode", "source")})
        return summary

    return submit("ais_import", case_id, _job)


def ais_live_record_job(case_id: str, bbox: list[float], minutes: float) -> str:
    from ..ais.live import record_aisstream

    get_case(case_id)

    def _job(ctx: JobContext) -> dict[str, Any]:
        ctx.progress(0.02, "connecting to aisstream.io")
        out = record_aisstream(case_id, bbox, minutes, progress=ctx.progress)
        audit(case_id, "ais.live_record", out)
        return out

    return submit("ais_live_record", case_id, _job)


def attribute_job(case_id: str, hindcast_run_id: str | None, weights: dict[str, float] | None, excluded_mmsi: list[int] | None) -> str:
    case = get_case(case_id)
    hc = get_hindcast(case_id, hindcast_run_id, include_spacetime=True)
    if not hc or hc.get("status") != "completed":
        raise ValueError("no completed hindcast run; run a hindcast first")
    if case["ais_summary"]["positions"] == 0:
        raise ValueError("no AIS positions loaded; import an AIS CSV or record live AIS first")
    feat = next((f for f in case["slicks"]["features"] if f["id"] == hc["slick_id"]), None)
    centroid = feat["properties"].get("centroid") if feat else None
    if not centroid:
        c = shape(feat["geometry"]).centroid if feat else shape(hc["origin_geometry"]).centroid
        centroid = [c.x, c.y]
    acq = _epoch(case["scene"]["acquisition_time"])
    cfg = get_algo_config()
    detail = hc.get("detail") or {}
    hindcast_payload = {"origin_geometry": hc["origin_geometry"], "origin_regions": detail.get("origin_regions", {}), "release_time_start_utc": hc["release_start"], "release_time_end_utc": hc["release_end"],
                        "confidence_level": hc["metrics"].get("confidence_level", 0.7), "spacetime_cloud": detail.get("spacetime_cloud")}

    def _job(ctx: JobContext) -> dict[str, Any]:
        ctx.progress(0.1, "querying AIS tracks around origin region and release window")
        res = attribute(case_id, hindcast_payload, centroid, acq, cfg.section("ais"), cfg.section("attribution"), weights=weights, excluded_mmsi=excluded_mmsi)
        ctx.progress(0.9, f"{res['all_scored']} vessels scored")
        run_id = persist_attribution(case_id, hc["id"], res)
        _set_status(case_id, "attributed")
        audit(case_id, "attribution.run", {"run_id": run_id, "hindcast_run_id": hc["id"], "weights": res["summary"]["weights"], "vessels_scored": res["all_scored"], "excluded": excluded_mmsi or []})
        return {"attribution_run_id": run_id, "vessels_scored": res["all_scored"], "top": [{"mmsi": c["mmsi"], "score": c["score"], "priority": c["priority"]} for c in res["candidates"][:3]], "summary": {k: v for k, v in res["summary"].items() if k != "search_geometry"}}

    return submit("attribution", case_id, _job)


def get_attribution(case_id: str, run_id: str | None = None) -> dict[str, Any] | None:
    with db() as conn:
        if run_id:
            row = conn.execute("SELECT * FROM attribution_runs WHERE case_id=? AND id=?", (case_id, run_id)).fetchone()
        else:
            row = conn.execute("SELECT * FROM attribution_runs WHERE case_id=? ORDER BY created_at DESC LIMIT 1", (case_id,)).fetchone()
        return _attribution_dict(conn, row)


def ais_tracks_geojson(case_id: str, t0: str | None, t1: str | None, bbox: list[float] | None, max_vessels: int = 300) -> dict[str, Any]:
    q = "SELECT mmsi, ts, lon, lat, sog, vessel_name, vessel_type FROM ais_positions WHERE case_id=?"
    args: list[Any] = [case_id]
    if t0:
        q += " AND ts_epoch>=?"
        args.append(_epoch(t0))
    if t1:
        q += " AND ts_epoch<=?"
        args.append(_epoch(t1))
    if bbox:
        q += " AND lon BETWEEN ? AND ? AND lat BETWEEN ? AND ?"
        args += [bbox[0], bbox[2], bbox[1], bbox[3]]
    q += " ORDER BY mmsi, ts_epoch"
    with db() as conn:
        rows = conn.execute(q, args).fetchall()
    tracks: dict[int, dict[str, Any]] = {}
    for r in rows:
        t = tracks.setdefault(r["mmsi"], {"coords": [], "name": r["vessel_name"], "type": r["vessel_type"], "t0": r["ts"], "t1": r["ts"], "n": 0})
        t["coords"].append([round(r["lon"], 5), round(r["lat"], 5)])
        t["t1"] = r["ts"]
        t["n"] += 1
        t["name"] = t["name"] or r["vessel_name"]
        t["type"] = t["type"] or r["vessel_type"]
    feats = []
    for mmsi, t in list(tracks.items())[:max_vessels]:
        if len(t["coords"]) < 2:
            geom = {"type": "Point", "coordinates": t["coords"][0]}
        else:
            step = max(1, len(t["coords"]) // 500)
            geom = {"type": "LineString", "coordinates": t["coords"][::step]}
        feats.append({"type": "Feature", "id": str(mmsi), "geometry": geom, "properties": {"mmsi": mmsi, "vessel_name": t["name"], "vessel_type": t["type"], "positions": t["n"], "time_start_utc": t["t0"], "time_end_utc": t["t1"]}})
    return {"type": "FeatureCollection", "features": feats, "vessels_total": len(tracks), "vessels_returned": len(feats)}


# ---------------------------------------------------------------------------
# review + export
# ---------------------------------------------------------------------------
def review(case_id: str, slick: dict[str, Any] | None, candidate: dict[str, Any] | None, notes: str | None) -> dict[str, Any]:
    get_case(case_id)
    with db() as conn:
        if slick:
            if slick.get("status") not in ("confirmed", "look_alike", "uncertain", None):
                raise ValueError("slick status must be confirmed|look_alike|uncertain")
            conn.execute("UPDATE slicks SET review_status=?, review_note=? WHERE id=? AND case_id=?", (slick.get("status"), slick.get("note"), slick["slick_id"], case_id))
        if candidate:
            if candidate.get("status") not in ("investigate", "dismiss", "unknown", None):
                raise ValueError("candidate status must be investigate|dismiss|unknown")
            conn.execute("UPDATE vessel_scores SET review_status=?, review_note=? WHERE case_id=? AND mmsi=? AND attribution_run_id=(SELECT id FROM attribution_runs WHERE case_id=? ORDER BY created_at DESC LIMIT 1)",
                         (candidate.get("status"), candidate.get("note"), case_id, int(candidate["mmsi"]), case_id))
        if notes is not None:
            conn.execute("UPDATE cases SET notes=?, updated_at=? WHERE id=?", (notes, utcnow(), case_id))
    audit(case_id, "review.update", {"slick": slick, "candidate": candidate, "notes_changed": notes is not None})
    return get_case(case_id)


def export_case(case_id: str, fmt: str) -> dict[str, Any]:
    case = get_case(case_id)
    hc = get_hindcast(case_id)
    at = get_attribution(case_id)
    with db() as conn:
        log_rows = [dict(r) for r in conn.execute("SELECT ts, actor, action, detail FROM audit_log WHERE case_id=? ORDER BY id", (case_id,)).fetchall()]
        cfg_row = conn.execute("SELECT config_snapshot FROM cases WHERE id=?", (case_id,)).fetchone()
    for r in log_rows:
        r["detail"] = loads(r["detail"])
    cfg_snapshot = loads(cfg_row["config_snapshot"]) if cfg_row else None
    disclaimer = ("OceanTrace AI output supports investigation prioritisation. Slick classes, origin regions, release windows and vessel scores are model estimates with "
                  "stated uncertainty; they are not legal findings and do not establish responsibility. Data modes: " + case["data_mode"].upper() + ".")
    if fmt == "geojson":
        feats = [{**f, "properties": {**f["properties"], "layer": "slick"}} for f in case["slicks"]["features"]]
        if hc:
            feats.append({"type": "Feature", "id": hc["id"], "geometry": hc["origin_geometry"], "properties": {"layer": "origin_region", "confidence_level": hc["metrics"].get("confidence_level"), "release_time_start_utc": hc["release_start"], "release_time_end_utc": hc["release_end"], "limitations": hc["metrics"].get("limitations")}})
            for k, v in (hc.get("detail") or {}).get("origin_regions", {}).items():
                feats.append({"type": "Feature", "id": f"{hc['id']}_p{k}", "geometry": v["geometry"], "properties": {"layer": f"origin_region_{k}", "area_km2": v["area_km2"]}})
        if at:
            for c in at["candidates"]:
                tr = dict(c["track"])
                tr["properties"] = {**tr.get("properties", {}), "layer": "candidate_track", "rank": c["rank"], "score": c["score"], "priority": c["priority"], "vessel": c["vessel"]}
                feats.append(tr)
        return {"type": "FeatureCollection", "features": feats, "case_id": case_id, "data_mode": case["data_mode"], "disclaimer": disclaimer, "exported_at_utc": utcnow()}
    return {
        "schema": "oceantrace.case.v1", "exported_at_utc": utcnow(), "disclaimer": disclaimer,
        "case": {k: case[k] for k in ("id", "name", "status", "data_mode", "created_at", "updated_at", "notes", "config_hash")},
        "scene": case["scene"], "slicks": case["slicks"], "environment": case["environment"],
        "hindcast": ({k: v for k, v in hc.items() if k != "detail"} | {"origin_regions": (hc.get("detail") or {}).get("origin_regions"), "steps": (hc.get("detail") or {}).get("steps"), "member_params": (hc.get("detail") or {}).get("member_params")}) if hc else None,
        "ais_imports": case["ais_imports"], "ais_summary": case["ais_summary"], "attribution": at, "detector": case["detector"],
        "config_snapshot": cfg_snapshot, "audit_log": log_rows,
    }
