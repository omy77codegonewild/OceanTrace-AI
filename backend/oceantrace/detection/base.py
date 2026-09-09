"""Detection adapter contract. Every adapter consumes a float32 tile (H, W) of
SAR backscatter and returns per-pixel probabilities. The pipeline (tiling,
merging, polygonisation, characterisation) is shared and model-agnostic."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import numpy as np


@dataclass
class TileProbabilities:
    """Per-pixel class probabilities for one tile."""

    oil: np.ndarray                      # (H, W) float32 in [0,1]
    look_alike: np.ndarray | None = None  # (H, W) or None if model has no such class
    extra: dict[str, Any] = field(default_factory=dict)


class DetectionAdapter(ABC):
    name: str = "abstract"
    model_version: str = "n/a"
    data_mode: str = "real"  # detection output is derived from the real uploaded image
    tile_size: int = 512
    tile_overlap: int = 64

    @abstractmethod
    def predict_tile(self, tile: np.ndarray, valid_mask: np.ndarray) -> TileProbabilities: ...

    def describe(self) -> dict[str, Any]:
        return {"adapter": self.name, "model_version": self.model_version, "tile_size": self.tile_size, "tile_overlap": self.tile_overlap}

    def warmup(self) -> None:  # optional
        return None
