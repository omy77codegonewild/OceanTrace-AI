"""Configuration loading: secrets from .env (pydantic-settings), algorithm
parameters from versioned YAML. A resolved snapshot is persisted per case."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    """Environment-driven settings (secrets + deployment)."""

    model_config = SettingsConfigDict(env_file=str(REPO_ROOT / ".env"), env_prefix="OT_", extra="ignore")

    data_dir: Path = REPO_ROOT / "data"
    config_path: Path = REPO_ROOT / "configs" / "default.yaml"
    db_path: Path | None = None  # defaults to data_dir / oceantrace.sqlite
    host: str = "0.0.0.0"
    port: int = 8000
    # Optional external credentials (never rendered to the frontend)
    aisstream_api_key: str | None = None
    datalastic_api_key: str | None = None
    max_workers: int = 2
    log_level: str = "INFO"

    @property
    def resolved_db_path(self) -> Path:
        return self.db_path or (self.data_dir / "oceantrace.sqlite")


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    for sub in ("scenes", "cases", "models", "env", "ais", "exports"):
        (s.data_dir / sub).mkdir(parents=True, exist_ok=True)
    return s


class AlgoConfig(BaseModel):
    """Thin wrapper over the YAML mapping with dotted access helpers."""

    raw: dict[str, Any] = Field(default_factory=dict)

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.raw
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def section(self, name: str) -> dict[str, Any]:
        return copy.deepcopy(self.raw.get(name, {}))

    @property
    def version(self) -> str:
        return str(self.raw.get("config_version", "unversioned"))

    def snapshot(self, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
        merged = copy.deepcopy(self.raw)
        if overrides:
            _deep_merge(merged, overrides)
        digest = hashlib.sha256(json.dumps(merged, sort_keys=True, default=str).encode()).hexdigest()[:16]
        return {"config": merged, "config_hash": digest, "config_version": self.version}


def _deep_merge(base: dict[str, Any], upd: dict[str, Any]) -> None:
    for k, v in upd.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


@lru_cache
def get_algo_config() -> AlgoConfig:
    path = get_settings().config_path
    if not path.exists():
        raise FileNotFoundError(f"Algorithm config not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return AlgoConfig(raw=raw)


def resolve_path(p: str | os.PathLike[str]) -> Path:
    path = Path(p)
    return path if path.is_absolute() else REPO_ROOT / path
