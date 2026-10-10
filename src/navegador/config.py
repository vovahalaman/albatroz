"""Configuration: paths, `.env`, `config/settings.yaml` and `config/regions.yaml`.

All tunables live in YAML. Python never hardcodes model IDs, thresholds or prompt text.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"
PROMPTS_DIR = ROOT / "prompts"
KNOWLEDGE_DIR = ROOT / "knowledge"
PANOS_DIR = ROOT / "panos"

load_dotenv(ROOT / ".env")

Resolution = Literal["LOW", "MEDIUM", "HIGH"]


class PerLabel(BaseModel):
    urban: float
    non_urban: float

    def get(self, label: str) -> float:
        return getattr(self, label)


class Budgets(BaseModel):
    detective_max_tool_calls: int = 12
    llm_timeout_s: float = 120
    pano_soft_budget_s: float = 420


class MCQSettings(BaseModel):
    max_per_pano: int = 2
    q_trust: float = 0.85
    top_cluster_max: float = 0.6
    top3_min: float = 0.7
    cluster_eps_m: PerLabel
    far_m: PerLabel


class StrategistSettings(BaseModel):
    mode: Literal["ev", "hedge"] = "ev"
    mc_samples: int = 4000
    grid_step_m: PerLabel
    estimated_weight_factor: float = 0.3


class GeoSettings(BaseModel):
    nominatim_min_interval_s: float = 1.1
    max_geocode_calls_per_pano: int = 20
    user_agent: str


class Flags(BaseModel):
    use_exif_gps: bool = False  # [CONFIRM] may EXIF GPS be used?
    timestamp_is_local: bool = True  # [CONFIRM] local time or UTC?
    commit_then_ask: bool = False
    advocate_enabled: bool = False


class Scoring(BaseModel):
    rank_decay: float = 0.95
    special_one_multiplier: float = 3.0
    zero_distance_m: float = 10.0
    theta_scale_m: PerLabel
    penalty_factors: dict[Literal["cue", "mcq", "text_location"], float]


class AstrolabeSettings(BaseModel):
    detect_size: tuple[int, int] = (1520, 760)
    brightness_min: int = 250
    saturation_max: float = 0.15
    min_pixels: int = 3
    max_solid_angle_deg2: float = 800
    max_extent_deg: float = 40
    min_circularity: float = 0.4
    ring_inner: float = 1.5
    ring_outer: float = 3.0
    min_contrast: float = 0.2
    top_edge_max_span_deg: float = 10
    ambiguity_ratio: float = 0.8
    min_confidence: float = 0.3
    tilt_margin_deg: float = 3
    core_min: int = 252
    min_core_radius_deg: float = 4
    min_core_fraction: float = 0.3
    min_elev_deg: float = 3
    ceiling_lat_deg: float = 27.5
    time_hypotheses: list[str] = Field(default_factory=lambda: ["local", "UTC", "Europe/Lisbon"])
    consistency_falloff_deg: float = 3


class Settings(BaseModel):
    gcp: dict[str, str] = Field(default_factory=dict)
    models: dict[str, str]
    media_resolution: dict[str, Resolution]
    thinking_level: dict[str, str | None] = Field(default_factory=dict)
    budgets: Budgets
    mcq: MCQSettings
    strategist: StrategistSettings
    geo: GeoSettings
    flags: Flags
    scoring: Scoring
    astrolabe: AstrolabeSettings = Field(default_factory=AstrolabeSettings)


class Regions(BaseModel):
    regions: dict[str, tuple[float, float, float, float]]  # lat_min, lat_max, lng_min, lng_max
    fallback_point: tuple[float, float]

    def contains(self, lat: float, lng: float) -> bool:
        return any(a <= lat <= b and c <= lng <= d for a, b, c, d in self.regions.values())

    def clamp(self, lat: float, lng: float) -> tuple[float, float]:
        """Return the point unchanged if inside a region, else the nearest point on the nearest box."""
        if self.contains(lat, lng):
            return lat, lng
        best: tuple[float, tuple[float, float]] | None = None
        for a, b, c, d in self.regions.values():
            p = (min(max(lat, a), b), min(max(lng, c), d))
            dist = (p[0] - lat) ** 2 + (p[1] - lng) ** 2
            if best is None or dist < best[0]:
                best = (dist, p)
        assert best is not None
        return best[1]


class Env(BaseModel):
    project: str | None
    location: str
    gemini_api_key: str | None
    team_email: str | None


def _read_yaml(path: Path) -> dict:
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


@lru_cache
def settings() -> Settings:
    s = Settings.model_validate(_read_yaml(CONFIG_DIR / "settings.yaml"))
    email = env().team_email
    if email and "<team email>" in s.geo.user_agent:
        s.geo.user_agent = s.geo.user_agent.replace("<team email>", email)
    return s


@lru_cache
def regions() -> Regions:
    return Regions.model_validate(_read_yaml(CONFIG_DIR / "regions.yaml"))


@lru_cache
def env() -> Env:
    return Env(
        project=os.getenv("GOOGLE_CLOUD_PROJECT") or None,
        location=os.getenv("GOOGLE_CLOUD_LOCATION") or settings_location(),
        gemini_api_key=os.getenv("GEMINI_API_KEY") or None,
        team_email=os.getenv("TEAM_EMAIL") or None,
    )


def settings_location() -> str:
    return str(_read_yaml(CONFIG_DIR / "settings.yaml").get("gcp", {}).get("location", "global"))


def runs_dir() -> Path:
    """Run directory root; override with NAVEGADOR_RUNS_DIR (used by tests)."""
    return Path(os.getenv("NAVEGADOR_RUNS_DIR") or ROOT / "runs")


def cache_dir() -> Path:
    return Path(os.getenv("NAVEGADOR_CACHE_DIR") or ROOT / "cache")
