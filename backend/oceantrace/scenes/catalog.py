"""Sentinel-1 GRD catalog adapter (Microsoft Planetary Computer STAC, free, no key).

Search real scenes by bbox/date and pull a *subset* (bbox window) of the VV
measurement GeoTIFF through GDAL's HTTP range reads — a 1 GB scene is never
downloaded whole. The subset is written as a proper GeoTIFF with GCP-derived
georeference and the acquisition time from the STAC item, then goes through the
normal `ingest_scene` path (so provenance = `source_type: stac`).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import rasterio
from rasterio.control import GroundControlPoint
from rasterio.crs import CRS
from rasterio.enums import Resampling
from rasterio.transform import from_bounds
from rasterio.warp import calculate_default_transform, reproject, transform as warp_transform

log = logging.getLogger("oceantrace.catalog")

STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1/search"
SAS_URL = "https://planetarycomputer.microsoft.com/api/sas/v1/token/sentinel-1-grd"
GDAL_ENV = dict(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tiff,.tif", GDAL_HTTP_MULTIRANGE="YES", GDAL_HTTP_MERGE_CONSECUTIVE_RANGES="YES", VSI_CACHE="TRUE")


class CatalogError(RuntimeError):
    pass


def search_scenes(bbox: list[float], start: str, end: str, limit: int = 20) -> list[dict[str, Any]]:
    body = {"collections": ["sentinel-1-grd"], "bbox": bbox, "datetime": f"{start}/{end}", "limit": int(limit), "sortby": [{"field": "datetime", "direction": "desc"}]}
    try:
        r = httpx.post(STAC_URL, json=body, timeout=60)
    except httpx.HTTPError as e:
        raise CatalogError(f"STAC search failed: {e}") from e
    if r.status_code != 200:
        raise CatalogError(f"STAC search HTTP {r.status_code}: {r.text[:200]}")
    out = []
    for f in r.json().get("features", []):
        p = f["properties"]
        out.append({
            "id": f["id"], "datetime": p.get("datetime"), "bbox": f.get("bbox"), "geometry": f.get("geometry"), "polarizations": p.get("sar:polarizations"),
            "orbit_state": p.get("sat:orbit_state"), "platform": p.get("platform"), "instrument_mode": p.get("sar:instrument_mode"), "relative_orbit": p.get("sat:relative_orbit"),
            "thumbnail": (f.get("assets", {}).get("thumbnail") or {}).get("href"), "assets": {k: v.get("href") for k, v in f.get("assets", {}).items() if k in ("vv", "vh", "hh", "hv")},
            "provider": "Microsoft Planetary Computer / ESA Copernicus Sentinel-1 GRD",
        })
    return out


def _sas_token() -> str:
    r = httpx.get(SAS_URL, timeout=30)
    if r.status_code != 200:
        raise CatalogError(f"SAS token HTTP {r.status_code}")
    return r.json()["token"]


def fetch_subset(item_id: str, bbox: list[float], dest: Path, polarization: str = "vv", max_px: int = 3000) -> dict[str, Any]:
    """Pull a WGS84-warped subset of one polarisation for `bbox` into `dest` (GeoTIFF)."""
    r = httpx.post(STAC_URL, json={"collections": ["sentinel-1-grd"], "ids": [item_id]}, timeout=60)
    feats = r.json().get("features", []) if r.status_code == 200 else []
    if not feats:
        raise CatalogError(f"scene {item_id} not found in catalog")
    item = feats[0]
    pol = polarization.lower()
    asset = item["assets"].get(pol)
    if not asset:
        raise CatalogError(f"scene has no {pol.upper()} asset; available: {list(item['assets'])}")
    href = asset["href"] + "?" + _sas_token()
    acq = item["properties"]["datetime"]
    min_lon, min_lat, max_lon, max_lat = bbox
    with rasterio.Env(**GDAL_ENV):
        with rasterio.open(href) as ds:
            gcps, gcp_crs = ds.gcps
            if not gcps:
                raise CatalogError("scene lacks GCPs; cannot georeference")
            # map the requested bbox into pixel space through the GCP polynomial (approx affine)
            from rasterio.transform import from_gcps, rowcol

            aff = from_gcps(gcps)
            xs = [min_lon, max_lon, max_lon, min_lon]
            ys = [min_lat, min_lat, max_lat, max_lat]
            if gcp_crs and gcp_crs.to_epsg() != 4326:
                xs, ys = warp_transform(CRS.from_epsg(4326), gcp_crs, xs, ys)
            rows, cols = zip(*[rowcol(aff, x, y) for x, y in zip(xs, ys)])
            r0, r1 = max(0, min(rows) - 50), min(ds.height, max(rows) + 50)
            c0, c1 = max(0, min(cols) - 50), min(ds.width, max(cols) + 50)
            if r1 <= r0 or c1 <= c0:
                raise CatalogError("requested bbox does not intersect the scene footprint")
            win_h, win_w = r1 - r0, c1 - c0
            ov = max(1, int(np.ceil(max(win_h, win_w) / max_px)))
            out_shape = (win_h // ov, win_w // ov)
            from rasterio.windows import Window

            arr = ds.read(1, window=Window(c0, r0, win_w, win_h), out_shape=out_shape, resampling=Resampling.average).astype("float32")
            # GCPs relative to the window and overview factor
            g2 = [GroundControlPoint(row=(g.row - r0) / ov, col=(g.col - c0) / ov, x=g.x, y=g.y, z=g.z) for g in gcps]
            dst_crs = CRS.from_epsg(4326)
            transform, w, h = calculate_default_transform(gcp_crs, dst_crs, out_shape[1], out_shape[0], gcps=g2)
            dst = np.zeros((h, w), dtype="float32")
            reproject(arr, dst, gcps=g2, src_crs=gcp_crs, dst_transform=transform, dst_crs=dst_crs, resampling=Resampling.bilinear, src_nodata=0.0, dst_nodata=0.0)
    # crop the warped raster to the requested bbox
    from rasterio.windows import from_bounds as win_from_bounds

    win = win_from_bounds(min_lon, min_lat, max_lon, max_lat, transform=transform).round_offsets().round_lengths()
    rr0, cc0 = max(0, int(win.row_off)), max(0, int(win.col_off))
    rr1, cc1 = min(h, rr0 + int(win.height)), min(w, cc0 + int(win.width))
    crop = dst[rr0:rr1, cc0:cc1]
    if crop.size == 0 or (crop > 0).mean() < 0.01:
        raise CatalogError("subset contains no valid SAR samples inside the requested bbox")
    crop_transform = transform @ transform.translation(cc0, rr0)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(dest, "w", driver="GTiff", height=crop.shape[0], width=crop.shape[1], count=1, dtype="float32", crs=dst_crs, transform=crop_transform, nodata=0.0, compress="deflate") as out:
        out.write(crop, 1)
        out.update_tags(ACQUISITION_START_TIME=acq.replace("+00:00", "Z")[:19] + "Z" if "Z" not in acq else acq[:19] + "Z", POLARISATION=pol.upper(), STAC_ID=item_id,
                        PROVIDER="Microsoft Planetary Computer / ESA Sentinel-1 GRD", DOWNSAMPLE_FACTOR=str(ov))
    return {"item_id": item_id, "acquisition_time_utc": acq, "polarization": pol.upper(), "downsample_factor": ov, "shape": list(crop.shape), "bbox": [min_lon, min_lat, max_lon, max_lat],
            "platform": item["properties"].get("platform"), "orbit_state": item["properties"].get("sat:orbit_state"), "provider": "Microsoft Planetary Computer / ESA Copernicus Sentinel-1 GRD"}
