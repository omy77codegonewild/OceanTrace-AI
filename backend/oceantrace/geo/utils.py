"""CRS-aware geometry helpers. No hardcoded zones: a local metric CRS is
derived from the geometry's own centroid (UTM) for every computation."""
from __future__ import annotations

import math
from typing import Any

import numpy as np
from pyproj import CRS, Geod, Transformer
from shapely.geometry import MultiPolygon, Polygon, mapping, shape
from shapely.ops import transform as shp_transform

WGS84 = CRS.from_epsg(4326)
_GEOD = Geod(ellps="WGS84")
EARTH_RADIUS_M = 6371008.8


def utm_crs_for(lon: float, lat: float) -> CRS:
    zone = int(math.floor((lon + 180) / 6) + 1)
    zone = max(1, min(60, zone))
    epsg = (32600 if lat >= 0 else 32700) + zone
    return CRS.from_epsg(epsg)


def to_local(geom, crs: CRS | None = None):
    """Project a WGS84 shapely geometry to a local UTM CRS. Returns (geom_m, crs)."""
    c = geom.centroid
    crs = crs or utm_crs_for(c.x, c.y)
    tf = Transformer.from_crs(WGS84, crs, always_xy=True)
    return shp_transform(tf.transform, geom), crs


def to_wgs84(geom, crs: CRS):
    tf = Transformer.from_crs(crs, WGS84, always_xy=True)
    return shp_transform(tf.transform, geom)


def geodesic_area_km2(geom) -> float:
    """Ellipsoidal area (km²) of a Polygon/MultiPolygon in WGS84."""
    if geom.is_empty:
        return 0.0
    area, _ = _GEOD.geometry_area_perimeter(geom)
    return abs(area) / 1e6


def geodesic_perimeter_km(geom) -> float:
    if geom.is_empty:
        return 0.0
    _, per = _GEOD.geometry_area_perimeter(geom)
    return abs(per) / 1e3


def haversine_km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a)) / 1000.0


def haversine_km_vec(lon1: np.ndarray, lat1: np.ndarray, lon2: float, lat2: float) -> np.ndarray:
    p1, p2 = np.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dl = np.radians(lon2 - lon1)
    a = np.sin(dphi / 2) ** 2 + np.cos(p1) * math.cos(p2) * np.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * np.arcsin(np.sqrt(a)) / 1000.0


def bearing_deg(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    az, _, _ = _GEOD.inv(lon1, lat1, lon2, lat2)
    return az % 360.0


def angular_diff_deg(a: float, b: float) -> float:
    d = abs((a - b + 180) % 360 - 180)
    return d


def shape_metrics(geom) -> dict[str, Any]:
    """Area, perimeter, centroid, orientation & axes via local UTM projection."""
    if geom.is_empty:
        return {}
    local, crs = to_local(geom)
    mrr = local.minimum_rotated_rectangle
    coords = list(mrr.exterior.coords)[:4]
    edges = [(coords[i], coords[(i + 1) % 4]) for i in range(4)]
    lengths = [math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in edges]
    if not lengths:
        return {}
    major_idx = int(np.argmax(lengths[:2]))
    major_len = max(lengths[:2])
    minor_len = min(lengths[:2])
    (ax, ay), (bx, by) = edges[major_idx]
    # orientation: compass bearing of major axis (0=N, clockwise), folded to [0,180)
    ang = math.degrees(math.atan2(bx - ax, by - ay)) % 180.0
    c = geom.centroid
    return {
        "area_km2": round(geodesic_area_km2(geom), 4),
        "perimeter_km": round(geodesic_perimeter_km(geom), 3),
        "centroid": [round(c.x, 6), round(c.y, 6)],
        "major_axis_km": round(major_len / 1000, 3),
        "minor_axis_km": round(minor_len / 1000, 3),
        "elongation": round(major_len / minor_len, 2) if minor_len > 0 else None,
        "orientation_deg": round(ang, 1),
        "local_crs": crs.to_string(),
    }


def buffer_km(geom, km: float):
    local, crs = to_local(geom)
    return to_wgs84(local.buffer(km * 1000.0), crs)


def geom_to_geojson(geom) -> dict[str, Any]:
    return mapping(geom)


def geojson_to_geom(gj: dict[str, Any]):
    return shape(gj)


def ensure_multipolygon(geom) -> MultiPolygon:
    if isinstance(geom, MultiPolygon):
        return geom
    if isinstance(geom, Polygon):
        return MultiPolygon([geom])
    polys = [g for g in getattr(geom, "geoms", []) if isinstance(g, Polygon)]
    return MultiPolygon(polys)


def meters_per_degree(lat: float) -> tuple[float, float]:
    """(m per deg lon, m per deg lat) at latitude."""
    lat_r = math.radians(lat)
    m_lat = 111132.954 - 559.822 * math.cos(2 * lat_r) + 1.175 * math.cos(4 * lat_r)
    m_lon = 111412.84 * math.cos(lat_r) - 93.5 * math.cos(3 * lat_r)
    return m_lon, m_lat
