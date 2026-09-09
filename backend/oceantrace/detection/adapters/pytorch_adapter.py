"""PyTorch segmentation adapter — slot for the user's trained U-Net model.

Loads `best_oil_spill_unet.pth` weights and executes high-speed sliding-window
inference over SAR scenes. Supports both in-process PyTorch execution and
cross-environment worker execution.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import numpy as np

from ..base import DetectionAdapter, TileProbabilities

log = logging.getLogger("oceantrace.detection.pytorch")


class PyTorchModelNotAvailable(RuntimeError):
    pass


def _find_torch_python() -> Path | None:
    """Find a Python interpreter with torch available."""
    # 1. Current interpreter
    try:
        import torch  # noqa: F401
        return Path(sys.executable)
    except ImportError:
        pass

    # 2. Sibling / workspace venvs
    candidates = [
        Path(__file__).resolve().parents[5] / "oil-spill-detection-model" / ".venv" / "Scripts" / "python.exe",
        Path("c:/Users/DELL/OneDrive/Desktop/OTtest/oil-spill-detection-model/.venv/Scripts/python.exe"),
        Path("../oil-spill-detection-model/.venv/Scripts/python.exe"),
        Path("../../oil-spill-detection-model/.venv/Scripts/python.exe"),
        Path("../../../oil-spill-detection-model/.venv/Scripts/python.exe"),
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


def _get_unet_class():
    import torch
    import torch.nn as nn

    class DoubleConv(nn.Module):
        def __init__(self, in_channels: int, out_channels: int):
            super().__init__()
            self.block = nn.Sequential(
                nn.Conv2d(in_channels, out_channels, 3, padding=1),
                nn.BatchNorm2d(out_channels),
                nn.ReLU(inplace=True),
                nn.Conv2d(out_channels, out_channels, 3, padding=1),
                nn.BatchNorm2d(out_channels),
                nn.ReLU(inplace=True)
            )

        def forward(self, x: torch.Tensor) -> torch.Tensor:
            return self.block(x)

    class UNet(nn.Module):
        def __init__(self, in_channels: int = 2, out_channels: int = 1):
            super().__init__()
            self.enc1 = DoubleConv(in_channels, 64)
            self.enc2 = DoubleConv(64, 128)
            self.enc3 = DoubleConv(128, 256)
            self.enc4 = DoubleConv(256, 512)

            self.pool = nn.MaxPool2d(2)
            self.bottleneck = DoubleConv(512, 1024)

            self.up4 = nn.ConvTranspose2d(1024, 512, 2, stride=2)
            self.dec4 = DoubleConv(1024, 512)

            self.up3 = nn.ConvTranspose2d(512, 256, 2, stride=2)
            self.dec3 = DoubleConv(512, 256)

            self.up2 = nn.ConvTranspose2d(256, 128, 2, stride=2)
            self.dec2 = DoubleConv(256, 128)

            self.up1 = nn.ConvTranspose2d(128, 64, 2, stride=2)
            self.dec1 = DoubleConv(128, 64)

            self.final = nn.Conv2d(64, out_channels, 1)

        def forward(self, x: torch.Tensor) -> torch.Tensor:
            e1 = self.enc1(x)
            e2 = self.enc2(self.pool(e1))
            e3 = self.enc3(self.pool(e2))
            e4 = self.enc4(self.pool(e3))

            b = self.bottleneck(self.pool(e4))

            d4 = self.up4(b)
            d4 = torch.cat([d4, e4], dim=1)
            d4 = self.dec4(d4)

            d3 = self.up3(d4)
            d3 = torch.cat([d3, e3], dim=1)
            d3 = self.dec3(d3)

            d2 = self.up2(d3)
            d2 = torch.cat([d2, e2], dim=1)
            d2 = self.dec2(d2)

            d1 = self.up1(d2)
            d1 = torch.cat([d1, e1], dim=1)
            d1 = self.dec1(d1)

            return self.final(d1)

    return UNet


class PyTorchSegmentationAdapter(DetectionAdapter):
    name = "pytorch_unet"

    def __init__(self, cfg: dict[str, Any], model_path: Path):
        # Resolve model path
        candidates = [
            model_path,
            Path("data/models/best_oil_spill_unet.pth"),
            Path(__file__).resolve().parents[5] / "oil-spill-detection-model" / "best_oil_spill_unet.pth",
            Path("c:/Users/DELL/OneDrive/Desktop/OTtest/oil-spill-detection-model/best_oil_spill_unet.pth"),
            Path("../oil-spill-detection-model/best_oil_spill_unet.pth"),
            Path("../../oil-spill-detection-model/best_oil_spill_unet.pth"),
            Path("../../../oil-spill-detection-model/best_oil_spill_unet.pth"),
        ]
        found = next((p for p in candidates if p.exists()), None)
        if not found:
            raise PyTorchModelNotAvailable(f"PyTorch weights not found at {model_path}")
        self.model_path = found

        self.cfg = cfg
        self.model_version = f"{cfg.get('model_version', 'trained-unet-v1.0')}@{_sha256(self.model_path)[:10]}"
        self.tile_size = int(cfg.get("input_size", 512))
        self.tile_overlap = int(cfg.get("tile_overlap", 64))
        self.input_channels = int(cfg.get("input_channels", 2))
        self.normalize = str(cfg.get("normalize", "minmax"))

        # Check if torch is available in-process
        self.in_process = False
        try:
            import torch
            self.in_process = True
            dev_cfg = cfg.get("device", "auto")
            if dev_cfg == "cuda" and torch.cuda.is_available():
                self.device = torch.device("cuda")
            elif dev_cfg == "auto":
                self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
            else:
                self.device = torch.device("cpu")

            UNet = _get_unet_class()
            self.model = UNet(in_channels=self.input_channels, out_channels=1).to(self.device)

            try:
                checkpoint = torch.load(str(self.model_path), map_location=self.device, weights_only=True)
            except Exception:
                checkpoint = torch.load(str(self.model_path), map_location=self.device)

            if isinstance(checkpoint, dict) and "model_state_dict" in checkpoint:
                self.model.load_state_dict(checkpoint["model_state_dict"])
            elif isinstance(checkpoint, dict) and "state_dict" in checkpoint:
                self.model.load_state_dict(checkpoint["state_dict"])
            else:
                self.model.load_state_dict(checkpoint)

            self.model.eval()
            log.info("PyTorch U-Net model loaded in-process: %s on %s", self.model_version, self.device)
        except ImportError:
            # Look for worker python
            self.worker_python = _find_torch_python()
            if not self.worker_python:
                raise PyTorchModelNotAvailable("PyTorch is not installed in the environment or worker venvs")
            self.device = "worker_cpu"
            log.info("PyTorch U-Net model using worker python at %s", self.worker_python)

    def describe(self) -> dict[str, Any]:
        d = super().describe()
        d.update({
            "model_path": str(self.model_path),
            "device": str(self.device),
            "input_channels": self.input_channels,
            "normalize": self.normalize,
            "in_process": self.in_process,
        })
        return d

    def _prep(self, tile: np.ndarray, valid: np.ndarray) -> np.ndarray:
        x = tile.astype("float32")
        fin = valid & np.isfinite(x)
        fill = float(np.nanmedian(x[fin])) if fin.any() else 0.0
        x = np.where(fin, x, fill)

        if self.normalize == "minmax":
            lo, hi = (np.percentile(x[fin], [1, 99]) if fin.any() else (0.0, 1.0))
            x = np.clip((x - lo) / max(hi - lo, 1e-6), 0.0, 1.0)
        elif self.normalize == "zscore":
            mu, sd = (x[fin].mean(), x[fin].std() + 1e-6) if fin.any() else (0.0, 1.0)
            x = (x - mu) / sd
        elif self.normalize == "none":
            if fin.any() and np.max(x[fin]) > 1.0 and np.max(x[fin]) <= 255.0:
                x = x / 255.0

        # Replicate / format to expected input_channels (e.g. 2 channels for dual-pol SAR)
        if x.ndim == 2:
            arr = np.repeat(x[None, ...], self.input_channels, axis=0)  # (C, H, W)
        elif x.ndim == 3 and x.shape[0] < self.input_channels:
            arr = np.pad(x, ((0, self.input_channels - x.shape[0]), (0, 0), (0, 0)), mode="edge")
        else:
            arr = x[:self.input_channels]

        return np.ascontiguousarray(arr.astype("float32"))

    def predict_tile(self, tile: np.ndarray, valid_mask: np.ndarray) -> TileProbabilities:
        h, w = tile.shape[-2:]
        pad_h = max(0, self.tile_size - h)
        pad_w = max(0, self.tile_size - w)

        t = tile
        v = valid_mask
        if pad_h > 0 or pad_w > 0:
            if tile.ndim == 2:
                t = np.pad(tile, ((0, pad_h), (0, pad_w)), mode="reflect")
            else:
                t = np.pad(tile, ((0, 0), (0, pad_h), (0, pad_w)), mode="reflect")
            v = np.pad(valid_mask, ((0, pad_h), (0, pad_w)), mode="constant")

        inp = self._prep(t, v)  # (C, tile_size, tile_size)

        if self.in_process:
            import torch
            inp_tensor = torch.from_numpy(inp).unsqueeze(0).float().to(self.device)
            with torch.no_grad():
                raw_out = self.model(inp_tensor)
                probs_tensor = torch.sigmoid(raw_out)
                probs = probs_tensor.cpu().numpy()[0, 0]  # (tile_size, tile_size)
        else:
            probs = self._predict_via_worker(inp)

        oil = probs[:h, :w].astype("float32")
        oil[~valid_mask] = 0.0
        return TileProbabilities(oil=oil, look_alike=None, extra={"adapter": "pytorch_unet", "device": str(self.device)})

    def _predict_via_worker(self, inp: np.ndarray) -> np.ndarray:
        """Fallback worker prediction via worker Python interpreter."""
        with tempfile.TemporaryDirectory() as td:
            inp_path = Path(td) / "inp.npy"
            out_path = Path(td) / "out.npy"
            np.save(inp_path, inp)

            worker_script = f"""
import numpy as np
import torch
import torch.nn as nn

class DoubleConv(nn.Module):
    def __init__(self, in_channels, out_channels):
        super().__init__()
        self.block = nn.Sequential(
            nn.Conv2d(in_channels, out_channels, 3, padding=1),
            nn.BatchNorm2d(out_channels),
            nn.ReLU(inplace=True),
            nn.Conv2d(out_channels, out_channels, 3, padding=1),
            nn.BatchNorm2d(out_channels),
            nn.ReLU(inplace=True)
        )
    def forward(self, x):
        return self.block(x)

class UNet(nn.Module):
    def __init__(self, in_channels=2, out_channels=1):
        super().__init__()
        self.enc1 = DoubleConv(in_channels, 64)
        self.enc2 = DoubleConv(64, 128)
        self.enc3 = DoubleConv(128, 256)
        self.enc4 = DoubleConv(256, 512)
        self.pool = nn.MaxPool2d(2)
        self.bottleneck = DoubleConv(512, 1024)
        self.up4 = nn.ConvTranspose2d(1024, 512, 2, stride=2)
        self.dec4 = DoubleConv(1024, 512)
        self.up3 = nn.ConvTranspose2d(512, 256, 2, stride=2)
        self.dec3 = DoubleConv(512, 256)
        self.up2 = nn.ConvTranspose2d(256, 128, 2, stride=2)
        self.dec2 = DoubleConv(256, 128)
        self.up1 = nn.ConvTranspose2d(128, 64, 2, stride=2)
        self.dec1 = DoubleConv(128, 64)
        self.final = nn.Conv2d(64, out_channels, 1)

    def forward(self, x):
        e1 = self.enc1(x)
        e2 = self.enc2(self.pool(e1))
        e3 = self.enc3(self.pool(e2))
        e4 = self.enc4(self.pool(e3))
        b = self.bottleneck(self.pool(e4))
        d4 = self.dec4(torch.cat([self.up4(b), e4], dim=1))
        d3 = self.dec3(torch.cat([self.up3(d4), e3], dim=1))
        d2 = self.dec2(torch.cat([self.up2(d3), e2], dim=1))
        d1 = self.dec1(torch.cat([self.up1(d2), e1], dim=1))
        return self.final(d1)

dev = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
m = UNet(in_channels={self.input_channels}, out_channels=1).to(dev)
ckpt = torch.load(r'{self.model_path}', map_location=dev)
if isinstance(ckpt, dict) and 'model_state_dict' in ckpt:
    m.load_state_dict(ckpt['model_state_dict'])
else:
    m.load_state_dict(ckpt)
m.eval()

inp = np.load(r'{inp_path}')
t = torch.from_numpy(inp).unsqueeze(0).float().to(dev)
with torch.no_grad():
    out = torch.sigmoid(m(t)).cpu().numpy()[0, 0]
np.save(r'{out_path}', out)
"""
            script_path = Path(td) / "worker.py"
            script_path.write_text(worker_script, encoding="utf-8")
            subprocess.run([str(self.worker_python), str(script_path)], check=True, capture_output=True)
            return np.load(out_path)


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
