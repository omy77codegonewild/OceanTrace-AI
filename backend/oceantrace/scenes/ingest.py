"""Scene intake (FR-1): validate an uploaded SAR raster, extract georeference &
acquisition metadata, and build a WGS84 preview for the map overlay.

Two input kinds are supported:
  * GeoTIFF/COG with embedded CRS + transform (preferred; Sentinel-1 GRD/RTC).
  * Plain PNG/JPG/TIFF *without* georeference — accepted only when the analyst
    supplies bounds + acquisition time (typical for dataset chips). The scene
    is then labelled `georef_source = "manual"` everywhere.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
from PIL import Image
from rasterio.enums import Resampling
from rasterio.errors import RasterioIOError
from rasterio.transform import from_bounds
from rasterio.warp import calculate_default_transform, reproject
from rasterio.crs import CRS as RioCRS

from oceantrace.geo.utils import meters_per_degree

log = logging.getLogger("oceantrace.scenes")

Image.MAX_IMAGE_PIXELS = None  # large SAR scenes; size is validated separately

# S1A_IW_GRDH_1SDV_20260906T010237_20260906T010309_004450_0083F6_023C
_S1_RE = re.compile(r"S1[ABCD]_(?P<mode>[A-Z0-9]{2})_(?P<prod>[A-Z0-9]{4})_(?P<lvl>[0-9A-Z]{4})_(?P<start>\d{8}T\d{6})_(?P<stop>\d{8}T\d{6})")
_GENERIC_TS_RE = re.compile(r"(?<!\d)(\d{4})(\d{2})(\d{2})[T_-]?(\d{2})(\d{2})(\d{2})(?!\d)")


class SceneValidationError(ValueError):
    pass


def parse_time_from_name(name: str) -> tuple[datetime | None, str | None]:
    m = _S1_RE.search(name)
    if m:
        t = datetime.strptime(m.group("start"), "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
        return t, "sentinel1_filename"
    m = _GENERIC_TS_RE.search(name)
    if m:
        try:
            t = datetime(*[int(x) for x in m.groups()], tzinfo=timezone.utc)
            return t, "filename_timestamp"
        except ValueError:
            pass
    return None, None


def parse_iso_utc(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.strip().replace("Z", "+00:00")
    try:
        t = datetime.fromisoformat(s)
    except ValueError as e:
        raise SceneValidationError(f"acquisition_time_utc is not ISO-8601: {s!r}") from e
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.astimezone(timezone.utc)


def _time_from_tags(tags: dict[str, str]) -> tuple[datetime | None, str | None]:
    for key in ("ACQUISITION_START_TIME", "ACQUISITION_TIME", "TIFFTAG_DATETIME", "start_datetime", "datetime", "PRODUCT_START_TIME", "SENSING_START"):
        val = tags.get(key)
        if not val:
            continue
        for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(val, fmt).replace(tzinfo=timezone.utc), f"tiff_tag:{key}"
            except ValueError:
                continue
    return None, None


def _polarization_guess(name: str, tags: dict[str, str]) -> str | None:
    for key in ("POLARISATION", "POLARIZATION", "polarization"):
        if tags.get(key):
            return tags[key].upper()
    m = re.search(r"[-_](vv|vh|hh|hv)\b", name, re.I)
    if m:
        return m.group(1).upper()
    return None


def _stretch_to_uint8(arr: np.ndarray, nodata: float | None) -> np.ndarray:
    a = arr.astype("float32")
    mask = np.isfinite(a)
    if nodata is not None:
        mask &= a != nodata
    if not mask.any():
        return np.zeros(a.shape, dtype=np.uint8)
    vals = a[mask]
    # SAR backscatter is heavy-tailed: convert linear intensity/amplitude to dB,
    # then apply a 2–98 percentile stretch. Already-dB or 8-bit data pass through.
    vmin, vmax = float(np.min(vals)), float(np.max(vals))
    looks_linear = vmin >= 0 and vmax > 1.0 and not (vmax <= 255 and np.issubdtype(arr.dtype, np.integer))
    if looks_linear:
        valid = mask & (a > 0)
        a = np.where(valid, 10.0 * np.log10(np.maximum(a, 1e-6)), np.nan)
        mask = valid
        if not mask.any():
            return np.zeros(a.shape, dtype=np.uint8)
        vals = a[mask]
    lo, hi = np.nanpercentile(vals, [2, 98])
    if hi <= lo:
        hi = lo + 1e-6
    out = np.clip((a - lo) / (hi - lo), 0, 1) * 255.0
    out = np.where(mask, out, 0)
    return out.astype(np.uint8)


def ingest_scene(
    src_path: Path,
    scene_dir: Path,
    *,
    manual_bounds: list[float] | None = None,
    acquisition_time_utc: str | None = None,
    preview_max_px: int = 2048,
    working_crs: str = "EPSG:4326",
    max_analysis_px: int = 4000,
    original_name: str | None = None,
) -> dict[str, Any]:
    """Validate + normalise a scene. Writes `<scene_dir>/preview.png` and
    `<scene_dir>/analysis.tif` (single-band float32 in working CRS) and returns
    the scene metadata contract."""
    scene_dir.mkdir(parents=True, exist_ok=True)
    name = original_name or src_path.name
    meta: dict[str, Any] = {
        "source_type": "upload",
        "sensor": "Sentinel-1" if _S1_RE.search(name) else "unknown",
        "original_filename": name,
        "file_size_bytes": src_path.stat().st_size,
        "warnings": [],
    }

    t_name, t_src = parse_time_from_name(name)
    t_manual = parse_iso_utc(acquisition_time_utc)

    try:
        ds = rasterio.open(src_path)
    except RasterioIOError as e:
        raise SceneValidationError(f"Unreadable raster: {e}") from e

    with ds:
        tags = ds.tags()
        t_tag, t_tag_src = _time_from_tags(tags)
        gcps, gcp_crs = ds.gcps
        has_gcps = bool(gcps) and gcp_crs is not None and ds.transform.is_identity
        has_georef = (ds.crs is not None and not ds.transform.is_identity) or has_gcps
        meta["polarization"] = _polarization_guess(name, tags)
        meta["band_count"] = ds.count
        meta["dtype"] = str(ds.dtypes[0])
        meta["width"], meta["height"] = ds.width, ds.height

        min_lon = min_lat = max_lon = max_lat = 0.0
        if has_georef:
            src_crs = gcp_crs if has_gcps else ds.crs
            meta["crs_original"] = src_crs.to_string() if src_crs is not None else None
            meta["georef_source"] = f"embedded_gcps({len(gcps)})" if has_gcps else "embedded"
            if ds.res:
                meta["pixel_resolution_native"] = [abs(ds.res[0]), abs(ds.res[1])]
            if manual_bounds:
                meta["warnings"].append("manual_bounds ignored: raster already georeferenced")
        else:
            if not manual_bounds or len(manual_bounds) != 4:
                raise SceneValidationError(
                    "Raster has no CRS/geotransform. Provide bounds [min_lon, min_lat, max_lon, max_lat] for this image."
                )
            min_lon, min_lat, max_lon, max_lat = map(float, manual_bounds)
            if not (-180 <= min_lon < max_lon <= 180 and -90 <= min_lat < max_lat <= 90):
                raise SceneValidationError("bounds must be [min_lon, min_lat, max_lon, max_lat] in WGS84 with min < max")
            src_crs = RioCRS.from_epsg(4326)
            meta["crs_original"] = None
            meta["georef_source"] = "manual"
            meta["warnings"].append("Georeference supplied manually by analyst; positional accuracy depends on the entered bounds.")

        # acquisition time precedence: manual > tiff tag > filename > current UTC fallback
        acq, acq_src = (t_manual, "analyst_input") if t_manual else ((t_tag, t_tag_src) if t_tag else (t_name, t_src))
        if acq is None:
            acq = datetime.now(timezone.utc).replace(microsecond=0)
            acq_src = "default_fallback"
            meta["warnings"].append("Acquisition timestamp not in file tags or filename; defaulted to current UTC.")
        meta["acquisition_time_utc"] = acq.strftime("%Y-%m-%dT%H:%M:%SZ")
        meta["acquisition_time_source"] = acq_src

        # ---- read bands (e.g. dual-pol VV+VH), downsampling very large scenes ----
        num_bands = min(ds.count, 2)
        ov = 1
        max_px = int(max_analysis_px)
        if max(ds.width, ds.height) > max_px:
            ov = int(np.ceil(max(ds.width, ds.height) / max_px))
            meta["warnings"].append(f"Scene downsampled {ov}x for analysis (native {ds.width}x{ds.height}); native-resolution tiling is a post-MVP item.")
        out_shape = (max(1, ds.height // ov), max(1, ds.width // ov))

        dst_crs = RioCRS.from_string(working_crs)
        bands_out = []
        out_transform = None

        for b in range(1, num_bands + 1):
            data = ds.read(b, out_shape=out_shape, resampling=Resampling.average, masked=True).astype("float32")
            nodata = ds.nodata
            arr_b = data.filled(np.nan)
            if nodata is not None:
                arr_b = np.where(arr_b == nodata, np.nan, arr_b)
            arr_b = np.where(arr_b == 0, np.nan, arr_b)

            rh, rw = arr_b.shape
            if has_gcps:
                from rasterio.control import GroundControlPoint

                g2 = [GroundControlPoint(row=g.row / ov, col=g.col / ov, x=g.x, y=g.y, z=g.z) for g in gcps]
                transform, w, h = calculate_default_transform(src_crs, dst_crs, rw, rh, gcps=g2)
                dst = np.zeros((h, w), dtype="float32")
                reproject(source=np.nan_to_num(arr_b, nan=0.0), destination=dst, gcps=g2, src_crs=src_crs, dst_transform=transform, dst_crs=dst_crs,
                          resampling=Resampling.bilinear, src_nodata=0.0, dst_nodata=0.0)
                arr_b = np.where(dst == 0, np.nan, dst)
                out_transform = transform
            else:
                if has_georef:
                    src_transform = ds.transform @ ds.transform.scale(ds.width / rw, ds.height / rh)
                else:
                    src_transform = from_bounds(min_lon, min_lat, max_lon, max_lat, rw, rh)
                if has_georef and src_crs != dst_crs:
                    transform, w, h = calculate_default_transform(src_crs, dst_crs, rw, rh, *ds.bounds)
                    dst = np.zeros((h, w), dtype="float32")
                    reproject(source=np.nan_to_num(arr_b, nan=0.0), destination=dst, src_transform=src_transform, src_crs=src_crs,
                              dst_transform=transform, dst_crs=dst_crs, resampling=Resampling.bilinear, src_nodata=0.0, dst_nodata=0.0)
                    arr_b, out_transform = np.where(dst == 0, np.nan, dst), transform
                else:
                    out_transform = src_transform
            bands_out.append(arr_b)

        meta["downsample_factor"] = ov
        arr = bands_out[0]

    if out_transform is None:
        raise SceneValidationError("Failed to calculate transform for scene.")

    h, w = arr.shape
    analysis_path = scene_dir / "analysis.tif"
    with rasterio.open(
        analysis_path, "w", driver="GTiff", height=h, width=w, count=len(bands_out), dtype="float32", crs=dst_crs,
        transform=out_transform, nodata=np.nan, compress="deflate", tiled=True, blockxsize=256, blockysize=256,
    ) as out:
        for bi, b_arr in enumerate(bands_out, start=1):
            out.write(b_arr, bi)
        out.update_tags(ACQUISITION_START_TIME=meta["acquisition_time_utc"], OCEANTRACE_SOURCE=name)

    left, top = out_transform @ (0, 0)
    right, bottom = out_transform @ (w, h)
    bounds = [float(min(left, right)), float(min(top, bottom)), float(max(left, right)), float(max(top, bottom))]
    meta["bounds"] = bounds
    meta["crs"] = working_crs
    meta["pixel_size_deg"] = [abs(out_transform.a), abs(out_transform.e)]
    mlon, mlat = meters_per_degree((bounds[1] + bounds[3]) / 2)
    meta["pixel_size_m"] = [round(abs(out_transform.a) * mlon, 2), round(abs(out_transform.e) * mlat, 2)]
    meta["analysis_shape"] = [h, w]

    # ---- preview PNG (north-up, WGS84, bounds = meta.bounds) ----
    scale = max(1.0, max(h, w) / float(preview_max_px))
    pw, ph = max(1, int(w / scale)), max(1, int(h / scale))
    u8 = _stretch_to_uint8(arr, None)
    img = Image.fromarray(u8)
    _resample_bilinear = getattr(getattr(Image, "Resampling", None), "BILINEAR", 2)
    _resample_nearest = getattr(getattr(Image, "Resampling", None), "NEAREST", 0)
    if (pw, ph) != (w, h):
        img = img.resize((pw, ph), _resample_bilinear)
    alpha = Image.fromarray(((np.isfinite(arr)) * 255).astype(np.uint8)).resize((pw, ph), _resample_nearest)
    rgba = Image.merge("LA", (img, alpha))
    preview_path = scene_dir / "preview.png"
    rgba.save(preview_path, optimize=True)
    meta["preview_size"] = [pw, ph]
    meta["is_analysis_ready"] = True
    return meta
