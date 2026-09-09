"""Detection pipeline (FR-2/FR-3): tile → adapter → merge probabilities →
threshold → polygonise (CRS-aware) → land-mask → characterise.

Tiles are overlapped and blended with a cosine window so seams are removed and
the merged probability raster keeps the scene's geotransform, guaranteeing the
polygons are in true geographic coordinates."""
from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any, Callable

import numpy as np
import rasterio
from rasterio import features as rfeatures
from rasterio.transform import Affine
from shapely.geometry import shape
from shapely.ops import unary_union

from ..core.config import get_algo_config, get_settings, resolve_path
from ..geo.utils import buffer_km, geom_to_geojson, shape_metrics, to_local, to_wgs84
from .base import DetectionAdapter
from .adapters.classical import ClassicalDarkSpotAdapter
from .adapters.onnx_adapter import ModelNotAvailable, OnnxSegmentationAdapter

log = logging.getLogger("oceantrace.detection")


def build_adapter(override: str | None = None) -> DetectionAdapter:
    cfg = get_algo_config().section("detection")
    choice = (override or cfg.get("adapter", "auto")).lower()
    onnx_cfg = cfg.get("onnx", {})
    model_path = resolve_path(onnx_cfg.get("model_path", "data/models/oilspill_seg.onnx"))
    if choice in ("onnx", "auto"):
        try:
            return OnnxSegmentationAdapter(onnx_cfg, model_path)
        except ModelNotAvailable as e:
            if choice == "onnx":
                raise
            log.info("ONNX model unavailable (%s); using classical dark-spot adapter", e)
    return ClassicalDarkSpotAdapter(cfg.get("classical", {}))


def adapter_status() -> dict[str, Any]:
    cfg = get_algo_config().section("detection")
    onnx_cfg = cfg.get("onnx", {})
    model_path = resolve_path(onnx_cfg.get("model_path", "data/models/oilspill_seg.onnx"))
    return {
        "configured_adapter": cfg.get("adapter", "auto"),
        "onnx_model_path": str(model_path),
        "onnx_model_present": model_path.exists(),
        "active_adapter": "onnx" if model_path.exists() and cfg.get("adapter", "auto") in ("auto", "onnx") else "classical",
        "classical_version": cfg.get("classical", {}).get("model_version"),
    }


def _cosine_window(h: int, w: int) -> np.ndarray:
    wy = np.hanning(h + 2)[1:-1] if h > 1 else np.ones(1)
    wx = np.hanning(w + 2)[1:-1] if w > 1 else np.ones(1)
    win = np.outer(wy, wx).astype("float32")
    return np.maximum(win, 1e-3)


def _tile_grid(n: int, size: int, overlap: int) -> list[tuple[int, int]]:
    if n <= size:
        return [(0, n)]
    step = max(1, size - overlap)
    starts = list(range(0, n - size + 1, step))
    if starts[-1] + size < n:
        starts.append(n - size)
    return [(s, s + size) for s in starts]


def run_detection(
    analysis_tif: Path,
    out_dir: Path,
    scene_id: str,
    *,
    adapter: DetectionAdapter | None = None,
    overrides: dict[str, Any] | None = None,
    progress: Callable[[float, str], None] | None = None,
) -> dict[str, Any]:
    cfg = get_algo_config().section("detection")
    post = cfg.get("postprocess", {})
    if overrides:
        post.update({k: v for k, v in overrides.items() if k in post})
    adapter = adapter or build_adapter(overrides.get("adapter") if overrides else None)
    out_dir.mkdir(parents=True, exist_ok=True)

    with rasterio.open(analysis_tif) as ds:
        arr = ds.read(1).astype("float32")
        transform: Affine = ds.transform
        crs = ds.crs
    valid = np.isfinite(arr) & (arr != 0)
    H, W = arr.shape
    ts, ov = adapter.tile_size, adapter.tile_overlap
    rows, cols = _tile_grid(H, ts, ov), _tile_grid(W, ts, ov)
    n_tiles = len(rows) * len(cols)
    prob_acc = np.zeros((H, W), dtype="float32")
    la_acc = np.zeros((H, W), dtype="float32")
    w_acc = np.zeros((H, W), dtype="float32")
    has_la = False
    manifest: list[dict[str, Any]] = []
    k = 0
    for r0, r1 in rows:
        for c0, c1 in cols:
            k += 1
            tile = arr[r0:r1, c0:c1]
            vm = valid[r0:r1, c0:c1]
            if vm.mean() < 0.02:
                continue
            tp = adapter.predict_tile(tile, vm)
            win = _cosine_window(r1 - r0, c1 - c0)
            prob_acc[r0:r1, c0:c1] += tp.oil * win
            if tp.look_alike is not None:
                has_la = True
                la_acc[r0:r1, c0:c1] += tp.look_alike * win
            w_acc[r0:r1, c0:c1] += win
            x0, y0 = transform @ (c0, r0)
            x1, y1 = transform @ (c1, r1)
            manifest.append({"row": [r0, r1], "col": [c0, c1], "bounds": [min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)]})
            if progress and (k % 4 == 0 or k == n_tiles):
                progress(0.1 + 0.6 * k / n_tiles, f"inference tile {k}/{n_tiles}")
    prob = np.where(w_acc > 0, prob_acc / np.maximum(w_acc, 1e-6), 0.0).astype("float32")
    la = np.where(w_acc > 0, la_acc / np.maximum(w_acc, 1e-6), 0.0).astype("float32") if has_la else None
    prob[~valid] = 0.0

    # persist probability raster (analysis artifact; thresholds are configurable downstream)
    prob_path = out_dir / "oil_probability.tif"
    with rasterio.open(prob_path, "w", driver="GTiff", height=H, width=W, count=1, dtype="float32", crs=crs, transform=transform, compress="deflate", tiled=True) as dst:
        dst.write(prob, 1)

    thr = float(post.get("prob_threshold", 0.5))
    unc_lo, unc_hi = post.get("uncertain_band", [0.35, 0.5])
    min_area = float(post.get("min_area_km2", 0.05))
    max_feats = int(post.get("max_features", 25))
    simplify_m = float(post.get("simplify_tolerance_m", 30))
    use_land_mask = bool(post.get("land_mask", True))

    if progress:
        progress(0.75, "polygonising probability mask")

    features: list[dict[str, Any]] = []
    candidates: list[tuple[float, Any, str, float]] = []
    for label_name, mask in (("oil", prob >= thr), ("uncertain", (prob >= float(unc_lo)) & (prob < thr))):
        if not mask.any():
            continue
        for geom_json, val in rfeatures.shapes(mask.astype("uint8"), mask=mask, transform=transform, connectivity=8):
            if val != 1:
                continue
            g = shape(geom_json)
            if g.is_empty:
                continue
            # mean probability inside polygon via raster window
            candidates.append((0.0, g, label_name, 0.0))

    # merge touching pieces per class, compute stats
    merged: list[tuple[Any, str]] = []
    for label_name in ("oil", "uncertain"):
        geoms = [g for _, g, ln, _ in candidates if ln == label_name]
        if not geoms:
            continue
        u = unary_union(geoms)
        parts = list(u.geoms) if hasattr(u, "geoms") else [u]
        merged.extend((p, label_name) for p in parts)

    land_checker = None
    if use_land_mask:
        from ..geo import landmask

        if landmask.available():
            land_checker = lambda lon, lat: bool(landmask.is_land(lon, lat))  # noqa: E731

    def _coast_distance_km(g) -> float | None:
        """Distance (km, capped at 50) from the polygon centroid to the nearest coastline (Natural Earth 10 m)."""
        if not land_checker:
            return None
        from ..geo import landmask

        c = g.centroid
        return landmask.distance_to_land_km(float(c.x), float(c.y), max_km=50.0)

    scored: list[dict[str, Any]] = []
    for g, label_name in merged:
        metrics = shape_metrics(g)
        if not metrics or metrics["area_km2"] < min_area:
            continue
        if land_checker and land_checker(*metrics["centroid"]):
            continue
        coast_km = _coast_distance_km(g)
        # mean/max probability inside polygon
        rmask = rfeatures.geometry_mask([geom_to_geojson(g)], out_shape=(H, W), transform=transform, invert=True)
        pv = prob[rmask]
        if pv.size == 0:
            continue
        conf = float(np.clip(np.percentile(pv, 75), 0, 1))
        la_mean = float(la[rmask].mean()) if la is not None else None
        cls = label_name
        if la_mean is not None and la_mean > conf:
            cls = "look_alike"
        # simplify in metric CRS
        local, lcrs = to_local(g)
        gs = to_wgs84(local.simplify(simplify_m, preserve_topology=True).buffer(0), lcrs)
        if gs.is_empty:
            gs = g
        scored.append({
            "geom": gs, "class": cls, "confidence": round(conf, 3), "metrics": metrics, "coast_distance_km": coast_km,
            "prob_mean": round(float(pv.mean()), 3), "prob_max": round(float(pv.max()), 3),
            "look_alike_prob": round(la_mean, 3) if la_mean is not None else None, "pixel_count": int(pv.size),
        })
    scored.sort(key=lambda d: (d["class"] != "oil", -d["confidence"] * d["metrics"]["area_km2"]))
    scored = scored[:max_feats]

    for i, s in enumerate(scored, start=1):
        sid = f"slick_{uuid.uuid4().hex[:8]}"
        props = {
            "class": s["class"], "confidence": s["confidence"], "scene_id": scene_id,
            "model_version": adapter.model_version, "adapter": adapter.name, "rank": i,
            "prob_mean": s["prob_mean"], "prob_max": s["prob_max"], "look_alike_prob": s["look_alike_prob"],
            "pixel_count": s["pixel_count"], "threshold": thr, **s["metrics"], "coast_distance_km": s["coast_distance_km"],
            "look_alike_risk": _look_alike_risk(s),
            "limitations": _limitations(adapter, s["class"], s),
        }
        features.append({"type": "Feature", "id": sid, "geometry": geom_to_geojson(s["geom"]), "properties": props})

    if progress:
        progress(0.95, f"{len(features)} candidate polygons")
    return {
        "type": "FeatureCollection",
        "features": features,
        "provenance": {
            **adapter.describe(), "tiles": n_tiles, "threshold": thr, "uncertain_band": [unc_lo, unc_hi],
            "min_area_km2": min_area, "probability_raster": str(prob_path), "tiles_manifest": manifest[:200],
            "raster_shape": [H, W], "valid_fraction": round(float(valid.mean()), 4),
        },
    }


def _look_alike_risk(s: dict[str, Any]) -> str:
    """Rule-based contextual risk label (documented heuristic, shown to the analyst)."""
    risk = 0
    if s.get("coast_distance_km") is not None and s["coast_distance_km"] <= 2:
        risk += 2  # sheltered coastal water / creeks / harbours
    elif s.get("coast_distance_km") is not None and s["coast_distance_km"] <= 5:
        risk += 1
    el = s["metrics"].get("elongation") or 1.0
    if el < 1.5:
        risk += 1  # compact blobs are more often wind-shadow / rain cells than ship discharges
    if s.get("look_alike_prob") is not None and s["look_alike_prob"] > 0.3:
        risk += 2
    return "high" if risk >= 3 else "medium" if risk == 2 else "low"


def _limitations(adapter: DetectionAdapter, cls: str, s: dict[str, Any] | None = None) -> list[str]:
    lim = ["SAR dark areas may be oil or look-alikes (low wind, biogenic film, internal waves); analyst verification required."]
    if s and s.get("coast_distance_km") is not None and s["coast_distance_km"] <= 2:
        lim.append(f"Within ~{s['coast_distance_km']:.0f} km of the coastline: sheltered/calm water is a frequent look-alike source.")
    if adapter.name == "classical":
        lim.append("Produced by the classical adaptive dark-spot detector (no trained network). Connect an ONNX model for learned oil/look-alike discrimination.")
    if cls == "uncertain":
        lim.append("Probability below the oil threshold; treat as uncertain.")
    return lim
