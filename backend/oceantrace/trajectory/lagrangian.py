"""Lagrangian particle integrator (FR-5) — backward hindcast and forward forecast.

Motion model (see docs/methods.md and 13_DOCUMENT_MAP §Backward-drift math):

    dx/dt = c_e · R(θ_e) · u_current(x,t) + α_e · w_e · u_wind10(x,t) + ε

* c_e, θ_e, α_e, w_e are sampled once per ensemble member e (current scale,
  current direction jitter, windage/leeway, wind-speed factor).
* ε is a horizontal random walk, σ = sqrt(2·K·Δt), K = eddy diffusivity.
* Integration is RK4 (default) or Euler over the geographic state (lon, lat)
  using latitude-dependent metres-per-degree — no fixed projection is assumed.
* Backward integration simply uses Δt < 0 (time-reversed advection).

A deterministic *control* run (no perturbations, no diffusion) is integrated
alongside the ensemble. Its area evolution isolates flow-driven convergence
(forward divergence) from diffusive spreading, which is what the release-window
heuristic looks at. Nothing here claims an exact release time.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

import numpy as np
from shapely import contains_xy
from shapely.geometry import MultiPoint, Polygon, box, mapping
from shapely.ops import unary_union


from oceantrace.environment.fields import EnvField
from oceantrace.geo.utils import geodesic_area_km2, meters_per_degree
from oceantrace.geo import landmask as _landmask

def set_precision_5(geom):
    """Round coordinates to 1e-5 deg (~1 m) to keep GeoJSON compact."""
    try:
        from shapely import set_precision
        g = set_precision(geom, 1e-5)
        return g if not g.is_empty else geom
    except Exception:
        return geom


@dataclass
class HindcastParams:
    hindcast_hours: float = 24.0
    time_step_minutes: float = 15.0
    particle_count: int = 600
    ensemble_members: int = 20
    windage_range: tuple[float, float] = (0.02, 0.04)
    current_scale_range: tuple[float, float] = (0.8, 1.2)
    current_direction_jitter_deg: float = 15.0
    wind_speed_jitter_frac: float = 0.15
    diffusion_m2_s: float = 1.0
    probability_mass: float = 0.70
    integrator: str = "rk4"
    seed: int = 42
    direction: str = "backward"  # backward | forward

    @property
    def n_steps(self) -> int:
        return int(round(self.hindcast_hours * 60.0 / self.time_step_minutes))

    def to_dict(self) -> dict[str, Any]:
        return {k: (list(v) if isinstance(v, tuple) else v) for k, v in self.__dict__.items()}


def validate_params(user: dict[str, Any], defaults: dict[str, Any], limits: dict[str, Any], direction: str = "backward") -> HindcastParams:
    """Merge user overrides on config defaults and validate against min/max limits."""
    merged = {**defaults, **{k: v for k, v in user.items() if v is not None}}

    def _rng(name: str, val: float, key: str | None = None) -> float:
        lo, hi = limits.get(key or name, (-np.inf, np.inf))
        if not (lo <= float(val) <= hi):
            raise ValueError(f"{name}={val} outside allowed range [{lo}, {hi}]")
        return float(val)

    wr = merged.get("windage_range", [0.02, 0.04])
    if len(wr) != 2 or wr[0] > wr[1]:
        raise ValueError("windage_range must be [min, max] with min <= max")
    cr = merged.get("current_scale_range", [0.8, 1.2])
    if len(cr) != 2 or cr[0] > cr[1]:
        raise ValueError("current_scale_range must be [min, max] with min <= max")
    p = HindcastParams(
        hindcast_hours=_rng("hindcast_hours", merged["hindcast_hours"]),
        time_step_minutes=_rng("time_step_minutes", merged["time_step_minutes"]),
        particle_count=int(_rng("particle_count", merged["particle_count"])),
        ensemble_members=int(_rng("ensemble_members", merged["ensemble_members"])),
        windage_range=(_rng("windage_min", wr[0], "windage"), _rng("windage_max", wr[1], "windage")),
        current_scale_range=(_rng("current_scale_min", cr[0], "current_scale"), _rng("current_scale_max", cr[1], "current_scale")),
        current_direction_jitter_deg=float(merged.get("current_direction_jitter_deg", 15.0)),
        wind_speed_jitter_frac=float(merged.get("wind_speed_jitter_frac", 0.15)),
        diffusion_m2_s=_rng("diffusion_m2_s", merged.get("diffusion_m2_s", 1.0)),
        probability_mass=float(np.clip(merged.get("probability_mass", 0.7), 0.3, 0.99)),
        integrator=str(merged.get("integrator", "rk4")).lower(),
        seed=int(merged.get("seed", 42)),
        direction=direction,
    )
    if p.integrator not in ("rk4", "euler"):
        raise ValueError("integrator must be rk4 or euler")
    return p


# ---------------------------------------------------------------------------
# seeding
# ---------------------------------------------------------------------------
def seed_particles(polygon, n: int, rng: np.random.Generator) -> tuple[np.ndarray, np.ndarray]:
    """Uniform random points inside a (Multi)Polygon via rejection sampling."""
    minx, miny, maxx, maxy = polygon.bounds
    lons = np.empty(0)
    lats = np.empty(0)
    tries = 0
    while lons.size < n and tries < 200:
        m = max(2 * (n - lons.size), 256)
        xs = rng.uniform(minx, maxx, m)
        ys = rng.uniform(miny, maxy, m)
        inside = contains_xy(polygon, xs, ys)
        lons = np.concatenate([lons, xs[inside]])
        lats = np.concatenate([lats, ys[inside]])
        tries += 1
    if lons.size == 0:  # degenerate polygon: fall back to representative point
        rp = polygon.representative_point()
        return np.full(n, rp.x), np.full(n, rp.y)
    return lons[:n], lats[:n]


# ---------------------------------------------------------------------------
# integration
# ---------------------------------------------------------------------------
class _Member:
    def __init__(self, field: EnvField, alpha: float, cscale: float, theta_deg: float, wfac: float):
        self.field = field
        self.alpha = alpha
        self.cscale = cscale
        self.cos_t = np.cos(np.deg2rad(theta_deg))
        self.sin_t = np.sin(np.deg2rad(theta_deg))
        self.wfac = wfac

    def velocity_deg_per_s(self, lon: np.ndarray, lat: np.ndarray, t: float) -> tuple[np.ndarray, np.ndarray]:
        uc, vc, uw, vw = self.field.sample(lon, lat, t)
        # rotate current by θ, scale
        ucr = self.cscale * (uc * self.cos_t - vc * self.sin_t)
        vcr = self.cscale * (uc * self.sin_t + vc * self.cos_t)
        u = ucr + self.alpha * self.wfac * uw
        v = vcr + self.alpha * self.wfac * vw
        mlon, mlat = _mpd(lat)
        return u / mlon, v / mlat


def _mpd(lat: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    lat_r = np.deg2rad(lat)
    m_lat = 111132.954 - 559.822 * np.cos(2 * lat_r) + 1.175 * np.cos(4 * lat_r)
    m_lon = 111412.84 * np.cos(lat_r) - 93.5 * np.cos(3 * lat_r)
    return np.maximum(m_lon, 1.0), m_lat


def _step(member: _Member, lon: np.ndarray, lat: np.ndarray, t: float, dt: float, integrator: str) -> tuple[np.ndarray, np.ndarray]:
    if integrator == "euler":
        u, v = member.velocity_deg_per_s(lon, lat, t)
        return lon + u * dt, lat + v * dt
    k1u, k1v = member.velocity_deg_per_s(lon, lat, t)
    k2u, k2v = member.velocity_deg_per_s(lon + 0.5 * dt * k1u, lat + 0.5 * dt * k1v, t + 0.5 * dt)
    k3u, k3v = member.velocity_deg_per_s(lon + 0.5 * dt * k2u, lat + 0.5 * dt * k2v, t + 0.5 * dt)
    k4u, k4v = member.velocity_deg_per_s(lon + dt * k3u, lat + dt * k3v, t + dt)
    return lon + dt / 6.0 * (k1u + 2 * k2u + 2 * k3u + k4u), lat + dt / 6.0 * (k1v + 2 * k2v + 2 * k3v + k4v)


@dataclass
class TrajectoryResult:
    times: np.ndarray            # (S+1,) epoch seconds (decreasing for backward)
    lon: np.ndarray              # (E, N, S+1) float32
    lat: np.ndarray              # (E, N, S+1)
    stranded: np.ndarray         # (E, N) bool — particle hit land
    ctrl_lon: np.ndarray         # (N, S+1) control run (no perturbation/diffusion)
    ctrl_lat: np.ndarray
    member_params: list[dict[str, float]]
    params: HindcastParams


def integrate(polygon, field: EnvField, t0_epoch: float, params: HindcastParams, progress: Callable[[float, str], None] | None = None) -> TrajectoryResult:
    rng = np.random.default_rng(params.seed)
    n, E, S = params.particle_count, params.ensemble_members, params.n_steps
    sign = -1.0 if params.direction == "backward" else 1.0
    dt = sign * params.time_step_minutes * 60.0
    times = t0_epoch + dt * np.arange(S + 1)

    lon0, lat0 = seed_particles(polygon, n, rng)
    lon = np.empty((E, n, S + 1), dtype="float32")
    lat = np.empty((E, n, S + 1), dtype="float32")
    stranded = np.zeros((E, n), dtype=bool)
    member_params: list[dict[str, float]] = []
    sigma_diff = float(np.sqrt(2.0 * params.diffusion_m2_s * abs(dt)))

    # --- control run (deterministic mean parameters, no diffusion) ---
    ctrl = _Member(field, alpha=float(np.mean(params.windage_range)), cscale=float(np.mean(params.current_scale_range)), theta_deg=0.0, wfac=1.0)
    clon, clat = lon0.astype("float64").copy(), lat0.astype("float64").copy()
    ctrl_lon = np.empty((n, S + 1), dtype="float32")
    ctrl_lat = np.empty((n, S + 1), dtype="float32")
    ctrl_lon[:, 0], ctrl_lat[:, 0] = clon, clat
    for s in range(S):
        clon, clat = _step(ctrl, clon, clat, float(times[s]), dt, params.integrator)
        ctrl_lon[:, s + 1], ctrl_lat[:, s + 1] = clon, clat

    land_ok = _landmask.available()
    for e in range(E):
        alpha = float(rng.uniform(*params.windage_range))
        cscale = float(rng.uniform(*params.current_scale_range))
        theta = float(rng.normal(0.0, params.current_direction_jitter_deg))
        wfac = float(max(0.2, rng.normal(1.0, params.wind_speed_jitter_frac)))
        member_params.append({"windage": round(alpha, 4), "current_scale": round(cscale, 3), "current_dir_jitter_deg": round(theta, 1), "wind_factor": round(wfac, 3)})
        m = _Member(field, alpha, cscale, theta, wfac)
        plon, plat = lon0.astype("float64").copy(), lat0.astype("float64").copy()
        lon[e, :, 0], lat[e, :, 0] = plon, plat
        alive = np.ones(n, dtype=bool)
        for s in range(S):
            nlon, nlat = _step(m, plon, plat, float(times[s]), dt, params.integrator)
            if sigma_diff > 0:
                mlon, mlat = _mpd(nlat)
                nlon = nlon + rng.normal(0.0, sigma_diff, n) / mlon
                nlat = nlat + rng.normal(0.0, sigma_diff, n) / mlat
            if land_ok and (s % 4 == 0):
                on_land = _landmask.is_land(nlon, nlat)
                newly = on_land & alive
                if newly.any():
                    alive[newly] = False
                    stranded[e, newly] = True
            nlon = np.where(alive, nlon, plon)
            nlat = np.where(alive, nlat, plat)
            plon, plat = nlon, nlat
            lon[e, :, s + 1], lat[e, :, s + 1] = plon, plat
        if progress:
            progress(0.25 + 0.5 * (e + 1) / E, f"ensemble member {e + 1}/{E}")
    return TrajectoryResult(times=times, lon=lon, lat=lat, stranded=stranded, ctrl_lon=ctrl_lon, ctrl_lat=ctrl_lat, member_params=member_params, params=params)


# ---------------------------------------------------------------------------
# density / probability regions
# ---------------------------------------------------------------------------
class DensityGrid:
    """Fixed lon/lat histogram grid spanning the full trajectory extent so
    that areas are comparable across time steps."""

    def __init__(self, lon_all: np.ndarray, lat_all: np.ndarray, n_cells: int = 60):
        lo_x, hi_x = float(np.nanmin(lon_all)), float(np.nanmax(lon_all))
        lo_y, hi_y = float(np.nanmin(lat_all)), float(np.nanmax(lat_all))
        pad_x = max((hi_x - lo_x) * 0.05, 0.002)
        pad_y = max((hi_y - lo_y) * 0.05, 0.002)
        self.xe = np.linspace(lo_x - pad_x, hi_x + pad_x, n_cells + 1)
        self.ye = np.linspace(lo_y - pad_y, hi_y + pad_y, n_cells + 1)
        mlon, mlat = meters_per_degree((lo_y + hi_y) / 2)
        self.cell_km2 = (self.xe[1] - self.xe[0]) * mlon * (self.ye[1] - self.ye[0]) * mlat / 1e6

    def hist(self, lon: np.ndarray, lat: np.ndarray) -> np.ndarray:
        h, _, _ = np.histogram2d(lon.ravel(), lat.ravel(), bins=[self.xe, self.ye])
        return h.T  # (ny, nx)

    def mass_region(self, h: np.ndarray, mass: float) -> tuple[np.ndarray, float]:
        """Smallest set of cells containing `mass` fraction of particles → (bool mask, area km²)."""
        flat = h.ravel()
        total = flat.sum()
        if total <= 0:
            return np.zeros_like(h, dtype=bool), 0.0
        order = np.argsort(flat)[::-1]
        csum = np.cumsum(flat[order])
        k = int(np.searchsorted(csum, mass * total)) + 1
        mask = np.zeros(flat.shape, dtype=bool)
        mask[order[:k]] = True
        mask = mask.reshape(h.shape)
        return mask, float(mask.sum() * self.cell_km2)

    def mask_to_polygon(self, mask: np.ndarray, smooth_km: float = 0.0):
        cells = []
        ys, xs = np.nonzero(mask)
        for y, x in zip(ys, xs):
            cells.append(box(self.xe[x], self.ye[y], self.xe[x + 1], self.ye[y + 1]))
        if not cells:
            return Polygon()
        geom = unary_union(cells)
        if smooth_km > 0:
            deg = smooth_km / 111.0
            geom = geom.buffer(deg, join_style=1).buffer(-deg * 0.6, join_style=1)
            # buffering creates dense arcs; simplify to ~1/10 of a grid cell so payloads stay small
            geom = geom.simplify(min(deg * 0.2, 0.15 * float(self.xe[1] - self.xe[0])), preserve_topology=True)
        return set_precision_5(geom)


def analyse(res: TrajectoryResult, slick_area_km2: float, cfg: dict[str, Any], progress: Callable[[float, str], None] | None = None) -> dict[str, Any]:
    """Per-step convergence metrics, origin window, probability polygons and
    packed visualisation data."""
    p = res.params
    S = p.n_steps
    grid = DensityGrid(res.lon, res.lat, int(cfg.get("density_grid_cells", 60)))
    mass = p.probability_mass
    E, N = res.lon.shape[0], res.lon.shape[1]

    steps: list[dict[str, Any]] = []
    ens_area = np.empty(S + 1)
    ctrl_area = np.empty(S + 1)
    spread_km = np.empty(S + 1)
    peak_density = np.empty(S + 1)
    for s in range(S + 1):
        lo, la = res.lon[:, :, s], res.lat[:, :, s]
        h = grid.hist(lo, la)
        _, a = grid.mass_region(h, mass)
        ens_area[s] = a
        peak_density[s] = h.max() / max(h.sum(), 1)
        # ensemble agreement: dispersion of member centroids (km)
        cx, cy = lo.mean(axis=1), la.mean(axis=1)
        mlon, mlat = meters_per_degree(float(cy.mean()))
        spread_km[s] = float(np.sqrt(((cx - cx.mean()) * mlon) ** 2 + ((cy - cy.mean()) * mlat) ** 2).mean()) / 1000.0 if E > 1 else 0.0
        # control-run dispersion area: continuous covariance-ellipse measure (km²), not a
        # quantised histogram, so a uniform flow yields exactly ratio 1.0 and only true
        # flow convergence/divergence moves it.
        clon_m = (res.ctrl_lon[:, s] - res.ctrl_lon[:, s].mean()) * mlon
        clat_m = (res.ctrl_lat[:, s] - res.ctrl_lat[:, s].mean()) * mlat
        cov = np.cov(np.vstack([clon_m, clat_m]))
        ctrl_area[s] = float(np.pi * np.sqrt(max(np.linalg.det(cov), 1e-6))) / 1e6
    # avoid zero areas for ratios
    ens_area = np.maximum(ens_area, grid.cell_km2)
    ctrl_area = np.maximum(ctrl_area, 1e-4)

    hours_back = np.abs(res.times - res.times[0]) / 3600.0
    # Physical signal: clustering of the deterministic control run (backward convergence
    # == forward divergence of the flow). >1 means particles were closer together then.
    convergence = ctrl_area[0] / ctrl_area
    excess = convergence - 1.0
    # Ensemble agreement is reported for transparency but NOT used to pick the window:
    # it always decays backward in time and would bias the estimate towards T-0.
    agreement = 1.0 / (1.0 + spread_km / max(float(cfg.get("agreement_scale_km", 5.0)), 0.1))
    min_age_h = float(cfg.get("min_age_hours", 0.5))
    valid_age = hours_back >= min_age_h
    score = np.where(valid_age, np.maximum(excess, 0.0), 0.0)

    # ---- window selection (documented heuristic, see docs/methods.md) ----
    frac = float(cfg.get("score_fraction_of_peak", 0.5))
    min_win_h = float(cfg.get("min_window_hours", 2.0))
    min_signal = float(cfg.get("min_convergence_signal", 0.03))
    basis: str
    if valid_age.any() and float(np.nanmax(score)) >= min_signal:
        peak_i = int(np.nanargmax(score))
        thr = frac * score[peak_i]
        i0 = i1 = peak_i
        while i0 > 0 and score[i0 - 1] >= thr and valid_age[i0 - 1]:
            i0 -= 1
        while i1 < S and score[i1 + 1] >= thr:
            i1 += 1
        basis = "flow_convergence_peak"
    else:
        # No usable convergence signal in the flow field: the honest answer is the full
        # plausible-age horizon. Attribution then relies on space-time matching along
        # the backtrack corridor rather than on a narrow window.
        idx = np.nonzero(valid_age)[0] if valid_age.any() else np.array([0, S])
        i0, i1 = int(idx.min()), int(idx.max())
        peak_i = i1
        basis = "no_convergence_signal_full_horizon"
    # enforce minimum window width
    while (hours_back[i1] - hours_back[i0]) < min_win_h and (i0 > 0 or i1 < S):
        if i1 < S:
            i1 += 1
        if (hours_back[i1] - hours_back[i0]) < min_win_h and i0 > 0:
            i0 -= 1
    t_a, t_b = float(res.times[i1]), float(res.times[i0])  # backward: times decreasing
    release_start, release_end = (min(t_a, t_b), max(t_a, t_b))

    if progress:
        progress(0.85, "building origin probability polygons")

    # ---- origin polygons aggregated over the window (50/70/90 %) ----
    win = slice(min(i0, i1), max(i0, i1) + 1)
    lon_w, lat_w = res.lon[:, :, win], res.lat[:, :, win]
    hw = grid.hist(lon_w, lat_w)
    polys = {}
    for m_ in (0.5, mass, 0.9):
        mask_, area_ = grid.mass_region(hw, m_)
        geom = grid.mask_to_polygon(mask_, smooth_km=0.5)
        polys[f"{int(round(m_ * 100))}"] = {"geometry": mapping(geom), "area_km2": round(geodesic_area_km2(geom), 2) if not geom.is_empty else 0.0}
    origin_geom = polys[f"{int(round(mass * 100))}"]["geometry"]

    # ---- per-step 70% region for animation + centroid track ----
    step_rows = []
    hourly = max(1, int(round(60.0 / p.time_step_minutes)))
    for s in range(0, S + 1):
        row = {
            "step": s, "hours": round(float(hours_back[s]), 3), "time_utc": _iso(float(res.times[s])),
            "area_km2": round(float(ens_area[s]), 2), "control_area_km2": round(float(ctrl_area[s]), 2),
            "convergence": round(float(convergence[s]), 3), "spread_km": round(float(spread_km[s]), 2),
            "agreement": round(float(agreement[s]), 3), "score": round(float(score[s]), 3),
            "centroid": [round(float(res.lon[:, :, s].mean()), 5), round(float(res.lat[:, :, s].mean()), 5)],
        }
        if s % hourly == 0 or s == S:
            h = grid.hist(res.lon[:, :, s], res.lat[:, :, s])
            mk, _ = grid.mass_region(h, mass)
            row["region"] = mapping(grid.mask_to_polygon(mk, smooth_km=0.4))
        step_rows.append(row)

    # ---- packed particles for the UI (subsample) ----
    rng = np.random.default_rng(p.seed + 1)
    n_vis = min(int(cfg.get("vis_particles", 400)), E * N)
    flat_idx = rng.choice(E * N, n_vis, replace=False)
    ee, nn = np.divmod(flat_idx, N)
    vis_steps = list(range(0, S + 1, max(1, hourly // 2))) if S > 0 else [0]
    if vis_steps[-1] != S:
        vis_steps.append(S)
    cloud = [[[round(float(x), 5), round(float(y), 5)] for x, y in zip(res.lon[ee, nn, s], res.lat[ee, nn, s])] for s in vis_steps]
    n_paths = min(int(cfg.get("vis_paths", 60)), n_vis)
    paths = [[[round(float(res.lon[ee[i], nn[i], s]), 5), round(float(res.lat[ee[i], nn[i], s]), 5)] for s in vis_steps] for i in range(n_paths)]

    # compact space-time cloud for attribution (every step, <=300 particles): lon/lat arrays
    n_att = min(300, E * N)
    att_idx = rng.choice(E * N, n_att, replace=False)
    ae, an = np.divmod(att_idx, N)
    st_cloud = {"times": [float(t) for t in res.times], "lon": np.round(res.lon[ae, an, :].T, 5).tolist(), "lat": np.round(res.lat[ae, an, :].T, 5).tolist()}

    stranded_frac = float(res.stranded.mean())
    limitations = [
        "Release window is estimated under model assumptions (Open-Meteo/Copernicus ~8 km currents, 10 m wind, windage range); it is not an observed time.",
        "A single SAR acquisition observes the slick at one instant; continuous discharge or fragmented slicks widen the true window.",
        f"Ensemble mean centroid spread at window start: {spread_km[i1]:.1f} km; at window end: {spread_km[i0]:.1f} km.",
    ]
    if basis == "no_convergence_signal_full_horizon":
        limitations.append("No flow-convergence signal in the current/wind field over this horizon; the release window spans the full plausible-age range. Vessel matching uses space-time proximity along the backtrack corridor.")
    else:
        limitations.append(f"Window derived from the deterministic-flow convergence peak (T-{hours_back[peak_i]:.1f} h, control-run clustering ratio {convergence[peak_i]:.2f}); this is a heuristic, not a measurement.")
    if stranded_frac > 0.05:
        limitations.append(f"{stranded_frac * 100:.0f}% of particles reached the coastline during backtracking and were frozen.")

    return {
        "release_time_start_utc": _iso(release_start), "release_time_end_utc": _iso(release_end),
        "window_basis": basis, "window_hours_back": [round(float(hours_back[min(i0, i1)]), 2), round(float(hours_back[max(i0, i1)]), 2)],
        "peak_hours_back": round(float(hours_back[peak_i]), 2), "peak_time_utc": _iso(float(res.times[peak_i])),
        "confidence_level": mass, "origin_geometry": origin_geom, "origin_regions": polys,
        "steps": step_rows, "cloud_steps": [step_rows[s]["time_utc"] for s in vis_steps], "cloud_hours": [round(float(hours_back[s]), 2) for s in vis_steps],
        "cloud": cloud, "paths": paths, "spacetime_cloud": st_cloud, "member_params": res.member_params, "stranded_fraction": round(stranded_frac, 4),
        "limitations": limitations, "grid_cell_km2": round(grid.cell_km2, 4),
    }


def _iso(t: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def forecast_summary(res: TrajectoryResult, cfg: dict[str, Any]) -> dict[str, Any]:
    """Forward drift envelope: hourly 70% regions + swept envelope."""
    p = res.params
    S = p.n_steps
    E, N = res.lon.shape[:2]
    grid = DensityGrid(res.lon, res.lat, int(cfg.get("density_grid_cells", 60)))
    hourly = max(1, int(round(60.0 / p.time_step_minutes)))

    n_vis = min(int(cfg.get("vis_particles", 3000)), E * N)
    rng = np.random.default_rng(42)
    flat_idx = rng.choice(E * N, n_vis, replace=False)
    ee, nn = np.divmod(flat_idx, N)
    vis_steps = list(range(0, S + 1, max(1, hourly // 2))) if S > 0 else [0]
    if vis_steps[-1] != S:
        vis_steps.append(S)
    cloud = [[[round(float(x), 5), round(float(y), 5)] for x, y in zip(res.lon[ee, nn, s], res.lat[ee, nn, s])] for s in vis_steps]

    rows = []
    for s in range(0, S + 1, hourly):
        h = grid.hist(res.lon[:, :, s], res.lat[:, :, s])
        mk, a = grid.mass_region(h, p.probability_mass)
        rows.append({"hours": round(float(abs(res.times[s] - res.times[0]) / 3600.0), 2), "time_utc": _iso(float(res.times[s])), "area_km2": round(a, 2),
                     "region": mapping(grid.mask_to_polygon(mk, smooth_km=0.4)), "centroid": [round(float(res.lon[:, :, s].mean()), 5), round(float(res.lat[:, :, s].mean()), 5)]})
    hull = MultiPoint([(float(x), float(y)) for x, y in zip(res.lon[:, :, ::hourly].ravel()[::7], res.lat[:, :, ::hourly].ravel()[::7])]).convex_hull
    return {"steps": rows, "cloud": cloud, "envelope": mapping(hull), "envelope_area_km2": round(geodesic_area_km2(hull), 2), "stranded_fraction": round(float(res.stranded.mean()), 4),
            "coastal_impact_risk": bool(res.stranded.mean() > 0.02)}
