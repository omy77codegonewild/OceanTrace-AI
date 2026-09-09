"""Classical SAR dark-spot detector (no ML weights required).

Documented fallback used until an ONNX segmentation model is connected. It is
the standard first stage of operational oil-spill detection chains:

    1. Backscatter → dB, light Gaussian speckle smoothing.
    2. Robust local background: block-downsample, *median* filter (robust to
       ~50 % contamination, so wide slicks do not bias their own background),
       upsample. Noise scale σ from the MAD of residuals over valid pixels.
    3. contrast = background − pixel (dB darker);  z = contrast / σ.
    4. Hysteresis: strong seeds (z > k, contrast > min_contrast) grow into weak
       pixels (z > k/2, contrast > min_contrast/2) — standard for thin slicks.
    5. Connected components → calibrated probability from mean contrast:
       p = 0.5 + 0.5·clip(contrast / full_conf_db). Low-contrast regions land
       in the uncertain band (0.40–0.49); tiny regions are dropped.

It is honest about what it is: `model_version = classical-darkspot-*`, and the
UI shows a badge that a classical detector (not a trained network) produced the
mask. Look-alike discrimination is not attempted; every detection is a
*candidate* requiring analyst verification.
"""
from __future__ import annotations

from typing import Any

import warnings

import numpy as np
from scipy import ndimage as ndi

from ..base import DetectionAdapter, TileProbabilities


def to_db(tile: np.ndarray) -> np.ndarray:
    """Relative dB. Integer-like data (Sentinel-1 GRD DN amplitude) → 20·log10;
    linear float intensity → 10·log10; data containing negatives is treated as dB."""
    t = tile.astype("float32")
    finite = np.isfinite(t)
    if not finite.any():
        return np.full_like(t, np.nan)
    vals = t[finite]
    vmin, vmax = float(vals.min()), float(vals.max())
    if vmin < 0:
        return np.where(finite, t, np.nan)
    if vmax <= 1.0:
        factor = 10.0
    else:
        sample = vals[:: max(1, vals.size // 5000)]
        factor = 20.0 if np.all(np.mod(sample, 1.0) == 0) else 10.0
    with np.errstate(divide="ignore"):
        out = factor * np.log10(np.maximum(t, 1e-6))
    out[~finite | (t <= 0)] = np.nan
    return out


def _block_reduce_nanmean(x: np.ndarray, f: int) -> np.ndarray:
    h, w = x.shape
    H, W = -(-h // f) * f, -(-w // f) * f
    pad = np.full((H, W), np.nan, dtype="float32")
    pad[:h, :w] = x
    blocks = pad.reshape(H // f, f, W // f, f)
    with np.errstate(invalid="ignore"), warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)  # all-NaN (nodata) blocks are expected
        return np.nanmean(np.nanmean(blocks, axis=3), axis=1)


def robust_background(x: np.ndarray, valid: np.ndarray, window_px: int, factor: int = 8) -> tuple[np.ndarray, float]:
    """Local median background (upsampled) and a global robust noise scale."""
    xs = _block_reduce_nanmean(np.where(valid, x, np.nan), factor)
    nan = np.isnan(xs)
    if nan.all():
        return np.full(x.shape, np.nan, dtype="float32"), 1.0
    if nan.any():
        # fill holes with nearest valid block value
        idx = ndi.distance_transform_edt(nan, return_distances=False, return_indices=True)
        xs = xs[tuple(idx)]
    size = max(3, int(round(window_px / factor)) | 1)
    med = ndi.median_filter(xs, size=size, mode="reflect")
    bg = ndi.zoom(med, factor, order=1)[: x.shape[0], : x.shape[1]]
    resid = (x - bg)[valid]
    sigma = float(1.4826 * np.median(np.abs(resid - np.median(resid)))) if resid.size else 1.0
    return bg.astype("float32"), max(sigma, 1e-3)


class ClassicalDarkSpotAdapter(DetectionAdapter):
    name = "classical"

    def __init__(self, cfg: dict[str, Any], tile_size: int = 1024, tile_overlap: int = 160):
        self.cfg = cfg
        self.model_version = str(cfg.get("model_version", "classical-darkspot-v0"))
        self.window = int(cfg.get("window_px", 151)) | 1
        self.k = float(cfg.get("k_sigma", 3.0))
        self.smooth_sigma = float(cfg.get("smooth_sigma_px", 2.0))
        self.morph = int(cfg.get("morphology_px", 3))
        self.full_conf_db = float(cfg.get("contrast_full_conf_db", 6.0))
        self.min_contrast_db = float(cfg.get("min_contrast_db", 2.0))
        self.min_region_px = int(cfg.get("min_region_px", 60))
        self.tile_size = tile_size
        self.tile_overlap = tile_overlap

    def describe(self) -> dict[str, Any]:
        d = super().describe()
        d.update({"method": "robust-median background + hysteresis dark-spot", "window_px": self.window, "k_sigma": self.k, "min_contrast_db": self.min_contrast_db, "min_region_px": self.min_region_px, "smooth_sigma_px": self.smooth_sigma})
        return d

    def predict_tile(self, tile: np.ndarray, valid_mask: np.ndarray) -> TileProbabilities:
        db = to_db(tile)
        valid = valid_mask & np.isfinite(db)
        prob = np.zeros(tile.shape, dtype="float32")
        if valid.sum() < 200:
            return TileProbabilities(oil=prob)
        fill = float(np.nanmedian(db[valid]))
        x = np.where(valid, db, fill).astype("float32")
        if self.smooth_sigma > 0:
            x = ndi.gaussian_filter(x, self.smooth_sigma)

        bg, sigma = robust_background(x, valid, self.window)
        contrast = bg - x
        z = contrast / sigma
        strong = valid & (z > self.k) & (contrast > self.min_contrast_db)
        weak = valid & (z > 0.5 * self.k) & (contrast > 0.5 * self.min_contrast_db)
        if self.morph > 0:
            weak = ndi.binary_opening(weak, structure=np.ones((self.morph, self.morph)))
            weak = ndi.binary_closing(weak, structure=np.ones((self.morph, self.morph))) & valid
        labels, n = ndi.label(weak, structure=np.ones((3, 3)))
        if n == 0:
            return TileProbabilities(oil=prob, extra={"method": "median_hysteresis_dark_spot", "sigma_db": round(sigma, 3)})
        idx = np.arange(1, n + 1)
        sizes = ndi.sum(weak, labels, idx)
        has_seed = ndi.maximum(strong.astype("uint8"), labels, idx)
        mean_c = ndi.mean(contrast, labels, idx)
        lut = np.zeros(n + 1, dtype="float32")
        for i, (sz, seed, mc) in enumerate(zip(sizes, has_seed, mean_c), start=1):
            if sz < self.min_region_px or not seed:
                continue
            if mc < self.min_contrast_db:
                lut[i] = 0.40 + 0.09 * float(np.clip(mc / self.min_contrast_db, 0, 1))
            else:
                lut[i] = 0.5 + 0.5 * float(np.clip(mc / self.full_conf_db, 0, 1))
        prob = lut[labels]
        prob[~valid] = 0.0
        return TileProbabilities(oil=prob.astype("float32"), extra={"method": "median_hysteresis_dark_spot", "k_sigma": self.k, "window_px": self.window, "sigma_db": round(sigma, 3), "regions": int(n)})
