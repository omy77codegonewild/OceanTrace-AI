"""ONNX segmentation adapter — the slot for the user's trained model.

Contract (configurable in configs/default.yaml -> detection.onnx):
  input : float32 tensor [1, C, H, W] (or NHWC), C = 1 or 3, H = W = input_size
  output: one of
          * [1, 1, H, W] logits  -> sigmoid           (output_activation: sigmoid)
          * [1, K, H, W] logits  -> softmax over K    (output_activation: softmax)
          * probabilities already in [0,1]            (output_activation: prob)
          `auto` inspects the tensor shape/range and picks one; the choice is
          recorded in provenance so it is auditable.
  Export from PyTorch:  torch.onnx.export(model, dummy, "oilspill_seg.onnx",
                        input_names=["input"], output_names=["output"],
                        dynamic_axes={"input": {0: "b"}, "output": {0: "b"}}, opset_version=17)
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any

import numpy as np

from ..base import DetectionAdapter, TileProbabilities

log = logging.getLogger("oceantrace.detection.onnx")


class ModelNotAvailable(RuntimeError):
    pass


class OnnxSegmentationAdapter(DetectionAdapter):
    name = "onnx"

    def __init__(self, cfg: dict[str, Any], model_path: Path):
        try:
            import onnxruntime as ort  # noqa: WPS433 (lazy import: optional dependency)
        except ImportError as e:  # pragma: no cover
            raise ModelNotAvailable("onnxruntime is not installed") from e
        if not model_path.exists():
            raise ModelNotAvailable(f"ONNX weights not found at {model_path}")
        self.cfg = cfg
        self.model_path = model_path
        self.model_version = f"{cfg.get('model_version', 'user-onnx')}@{_sha256(model_path)[:10]}"
        self.tile_size = int(cfg.get("input_size", 512))
        self.tile_overlap = int(cfg.get("tile_overlap", 64))
        self.normalize = str(cfg.get("normalize", "minmax"))
        self.layout = str(cfg.get("input_layout", "NCHW")).upper()
        self.channels = int(cfg.get("input_channels", 1))
        self.activation = str(cfg.get("output_activation", "auto"))
        self.idx_oil = int(cfg.get("class_index_oil", 1))
        self.idx_la = int(cfg.get("class_index_lookalike", -1))
        so = ort.SessionOptions()
        so.intra_op_num_threads = 2
        self.session = ort.InferenceSession(str(model_path), so, providers=["CPUExecutionProvider"])
        self.input_name = self.session.get_inputs()[0].name
        self.output_name = self.session.get_outputs()[0].name
        shp = self.session.get_inputs()[0].shape
        # respect a fixed spatial size baked into the model
        try:
            dims = [d for d in shp if isinstance(d, int)]
            spatial = [d for d in dims if d > 8]
            if spatial:
                self.tile_size = int(spatial[-1])
        except Exception:  # pragma: no cover
            pass
        log.info("ONNX model loaded: %s input=%s tile=%d", self.model_version, shp, self.tile_size)

    def describe(self) -> dict[str, Any]:
        d = super().describe()
        d.update({"model_path": str(self.model_path), "normalize": self.normalize, "layout": self.layout, "activation": self.activation})
        return d

    def _prep(self, tile: np.ndarray, valid: np.ndarray) -> np.ndarray:
        x = tile.astype("float32")
        fin = valid & np.isfinite(x)
        fill = float(np.nanmedian(x[fin])) if fin.any() else 0.0
        x = np.where(fin, x, fill)
        if self.normalize == "minmax":
            lo, hi = (np.percentile(x[fin], [1, 99]) if fin.any() else (0.0, 1.0))
            x = np.clip((x - lo) / max(hi - lo, 1e-6), 0, 1)
        elif self.normalize == "zscore":
            mu, sd = (x[fin].mean(), x[fin].std() + 1e-6) if fin.any() else (0.0, 1.0)
            x = (x - mu) / sd
        arr = np.repeat(x[None, ...], self.channels, axis=0)  # C,H,W
        arr = arr[None, ...]  # N,C,H,W
        if self.layout == "NHWC":
            arr = np.transpose(arr, (0, 2, 3, 1))
        return np.ascontiguousarray(arr.astype("float32"))

    def predict_tile(self, tile: np.ndarray, valid_mask: np.ndarray) -> TileProbabilities:
        h, w = tile.shape
        pad_h, pad_w = self.tile_size - h, self.tile_size - w
        t = tile
        v = valid_mask
        if pad_h > 0 or pad_w > 0:
            t = np.pad(tile, ((0, max(0, pad_h)), (0, max(0, pad_w))), mode="reflect")
            v = np.pad(valid_mask, ((0, max(0, pad_h)), (0, max(0, pad_w))), mode="constant")
        inp = self._prep(t, v)
        out = self.session.run([self.output_name], {self.input_name: inp})[0]
        out = np.asarray(out, dtype="float32")
        if out.ndim == 3:
            out = out[:, None, ...]
        if self.layout == "NHWC" and out.shape[-1] <= 8:
            out = np.transpose(out, (0, 3, 1, 2))
        k = out.shape[1]
        act = self.activation
        if act == "auto":
            if k == 1:
                act = "prob" if (out.min() >= 0.0 and out.max() <= 1.0) else "sigmoid"
            else:
                act = "prob" if np.allclose(out.sum(axis=1), 1.0, atol=1e-2) else "softmax"
        if act == "sigmoid":
            probs = 1.0 / (1.0 + np.exp(-out))
        elif act == "softmax":
            e = np.exp(out - out.max(axis=1, keepdims=True))
            probs = e / e.sum(axis=1, keepdims=True)
        else:
            probs = out
        if k == 1:
            oil = probs[0, 0]
            la = None
        else:
            oil = probs[0, min(self.idx_oil, k - 1)]
            la = probs[0, self.idx_la] if 0 <= self.idx_la < k else None
        oil = oil[:h, :w].astype("float32")
        oil[~valid_mask] = 0.0
        if la is not None:
            la = la[:h, :w].astype("float32")
            la[~valid_mask] = 0.0
        return TileProbabilities(oil=oil, look_alike=la, extra={"activation": act, "classes": int(k)})


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
