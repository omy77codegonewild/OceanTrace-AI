"""Environmental fields (FR-4): a gridded (time, lat, lon) surface-current and
10 m wind field with trilinear sampling, plus providers:

  * OpenMeteoProvider   — live/archival model data (Copernicus SMOC currents via
                          Open-Meteo Marine API; ERA5/GFS winds via Open-Meteo
                          archive/forecast APIs). Free, no key, attribution
                          required. Provenance + coverage are recorded.
  * NetCDFProvider      — analyst-uploaded CF-style NetCDF (u_current, v_current,
                          u10_wind, v10_wind).
  * ConstantProvider    — deterministic field for tests (never used silently).

Conventions (documented, unit-tested):
  currents: direction is *towards* (oceanographic);  u = |c| sin θ, v = |c| cos θ
  wind:     direction is *from* (meteorological);    u = −|w| sin θ, v = −|w| cos θ
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import numpy as np
from scipy.interpolate import RegularGridInterpolator

log = logging.getLogger("oceantrace.environment")


def _epoch(t: datetime) -> float:
    return t.replace(tzinfo=t.tzinfo or timezone.utc).timestamp()


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class EnvField:
    times: np.ndarray            # (T,) epoch seconds UTC, increasing
    lats: np.ndarray             # (NY,) increasing
    lons: np.ndarray             # (NX,) increasing
    u_cur: np.ndarray            # (T, NY, NX) m/s eastward
    v_cur: np.ndarray            # (T, NY, NX) m/s northward
    u_wind: np.ndarray           # (T, NY, NX) m/s eastward (10 m)
    v_wind: np.ndarray           # (T, NY, NX) m/s northward (10 m)
    provider: str
    data_mode: str               # real | imported | synthetic | constant
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._interp = {}
        for name in ("u_cur", "v_cur", "u_wind", "v_wind"):
            arr = getattr(self, name)
            self._interp[name] = RegularGridInterpolator(
                (self.times, self.lats, self.lons), arr, method="linear", bounds_error=False, fill_value=None
            )

    # --- sampling -----------------------------------------------------------
    def sample(self, lons: np.ndarray, lats: np.ndarray, t_epoch: float) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """Vectorised trilinear sample at (lon, lat, t). Outside the grid the
        nearest edge value is used (linear extrapolation is clamped)."""
        t = np.clip(t_epoch, self.times[0], self.times[-1])
        la = np.clip(lats, self.lats[0], self.lats[-1])
        lo = np.clip(lons, self.lons[0], self.lons[-1])
        pts = np.column_stack([np.full(lo.shape, t), la, lo])
        return tuple(self._interp[n](pts) for n in ("u_cur", "v_cur", "u_wind", "v_wind"))  # type: ignore[return-value]

    # --- coverage -----------------------------------------------------------
    def covers(self, t_start: float, t_end: float, bounds: list[float]) -> tuple[bool, list[str]]:
        problems = []
        if t_start < self.times[0] - 1800:
            problems.append(f"time coverage starts {_iso(float(self.times[0]))}, needed {_iso(t_start)}")
        if t_end > self.times[-1] + 1800:
            problems.append(f"time coverage ends {_iso(float(self.times[-1]))}, needed {_iso(t_end)}")
        pad = 0.05
        if bounds[0] < self.lons[0] - pad or bounds[2] > self.lons[-1] + pad or bounds[1] < self.lats[0] - pad or bounds[3] > self.lats[-1] + pad:
            problems.append("spatial coverage does not include the full area of interest")
        return (not problems), problems

    def summary(self) -> dict[str, Any]:
        spd_c = np.hypot(self.u_cur, self.v_cur)
        spd_w = np.hypot(self.u_wind, self.v_wind)
        return {
            "provider": self.provider, "data_mode": self.data_mode,
            "time_start_utc": _iso(float(self.times[0])), "time_end_utc": _iso(float(self.times[-1])),
            "n_times": int(len(self.times)), "grid": [int(len(self.lats)), int(len(self.lons))],
            "bounds": [float(self.lons[0]), float(self.lats[0]), float(self.lons[-1]), float(self.lats[-1])],
            "current_speed_ms": {"mean": round(float(spd_c.mean()), 3), "max": round(float(spd_c.max()), 3)},
            "wind_speed_ms": {"mean": round(float(spd_w.mean()), 2), "max": round(float(spd_w.max()), 2)},
            **self.metadata,
        }

    # --- persistence --------------------------------------------------------
    def save(self, path: Path) -> str:
        path.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(path, times=self.times, lats=self.lats, lons=self.lons, u_cur=self.u_cur, v_cur=self.v_cur,
                            u_wind=self.u_wind, v_wind=self.v_wind,
                            meta=np.array(json.dumps({"provider": self.provider, "data_mode": self.data_mode, "metadata": self.metadata}, default=str)))
        h = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
        self.metadata["checksum"] = h
        return h

    @classmethod
    def load(cls, path: Path) -> "EnvField":
        z = np.load(path, allow_pickle=False)
        meta = json.loads(str(z["meta"]))
        return cls(times=z["times"], lats=z["lats"], lons=z["lons"], u_cur=z["u_cur"], v_cur=z["v_cur"], u_wind=z["u_wind"],
                   v_wind=z["v_wind"], provider=meta["provider"], data_mode=meta["data_mode"], metadata=meta.get("metadata", {}))


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
_SPEED_FACTORS = {"m/s": 1.0, "ms": 1.0, "km/h": 1 / 3.6, "kmh": 1 / 3.6, "kn": 0.514444, "knots": 0.514444, "kt": 0.514444, "mp/h": 0.44704, "mph": 0.44704}


def _speed_factor(unit: str | None) -> float:
    if not unit:
        return 1.0
    u = unit.strip().lower()
    if u not in _SPEED_FACTORS:
        raise ValueError(f"Unknown speed unit from provider: {unit!r}")
    return _SPEED_FACTORS[u]


def dir_towards_to_uv(speed: np.ndarray, deg: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    r = np.deg2rad(deg)
    return speed * np.sin(r), speed * np.cos(r)


def dir_from_to_uv(speed: np.ndarray, deg: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    r = np.deg2rad(deg)
    return -speed * np.sin(r), -speed * np.cos(r)


def _fill_nan(arr: np.ndarray) -> tuple[np.ndarray, float]:
    """Fill NaNs: spatial mean per time step, then temporal interpolation.
    Returns (filled, fraction_filled). Raises if nothing is usable."""
    a = arr.astype("float64").copy()
    total = a.size
    n_nan = int(np.isnan(a).sum())
    if n_nan == total:
        raise ValueError("provider returned no valid values for a required variable")
    if n_nan:
        for ti in range(a.shape[0]):
            sl = a[ti]
            if np.isnan(sl).any() and not np.isnan(sl).all():
                sl[np.isnan(sl)] = np.nanmean(sl)
        # temporal interpolation for fully-missing steps
        ts = np.arange(a.shape[0])
        for yi in range(a.shape[1]):
            for xi in range(a.shape[2]):
                col = a[:, yi, xi]
                bad = np.isnan(col)
                if bad.any():
                    col[bad] = np.interp(ts[bad], ts[~bad], col[~bad])
    return a, n_nan / total


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------
class EnvironmentUnavailable(RuntimeError):
    pass


class OpenMeteoProvider:
    name = "open_meteo"
    data_mode = "real"

    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self.marine_url = cfg.get("marine_url", "https://marine-api.open-meteo.com/v1/marine")
        self.archive_url = cfg.get("archive_url", "https://archive-api.open-meteo.com/v1/archive")
        self.forecast_url = cfg.get("forecast_url", "https://api.open-meteo.com/v1/forecast")
        self.n = int(cfg.get("grid_points_per_axis", 4))
        self.pad = float(cfg.get("aoi_pad_deg", 0.6))
        self.timeout = float(cfg.get("timeout_s", 40))
        self.archive_lag_days = int(cfg.get("archive_lag_days", 6))

    def fetch(self, bounds: list[float], t_start: datetime, t_end: datetime) -> EnvField:
        min_lon, min_lat, max_lon, max_lat = bounds
        lons = np.linspace(min_lon - self.pad, max_lon + self.pad, self.n)
        lats = np.linspace(min_lat - self.pad, max_lat + self.pad, self.n)
        LON, LAT = np.meshgrid(lons, lats)
        lat_s = ",".join(f"{v:.4f}" for v in LAT.ravel())
        lon_s = ",".join(f"{v:.4f}" for v in LON.ravel())
        d0 = (t_start - timedelta(hours=1)).strftime("%Y-%m-%d")
        d1 = (t_end + timedelta(hours=1)).strftime("%Y-%m-%d")
        prov: dict[str, Any] = {"requests": []}

        with httpx.Client(timeout=self.timeout, verify=False, headers={"User-Agent": "OceanTrace-AI/0.3 (research prototype)"}) as client:
            # --- currents ---
            mp = {"latitude": lat_s, "longitude": lon_s, "hourly": "ocean_current_velocity,ocean_current_direction",
                  "start_date": d0, "end_date": d1, "timezone": "UTC", "cell_selection": "sea"}
            mr = client.get(self.marine_url, params=mp)
            if mr.status_code != 200:
                raise EnvironmentUnavailable(f"Open-Meteo marine API error {mr.status_code}: {mr.text[:200]}")
            mdata = mr.json()
            if isinstance(mdata, dict):
                mdata = [mdata]
            prov["requests"].append({"url": str(mr.request.url)[:300], "dataset": "Open-Meteo Marine (MeteoFrance/Copernicus SMOC currents, ~8 km)"})
            times_iso = mdata[0]["hourly"]["time"]
            times = np.array([_epoch(datetime.fromisoformat(t).replace(tzinfo=timezone.utc)) for t in times_iso])
            cf = _speed_factor(mdata[0].get("hourly_units", {}).get("ocean_current_velocity"))
            spd = np.full((len(times), self.n, self.n), np.nan)
            ddeg = np.full_like(spd, np.nan)
            for k, item in enumerate(mdata):
                yi, xi = divmod(k, self.n)
                h = item["hourly"]
                spd[:, yi, xi] = np.array([np.nan if v is None else v for v in h["ocean_current_velocity"]], dtype=float) * cf
                ddeg[:, yi, xi] = np.array([np.nan if v is None else v for v in h["ocean_current_direction"]], dtype=float)
            spd, f1 = _fill_nan(spd)
            ddeg, _ = _fill_nan(ddeg)
            u_cur, v_cur = dir_towards_to_uv(spd, ddeg)

            # --- wind: archive (ERA5) if old enough, else forecast API (includes recent past) ---
            now = datetime.now(timezone.utc)
            use_archive = t_end < now - timedelta(days=self.archive_lag_days)
            wurl = self.archive_url if use_archive else self.forecast_url
            wp = {"latitude": lat_s, "longitude": lon_s, "hourly": "wind_speed_10m,wind_direction_10m", "start_date": d0,
                  "end_date": d1, "timezone": "UTC", "wind_speed_unit": "ms"}
            wr = client.get(wurl, params=wp)
            if wr.status_code != 200:
                raise EnvironmentUnavailable(f"Open-Meteo wind API error {wr.status_code}: {wr.text[:200]}")
            wdata = wr.json()
            if isinstance(wdata, dict):
                wdata = [wdata]
            prov["requests"].append({"url": str(wr.request.url)[:300], "dataset": "Open-Meteo ERA5 reanalysis 10 m wind" if use_archive else "Open-Meteo forecast-model 10 m wind (recent past + forecast)"})
            wtimes = np.array([_epoch(datetime.fromisoformat(t).replace(tzinfo=timezone.utc)) for t in wdata[0]["hourly"]["time"]])
            wf = _speed_factor(wdata[0].get("hourly_units", {}).get("wind_speed_10m"))
            wspd = np.full((len(wtimes), self.n, self.n), np.nan)
            wdeg = np.full_like(wspd, np.nan)
            for k, item in enumerate(wdata):
                yi, xi = divmod(k, self.n)
                h = item["hourly"]
                wspd[:, yi, xi] = np.array([np.nan if v is None else v for v in h["wind_speed_10m"]], dtype=float) * wf
                wdeg[:, yi, xi] = np.array([np.nan if v is None else v for v in h["wind_direction_10m"]], dtype=float)
            wspd, f2 = _fill_nan(wspd)
            wdeg, _ = _fill_nan(wdeg)
            u_w, v_w = dir_from_to_uv(wspd, wdeg)
            # align wind onto the current time axis
            u_wind = np.stack([np.interp(times, wtimes, u_w[:, yi, xi]) for yi in range(self.n) for xi in range(self.n)], axis=1).reshape(len(times), self.n, self.n)
            v_wind = np.stack([np.interp(times, wtimes, v_w[:, yi, xi]) for yi in range(self.n) for xi in range(self.n)], axis=1).reshape(len(times), self.n, self.n)

        # restrict to requested window (+1h margins)
        sel = (times >= _epoch(t_start) - 3600) & (times <= _epoch(t_end) + 3600)
        if sel.sum() < 2:
            raise EnvironmentUnavailable("provider returned fewer than 2 time steps for the requested window")
        meta = {
            "source": "Open-Meteo (currents: MeteoFrance/Copernicus SMOC ~0.08°; wind: ERA5 ~0.25° or forecast model)",
            "attribution": "Weather data by Open-Meteo.com (CC BY 4.0); currents: Copernicus Marine Service via MeteoFrance",
            "resolution_note": "Currents ~8 km, hourly; accuracy is limited in coastal areas.",
            "sampled_grid_points": int(self.n * self.n), "nan_filled_fraction": {"current": round(f1, 4), "wind": round(f2, 4)},
            "provenance": prov, "fetched_at_utc": _iso(_epoch(datetime.now(timezone.utc))),
            "wind_source_mode": "archive_era5" if use_archive else "forecast_model",
        }
        return EnvField(times=times[sel], lats=lats, lons=lons, u_cur=u_cur[sel], v_cur=v_cur[sel], u_wind=u_wind[sel], v_wind=v_wind[sel],
                        provider=self.name, data_mode=self.data_mode, metadata=meta)


class ConstantProvider:
    name = "constant"
    data_mode = "constant"

    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg

    def fetch(self, bounds: list[float], t_start: datetime, t_end: datetime) -> EnvField:
        pad = 1.0
        lons = np.linspace(bounds[0] - pad, bounds[2] + pad, 3)
        lats = np.linspace(bounds[1] - pad, bounds[3] + pad, 3)
        times = np.array([_epoch(t_start) - 3600, _epoch(t_end) + 3600])
        shp = (2, 3, 3)
        return EnvField(
            times=times, lats=lats, lons=lons,
            u_cur=np.full(shp, float(self.cfg.get("u_current", 0.1))), v_cur=np.full(shp, float(self.cfg.get("v_current", 0.05))),
            u_wind=np.full(shp, float(self.cfg.get("u_wind", 3.0))), v_wind=np.full(shp, float(self.cfg.get("v_wind", 1.0))),
            provider=self.name, data_mode=self.data_mode,
            metadata={"source": "constant field (test/diagnostic only)", "warning": "NOT real ocean data — used only when explicitly requested"},
        )


class NetCDFProvider:
    """Analyst-uploaded CF-style NetCDF. Variables (configurable aliases):
    u_current/v_current (m/s), u10_wind/v10_wind (m/s), dims time/latitude/longitude."""

    name = "netcdf_upload"
    data_mode = "imported"
    ALIASES = {
        "u_cur": ("u_current", "uo", "u", "eastward_sea_water_velocity"),
        "v_cur": ("v_current", "vo", "v", "northward_sea_water_velocity"),
        "u_wind": ("u10_wind", "u10", "eastward_wind"),
        "v_wind": ("v10_wind", "v10", "northward_wind"),
    }

    def __init__(self, path: Path):
        self.path = path

    def fetch(self, bounds: list[float], t_start: datetime, t_end: datetime) -> EnvField:
        import xarray as xr

        ds = xr.open_dataset(self.path)
        lat_name = next((n for n in ("latitude", "lat", "y") if n in ds.coords or n in ds.dims), None)
        lon_name = next((n for n in ("longitude", "lon", "x") if n in ds.coords or n in ds.dims), None)
        t_name = next((n for n in ("time", "t") if n in ds.coords or n in ds.dims), None)
        if not (lat_name and lon_name and t_name):
            raise EnvironmentUnavailable("NetCDF must have time/latitude/longitude coordinates")
        arrays = {}
        for key, names in self.ALIASES.items():
            var = next((n for n in names if n in ds.data_vars), None)
            if var is None:
                raise EnvironmentUnavailable(f"NetCDF missing variable for {key}; accepted names: {names}")
            da = ds[var].transpose(t_name, lat_name, lon_name)
            units = str(da.attrs.get("units", "m/s")).lower()
            if units not in ("m/s", "m s-1", "meter/second", "m.s-1"):
                raise EnvironmentUnavailable(f"variable {var} units must be m/s, got {units}")
            arrays[key] = da.values.astype("float64")
        lats = ds[lat_name].values.astype("float64")
        lons = ds[lon_name].values.astype("float64")
        times = np.array([(np.datetime64(t, "s") - np.datetime64("1970-01-01T00:00:00", "s")) / np.timedelta64(1, "s") for t in ds[t_name].values], dtype="float64")
        order_lat = np.argsort(lats)
        order_lon = np.argsort(lons)
        order_t = np.argsort(times)
        for k in arrays:
            a = arrays[k][order_t][:, order_lat][:, :, order_lon]
            arrays[k], _ = _fill_nan(a)
        f = EnvField(times=times[order_t], lats=lats[order_lat], lons=lons[order_lon], provider=self.name, data_mode=self.data_mode,
                     metadata={"source": f"analyst NetCDF {self.path.name}", "attrs": {k: str(v) for k, v in ds.attrs.items()}}, **arrays)
        ok, problems = f.covers(_epoch(t_start), _epoch(t_end), bounds)
        if not ok:
            f.metadata["coverage_warnings"] = problems
        return f


def build_provider(name: str, cfg: dict[str, Any], netcdf_path: Path | None = None):
    if name == "open_meteo":
        return OpenMeteoProvider(cfg.get("open_meteo", {}))
    if name == "constant":
        return ConstantProvider(cfg.get("constant", {}))
    if name == "netcdf_upload":
        if not netcdf_path:
            raise EnvironmentUnavailable("netcdf_upload provider requires an uploaded file")
        return NetCDFProvider(netcdf_path)
    raise EnvironmentUnavailable(f"unknown environment provider {name!r}")
