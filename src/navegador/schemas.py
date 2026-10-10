"""Data contracts (SPEC §5) and agent output schemas (SPEC §10). Pydantic v2."""

from __future__ import annotations

import math
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, Field, ValidationInfo, field_validator

Label = Literal["urban", "non_urban"]
Mode = Literal["baseline", "autonomous", "assisted"]
Country = Literal["pt", "es", "fr"]
Confidence = Annotated[float, Field(ge=0, le=1)]

ESTIMATED = "estimated"  # the only provenance allowed without a registered tool call (R2)


# ---------------------------------------------------------------- core (§5)


class Hypothesis(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    radius_m: float = Field(gt=0)
    weight: float = Field(ge=0)
    label: str
    provenance: str = Field(min_length=1)
    evidence: list[str] = []
    why: str = ""
    source: str  # agent name

    @field_validator("provenance")
    @classmethod
    def _provenance_registered(cls, v: str, info: ValidationInfo) -> str:
        """R2: if validated with context={"registry": set_of_call_ids}, provenance must be in it."""
        registry = (info.context or {}).get("registry")
        if registry is not None and v != ESTIMATED and v not in registry:
            raise ValueError(f"provenance {v!r} is not a tool call_id of this run (R2)")
        return v


class MCQOption(BaseModel):
    id: str
    text: str
    cluster_id: str | None = None


class MCQ(BaseModel):
    mcq_id: str
    question: str
    options: Annotated[list[MCQOption], Field(min_length=2, max_length=4)]
    answer: str | None = None
    asked_at: datetime
    answered_at: datetime | None = None


class Cue(BaseModel):
    text: str = Field(max_length=1000)
    ts: datetime
    guard_flag: bool
    override: bool


class CueResult(BaseModel):
    accepted: bool
    guard_flag: bool
    why: str = ""
    cue: Cue | None = None


class Penalty(BaseModel):
    kind: Literal["cue", "mcq", "text_location"]
    factor: float = Field(gt=0, le=1)
    ts: datetime
    ref: str


class Candidate(BaseModel):
    rank: int = Field(ge=1, le=10)
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)


class Submission(BaseModel):
    pano_id: str
    filename: str
    label: Label
    mode: str
    candidates: Annotated[list[Candidate], Field(min_length=10, max_length=10)]
    penalty_factor: float
    expected_score: float
    special_one: bool


class ExcludedZone(BaseModel):
    lat: float
    lng: float
    radius_m: float = Field(gt=0)
    label: str = ""
    ref: str = ""  # mcq_id that excluded it


# ---------------------------------------------------------------- tools (§9)


class View(BaseModel):
    name: str
    yaw: float
    pitch: float
    fov: float
    path: Path


class SunObs(BaseModel):
    yaw: float  # image yaw (0 = centre column, + right), not compass
    elevation: float
    area_px: int  # in the downscaled detection image
    confidence: Confidence
    uncertainty_deg: float = Field(default=0, ge=0)  # blob angular radius + tilt margin
    pixel_xy: tuple[float, float] | None = None  # full-resolution pano pixels
    source: Literal["detect", "vlm"] = "detect"  # vlm = Gemini fallback (low confidence)


class SunLocateResult(BaseModel):
    """Astrolabe VLM fallback (prompts/sun_locate.md); x, y in 0–1000 of the sent sky image."""

    sun_visible: bool
    x: int | None = Field(default=None, ge=0, le=1000)
    y: int | None = Field(default=None, ge=0, le=1000)
    kind: Literal["disc", "behind_cloud", "behind_object"] | None = None


class SunVerifyResult(BaseModel):
    """Astrolabe VLM verifier (prompts/sun_verify.md)."""

    sun_at_point: bool


# ---------------------------------------------------------------- agents (§10)


class CountryProbs(BaseModel):
    pt: Confidence = 0
    es: Confidence = 0
    fr: Confidence = 0


class BaselinePlace(BaseModel):
    query: str
    country: Country
    confidence: Confidence
    why: str = ""


class BaselineResult(BaseModel):
    country_probs: CountryProbs
    places: list[BaselinePlace] = []
    clues: list[str] = []


class ReaderText(BaseModel):
    text: str
    view: str
    kind: Literal["sign", "shop", "plate_format", "phone", "domain", "other"]
    language: Literal["pt", "es", "fr", "gl", "ca", "eu", "other"]
    confidence: Confidence


class LanguageVerdict(BaseModel):
    pt: Confidence = 0
    es: Confidence = 0
    fr: Confidence = 0
    other: Confidence = 0


def _clamp(v: object, lo: float, hi: float) -> object:
    """Clamp finite numbers; leave anything else for normal validation to reject."""
    if isinstance(v, (int, float)) and math.isfinite(v):
        return min(max(float(v), lo), hi)
    return v


class ZoomRequest(BaseModel):
    """Zoom target in image angles. A slightly-off number from the model is corrected (yaw wrapped,
    pitch and FOV clamped) instead of failing the whole Reader answer. The bounds stay in the JSON
    schema so the model still sees them. NaN/inf is rejected."""

    yaw: float = Field(ge=-180, le=180, allow_inf_nan=False)
    pitch: float = Field(ge=-90, le=90, allow_inf_nan=False)
    fov: float = Field(ge=8, le=90, allow_inf_nan=False)
    why: str = ""

    @field_validator("yaw", mode="before")
    @classmethod
    def _wrap_yaw(cls, v: object) -> object:
        if isinstance(v, (int, float)) and math.isfinite(v):
            y = (float(v) + 180.0) % 360.0 - 180.0
            return 180.0 if y == -180.0 else y
        return v

    @field_validator("pitch", mode="before")
    @classmethod
    def _clamp_pitch(cls, v: object) -> object:
        return _clamp(v, -90, 90)

    @field_validator("fov", mode="before")
    @classmethod
    def _clamp_fov(cls, v: object) -> object:
        return _clamp(v, 8, 90)


class ReaderResult(BaseModel):
    texts: list[ReaderText] = []
    language_verdict: LanguageVerdict
    search_terms: list[str] = []
    zoom_requests: Annotated[list[ZoomRequest], Field(max_length=6)] = []


class GeographerObservation(BaseModel):
    cue: str
    value: str
    view: str
    status: Literal["observed", "not_observed", "uncertain"]
    confidence: Confidence


class RegionGuess(BaseModel):
    region: str
    country: Country
    prob: Confidence
    why: str = ""


class WaterOrCoast(BaseModel):
    present: bool = False
    yaw: float | None = None
    type: str | None = None


class GeographerResult(BaseModel):
    observations: list[GeographerObservation] = []
    country_probs: CountryProbs
    region_guesses: list[RegionGuess] = []
    water_or_coast: WaterOrCoast = WaterOrCoast()
    terrain: str = ""
    season: str = ""


class MCQPhrasing(BaseModel):
    question: str
    options: Annotated[list[MCQOption], Field(min_length=2, max_length=4)]  # ids "A"–"D"


class CueGuardResult(BaseModel):
    references_location: bool
    why: str


class Objection(BaseModel):
    hypothesis_label: str
    issue: str
    severity: Literal["low", "medium", "high"]


class AdvocateResult(BaseModel):
    objections: list[Objection] = []
    weight_multipliers: dict[str, float] = {}
