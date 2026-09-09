"""Generate a labelled SYNTHETIC end-to-end fixture: a speckled SAR GeoTIFF with an
elongated dark slick + an AIS CSV with one vessel crossing the slick's approximate
origin during a release window.

Every artifact is stamped SYNTHETIC (GeoTIFF tag OCEANTRACE_DATA_MODE=synthetic,
AIS `source=synthetic`) — the UI/exports keep that label. Nothing here is a
substitute for real data; it exists so the pipeline can be exercised end to end
before the user's model / AIS provider are connected.

Usage:
  python tools/make_fixture.py --out ../data/fixtures --bounds 72.0 18.4 72.5 18.9 \
      --acq 2026-09-01T01:00:00Z --hours-back 6
The AIS suspect crosses the SLICK CENTROID `hours-back` hours before acquisition;
after running a hindcast with real Open-Meteo forcing, the true origin will be
displaced by the drift, so ranking results vary with the met-ocean conditions —
that is expected behaviour, not a bug.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.synth import make_synthetic_ais_csv, make_synthetic_sar_geotiff  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, help="output directory")
    ap.add_argument("--bounds", nargs=4, type=float, required=True, metavar=("MIN_LON", "MIN_LAT", "MAX_LON", "MAX_LAT"), help="scene bounds (must be open sea for a meaningful hindcast)")
    ap.add_argument("--acq", required=True, help="acquisition time UTC, e.g. 2026-09-01T01:00:00Z")
    ap.add_argument("--hours-back", type=float, default=6.0, help="hours before acquisition at which the synthetic suspect crosses the slick centroid")
    ap.add_argument("--size", type=int, default=800, help="scene size in pixels")
    ap.add_argument("--background", type=int, default=12, help="number of background synthetic vessels")
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    acq = datetime.strptime(a.acq, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    tif = out / "synthetic_sar.tif"
    truth = make_synthetic_sar_geotiff(tif, a.bounds, size=a.size, acq=acq, seed=a.seed)
    release = acq - timedelta(hours=a.hours_back)
    csv_path = out / "synthetic_ais.csv"
    ais = make_synthetic_ais_csv(csv_path, truth["centroid"], release, n_background=a.background, seed=a.seed)
    manifest = {
        "data_mode": "synthetic",
        "scene": str(tif),
        "ais_csv": str(csv_path),
        "bounds": a.bounds,
        "acquisition_time_utc": a.acq,
        "synthetic_release_time_utc": release.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "slick_centroid": truth["centroid"],
        "suspect_mmsi": ais["suspect_mmsi"],
        "decoy_mmsi": ais["decoy_mmsi"],
        "ais_rows": ais["rows"],
        "note": "SYNTHETIC fixture — label it as such when importing (data_mode=synthetic).",
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
