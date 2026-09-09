# OceanTrace AI — SAR oil-spill source attribution (SIH 2026 · PS 26143 · NTRO)

Decision-support prototype that turns a SAR acquisition into an **investigation priority list**:

```
SAR scene ─▶ slick segmentation ─▶ reverse Lagrangian drift ─▶ origin region + release window
          ─▶ AIS space-time correlation ─▶ ranked vessel candidates ─▶ dossier / exports
```

Every output carries a confidence, a data-mode label (`real | imported | synthetic`), the model
version and the config hash that produced it. Vessel rankings are **candidates for investigation
under stated model assumptions — never findings of responsibility** (see `docs/methods.md`).

---

## 1. Repository layout

```
backend/                 FastAPI service + algorithms (Python 3.11+)
  oceantrace/
    api/                 main.py (all routes), service.py (orchestration), schemas.py
    scenes/              GeoTIFF/PNG ingest, Sentinel-1 GRD subset fetch (Planetary Computer)
    detection/           pipeline + adapters: onnx_adapter.py (your model), classical.py (fallback)
    environment/         met-ocean forcing: Open-Meteo (real), CF-NetCDF upload, constant
    trajectory/          backward/forward Lagrangian ensemble (RK4/Euler), origin polygons, window
    ais/                 CSV importer (column aliases), aisstream.io live recorder
    attribution/         doc-06 scoring S = 100·D·(0.30P+0.25T+0.10H+0.10L+0.15G+0.10V)
    reporting/           self-contained HTML dossier
    geo/                 geodesy helpers, Natural Earth vector land mask
    core/                settings (.env), SQLite schema + audit log, job runner
  tools/                 make_fixture.py (labelled SYNTHETIC test data), build_landmask.py
  tests/                 unit + integration tests (pytest)
apps/web/                React + Vite + TypeScript + MapLibre operations console
configs/default.yaml     every algorithm parameter (versioned; snapshot stored per case)
data/                    runtime data (OT_DATA_DIR): scenes, cases, models, fixtures, static
docs/methods.md          method notes, assumptions, limitations, attribution
```

## 2. Quick start

### Backend

```bash
cd backend
python -m venv .venv && source .venv/bin/activate      # optional
pip install -r requirements.txt
cp ../.env.example ../.env                              # edit if needed (all optional)
python -m uvicorn oceantrace.api.main:app --host 0.0.0.0 --port 8000
# API docs: http://localhost:8000/api/docs
```

### Frontend (development)

```bash
cd apps/web
npm install
npm run dev            # http://localhost:5173 (proxies /api → 127.0.0.1:8000)
```

### Frontend (production build served by the API)

```bash
cd apps/web && npm run build      # writes apps/web/dist; the API serves it at http://localhost:8000/
```

### Tests

```bash
cd backend && OT_DATA_DIR=/tmp/oceantrace-test python -m pytest -q
```

The integration test performs a full detect → hindcast (real Open-Meteo forcing) → AIS →
attribution run on a generated synthetic scene.

## 3. Operating the console

1. **Create a case** on the landing page (choose the data mode; `synthetic` permanently labels the
   case and its exports).
2. **Scene intake** (satellite icon): upload a georeferenced GeoTIFF (Sentinel-1 GRD / any SAR
   amplitude raster; GCP-only rasters are supported), a PNG/JPG with manual bounds + acquisition
   time, *or* search the Sentinel-1 GRD catalogue by bbox/date and fetch a subset. Detection runs
   automatically → slick polygons (GeoJSON) with confidence, geometry metrics, coast distance and
   look-alike risk. Analyst review buttons record `confirmed / look-alike / uncertain`.
3. **Reverse drift** (drift icon): configure duration, time step, particles, ensemble members,
   windage range, current scale, direction jitter, diffusivity, probability mass, integrator and
   environment source, then *Run backtrack*. Output: 50/70/90 % origin regions, release window,
   per-step particle cloud (replay slider), met-ocean vectors, convergence metrics. *Run forward*
   produces a T+N h forecast envelope with coastal-impact check.
4. **AIS** (ship icon): import a historical CSV (MMSI, timestamp, lon, lat, optional
   SOG/COG/name/type; provider column aliases auto-mapped), optionally record live AIS
   (`OT_AISSTREAM_API_KEY`), tune weights, *Run AIS correlation*.
5. **Suspects / What-If / Evidence / Dossier** (left rail): attribution matrix with factor
   breakdown and evidence chain, weight sensitivity (rank stability), provenance graph, and the
   dossier with JSON / GeoJSON / HTML exports and analyst review actions.

Focus buttons (Full Corridor / Slick Poly / Spill Origin / Top Suspect / Dark Segment) drive the map
camera; the layer panel toggles every layer; the bottom bar replays the backtrack in time.

## 4. Plugging in your segmentation model

The detector is an adapter slot. Drop your ONNX model at `data/models/oilspill_seg.onnx`
(or change `detection.onnx.model_path`) and restart — `detection.adapter: auto` selects it
whenever the weights exist; otherwise the classical dark-spot detector runs and every result is
labelled with the adapter that actually executed.

Contract (`configs/default.yaml → detection.onnx`):

| key | meaning |
|---|---|
| `input_size`, `tile_overlap` | scene is tiled to `input_size²` with overlap; a fixed size baked into the model overrides it |
| `input_layout` `NCHW\|NHWC`, `input_channels` `1\|3` | tensor layout; 3 replicates the SAR band |
| `normalize` `minmax\|zscore\|none` | per-tile normalisation of the dB/amplitude image |
| `output_activation` `auto\|sigmoid\|softmax\|prob` | how logits become probabilities |
| `class_index_oil`, `class_index_lookalike` | class channels for multi-class heads (`-1` = none) |

Export from PyTorch:

```python
torch.onnx.export(model, torch.zeros(1, 1, 512, 512), "oilspill_seg.onnx",
                  input_names=["input"], output_names=["output"],
                  dynamic_axes={"input": {0: "b"}, "output": {0: "b"}}, opset_version=17)
```

Other frameworks: implement `DetectionAdapter.predict_tile()` in
`backend/oceantrace/detection/adapters/` and register it in `detection/pipeline.py`.

## 5. Data & providers

| Component | Source | Key | Label |
|---|---|---|---|
| Sentinel-1 GRD scenes | Microsoft Planetary Computer STAC (anonymous SAS) | none | real |
| Currents / wind | Open-Meteo Marine + Archive/Forecast APIs (Copernicus/MeteoFrance SMOC currents, ERA5/forecast wind) | none | real |
| Own met-ocean model | CF-NetCDF upload (`u_current, v_current, u10_wind, v10_wind`) | – | imported |
| AIS history | CSV import (any provider; aliases mapped) | – | imported |
| AIS live | aisstream.io | `OT_AISSTREAM_API_KEY` | real |
| Test data | `python tools/make_fixture.py --out ../data/fixtures --bounds … --acq …` | – | **synthetic** (always labelled) |
| Land mask | Natural Earth 10 m land (public domain), `data/static/ne_10m_land.wkb` | – | – |

Rebuild the land mask (needs internet, ~35 s, no GDAL required):
`cd backend && python tools/build_landmask.py [--zip ne_10m_land.zip] [--cell-deg 2]`.

## 6. API surface (`/api/v1`)

`health`, `config`, `cases` (CRUD), `cases/{id}/scene` (upload+detect), `catalog/search`,
`cases/{id}/scene/catalog`, `cases/{id}/analyze`, `scenes/{id}/preview.png`, `jobs/{id}`,
`cases/{id}/layers/{slicks|scene_footprint|hindcast|attribution|ais_tracks}`,
`cases/{id}/slicks/{sid}/hindcast|forecast`, `cases/{id}/environment/netcdf`,
`cases/{id}/ais/import|live/record`, `cases/{id}/attribute`, `cases/{id}/review`,
`cases/{id}/export?format=json|geojson`, `cases/{id}/audit`, `cases/{id}/report.html`.
Long-running work returns `202 {job_id}`; poll `jobs/{id}` for progress/result.

## 7. Non-negotiables implemented

- No hardcoded AOI, times, vessels, geometry, scores or secrets; all parameters live in
  `configs/default.yaml` and are validated against `hindcast.limits` server-side.
- Every artifact carries `data_mode`, provenance (model version, adapter, provider, config hash,
  seed) and stated limitations; the audit log records each action.
- UTC everywhere (`…Z` ISO-8601); `acquisition_time` source is recorded (TIFF tag / filename / manual).
- Synthetic data is labelled `SYNTHETIC` in the UI, the DB and every export.
- Vessel output wording is "candidate for investigation"; the dossier includes the disclaimer.

## 8. Attribution

Map tiles © OpenFreeMap / © OpenMapTiles / © OpenStreetMap contributors · Esri World Ocean Base
(Esri, GEBCO, NOAA, Garmin) · Met-ocean data by Open-Meteo.com (CC BY 4.0) using Copernicus /
MeteoFrance SMOC and ERA5 · Sentinel-1 data © ESA/Copernicus via Microsoft Planetary Computer ·
Land polygons: Natural Earth (public domain).
