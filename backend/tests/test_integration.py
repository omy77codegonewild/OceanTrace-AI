"""Integration: upload synthetic scene → detect → hindcast (constant field) →
AIS import → attribution → export. Uses the FastAPI app in-process."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from oceantrace.api.main import app
from tests.synth import make_synthetic_ais_csv, make_synthetic_sar_geotiff


def _wait(client: TestClient, job_id: str, timeout: float = 240) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = client.get(f"/api/v1/jobs/{job_id}").json()
        if j["state"] in ("completed", "failed"):
            assert j["state"] == "completed", f"job failed: {j.get('error')}"
            return j
        time.sleep(0.5)
    raise AssertionError("job timeout")


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_full_pipeline(client: TestClient, tmp_path_factory):
    tmp = tmp_path_factory.mktemp("fixture")
    bounds = [72.0, 18.4, 72.5, 18.9]
    acq = datetime(2026, 3, 2, 1, 0, tzinfo=timezone.utc)
    tif = tmp / "S1A_IW_GRDH_1SDV_20260302T010000_20260302T010025_000001_000001_TEST.tif"
    truth = make_synthetic_sar_geotiff(tif, bounds, size=700, acq=acq)

    r = client.post("/api/v1/cases", json={"name": "integration", "data_mode": "synthetic"})
    assert r.status_code == 201
    case_id = r.json()["id"]

    with open(tif, "rb") as f:
        r = client.post(f"/api/v1/cases/{case_id}/scene", files={"file": (tif.name, f, "image/tiff")}, data={"run_detection": "true"})
    assert r.status_code == 202, r.text
    job = _wait(client, r.json()["job_id"])
    assert job["result"]["detection"]["n_features"] >= 1

    case = client.get(f"/api/v1/cases/{case_id}").json()
    assert case["scene"]["acquisition_time"] == "2026-03-02T01:00:00Z"
    assert case["scene"]["metadata"]["georef_source"] == "embedded"
    feats = case["slicks"]["features"]
    top = feats[0]
    c = top["properties"]["centroid"]
    assert abs(c[0] - truth["centroid"][0]) < 0.03 and abs(c[1] - truth["centroid"][1]) < 0.03, (c, truth["centroid"])
    assert top["properties"]["class"] in ("oil", "uncertain")
    assert top["properties"]["model_version"].startswith("classical")
    # image axis 35° below the +x axis (rows grow southward) → compass bearing 90°+35° = 125°
    assert 115 <= top["properties"]["orientation_deg"] <= 140
    assert top["properties"]["elongation"] > 3
    # preview served
    assert client.get(f"/api/v1/scenes/{case['scene']['id']}/preview.png").status_code == 200

    # hindcast on constant field (deterministic; no network needed in CI)
    r = client.post(f"/api/v1/cases/{case_id}/slicks/{top['id']}/hindcast", json={"hindcast_hours": 6, "time_step_minutes": 15, "particle_count": 120, "ensemble_members": 4, "environment_source": "constant"})
    assert r.status_code == 202, r.text
    job = _wait(client, r.json()["job_id"])
    hc = client.get(f"/api/v1/cases/{case_id}/layers/hindcast").json()
    assert hc["status"] == "completed" and hc["origin_geometry"]["type"] in ("Polygon", "MultiPolygon")
    rs, re_ = hc["release_start"], hc["release_end"]
    assert rs < re_ <= "2026-03-02T01:00:00Z"
    assert hc["detail"]["environment"]["data_mode"] == "constant"
    assert any("constant" in l.lower() for l in hc["metrics"]["limitations"])

    # AIS: synthetic traffic with a suspect passing through the origin during the window
    origin_c = hc["detail"]["steps"][len(hc["detail"]["steps"]) // 2]["centroid"]
    mid = datetime.fromisoformat(rs.replace("Z", "+00:00")) + (datetime.fromisoformat(re_.replace("Z", "+00:00")) - datetime.fromisoformat(rs.replace("Z", "+00:00"))) / 2
    csv_path = tmp / "ais.csv"
    from shapely.geometry import shape

    oc = shape(hc["origin_geometry"]).centroid
    info = make_synthetic_ais_csv(csv_path, [oc.x, oc.y], mid)
    with open(csv_path, "rb") as f:
        r = client.post(f"/api/v1/cases/{case_id}/ais/import", files={"file": ("ais.csv", f, "text/csv")}, data={"source_label": "synthetic fixture", "data_mode": "synthetic"})
    assert r.status_code == 202
    job = _wait(client, r.json()["job_id"])
    assert job["result"]["rows_quarantined"] == 2 and job["result"]["data_mode"] == "synthetic"

    r = client.post(f"/api/v1/cases/{case_id}/attribute", json={})
    assert r.status_code == 202, r.text
    job = _wait(client, r.json()["job_id"])
    at = client.get(f"/api/v1/cases/{case_id}/layers/attribution").json()
    cands = at["candidates"]
    assert len(cands) >= 2
    assert cands[0]["mmsi"] == info["suspect_mmsi"], [(c["mmsi"], c["score"]) for c in cands]
    assert cands[0]["score"] > next(c["score"] for c in cands if c["mmsi"] == info["decoy_mmsi"])
    assert cands[0]["raw"]["gaps"], "suspect gap should be detected"
    assert all(0 <= v <= 1 for v in cands[0]["factors"].values())
    assert "priority" in cands[0] and "culprit" not in str(at).lower()

    # weights change → rerun → different run id
    r = client.post(f"/api/v1/cases/{case_id}/attribute", json={"weights": {"proximity": 0.6, "temporal_overlap": 0.1, "heading_compatibility": 0.1, "loitering": 0.05, "ais_gap_relevance": 0.1, "vessel_type": 0.05}})
    job2 = _wait(client, r.json()["job_id"])
    assert job2["result"]["attribution_run_id"] != job["result"]["attribution_run_id"]

    # review + export
    r = client.patch(f"/api/v1/cases/{case_id}/review", json={"slick": {"slick_id": top["id"], "status": "confirmed", "note": "visually verified"}, "candidate": {"mmsi": info["suspect_mmsi"], "status": "investigate"}})
    assert r.status_code == 200
    exp = client.get(f"/api/v1/cases/{case_id}/export?format=json").json()
    assert exp["case"]["data_mode"] == "synthetic" and exp["hindcast"]["release_start"] == rs and exp["attribution"]["candidates"][0]["review_status"] == "investigate"
    assert exp["config_snapshot"]["config_hash"]
    gj = client.get(f"/api/v1/cases/{case_id}/export?format=geojson").json()
    layers = {f["properties"].get("layer") for f in gj["features"]}
    assert "origin_region" in layers and "candidate_track" in layers
    html = client.get(f"/api/v1/cases/{case_id}/report.html")
    assert html.status_code == 200 and "SYNTHETIC" in html.text
    tracks = client.get(f"/api/v1/cases/{case_id}/layers/ais_tracks").json()
    assert tracks["vessels_total"] >= 10


def test_manual_bounds_png_and_missing_time(client: TestClient, tmp_path: Path):
    import numpy as np
    from PIL import Image

    rng = np.random.default_rng(1)
    img = (rng.gamma(4, 12, (300, 300))).clip(0, 255).astype("uint8")
    img[120:160, 40:260] = (img[120:160, 40:260] * 0.25).astype("uint8")
    p = tmp_path / "chip.png"
    Image.fromarray(img).save(p)
    case_id = client.post("/api/v1/cases", json={"name": "png", "data_mode": "imported"}).json()["id"]
    with open(p, "rb") as f:
        r = client.post(f"/api/v1/cases/{case_id}/scene", files={"file": ("chip.png", f, "image/png")}, data={"run_detection": "false"})
    j = client.get(f"/api/v1/jobs/{r.json()['job_id']}").json()
    while j["state"] not in ("completed", "failed"):
        time.sleep(0.3)
        j = client.get(f"/api/v1/jobs/{r.json()['job_id']}").json()
    assert j["state"] == "failed" and "bounds" in j["error"]
    with open(p, "rb") as f:
        r = client.post(f"/api/v1/cases/{case_id}/scene", files={"file": ("chip.png", f, "image/png")}, data={"run_detection": "true", "bounds": "72.0,18.0,72.3,18.3", "acquisition_time_utc": "2026-02-01T05:30:00Z"})
    job = _wait(client, r.json()["job_id"])
    case = client.get(f"/api/v1/cases/{case_id}").json()
    assert case["scene"]["metadata"]["georef_source"] == "manual" and case["scene"]["acquisition_time"] == "2026-02-01T05:30:00Z"
    assert job["result"]["detection"]["n_features"] >= 1
