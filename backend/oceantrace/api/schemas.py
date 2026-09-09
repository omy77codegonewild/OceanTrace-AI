"""Pydantic request/response contracts (typed API surface)."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

DataMode = Literal["real", "imported", "synthetic", "demo"]


class CaseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    data_mode: DataMode = "imported"
    notes: str | None = None


class DetectRequest(BaseModel):
    adapter: Literal["auto", "onnx", "classical"] | None = None
    prob_threshold: float | None = Field(default=None, ge=0.05, le=0.95)
    min_area_km2: float | None = Field(default=None, ge=0.001, le=100)
    max_features: int | None = Field(default=None, ge=1, le=200)
    land_mask: bool | None = None

    def overrides(self) -> dict[str, Any]:
        return {k: v for k, v in self.model_dump().items() if v is not None}


class HindcastRequest(BaseModel):
    hindcast_hours: float | None = None
    time_step_minutes: float | None = None
    particle_count: int | None = None
    ensemble_members: int | None = None
    windage_range: list[float] | None = None
    current_scale_range: list[float] | None = None
    current_direction_jitter_deg: float | None = Field(default=None, ge=0, le=90)
    wind_speed_jitter_frac: float | None = Field(default=None, ge=0, le=1)
    diffusion_m2_s: float | None = None
    probability_mass: float | None = Field(default=None, ge=0.3, le=0.99)
    integrator: Literal["rk4", "euler"] | None = None
    seed: int | None = None
    environment_source: Literal["open_meteo", "netcdf_upload", "constant"] = "open_meteo"
    netcdf_env_id: str | None = None

    def params(self) -> dict[str, Any]:
        d = self.model_dump(exclude={"environment_source", "netcdf_env_id"})
        return {k: v for k, v in d.items() if v is not None}


class ForecastRequest(BaseModel):
    hours: float | None = Field(default=None, ge=1, le=96)
    particle_count: int | None = None
    ensemble_members: int | None = None
    windage_range: list[float] | None = None
    environment_source: Literal["open_meteo", "constant"] = "open_meteo"


class AttributeRequest(BaseModel):
    hindcast_run_id: str | None = None
    weights: dict[str, float] | None = None
    excluded_mmsi: list[int] | None = None

    @field_validator("weights")
    @classmethod
    def _nonneg(cls, v: dict[str, float] | None) -> dict[str, float] | None:
        if v is not None:
            allowed = {"proximity", "temporal_overlap", "heading_compatibility", "loitering", "ais_gap_relevance", "vessel_type"}
            bad = set(v) - allowed
            if bad:
                raise ValueError(f"unknown weight keys {sorted(bad)}")
            if any(x < 0 for x in v.values()):
                raise ValueError("weights must be non-negative")
        return v


class LiveRecordRequest(BaseModel):
    bbox: list[float] = Field(min_length=4, max_length=4)
    minutes: float = Field(default=10, ge=1, le=240)


class SlickReview(BaseModel):
    slick_id: str
    status: Literal["confirmed", "look_alike", "uncertain"] | None = None
    note: str | None = None


class CandidateReview(BaseModel):
    mmsi: int
    status: Literal["investigate", "dismiss", "unknown"] | None = None
    note: str | None = None


class ReviewRequest(BaseModel):
    slick: SlickReview | None = None
    candidate: CandidateReview | None = None
    notes: str | None = None


class JobAccepted(BaseModel):
    job_id: str
    state: str = "queued"
    poll: str


class CatalogSearchRequest(BaseModel):
    bbox: list[float] = Field(min_length=4, max_length=4)
    start: str
    end: str
    limit: int = Field(default=20, ge=1, le=100)


class CatalogFetchRequest(BaseModel):
    item_id: str
    bbox: list[float] = Field(min_length=4, max_length=4)
    polarization: Literal["vv", "vh", "hh", "hv"] = "vv"
    run_detection: bool = True
    detect_options: DetectRequest | None = None

    @field_validator("bbox")
    @classmethod
    def _bbox(cls, v: list[float]) -> list[float]:
        if not (-180 <= v[0] < v[2] <= 180 and -90 <= v[1] < v[3] <= 90):
            raise ValueError("bbox must be [min_lon, min_lat, max_lon, max_lat]")
        if (v[2] - v[0]) * (v[3] - v[1]) > 4.0:
            raise ValueError("bbox too large for a subset fetch (max ~2°x2°)")
        return v
