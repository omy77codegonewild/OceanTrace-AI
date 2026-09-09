# OceanTrace AI — Methods, assumptions and limitations

This note documents what each stage of the pipeline computes, which parameters drive it
(`configs/default.yaml`), what its provenance fields mean, and where it can be wrong.
It is the technical companion of the disclaimer printed on every export:

> OceanTrace AI output supports investigation prioritisation. Slick classes, origin regions,
> release windows and vessel scores are model estimates with stated uncertainty; they are not
> legal findings and do not establish responsibility.

---

## 1. Scene intake

| input | georeference | acquisition time |
|---|---|---|
| GeoTIFF / COG with CRS + transform | embedded | TIFF tag `ACQUISITION_START_TIME` / `TIFFTAG_DATETIME`, else parsed from a Sentinel-1 style filename, else manual |
| GeoTIFF with GCPs only (Sentinel-1 GRD as distributed) | warped to EPSG:4326 from GCPs | as above |
| PNG / JPG | manual bounds (`min_lon min_lat max_lon max_lat`) | manual (required) |
| Sentinel-1 GRD subset from the Planetary Computer STAC | GCP-warped window read | STAC `datetime` |

The scene is resampled to at most `scene.max_analysis_px` on the long side for detection and a
preview of `scene.preview_max_px` is written. The `time_source` field records where the
acquisition time came from; the working CRS for area/length computation is the scene-local UTM
zone (derived, never hardcoded).

## 2. Slick detection

Adapters share one contract: `predict_tile(tile) → probabilities in [0,1]` per pixel, optionally
with a look-alike channel. The pipeline tiles the scene, mosaics probabilities with overlap
blending, thresholds at `postprocess.prob_threshold`, applies `postprocess.uncertain_band` for the
`uncertain` class, drops regions under `postprocess.min_area_km2`, and vectorises.

* **onnx** — the user's trained segmentation network (see README §4). `model_version` records the
  configured name plus the SHA-256 prefix of the weights; the activation actually applied
  (`sigmoid|softmax|prob`) is stored in provenance.
* **classical** (`classical-darkspot-v0.3`) — fallback used only when no ONNX weights exist and
  always labelled as such. Two-pass adaptive dark-spot detector in the dB domain: coarse median
  background + MAD sigma, `dark if dB < background − k·σ` (`k_sigma`), background re-estimated
  excluding preliminary dark pixels, hysteresis growth with a seed requirement, morphological
  cleaning, calibrated confidence from contrast (`contrast_full_conf_db`). It has **no learned
  oil/look-alike discrimination**.

Per-feature properties: `class`, `confidence`, `area_km2`, `perimeter_km`, `length_km`,
`width_km`, `orientation_deg` (compass bearing of the major axis), `elongation`, `pixel_count`,
`centroid`, `coast_distance_km` (centroid → nearest Natural Earth coastline, capped at 50 km),
`look_alike_risk` (`low|medium|high`, heuristic: ≤2 km from coast +2, ≤5 km +1, elongation < 1.5
+1, model look-alike probability > 0.3 +2), `limitations[]`, `model_version`, `adapter`,
`review_status` (analyst: `pending|confirmed|look_alike|uncertain`).

**Known failure modes**: low-wind areas, biogenic films, rain cells, internal waves, ship wakes,
coastal calm water and land shadows all appear dark in SAR. Coastal detections on real Sentinel-1
scenes are flagged, not suppressed.

## 3. Met-ocean forcing

`EnvField` holds hourly surface current (u, v) and 10 m wind (u, v) on a small lon/lat grid
covering the scene ±buffer for the backtrack horizon; it is stored as `.npz` per case with
provider, data mode and time range, and sampled by trilinear interpolation (lon, lat, time).

| provider | data | notes |
|---|---|---|
| `open_meteo` (default, **real**) | Marine API `ocean_current_velocity/direction` (MeteoFrance/Copernicus SMOC ≈ 0.08°, hourly, from 2022); Archive/Forecast API `wind_speed_10m/direction_10m` (ERA5 ≈ 0.25°, or forecast model for recent dates) | current direction is *towards*, wind direction is *from*; both converted to u/v. Velocity units are read from the response (`hourly_units`) rather than assumed. Archive lags ≈ 5 days → forecast API used for recent times; the source is recorded. |
| `netcdf_upload` (**imported**) | CF-NetCDF with `u_current, v_current, u10_wind, v10_wind` | for operational models (HYCOM, INCOIS, WRF …) |
| `constant` | fixed vectors from config | for controlled tests only; never default |

The hindcast stores `environment.{provider, data_mode, source, wind_source, grid, time_range}`
and the Evidence panel shows it as a node in the chain.

## 4. Reverse Lagrangian drift (hindcast)

Particles are seeded uniformly inside the slick polygon at acquisition time and integrated
**backward** in time:

```
dx/dt = −( c · u_current(x,t) · R(θ) + w · u_wind10(x,t) ) + √(2K) ξ
```

* `c` — current scale sampled per ensemble member from `current_scale_range`
* `R(θ)` — rotation by a per-member direction error `θ ~ N(0, current_direction_jitter_deg)`
* `w` — windage (leeway) sampled per member from `windage_range` (2–4 % default)
* `K` — horizontal eddy diffusivity `diffusion_m2_s` (random walk)
* integrator `rk4` (default) or `euler`; time step `time_step_minutes`
* stranding: particles that land (Natural Earth vector mask, checked every 4th step) are frozen
* the deterministic **control run** (member 0, no jitter/diffusion) is kept separately

Ensemble size = `ensemble_members × particle_count` (default 20 × 600). Each run records
`seed`, all effective parameters and the config hash.

### 4.1 Origin regions

For every step the particle cloud is histogrammed on a `density_grid_cells²` grid over the
cloud extent; the smallest set of cells containing `probability_mass` (and 0.5/0.7/0.9) of the
particles is vectorised into the 50/70/90 % **origin regions**. Polygons are simplified and
rounded to 5 decimals to keep payloads small; areas are reported in km² in the scene UTM zone.
Region area is a direct measure of positional uncertainty — it is shown, never hidden.

### 4.2 Release window

A single SAR image observes the slick at one instant; the time of release is **not
observable** from the image alone. The window is therefore a documented heuristic:

1. Compute the deterministic control run's dispersion (covariance-ellipse area) per step.
2. `convergence(t) = area(T0) / area(t)`; backward clustering (> 1) marks periods when the
   observed slick would have been more compact — a physically plausible release period.
3. Window = the contiguous interval around the peak where `convergence − 1 ≥
   score_fraction_of_peak × peak`, no shorter than `min_window_hours` and starting at least
   `min_age_hours` before acquisition.
4. If the peak signal is below `min_convergence_signal` (uniform flow — the common case), the
   **full plausible horizon** is reported and `window_basis = "full_horizon"` is set; the UI and
   the report say so explicitly.

Ensemble agreement (spread of member centroids) is reported per step for transparency but is
**not** used to pick the window: it always decays backward and would bias the window to T‑0.

### 4.3 Other outputs

* `steps[]` — per-step time, ensemble/control areas, spread, agreement, peak density
* `cloud` — packed particle positions per step (`vis_particles`) and `paths` (`vis_paths`
  backtrack polylines) for replay; `spacetime_cloud` (full resolution) is stored for scoring
* `env_vectors` — current/wind sampled on a 9 × 7 grid over the corridor for every step
* forward **forecast** (`forecast.hours`) from the slick with the same physics, giving a T+N h
  envelope and a coastal-impact check against the land mask

## 5. AIS ingest and features

CSV columns (`ais.csv`): required `mmsi, timestamp_utc, longitude, latitude`; optional
`sog_knots, cog_deg, heading_deg, vessel_name, vessel_type, length_m, source`. Provider column
names (MarineCadastre, Spire, Datalastic, exactEarth …) are aliased automatically; rows with
invalid MMSI/coordinates/timestamps are quarantined and counted. Each import records its
`data_mode` (`imported|synthetic|real`) and label; the live recorder (aisstream.io) writes to the
same table with `source = aisstream.io`.

Candidate search: vessels with ≥ 2 positions inside the 90 % origin region buffered by
`candidate_search.origin_buffer_km` during the release window ± `time_pad_hours`. Per track:
minimum distance to the 70/90 % regions, dwell inside the 90 % region during the window,
slow-speed dwell (`loiter_speed_knots`, `loiter_min_minutes`), AIS gaps (interval >
`gap_multiple_of_expected` × the vessel's median interval and > `min_gap_minutes`), track
bearing vs. source→slick bearing, metadata completeness, and the **space-time distance**: for
each fix inside the horizon, the distance to the nearest backtracked particle *at that same
time* (interpolated between steps).

## 6. Attribution score (doc 06)

```
S = 100 · D · (0.30·P + 0.25·T + 0.10·H + 0.10·L + 0.15·G + 0.10·V)
```

| factor | meaning | computation |
|---|---|---|
| P proximity | how close the track came to where the oil was | `0.4·exp(−d_min/proximity_scale_km) + 0.6·exp(−d_spacetime/spacetime_scale_km)` (static only if no cloud) |
| T temporal overlap | presence in the origin region during the window | dwell fraction of the window; soft credit for near misses in space or time |
| H heading compatibility | track direction vs source→slick axis | `1 − Δangle/90°` (0 if > 90°) |
| L loitering | slow-speed dwell near the origin | scaled by `loiter_min_minutes` |
| G AIS gap relevance | transmission gaps overlapping the window | overlap fraction (0.3 floor if any gap near the window) |
| V vessel type prior | plausibility of discharge by type (`attribution.vessel_type_plausibility`) | tanker 1.0 … fishing 0.4 … unknown 0.6 |
| D data quality | multiplier from fix density and metadata completeness | `clip(0.5 + 0.3·density/6 + 0.2·meta, 0.3, 1)` |

Weights are configurable and normalised to 1; the What-If panel re-scores client-side and shows
rank stability. Priority bands: **High ≥ 80, Medium ≥ 55, Low ≥ 30, Insufficient < 30**. If
no vessel reaches Low or the corridor contains long AIS gaps, `dark_source_possible` is raised —
the polluter may not have been transmitting (dark vessel) or may not be in the dataset.

Every candidate carries the factor vector, raw features, an evidence chain in plain language,
`review_status` and analyst notes. **The ranking is an investigation priority under the stated
model assumptions; it is not evidence of responsibility.**

## 7. Land mask

Natural Earth 10 m land polygons (v5.1.1, public domain) packed as WKB, grid-subdivided at 2°
into 15 103 pieces and indexed with a shapely STRtree (`geo/landmask.py`). ≈ 1 km coastal
accuracy — adequate for stranding checks and coast-distance flags, not for harbour-scale work
(`OT_LANDMASK_PATH` can point at a higher-resolution build). Loads in ≈ 0.1 s / 68 MB; 10 k
point queries ≈ 8 ms.

## 8. Provenance and audit

Each case stores a resolved config snapshot with its SHA-256 (`config_hash`); scenes, slicks,
env fields, hindcast runs, AIS imports, attribution runs and vessel scores are separate tables
with timestamps, versions and data modes; every mutating action is written to `audit_log`.
Exports (`json`, `geojson`, `report.html`) embed all of it.

## 9. Limitations (summary)

* One SAR image = one instant; release time is inferred, not observed.
* Forcing resolution (≈ 8 km currents, ≈ 25 km wind) cannot resolve sub-mesoscale features;
  origin regions grow accordingly with hindcast length (report the area, choose the shortest
  horizon consistent with the slick's appearance).
* No oil weathering/spreading model; windage and current scale ranges stand in for it.
* Classical detector: no oil vs look-alike learning — connect a trained model.
* AIS coverage is incomplete (terrestrial vs satellite, spoofing, switched-off transponders).
* Synthetic fixtures exist only to exercise the pipeline and are labelled everywhere.
