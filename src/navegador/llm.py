"""LLM layer (SPEC §8). The only module that talks to the Gemini SDK (R5).

`call()` loads a prompt file (+ knowledge), sends text and images, optionally validates structured
output or runs an automatic function-calling loop, retries 429/500/503 and logs one `llm_call` event.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from google import genai
from google.auth.exceptions import GoogleAuthError
from google.genai import errors, types
from pydantic import BaseModel, ValidationError
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_exponential

from navegador.config import KNOWLEDGE_DIR, PROMPTS_DIR, env, settings
from navegador.core import scribe

ImageRef = tuple[str | Path, str]  # (path, "LOW"|"MEDIUM"|"HIGH" or a settings.media_resolution key)
RETRY_CODES = {429, 500, 503}
FALLBACK_ERRORS = (errors.APIError, GoogleAuthError, OSError)  # auth/permission/network → next client
_RESOLUTIONS = {"LOW", "MEDIUM", "HIGH"}


@dataclass
class LLMResult:
    text: str
    parsed: BaseModel | None
    tool_calls: list[dict[str, Any]]
    usage: dict[str, Any]
    latency_s: float
    model: str
    raw: Any = field(default=None, repr=False)


class LLMConfigError(RuntimeError):
    pass


# ---------------------------------------------------------------- clients


# Not cached: a client's async session is bound to the event loop it first ran on, and every
# asyncio.run() (one per pipeline stage / call_sync) creates a new loop. A cached client made the
# second call in a process fail with "Event loop is closed" (batch, Streamlit). Construction is cheap.
def vertex_client() -> genai.Client:
    e = env()
    if not e.project:
        raise LLMConfigError("GOOGLE_CLOUD_PROJECT is not set (.env)")
    return genai.Client(vertexai=True, project=e.project, location=e.location)


def api_key_client() -> genai.Client | None:
    key = env().gemini_api_key
    return genai.Client(api_key=key) if key else None


def clients() -> list[tuple[str, genai.Client]]:
    """Vertex first, AI Studio key as fallback (SPEC §8)."""
    out: list[tuple[str, genai.Client]] = []
    try:
        out.append(("vertex", vertex_client()))
    except Exception:  # noqa: BLE001 - any construction failure means "try the fallback"
        pass
    if (fallback := api_key_client()) is not None:
        out.append(("api_key", fallback))
    if not out:
        raise LLMConfigError("no LLM client: set GOOGLE_CLOUD_PROJECT (+ ADC login) or GEMINI_API_KEY in .env")
    return out


# ---------------------------------------------------------------- prompts


@dataclass(frozen=True)
class Prompt:
    path: Path
    system: str
    knowledge: tuple[str, ...]
    version: str  # git short hash of the prompt file


def load_prompt(prompt_file: str) -> Prompt:
    """Read `prompts/<file>` with optional YAML front-matter `knowledge: [...]`, append knowledge files."""
    path = PROMPTS_DIR / prompt_file
    text = path.read_text(encoding="utf-8")
    meta: dict[str, Any] = {}
    if text.startswith("---"):
        _, front, body = text.split("---", 2)
        meta = yaml.safe_load(front) or {}
        text = body.lstrip("\n")
    knowledge = tuple(meta.get("knowledge") or ())
    sections = [text.strip()]
    for name in knowledge:
        sections.append(f"# Knowledge file: {name}\n\n{(KNOWLEDGE_DIR / name).read_text(encoding='utf-8').strip()}")
    return Prompt(path, "\n\n---\n\n".join(sections), knowledge, scribe.git_file_hash(path))


# ---------------------------------------------------------------- request building


def resolve_resolution(level: str) -> str:
    level = settings().media_resolution.get(level, level)
    if level.upper() not in _RESOLUTIONS:
        raise ValueError(f"unknown media resolution {level!r}")
    return level.upper()


def image_part(path: str | Path, level: str) -> tuple[types.Part, dict[str, Any]]:
    data = Path(path).read_bytes()
    res = resolve_resolution(level)
    part = types.Part.from_bytes(
        data=data, mime_type="image/jpeg",
        media_resolution=types.PartMediaResolution(level=types.PartMediaResolutionLevel(f"MEDIA_RESOLUTION_{res}")),
    )
    return part, {"path": str(path), "sha256": scribe.sha256_file(Path(path)), "media_resolution": res}


def schema_instruction(schema: type[BaseModel]) -> str:
    """json_mode: the output contract as text (a generic llm-layer instruction, not agent prompt text)."""
    schema_json = json.dumps(schema.model_json_schema(), ensure_ascii=False)
    return f"Return only JSON that validates against this JSON Schema:\n{schema_json}"


def model_for(role: str) -> str:
    model = settings().models.get(role)
    if not model or model.startswith("<"):
        raise LLMConfigError(f"settings.yaml models.{role} is not set (got {model!r}); run `cli.py check-llm`")
    return model


def build_config(prompt: Prompt | None, role: str, schema: type[BaseModel] | None,
                 tools: Sequence[Callable] | None, max_tool_calls: int | None) -> types.GenerateContentConfig:
    kwargs: dict[str, Any] = {
        "http_options": types.HttpOptions(timeout=int(settings().budgets.llm_timeout_s * 1000)),
        "max_output_tokens": settings().budgets.llm_max_output_tokens,  # stops runaway (degenerate) JSON early
    }
    if prompt is not None:
        kwargs["system_instruction"] = prompt.system
    if schema is not None and tools:
        raise ValueError("do not combine tools with a response schema (SPEC §8)")
    if schema is not None:
        kwargs["response_mime_type"] = "application/json"
        mode = settings().structured_output
        if mode == "response_json_schema":  # SPEC §8 as written
            kwargs["response_json_schema"] = schema.model_json_schema()
        elif mode == "response_schema":
            kwargs["response_schema"] = schema
        # json_mode (default): JSON output without constrained decoding; the schema goes in as text
        # (schema_instruction) and Pydantic validates. Constrained decoding let Gemini write endless
        # digits in number fields ("0.92345678901234567890…") until max_output_tokens (CLAUDE.md).
    if tools:
        kwargs["tools"] = list(tools)
        kwargs["automatic_function_calling"] = types.AutomaticFunctionCallingConfig(
            maximum_remote_calls=max_tool_calls or settings().budgets.detective_max_tool_calls)
    levels = settings().thinking_level
    level = levels.get(role, levels.get("default"))
    if level:
        kwargs["thinking_config"] = types.ThinkingConfig(thinking_level=types.ThinkingLevel(level.upper()))
    return types.GenerateContentConfig(**kwargs)


# ---------------------------------------------------------------- response parsing


def extract_tool_calls(resp: types.GenerateContentResponse) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for content in resp.automatic_function_calling_history or []:
        for part in content.parts or []:
            if part.function_call:
                calls.append({"name": part.function_call.name, "args": dict(part.function_call.args or {})})
            elif part.function_response and calls:
                calls[-1]["result_summary"] = str(part.function_response.response)[:500]
    return calls


def usage_dict(resp: types.GenerateContentResponse) -> dict[str, Any]:
    um = resp.usage_metadata
    return um.model_dump(mode="json", exclude_none=True) if um else {}


def _response_text(resp: types.GenerateContentResponse) -> str:
    try:
        return resp.text or ""
    except ValueError:
        return ""


# ---------------------------------------------------------------- call


def _retryable(exc: BaseException) -> bool:
    return isinstance(exc, errors.APIError) and exc.code in RETRY_CODES


async def _generate(client: genai.Client, model: str, contents: list[Any],
                    config: types.GenerateContentConfig, sync: bool) -> types.GenerateContentResponse:
    async for attempt in AsyncRetrying(retry=retry_if_exception(_retryable), stop=stop_after_attempt(4),
                                       wait=wait_exponential(multiplier=2, max=30), reraise=True):
        with attempt:
            if sync:  # tool loop: the verified sync AFC path, off the event loop
                coro = asyncio.to_thread(client.models.generate_content, model=model, contents=contents, config=config)
            else:
                coro = client.aio.models.generate_content(model=model, contents=contents, config=config)
            return await asyncio.wait_for(coro, timeout=settings().budgets.llm_timeout_s)
    raise AssertionError("unreachable")


async def _generate_with_fallback(model: str, contents: list[Any], config: types.GenerateContentConfig,
                                  sync: bool) -> tuple[types.GenerateContentResponse, str]:
    failures: list[tuple[str, Exception]] = []
    for name, client in clients():
        try:
            return await _generate(client, model, contents, config, sync), name
        except FALLBACK_ERRORS as exc:
            failures.append((name, exc))
            if _retryable(exc):
                raise
    assert failures
    # Raise the primary backend's error: the fallback's (e.g. "402 credits depleted") would hide the real cause.
    primary = failures[0][1]
    for name, exc in failures[1:]:
        primary.add_note(f"fallback {name} also failed: {type(exc).__name__}: {str(exc)[:300]}")
    raise primary


async def call(role: str, prompt_file: str | None, *, pano_id: str, text_parts: Sequence[str] = (),
               images: Sequence[ImageRef] = (), schema: type[BaseModel] | None = None,
               tools: Sequence[Callable] | None = None, max_tool_calls: int | None = None,
               model: str | None = None) -> LLMResult:
    """One logged LLM call. `model` overrides `settings.models[role]` (used by the smoke test only)."""
    model = model or model_for(role)
    prompt = load_prompt(prompt_file) if prompt_file else None
    config = build_config(prompt, role, schema, tools, max_tool_calls)
    img_parts, img_log = zip(*(image_part(p, lvl) for p, lvl in images)) if images else ((), ())
    if schema is not None and settings().structured_output == "json_mode":
        text_parts = [*text_parts, schema_instruction(schema)]
    contents: list[Any] = [*text_parts, *img_parts]
    log: dict[str, Any] = {
        "role": role, "model": model, "prompt_file": str(prompt.path.name) if prompt else None,
        "prompt_version": prompt.version if prompt else None, "knowledge": list(prompt.knowledge) if prompt else [],
        "text_parts": list(text_parts), "images": list(img_log), "schema": schema.__name__ if schema else None,
        "tools": [t.__name__ for t in tools or ()],
    }
    t0 = time.perf_counter()
    try:
        resp, backend = await _generate_with_fallback(model, contents, config, sync=bool(tools))
        text, parsed, attempts = _response_text(resp), None, [_response_text(resp)]
        usage = [usage_dict(resp)]
        if schema is not None:
            try:
                parsed = schema.model_validate_json(text)
            except ValidationError as exc:  # retry once with the error appended
                retry_contents = [*contents, f"Your previous JSON failed validation:\n{exc}\nReturn corrected JSON only."]
                resp, backend = await _generate_with_fallback(model, retry_contents, config, sync=False)
                text = _response_text(resp)
                attempts.append(text)
                usage.append(usage_dict(resp))
                parsed = schema.model_validate_json(text)
    except Exception as exc:
        log.update(latency_s=round(time.perf_counter() - t0, 3), error=f"{type(exc).__name__}: {exc}")
        scribe.log_event(pano_id, "llm_call", role, log)
        scribe.log_event(pano_id, "error", role, {"where": "llm.call", "error": log["error"]})
        raise
    latency = time.perf_counter() - t0
    tool_calls = extract_tool_calls(resp)
    merged_usage = usage[-1] if len(usage) == 1 else {"attempts": usage}
    log.update(backend=backend, latency_s=round(latency, 3), response_text=attempts if len(attempts) > 1 else text,
               parsed=parsed.model_dump(mode="json") if parsed else None, tool_calls=tool_calls, usage=merged_usage)
    scribe.log_event(pano_id, "llm_call", role, log)
    return LLMResult(text, parsed, tool_calls, merged_usage, latency, model, raw=resp)


def call_sync(*args: Any, **kwargs: Any) -> LLMResult:
    """Blocking wrapper for code that is not inside an event loop."""
    return asyncio.run(call(*args, **kwargs))


# ---------------------------------------------------------------- smoke test (`cli.py check-llm`)

SMOKE_PANO_ID = "_check_llm"


def list_gemini_models() -> list[str]:
    last: Exception | None = None
    for _, client in clients():
        try:
            return sorted(m.name for m in client.models.list() if m.name and "gemini" in m.name)
        except FALLBACK_ERRORS as exc:
            last = exc
    assert last is not None
    raise last


def pick_flash(names: Sequence[str]) -> str | None:
    """Heuristic default for the smoke test only: highest-versioned plain `gemini-<version>-flash` model."""
    versions = []
    for name in names:
        short = name.split("/")[-1]
        m = re.fullmatch(r"gemini-(\d+(?:\.\d+)?)-flash", short)
        if m:
            versions.append((tuple(int(x) for x in m.group(1).split(".")), short))
    return max(versions)[1] if versions else None


async def smoke_test(model: str | None = None) -> dict[str, Any]:
    """1) list models  2) one text call  3) one image call  4) latency + usage."""
    from PIL import Image

    from navegador.config import cache_dir

    names = list_gemini_models()
    configured = settings().models.get("perception", "")
    model = model or (configured if configured and not configured.startswith("<") else pick_flash(names))
    if not model:
        raise LLMConfigError("no flash model found in the listing; pass --model")
    img = cache_dir() / "check_llm_red.jpg"
    img.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (256, 256), (220, 20, 20)).save(img, "JPEG")
    text = await call("check_llm", "check_llm.md", pano_id=SMOKE_PANO_ID, text_parts=["ping"], model=model)
    image = await call("check_llm", "check_llm.md", pano_id=SMOKE_PANO_ID, text_parts=["What colour is this image?"],
                       images=[(img, "LOW")], model=model)
    return {"models": names, "model": model,
            "text": {"reply": text.text.strip(), "latency_s": round(text.latency_s, 2), "usage": text.usage},
            "image": {"reply": image.text.strip(), "latency_s": round(image.latency_s, 2), "usage": image.usage}}
