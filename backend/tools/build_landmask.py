"""Build data/static/ne_10m_land.wkb from Natural Earth 10 m land polygons (public domain).

Polygons are subdivided on a regular grid so spatial-index queries touch only small
pieces (the raw Eurasia polygon alone has >100k vertices and makes point-in-polygon
slow). No GDAL/fiona required — a minimal shapefile reader is included.

Usage: python tools/build_landmask.py [--cell-deg 2] [--out ../data/static/ne_10m_land.wkb]
"""
from __future__ import annotations

import argparse
import io
import struct
import sys
import urllib.request
import zipfile
from pathlib import Path

from shapely import wkb
from shapely.geometry import MultiPolygon, Polygon, box
from shapely.validation import make_valid

URL = "https://naciscdn.org/naturalearth/10m/physical/ne_10m_land.zip"


def read_shp_polygons(data: bytes) -> list[Polygon]:
    pos = 100
    polys: list[Polygon] = []
    while pos < len(data):
        _, clen = struct.unpack(">ii", data[pos : pos + 8])
        pos += 8
        rec = data[pos : pos + clen * 2]
        pos += clen * 2
        if struct.unpack("<i", rec[:4])[0] != 5:
            continue
        nparts, npts = struct.unpack("<ii", rec[36:44])
        parts = struct.unpack(f"<{nparts}i", rec[44 : 44 + 4 * nparts])
        off = 44 + 4 * nparts
        pts = struct.unpack(f"<{2 * npts}d", rec[off : off + 16 * npts])
        rings = []
        for i, p0 in enumerate(parts):
            p1 = parts[i + 1] if i + 1 < nparts else npts
            rings.append([(pts[2 * k], pts[2 * k + 1]) for k in range(p0, p1)])

        def is_cw(r):
            a = 0.0
            for i in range(len(r) - 1):
                a += (r[i + 1][0] - r[i][0]) * (r[i + 1][1] + r[i][1])
            return a > 0

        outers = [r for r in rings if is_cw(r)]
        holes = [r for r in rings if not is_cw(r)]
        for o in outers:
            po = Polygon(o)
            hs = [h for h in holes if po.contains(Polygon(h).representative_point())]
            polys.append(Polygon(o, hs))
    return polys


def flatten(g) -> list[Polygon]:
    if g.is_empty:
        return []
    if g.geom_type == "Polygon":
        return [g]
    if hasattr(g, "geoms"):
        out = []
        for x in g.geoms:
            out.extend(flatten(x))
        return out
    return []


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cell-deg", type=float, default=2.0)
    ap.add_argument("--out", default=str(Path(__file__).resolve().parents[2] / "data" / "static" / "ne_10m_land.wkb"))
    ap.add_argument("--zip", default=None, help="local ne_10m_land.zip (skips download)")
    a = ap.parse_args()
    raw = Path(a.zip).read_bytes() if a.zip else urllib.request.urlopen(URL, timeout=120).read()
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        shp = z.read("ne_10m_land.shp")
        version = z.read("ne_10m_land.VERSION.txt").decode().strip()
    polys = read_shp_polygons(shp)
    polys = [p for p in polys if not (abs(p.centroid.x) < 0.01 and abs(p.centroid.y) < 0.01)]  # NE null-island placeholder
    print(f"{len(polys)} source polygons (Natural Earth {version})")
    pieces: list[Polygon] = []
    c = a.cell_deg
    for p in polys:
        if not p.is_valid:
            p = make_valid(p)
        minx, miny, maxx, maxy = p.bounds
        if (maxx - minx) <= c and (maxy - miny) <= c:
            pieces.extend(flatten(p))
            continue
        x0 = int(minx // c) * c
        y0 = int(miny // c) * c
        x = x0
        while x < maxx:
            y = y0
            while y < maxy:
                cell = box(x, y, x + c, y + c)
                if p.intersects(cell):
                    pieces.extend(flatten(p.intersection(cell)))
                y += c
            x += c
    mp = MultiPolygon(pieces)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(wkb.dumps(mp))
    (out.parent / "ne_10m_land.VERSION.txt").write_text(version + "\n")
    print(f"wrote {out} — {len(pieces)} pieces, {out.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    sys.exit(main())
