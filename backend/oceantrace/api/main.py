"""FastAPI application — OceanTrace AI local API (async job model, OpenAPI docs)."""
from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from ..core.config import get_algo_config, get_settings
from ..core.db import db, init_db
from ..core.jobs import get_job, list_jobs
from ..detection.pipeline import adapter_status
from . import service
from .schemas import AttributeRequest, CaseCreate, CatalogFetchRequest, CatalogSearchRequest, DetectRequest, ForecastRequest, HindcastRequest, JobAccepted, LiveRecordRequest, ReviewRequest

logging.basicConfig(level=get_settings().log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("oceantrace.api")

@asynccontextmanager
async def _lifespan(_: FastAPI):
    init_db()
    log.info("data_dir=%s db=%s detector=%s", get_settings().data_dir, get_settings().resolved_db_path, adapter_status())
    yield


app = FastAPI(title="Spill Forensics API", version="0.3.0", description="SAR oil-slick detection → hindcast → AIS attribution (investigation support, not legal findings).",
              docs_url="/api/docs", openapi_url="/api/openapi.json", lifespan=_lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def _accepted(job_id: str) -> JSONResponse:
    return JSONResponse(status_code=202, content=JobAccepted(job_id=job_id, poll=f"/api/v1/jobs/{job_id}").model_dump())


def _err(e: Exception) -> HTTPException:
    if isinstance(e, service.NotFound):
        return HTTPException(404, str(e))
    if isinstance(e, (ValueError, KeyError)):
        return HTTPException(422, str(e))
    log.exception("unhandled")
    return HTTPException(500, f"{type(e).__name__}: {e}")


async def _save_upload(up: UploadFile, max_mb: float, sub: str) -> Path:
    tmp_dir = get_settings().data_dir / sub / "_incoming"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    dest = tmp_dir / f"{uuid.uuid4().hex}_{Path(up.filename or 'upload').name}"
    size = 0
    with open(dest, "wb") as f:
        while chunk := await up.read(1 << 20):
            size += len(chunk)
            if size > max_mb * 1024 * 1024:
                f.close()
                dest.unlink(missing_ok=True)
                raise HTTPException(413, f"upload exceeds {max_mb} MB")
            f.write(chunk)
    return dest


# ---------------------------------------------------------------------------
@app.get("/api/v1/health")
def health() -> dict[str, Any]:
    s = get_settings()
    return {"status": "ok", "version": app.version, "config_version": get_algo_config().version, "detector": adapter_status(),
            "integrations": {"aisstream_key_configured": bool(s.aisstream_api_key), "gfw_token_configured": bool(s.gfw_api_token), "open_meteo": "no key required"}}


@app.get("/api/v1/config")
def config_public() -> dict[str, Any]:
    cfg = get_algo_config()
    return {"config_version": cfg.version, "hindcast": cfg.section("hindcast"), "attribution": cfg.section("attribution"), "detection_postprocess": cfg.get("detection.postprocess"), "ais": cfg.section("ais")}


@app.get("/api/v1/cases")
def cases_list() -> list[dict[str, Any]]:
    return service.list_cases()


@app.post("/api/v1/cases", status_code=201)
def cases_create(body: CaseCreate) -> dict[str, Any]:
    return service.create_case(body.name, body.data_mode, body.notes)


@app.post("/api/v1/cases/synthetic-demo", status_code=202)
def cases_create_synthetic_demo(name: str = Body(default="Synthetic Smoke Test Case", embed=True)) -> JSONResponse:
    job_id = service.create_synthetic_demo_case(name)
    return _accepted(job_id)


@app.get("/api/v1/cases/{case_id}")
def cases_get(case_id: str) -> dict[str, Any]:
    try:
        return service.get_case(case_id)
    except Exception as e:
        raise _err(e)


@app.delete("/api/v1/cases/{case_id}", status_code=204)
def cases_delete(case_id: str) -> Response:
    with db() as conn:
        for t in ("vessel_scores", "attribution_runs", "ais_positions", "ais_imports", "hindcast_runs", "env_fields", "slicks", "scenes", "jobs", "audit_log"):
            conn.execute(f"DELETE FROM {t} WHERE case_id=?", (case_id,))
        conn.execute("DELETE FROM cases WHERE id=?", (case_id,))
    shutil.rmtree(get_settings().data_dir / "cases" / case_id, ignore_errors=True)
    return Response(status_code=204)


@app.post("/api/v1/cases/{case_id}/scene", status_code=202)
async def scene_upload(case_id: str, file: UploadFile = File(...), bounds: str | None = Form(default=None), acquisition_time_utc: str | None = Form(default=None),
                       run_detection: bool = Form(default=True), detect_options: str | None = Form(default=None)) -> JSONResponse:
    cfg = get_algo_config()
    ext = Path(file.filename or "").suffix.lower()
    if ext not in cfg.get("scene.allowed_extensions", [".tif", ".tiff", ".png", ".jpg", ".jpeg"]):
        raise HTTPException(415, f"unsupported file type {ext!r}")
    manual = None
    if bounds:
        try:
            manual = [float(x) for x in json.loads(bounds)] if bounds.strip().startswith("[") else [float(x) for x in bounds.split(",")]
        except Exception:
            raise HTTPException(422, "bounds must be JSON array or comma list: min_lon,min_lat,max_lon,max_lat")
    overrides = None
    if detect_options:
        try:
            overrides = DetectRequest(**json.loads(detect_options)).overrides()
        except Exception as e:
            raise HTTPException(422, f"detect_options invalid: {e}")
    path = await _save_upload(file, float(cfg.get("scene.max_upload_mb", 1024)), "scenes")
    try:
        job_id = service.ingest_scene_job(case_id, path, file.filename or path.name, manual, acquisition_time_utc, run_detection, overrides)
    except Exception as e:
        raise _err(e)
    return _accepted(job_id)


@app.post("/api/v1/catalog/search")
def catalog_search(body: CatalogSearchRequest) -> dict[str, Any]:
    from ..scenes.catalog import CatalogError, search_scenes

    try:
        items = search_scenes(body.bbox, body.start, body.end, body.limit)
    except CatalogError as e:
        raise HTTPException(502, str(e))
    return {"items": items, "count": len(items), "provider": "Microsoft Planetary Computer (Sentinel-1 GRD)", "data_mode": "real"}


@app.post("/api/v1/cases/{case_id}/scene/catalog", status_code=202)
def scene_from_catalog(case_id: str, body: CatalogFetchRequest) -> JSONResponse:
    try:
        return _accepted(service.catalog_fetch_job(case_id, body.item_id, body.bbox, body.polarization, body.run_detection, body.detect_options.overrides() if body.detect_options else None))
    except Exception as e:
        raise _err(e)


@app.post("/api/v1/cases/{case_id}/analyze", status_code=202)
def analyze(case_id: str, body: DetectRequest | None = Body(default=None)) -> JSONResponse:
    try:
        return _accepted(service.detect_job(case_id, body.overrides() if body else None))
    except Exception as e:
        raise _err(e)


@app.get("/api/v1/scenes/{scene_id}/preview.png")
def scene_preview(scene_id: str) -> FileResponse:
    with db() as conn:
        row = conn.execute("SELECT preview_uri FROM scenes WHERE id=?", (scene_id,)).fetchone()
    if not row or not Path(row["preview_uri"]).exists():
        raise HTTPException(404, "preview not found")
    return FileResponse(row["preview_uri"], media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/v1/jobs/{job_id}")
def job_get(job_id: str) -> dict[str, Any]:
    j = get_job(job_id)
    if not j:
        raise HTTPException(404, "job not found")
    return j


@app.get("/api/v1/cases/{case_id}/jobs")
def jobs_for_case(case_id: str) -> list[dict[str, Any]]:
    return list_jobs(case_id)


@app.get("/api/v1/cases/{case_id}/layers/{layer}")
def layer(case_id: str, layer: str, t0: str | None = None, t1: str | None = None, bbox: str | None = None, run_id: str | None = None) -> dict[str, Any]:
    try:
        case = service.get_case(case_id)
        if layer == "slicks":
            return case["slicks"]
        if layer == "scene_footprint":
            sc = case["scene"]
            if not sc:
                return {"type": "FeatureCollection", "features": []}
            b = sc["bounds"]
            return {"type": "FeatureCollection", "features": [{"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [[[b[0], b[1]], [b[2], b[1]], [b[2], b[3]], [b[0], b[3]], [b[0], b[1]]]]}, "properties": {"scene_id": sc["id"], "acquisition_time_utc": sc["acquisition_time"]}}]}
        if layer == "hindcast":
            hc = service.get_hindcast(case_id, run_id)
            return hc or {}
        if layer == "ais_tracks":
            bb = [float(x) for x in bbox.split(",")] if bbox else None
            return service.ais_tracks_geojson(case_id, t0, t1, bb)
        if layer == "attribution":
            return service.get_attribution(case_id, run_id) or {}
        raise HTTPException(404, f"unknown layer {layer}")
    except HTTPException:
        raise
    except Exception as e:
        raise _err(e)


@app.post("/api/v1/cases/{case_id}/slicks/{slick_id}/hindcast", status_code=202)
def hindcast(case_id: str, slick_id: str, body: HindcastRequest | None = Body(default=None)) -> JSONResponse:
    body = body or HindcastRequest()
    nc = None
    if body.environment_source == "netcdf_upload":
        if not body.netcdf_env_id:
            raise HTTPException(422, "netcdf_env_id required for netcdf_upload")
        nc = get_settings().data_dir / "env" / f"{body.netcdf_env_id}.nc"
        if not nc.exists():
            raise HTTPException(404, "uploaded NetCDF not found")
    try:
        return _accepted(service.hindcast_job(case_id, slick_id, body.params(), body.environment_source, nc))
    except Exception as e:
        raise _err(e)


@app.post("/api/v1/cases/{case_id}/slicks/{slick_id}/forecast", status_code=202)
def forecast(case_id: str, slick_id: str, body: ForecastRequest | None = Body(default=None)) -> JSONResponse:
    body = body or ForecastRequest()
    params = {k: v for k, v in {"hindcast_hours": body.hours, "particle_count": body.particle_count, "ensemble_members": body.ensemble_members, "windage_range": body.windage_range}.items() if v is not None}
    try:
        return _accepted(service.forecast_job(case_id, slick_id, params, body.environment_source))
    except Exception as e:
        raise _err(e)


@app.get("/api/v1/cases/{case_id}/slicks/{slick_id}/forecast")
def forecast_get(case_id: str, slick_id: str) -> dict[str, Any]:
    """Latest forward forecast for the slick; `{"forecast": null}` when none has been run yet
    (a 200 rather than a 404 so the UI can probe without console noise)."""
    fc = service.get_forecast(case_id, slick_id)  # raises NotFound for unknown case
    return {"forecast": fc}


@app.post("/api/v1/cases/{case_id}/environment/netcdf")
async def env_upload(case_id: str, file: UploadFile = File(...)) -> dict[str, Any]:
    if not (file.filename or "").lower().endswith((".nc", ".nc4", ".netcdf")):
        raise HTTPException(415, "expected a NetCDF file")
    env_id = f"ncenv_{uuid.uuid4().hex[:10]}"
    dest = get_settings().data_dir / "env" / f"{env_id}.nc"
    path = await _save_upload(file, 2048, "env")
    shutil.move(str(path), dest)
    return {"netcdf_env_id": env_id, "filename": file.filename, "data_mode": "imported"}


@app.post("/api/v1/cases/{case_id}/ais/import", status_code=202)
async def ais_import(case_id: str, file: UploadFile = File(...), source_label: str = Form(default="analyst CSV"), data_mode: str = Form(default="imported")) -> JSONResponse:
    if data_mode not in ("real", "imported", "synthetic", "demo"):
        raise HTTPException(422, "data_mode must be real|imported|synthetic|demo")
    if not (file.filename or "").lower().endswith((".csv", ".txt")):
        raise HTTPException(415, "expected a CSV file")
    path = await _save_upload(file, 2048, "ais")
    try:
        return _accepted(service.ais_import_job(case_id, path, file.filename or path.name, source_label, data_mode))
    except Exception as e:
        raise _err(e)


@app.delete("/api/v1/cases/{case_id}/ais/{import_id}", status_code=204)
def ais_delete(case_id: str, import_id: str) -> Response:
    from ..ais.importer import delete_import

    delete_import(case_id, import_id)
    return Response(status_code=204)


@app.post("/api/v1/cases/{case_id}/ais/live/record", status_code=202)
def ais_live(case_id: str, body: LiveRecordRequest) -> JSONResponse:
    if not get_settings().aisstream_api_key:
        raise HTTPException(503, "Live AIS is not configured: set OT_AISSTREAM_API_KEY in .env (free key at aisstream.io).")
    try:
        return _accepted(service.ais_live_record_job(case_id, body.bbox, body.minutes))
    except Exception as e:
        raise _err(e)


@app.post("/api/v1/cases/{case_id}/ais/synthetic", status_code=202)
def ais_synthetic(case_id: str) -> JSONResponse:
    try:
        return _accepted(service.ais_generate_synthetic_job(case_id))
    except Exception as e:
        raise _err(e)


@app.post("/api/v1/cases/{case_id}/attribute", status_code=202)
def attribute(case_id: str, body: AttributeRequest | None = Body(default=None)) -> JSONResponse:
    body = body or AttributeRequest()
    try:
        return _accepted(service.attribute_job(case_id, body.hindcast_run_id, body.weights, body.excluded_mmsi))
    except Exception as e:
        raise _err(e)


@app.patch("/api/v1/cases/{case_id}/review")
def review(case_id: str, body: ReviewRequest) -> dict[str, Any]:
    try:
        return service.review(case_id, body.slick.model_dump() if body.slick else None, body.candidate.model_dump() if body.candidate else None, body.notes)
    except Exception as e:
        raise _err(e)


@app.get("/api/v1/cases/{case_id}/export")
def export(case_id: str, format: str = Query(default="json", pattern="^(json|geojson)$")) -> Response:
    try:
        data = service.export_case(case_id, format)
    except Exception as e:
        raise _err(e)
    fname = f"{case_id}.{format}"
    return Response(content=json.dumps(data, indent=2, default=str), media_type="application/geo+json" if format == "geojson" else "application/json",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@app.get("/api/v1/cases/{case_id}/audit")
def audit_log(case_id: str) -> list[dict[str, Any]]:
    with db() as conn:
        rows = conn.execute("SELECT ts, actor, action, detail FROM audit_log WHERE case_id=? ORDER BY id", (case_id,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["detail"] = json.loads(d["detail"]) if d["detail"] else None
        out.append(d)
    return out


@app.get("/api/v1/cases/{case_id}/report.html")
def report_html(case_id: str) -> Response:
    from ..reporting.report import render_report

    try:
        html = render_report(service.export_case(case_id, "json"))
    except Exception as e:
        raise _err(e)
    return Response(content=html, media_type="text/html")


@app.get("/api/v1/vessels/search")
def vessel_search(query: str = Query(..., description="Vessel name, MMSI, or IMO")) -> dict[str, Any]:
    from ..ais.gfw import search_vessel_gfw
    results = search_vessel_gfw(query)
    return {"query": query, "count": len(results), "results": results}


# Serve built frontend if present (single-process deployment)
_web_dist = Path(__file__).resolve().parents[3] / "apps" / "web" / "dist"
if _web_dist.exists():
    app.mount("/", StaticFiles(directory=str(_web_dist), html=True), name="web")
