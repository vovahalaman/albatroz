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
    llm_max_output_tokens: int = 8192
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
    http_timeout_s: float = 15
    overpass_timeout_s: int = 25
    overpass_max_results: int = 50
    retries: int = 3
    nominatim_url: str = "https://nominatim.openstreetmap.org"
    overpass_url: str = "https://overpass-api.de/api/interpreter"
    wiki_url: str = "https://{lang}.wikipedia.org/w/api.php"


class MagnifierSettings(BaseModel):
    view_fov: float = 90
    view_size: int = 1024
    ground_pitch: float = -50
    up_pitch: float = 50
    overview_wh: tuple[int, int] = (2048, 1024)
    zoom_fov: tuple[float, float] = (8, 90)  # min, max
    zoom_size: tuple[int, int] = (64, 2048)  # min, max px
    jpeg_quality: int = 90


class BaselineSettings(BaseModel):
    top_places: int = 3  # SPEC §10: geocode the top 3 places
    geocode_limit: int = 1  # Nominatim results kept per place
    radius_m: PerLabel = Field(default_factory=lambda: PerLabel(urban=300, non_urban=8000))


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
    magnifier: MagnifierSettings = Field(default_factory=MagnifierSettings)
    baseline: BaselineSettings = Field(default_factory=BaselineSettings)
    # SPEC §8 says response_json_schema; with images Gemini then wrote runaway floats (see CLAUDE.md).
    structured_output: Literal["json_mode", "response_schema", "response_json_schema"] = "json_mode"


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
