"""Configuration loading: YAML for parameters, .env for secrets."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = ROOT / "config" / "default.yaml"


@dataclass(frozen=True)
class BBox:
    lat_min: float
    lat_max: float
    lon_min: float
    lon_max: float

    def __post_init__(self) -> None:
        if not (-90 <= self.lat_min < self.lat_max <= 90):
            raise ValueError(f"invalid latitude range: {self.lat_min}..{self.lat_max}")
        if not (-180 <= self.lon_min < self.lon_max <= 180):
            raise ValueError(f"invalid longitude range: {self.lon_min}..{self.lon_max}")

    def as_aisstream(self) -> list[list[float]]:
        """aisstream format: two corners, each [lat, lon] (latitude first)."""
        return [[self.lat_min, self.lon_min], [self.lat_max, self.lon_max]]

    def contains(self, lat: float, lon: float) -> bool:
        return self.lat_min <= lat <= self.lat_max and self.lon_min <= lon <= self.lon_max


@dataclass(frozen=True)
class CollectorConfig:
    region: str
    bbox: BBox
    url: str
    message_types: tuple[str, ...]
    out_dir: Path
    log_dir: Path
    flush_interval_s: float
    flush_max_rows: int
    stats_interval_s: float
    idle_timeout_s: float
    subscribe_timeout_s: float
    backoff_initial_s: float
    backoff_max_s: float
    lock_port: int


def load_config(path: Path | None = None) -> dict[str, Any]:
    with open(path or DEFAULT_CONFIG, encoding="utf-8") as f:
        return yaml.safe_load(f)


def resolve(p: str | Path, root: Path = ROOT) -> Path:
    p = Path(p)
    return p if p.is_absolute() else (root / p).resolve()


def collector_config(cfg: dict[str, Any], root: Path = ROOT) -> CollectorConfig:
    c = cfg["collector"]
    region = cfg["regions"][c["region"]]
    private = resolve(cfg["paths"]["private"], root)
    return CollectorConfig(
        region=c["region"],
        bbox=BBox(**region["bbox"]),
        url=c["url"],
        message_types=tuple(c["message_types"]),
        out_dir=private / c["out_subdir"],
        log_dir=private / c["log_subdir"],
        flush_interval_s=float(c["flush_interval_s"]),
        flush_max_rows=int(c["flush_max_rows"]),
        stats_interval_s=float(c["stats_interval_s"]),
        idle_timeout_s=float(c["idle_timeout_s"]),
        subscribe_timeout_s=float(c["subscribe_timeout_s"]),
        backoff_initial_s=float(c["backoff_initial_s"]),
        backoff_max_s=float(c["backoff_max_s"]),
        lock_port=int(c["lock_port"]),
    )


def get_secret(name: str, root: Path = ROOT) -> str:
    """Read a secret from the environment, loading <root>/.env first. Never logged."""
    load_dotenv(root / ".env", override=False)
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is not set. Copy .env.example to .env and fill it in.")
    return value
