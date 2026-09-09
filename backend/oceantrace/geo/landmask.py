"""Lightweight vector land mask (Natural Earth 10 m land polygons, public domain).

Replaces the raster `global_land_mask` package, whose ~1 GB decompressed array
does not fit small deployments. Accuracy ~1 km at the coastline (NE 10 m scale).

Data file: data/static/ne_10m_land.wkb (MultiPolygon, WGS84).
Regenerate with `python tools/build_landmask.py` (downloads from naciscdn.org).
Set OT_LANDMASK_PATH to point at a different file (e.g. a higher-resolution
national coastline) — the interface is identical.
"""
from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

import numpy as np

log = logging.getLogger("oceantrace.landmask")

_lock = threading.Lock()
_tree = None
_geoms = None
_loaded_from: str | None = None


def _default_path() -> Path:
    env = os.environ.get("OT_LANDMASK_PATH")
    if env:
        return Path(env)
    # repo layout: backend/oceantrace/geo/landmask.py -> repo/data/static
    return Path(__file__).resolve().parents[3] / "data" / "static" / "ne_10m_land.wkb"


def _load() -> bool:
    global _tree, _geoms, _loaded_from
    if _tree is not None:
        return True
    with _lock:
        if _tree is not None:
            return True
        path = _default_path()
        if not path.exists():
            log.warning("land mask file not found at %s — land checks disabled", path)
            return False
        try:
            from shapely import STRtree, wkb

            mp = wkb.loads(path.read_bytes())
            geoms = list(mp.geoms) if hasattr(mp, "geoms") else [mp]
            _geoms = geoms
            _tree = STRtree(geoms)
            _loaded_from = str(path)
            log.info("land mask loaded: %d polygons from %s", len(geoms), path)
            return True
        except Exception as e:  # pragma: no cover
            log.warning("land mask unavailable: %s", e)
            return False


def available() -> bool:
    return _load()


def source() -> str | None:
    _load()
    return _loaded_from


def is_land(lon, lat):
    """Vectorised point-in-land test. Accepts scalars or arrays (lon, lat in degrees).
    Returns bool or bool ndarray. Unavailable mask -> all False (sea)."""
    lon_a = np.atleast_1d(np.asarray(lon, dtype="float64"))
    lat_a = np.atleast_1d(np.asarray(lat, dtype="float64"))
    out = np.zeros(lon_a.shape, dtype=bool)
    if lon_a.size and _load():
        from shapely import points

        lon_w = ((lon_a + 180.0) % 360.0) - 180.0
        pts = points(lon_w.ravel(), np.clip(lat_a.ravel(), -90.0, 90.0))
        idx_pts, _ = _tree.query(pts, predicate="within")
        flat = out.ravel()
        flat[np.unique(idx_pts)] = True
        out = flat.reshape(lon_a.shape)
    if np.isscalar(lon) and np.isscalar(lat):
        return bool(out.ravel()[0])
    return out


def distance_to_land_km(lon: float, lat: float, max_km: float = 50.0) -> float | None:
    """Approximate great-circle distance from a sea point to the nearest land polygon
    (capped at max_km). None if mask unavailable."""
    if not _load():
        return None
    from shapely.geometry import Point

    p = Point(lon, lat)
    deg = max_km / 111.0
    cand = _tree.query(p.buffer(deg))
    if len(cand) == 0:
        return float(max_km)
    coslat = max(0.2, np.cos(np.radians(lat)))
    best = max_km
    for i in cand:
        g = _geoms[int(i)]
        if g.contains(p):
            return 0.0
        # planar distance in degrees -> km with latitude scaling of lon
        d_deg = g.distance(p)
        # refine: distance in local metric approx (scale lon by cos(lat))
        from shapely.affinity import scale

        d_km = d_deg * 111.0
        if d_km < best:
            # more accurate anisotropic estimate for the winning candidate
            gs = scale(g, xfact=coslat, yfact=1.0, origin=(lon, lat))
            best = min(best, gs.distance(p) * 111.0)
    return float(round(min(best, max_km), 2))
