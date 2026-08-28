"""Place recognition: "where am I?" without GPS.

Two complementary signals, each optional:

- **Visual**: an ``Embedder`` maps a frame to a vector; the default
  ``HistogramEmbedder`` is a dependency-free HSV-histogram + spatial-grid
  baseline that separates visually distinct rooms. A CLIP embedder would
  be a strict upgrade (much better invariance to viewpoint/lighting) and
  can be added later as an optional extra (e.g. ``open-clip-torch``)
  implementing the same ``Embedder`` protocol — deliberately NOT a hard
  dependency at v0.
- **WiFi**: the set of visible BSSIDs + signal strengths is a strong
  indoor location fingerprint. ``WifiSignature.scan()`` uses ``nmcli``
  and degrades to an empty dict when unavailable.

``PlaceRecognizer`` fuses both: enroll a few samples per place, then
``recognize`` returns ranked ``(name, score)`` candidates.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

import cv2
import numpy as np

WifiSig = dict[str, float]  # BSSID -> signal strength (0..100, nmcli SIGNAL)


@runtime_checkable
class Embedder(Protocol):
    """Maps a BGR frame to a fixed-size embedding vector."""

    embedder_id: str

    def embed(self, frame: np.ndarray) -> np.ndarray: ...


class HistogramEmbedder:
    """HSV color histogram + coarse spatial grid, L2-normalized.

    Global hue/saturation histogram concatenated with per-cell
    hue histograms over a ``grid x grid`` layout, so both overall color
    mood and rough spatial arrangement contribute. Cheap, deterministic,
    dependency-free — a baseline that distinguishes visually distinct
    rooms, not a general scene embedding.
    """

    def __init__(self, *, h_bins: int = 24, s_bins: int = 8, grid: int = 2) -> None:
        for name, value in (("h_bins", h_bins), ("s_bins", s_bins), ("grid", grid)):
            if value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        self.h_bins = h_bins
        self.s_bins = s_bins
        self.grid = grid

    @property
    def embedder_id(self) -> str:
        """Stable identifier for persisted embedding compatibility."""
        return f"histogram-h{self.h_bins}-s{self.s_bins}-grid{self.grid}-v1"

    def embed(self, frame: np.ndarray) -> np.ndarray:
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        parts: list[np.ndarray] = []

        # Global H+S 2D histogram.
        global_hist = cv2.calcHist(
            [hsv], [0, 1], None, [self.h_bins, self.s_bins], [0, 180, 0, 256]
        )
        parts.append(global_hist.flatten())

        # Per-cell H+S histograms (coarse spatial layout).
        h, w = hsv.shape[:2]
        for gy in range(self.grid):
            for gx in range(self.grid):
                cell = hsv[
                    gy * h // self.grid : (gy + 1) * h // self.grid,
                    gx * w // self.grid : (gx + 1) * w // self.grid,
                ]
                # H+S (not hue alone): hue is meaningless at zero saturation,
                # so a hue-only cell histogram cannot tell e.g. black from red.
                if cell.size == 0:
                    parts.append(np.zeros(self.h_bins * self.s_bins))
                    continue
                cell_hist = cv2.calcHist(
                    [cell], [0, 1], None, [self.h_bins, self.s_bins], [0, 180, 0, 256]
                )
                parts.append(cell_hist.flatten())

        vec = np.concatenate(parts).astype(np.float64)
        norm = np.linalg.norm(vec)
        if norm > 0:
            vec /= norm
        return vec


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity in [-1, 1] (0.0 if either vector is zero)."""
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


class WifiSignature:
    """WiFi BSSID/RSSI fingerprints via nmcli."""

    @staticmethod
    def scan(interface: str | None = None) -> WifiSig:
        """Scan visible access points -> {bssid: signal 0..100}.

        Returns an empty dict on any failure (no nmcli, no wifi device,
        permission denied) — WiFi is an optional signal.
        """
        cmd = ["nmcli", "-t", "-f", "BSSID,SIGNAL", "dev", "wifi", "list"]
        if interface:
            cmd += ["ifname", interface]
        try:
            out = subprocess.run(
                cmd, capture_output=True, text=True, timeout=15, check=True
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return {}
        return WifiSignature.parse_nmcli(out)

    @staticmethod
    def parse_nmcli(output: str) -> WifiSig:
        """Parse `nmcli -t -f BSSID,SIGNAL` output.

        BSSIDs contain colons which nmcli escapes as ``\\:`` in terse
        mode; the last unescaped-colon field is the signal.
        """
        sig: WifiSig = {}
        for line in output.splitlines():
            line = line.strip()
            if not line:
                continue
            # Split on the last unescaped colon.
            idx = None
            for i in range(len(line) - 1, -1, -1):
                if line[i] == ":" and (i == 0 or line[i - 1] != "\\"):
                    idx = i
                    break
            if idx is None:
                continue
            bssid = line[:idx].replace("\\:", ":").upper()
            try:
                signal = float(line[idx + 1 :])
            except ValueError:
                continue
            if bssid:
                sig[bssid] = signal
        return sig

    @staticmethod
    def similarity(sig_a: WifiSig, sig_b: WifiSig) -> float:
        """Overlap-weighted signal-strength similarity in [0, 1].

        For BSSIDs seen in both scans, compare signal strengths; weight by
        the fraction of overlap (Jaccard), so seeing mostly the same APs at
        similar strengths scores high, and disjoint AP sets score 0.
        """
        if not sig_a or not sig_b:
            return 0.0
        keys_a, keys_b = set(sig_a), set(sig_b)
        common = keys_a & keys_b
        if not common:
            return 0.0
        jaccard = len(common) / len(keys_a | keys_b)
        # Mean signal agreement over common APs (signals are 0..100).
        diffs = [abs(sig_a[k] - sig_b[k]) for k in common]
        agreement = 1.0 - min(1.0, float(np.mean(diffs)) / 100.0)
        return jaccard * agreement


@dataclass
class PlaceSample:
    """One enrolled observation of a place."""

    embedding: np.ndarray | None = None
    wifi: WifiSig = field(default_factory=dict)


class PlaceRecognizer:
    """Enroll named places, then rank candidates for a new observation.

    Scores combine visual cosine similarity and WiFi fingerprint
    similarity. ``wifi_weight`` (default 0.5) applies when both signals
    are present for a comparison; otherwise whichever signal exists is
    used alone.
    """

    def __init__(
        self,
        embedder: Embedder | None = None,
        *,
        wifi_weight: float = 0.5,
    ) -> None:
        if not 0.0 <= wifi_weight <= 1.0:
            raise ValueError("wifi_weight must be in [0, 1]")
        self.embedder = embedder if embedder is not None else HistogramEmbedder()
        self.wifi_weight = wifi_weight
        self.places: dict[str, list[PlaceSample]] = {}

    def enroll(
        self,
        name: str,
        frame: np.ndarray | None = None,
        wifi_sig: WifiSig | None = None,
    ) -> PlaceSample:
        """Add one sample for a place. Multiple samples per place welcome."""
        if frame is None and not wifi_sig:
            raise ValueError("enroll needs a frame, a wifi signature, or both")
        sample = PlaceSample(
            embedding=self.embedder.embed(frame) if frame is not None else None,
            wifi=dict(wifi_sig) if wifi_sig else {},
        )
        self.places.setdefault(name, []).append(sample)
        return sample

    def _sample_score(
        self,
        sample: PlaceSample,
        embedding: np.ndarray | None,
        wifi_sig: WifiSig | None,
    ) -> float | None:
        vis: float | None = None
        wifi: float | None = None
        if embedding is not None and sample.embedding is not None:
            # Histogram embeddings are non-negative, so cosine is in [0, 1].
            vis = max(0.0, cosine_similarity(embedding, sample.embedding))
        if wifi_sig and sample.wifi:
            wifi = WifiSignature.similarity(wifi_sig, sample.wifi)
        if vis is not None and wifi is not None:
            return (1.0 - self.wifi_weight) * vis + self.wifi_weight * wifi
        if vis is not None:
            return vis
        if wifi is not None:
            return wifi
        return None

    def recognize(
        self,
        frame: np.ndarray | None = None,
        wifi_sig: WifiSig | None = None,
    ) -> list[tuple[str, float]]:
        """Rank enrolled places for an observation, best first.

        Each place's score is the max over its enrolled samples. Places
        with no comparable signal are omitted.
        """
        if frame is None and not wifi_sig:
            raise ValueError("recognize needs a frame, a wifi signature, or both")
        embedding = self.embedder.embed(frame) if frame is not None else None

        results: list[tuple[str, float]] = []
        for name, samples in self.places.items():
            scores = [
                s
                for s in (
                    self._sample_score(sample, embedding, wifi_sig)
                    for sample in samples
                )
                if s is not None
            ]
            if scores:
                results.append((name, max(scores)))
        results.sort(key=lambda item: item[1], reverse=True)
        return results

    # -- persistence ------------------------------------------------------

    def save(self, path: str | Path) -> None:
        """Persist the gallery as JSON (embeddings as float lists)."""
        payload = {
            "version": 1,
            "embedder": self.embedder.embedder_id,
            "wifi_weight": self.wifi_weight,
            "places": {
                name: [
                    {
                        "embedding": (
                            sample.embedding.tolist()
                            if sample.embedding is not None
                            else None
                        ),
                        "wifi": sample.wifi,
                    }
                    for sample in samples
                ]
                for name, samples in self.places.items()
            },
        }
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload))

    def load(self, path: str | Path) -> None:
        """Load a gallery saved by :meth:`save` (replaces current places)."""
        payload = json.loads(Path(path).read_text())
        if not isinstance(payload, dict):
            raise ValueError("gallery file must contain a JSON object")
        saved_embedder = payload.get("embedder")
        if saved_embedder is not None and saved_embedder != self.embedder.embedder_id:
            raise ValueError(
                "embedder mismatch: gallery uses "
                f"{saved_embedder!r}, current embedder is {self.embedder.embedder_id!r}"
            )
        saved_wifi_weight = float(payload.get("wifi_weight", self.wifi_weight))
        if not 0.0 <= saved_wifi_weight <= 1.0:
            raise ValueError("wifi_weight must be in [0, 1]")
        saved_places = payload.get("places", {})
        if not isinstance(saved_places, dict):
            raise ValueError("gallery places must be a JSON object")
        places: dict[str, list[PlaceSample]] = {}
        for name, samples in saved_places.items():
            if not isinstance(samples, list) or not all(
                isinstance(sample, dict) for sample in samples
            ):
                raise ValueError(
                    f"gallery place {name!r} samples must be a list of JSON objects"
                )
            loaded_samples: list[PlaceSample] = []
            for s in samples:
                wifi_raw = s.get("wifi") or {}
                if not isinstance(wifi_raw, dict):
                    raise ValueError(
                        f"gallery place {name!r} sample wifi must be a JSON object"
                    )
                loaded_samples.append(
                    PlaceSample(
                        embedding=(
                            np.asarray(s["embedding"], dtype=np.float64)
                            if s.get("embedding") is not None
                            else None
                        ),
                        wifi={k: float(v) for k, v in wifi_raw.items()},
                    )
                )
            places[name] = loaded_samples
        self.wifi_weight = saved_wifi_weight
        self.places = places

    @classmethod
    def from_file(
        cls,
        path: str | Path,
        embedder: Embedder | None = None,
        *,
        wifi_weight: float | None = None,
    ) -> PlaceRecognizer:
        payload = json.loads(Path(path).read_text())
        if not isinstance(payload, dict):
            raise ValueError("gallery file must contain a JSON object")
        recognizer = cls(
            embedder,
            wifi_weight=(
                wifi_weight
                if wifi_weight is not None
                else float(payload.get("wifi_weight", 0.5))
            ),
        )
        recognizer.load(path)
        if wifi_weight is not None:
            recognizer.wifi_weight = wifi_weight
        return recognizer
