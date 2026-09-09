"""Unit tests: CRS/area, AIS validation, deterministic constant-field hindcast,
scoring math, environment conventions."""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest
from shapely.geometry import Polygon, box

from oceantrace.environment.fields import ConstantProvider, dir_from_to_uv, dir_towards_to_uv
from oceantrace.geo.utils import buffer_km, geodesic_area_km2, haversine_km, shape_metrics, utm_crs_for
from oceantrace.trajectory.lagrangian import HindcastParams, integrate, seed_particles, validate_params


def test_utm_zone_selection_is_derived_not_hardcoded():
    assert utm_crs_for(72.8, 18.9).to_epsg() == 32643  # Mumbai → 43N
    assert utm_crs_for(-3.0, 40.0).to_epsg() == 32630  # Madrid → 30N
    assert utm_crs_for(151.2, -33.9).to_epsg() == 32756  # Sydney → 56S


def test_geodesic_area_of_known_square():
    # ~1° x 1° box at equator ≈ 12,364 km²
    a = geodesic_area_km2(box(0, 0, 1, 1))
    assert 12300 < a < 12400


def test_shape_metrics_orientation_and_axes():
    # thin rectangle elongated east-west near 19N
    g = box(72.0, 19.0, 72.2, 19.02)
    m = shape_metrics(g)
    assert 80 <= m["orientation_deg"] <= 100  # E–W axis → ~90°
    assert m["major_axis_km"] > m["minor_axis_km"] * 5
    assert abs(m["area_km2"] - 0.2 * 105.2 * 0.02 * 110.6) / m["area_km2"] < 0.05


def test_haversine():
    d = haversine_km(72.8777, 19.0760, 77.5946, 12.9716)  # Mumbai–Bengaluru ≈ 845 km
    assert 830 < d < 860


def test_buffer_km_grows_area():
    g = box(72.0, 19.0, 72.1, 19.1)
    b = buffer_km(g, 10)
    assert geodesic_area_km2(b) > geodesic_area_km2(g) * 3


def test_direction_conventions():
    u, v = dir_towards_to_uv(np.array([1.0]), np.array([90.0]))  # current flowing towards east
    assert u[0] == pytest.approx(1.0) and v[0] == pytest.approx(0.0, abs=1e-9)
    u, v = dir_from_to_uv(np.array([1.0]), np.array([270.0]))  # westerly wind (from west) blows towards east
    assert u[0] == pytest.approx(1.0) and v[0] == pytest.approx(0.0, abs=1e-9)


def test_constant_field_backward_displacement_is_deterministic():
    """Known displacement: constant current (0.2, 0) m/s, wind (0,0), no
    diffusion, 1 member. Backward 2 h → particles move WEST by 1440 m."""
    prov = ConstantProvider({"u_current": 0.2, "v_current": 0.0, "u_wind": 0.0, "v_wind": 0.0})
    t0 = datetime(2026, 1, 10, 6, 0, tzinfo=timezone.utc)
    field = prov.fetch([72.0, 18.0, 72.2, 18.2], t0 - timedelta(hours=3), t0)
    poly = box(72.1, 18.1, 72.1005, 18.1005)
    p = HindcastParams(hindcast_hours=2, time_step_minutes=10, particle_count=50, ensemble_members=1, windage_range=(0.0, 0.0), current_scale_range=(1.0, 1.0),
                       current_direction_jitter_deg=0.0, wind_speed_jitter_frac=0.0, diffusion_m2_s=0.0)
    res = integrate(poly, field, t0.timestamp(), p)
    d_lon = float(res.lon[0, :, -1].mean() - res.lon[0, :, 0].mean())
    m_per_deg = 111412.84 * math.cos(math.radians(18.1)) - 93.5 * math.cos(3 * math.radians(18.1))
    assert d_lon * m_per_deg == pytest.approx(-1440.0, rel=0.01)
    assert float(np.abs(res.lat[0, :, -1] - res.lat[0, :, 0]).max()) < 1e-6
    # reproducible
    res2 = integrate(poly, field, t0.timestamp(), p)
    assert np.array_equal(res.lon, res2.lon)


def test_windage_adds_wind_component():
    prov = ConstantProvider({"u_current": 0.0, "v_current": 0.0, "u_wind": 0.0, "v_wind": 10.0})
    t0 = datetime(2026, 1, 10, 6, 0, tzinfo=timezone.utc)
    field = prov.fetch([72.0, 18.0, 72.2, 18.2], t0 - timedelta(hours=2), t0)
    poly = box(72.1, 18.1, 72.1005, 18.1005)
    p = HindcastParams(hindcast_hours=1, time_step_minutes=15, particle_count=20, ensemble_members=1, windage_range=(0.03, 0.03), current_scale_range=(1.0, 1.0),
                       current_direction_jitter_deg=0.0, wind_speed_jitter_frac=0.0, diffusion_m2_s=0.0)
    res = integrate(poly, field, t0.timestamp(), p)
    d_lat_m = float((res.lat[0, :, -1] - res.lat[0, :, 0]).mean()) * 110852
    assert d_lat_m == pytest.approx(-0.03 * 10 * 3600, rel=0.02)  # backward → south


def test_validate_params_rejects_out_of_range():
    defaults = {"hindcast_hours": 24, "time_step_minutes": 15, "particle_count": 100, "ensemble_members": 5, "windage_range": [0.02, 0.04], "current_scale_range": [0.8, 1.2]}
    limits = {"hindcast_hours": [1, 96], "time_step_minutes": [5, 60], "particle_count": [50, 5000], "ensemble_members": [1, 100], "windage": [0, 0.08], "current_scale": [0.3, 2], "diffusion_m2_s": [0, 50]}
    with pytest.raises(ValueError):
        validate_params({"hindcast_hours": 500}, defaults, limits)
    with pytest.raises(ValueError):
        validate_params({"windage_range": [0.05, 0.01]}, defaults, limits)
    p = validate_params({"hindcast_hours": 48, "windage_range": [0.01, 0.02]}, defaults, limits)
    assert p.hindcast_hours == 48 and p.windage_range == (0.01, 0.02)


def test_seed_particles_inside_polygon():
    poly = Polygon([(72, 18), (72.1, 18.05), (72.05, 18.1)])
    rng = np.random.default_rng(0)
    lons, lats = seed_particles(poly, 300, rng)
    from shapely import contains_xy

    assert lons.size == 300 and contains_xy(poly, lons, lats).all()


def test_ais_csv_validation_quarantines_bad_rows(tmp_path: Path):
    from oceantrace.ais.importer import import_ais_csv
    from oceantrace.core.db import db

    p = tmp_path / "ais.csv"
    p.write_text(
        "MMSI,BaseDateTime,LAT,LON,SOG,COG,Heading,VesselName,VesselType\n"
        "419000001,2026-01-01T00:00:00,18.5,72.5,10.1,45,44,TEST A,tanker\n"
        "419000001,2026-01-01T00:05:00,18.51,72.51,10.0,45,511,TEST A,tanker\n"
        "12,2026-01-01T00:00:00,18.5,72.5,10,45,45,BAD MMSI,cargo\n"
        "419000002,garbage,18.5,72.5,10,45,45,BAD TIME,cargo\n"
        "419000003,2026-01-01T00:00:00,95,72.5,10,45,45,BAD LAT,cargo\n"
    )
    from oceantrace.api.service import create_case

    case_id = create_case("units", "synthetic")["id"]
    s = import_ais_csv(case_id, p, source_label="unit", data_mode="synthetic")
    assert s["rows_total"] == 5 and s["rows_valid"] == 2 and s["rows_quarantined"] == 3
    assert set(s["quarantine_reasons"]) == {"invalid_mmsi", "unparseable_timestamp", "invalid_position"}
    with db() as conn:
        hdg = conn.execute("SELECT heading FROM ais_positions WHERE case_id=? ORDER BY ts", (case_id,)).fetchall()
    assert hdg[0][0] == 44 and hdg[1][0] is None  # 511 = not available → NULL
