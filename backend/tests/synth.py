"""Test-fixture generators (used by tests and the `make_fixture` CLI).
Everything produced here is SYNTHETIC and labelled as such."""
from __future__ import annotations

import csv
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import from_bounds


def make_synthetic_sar_geotiff(path: Path, bounds: list[float], size: int = 800, acq: datetime | None = None, seed: int = 7) -> dict:
    """Speckled ocean backscatter (gamma-distributed, linear intensity) with an
    elongated dark slick. Returns truth polygon params."""
    rng = np.random.default_rng(seed)
    min_lon, min_lat, max_lon, max_lat = bounds
    base = rng.gamma(shape=4.0, scale=0.02, size=(size, size)).astype("float32")  # mean 0.08 linear (~ -11 dB)
    yy, xx = np.mgrid[0:size, 0:size]
    cx, cy = size * 0.55, size * 0.5
    ang = math.radians(35)
    xr = (xx - cx) * math.cos(ang) + (yy - cy) * math.sin(ang)
    yr = -(xx - cx) * math.sin(ang) + (yy - cy) * math.cos(ang)
    slick = (xr / (size * 0.22)) ** 2 + (yr / (size * 0.03)) ** 2 < 1.0
    damp = np.where(slick, 0.18, 1.0).astype("float32")  # ~ -7.4 dB darker
    img = base * damp
    # a land-like bright patch top-right corner (should not be detected as slick)
    img[: size // 12, -size // 12 :] *= 6.0
    transform = from_bounds(min_lon, min_lat, max_lon, max_lat, size, size)
    acq = acq or datetime.now(timezone.utc) - timedelta(days=3)
    with rasterio.open(path, "w", driver="GTiff", height=size, width=size, count=1, dtype="float32", crs="EPSG:4326", transform=transform) as ds:
        ds.write(img, 1)
        ds.update_tags(ACQUISITION_START_TIME=acq.strftime("%Y-%m-%dT%H:%M:%SZ"), POLARISATION="VV", OCEANTRACE_DATA_MODE="synthetic")
    lon_c = min_lon + (cx / size) * (max_lon - min_lon)
    lat_c = max_lat - (cy / size) * (max_lat - min_lat)
    return {"centroid": [lon_c, lat_c], "acq": acq, "slick_pixels": int(slick.sum()), "orientation_deg_img": 35}


def make_synthetic_ais_csv(path: Path, origin: list[float], release_utc: datetime, n_background: int = 12, seed: int = 3) -> dict:
    """Synthetic traffic: one tanker passing through the origin during the
    release window with an AIS gap, one that passes 20 km away, and background
    traffic. Every row carries source=synthetic."""
    rng = np.random.default_rng(seed)
    rows = []
    lon0, lat0 = origin

    def add_track(mmsi, name, vtype, start, heading_deg, speed_kn, t_start, hours, gap=None, report_s=120):
        lat, lon = start[1], start[0]
        v_ms = speed_kn * 0.514444
        n = int(hours * 3600 / report_s)
        for i in range(n):
            t = t_start + timedelta(seconds=i * report_s)
            if gap and gap[0] <= t <= gap[1]:
                continue
            d = v_ms * report_s
            dlat = d * math.cos(math.radians(heading_deg)) / 111320
            dlon = d * math.sin(math.radians(heading_deg)) / (111320 * math.cos(math.radians(lat)))
            lat += dlat
            lon += dlon
            rows.append({"mmsi": mmsi, "timestamp_utc": t.strftime("%Y-%m-%dT%H:%M:%SZ"), "longitude": f"{lon + rng.normal(0, 1e-5):.6f}", "latitude": f"{lat + rng.normal(0, 1e-5):.6f}",
                         "sog_knots": f"{speed_kn + rng.normal(0, 0.2):.1f}", "cog_deg": f"{heading_deg % 360:.0f}", "heading_deg": f"{heading_deg % 360:.0f}", "vessel_name": name, "vessel_type": vtype, "length_m": 180, "source": "synthetic"})

    # suspect: heading NE, passes through origin at release time, 40-min gap centred on release
    hdg = 45.0
    speed = 10.0
    hours_before = 3.0
    dist_m = speed * 0.514444 * hours_before * 3600
    start = [lon0 - dist_m * math.sin(math.radians(hdg)) / (111320 * math.cos(math.radians(lat0))), lat0 - dist_m * math.cos(math.radians(hdg)) / 111320]
    add_track(419001001, "SYNTH TANKER A", "tanker", start, hdg, speed, release_utc - timedelta(hours=hours_before), 6.0, gap=(release_utc - timedelta(minutes=20), release_utc + timedelta(minutes=20)))
    # decoy: passes 20 km north, continuous AIS
    add_track(419001002, "SYNTH CARGO B", "cargo", [start[0], start[1] + 0.18], hdg, 12.0, release_utc - timedelta(hours=hours_before), 6.0)
    # background traffic far away
    for k in range(n_background):
        mm = 419002000 + k
        s = [lon0 + rng.uniform(-0.8, 0.8), lat0 + rng.uniform(-0.8, 0.8)]
        if abs(s[0] - lon0) < 0.25 and abs(s[1] - lat0) < 0.25:
            s[0] += 0.4
        add_track(mm, f"SYNTH BG {k}", rng.choice(["cargo", "fishing", "container", "tanker"]), s, rng.uniform(0, 360), rng.uniform(4, 14), release_utc - timedelta(hours=rng.uniform(2, 6)), rng.uniform(3, 7), report_s=300)
    # some invalid rows to exercise quarantine
    rows.append({"mmsi": "12", "timestamp_utc": "not-a-time", "longitude": "999", "latitude": "0", "sog_knots": "", "cog_deg": "", "heading_deg": "", "vessel_name": "BAD", "vessel_type": "", "length_m": "", "source": "synthetic"})
    rows.append({"mmsi": "419009999", "timestamp_utc": release_utc.strftime("%Y-%m-%dT%H:%M:%SZ"), "longitude": "0", "latitude": "0", "sog_knots": "", "cog_deg": "", "heading_deg": "", "vessel_name": "NULL ISLAND", "vessel_type": "", "length_m": "", "source": "synthetic"})
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    return {"suspect_mmsi": 419001001, "decoy_mmsi": 419001002, "rows": len(rows)}
