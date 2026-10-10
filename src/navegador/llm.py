"""LLM layer (SPEC §8). The only module that talks to the Gemini SDK (R5).

`call()` loads a prompt file (+ knowledge), sends text and images, optionally validates structured
output or runs an automatic function-calling loop, retries 429/500/503 and logs one `llm_call` event.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
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


@lru_cache
def vertex_client() -> genai.Client:
    e = env()
    if not e.project:
        raise LLMConfigError("GOOGLE_CLOUD_PROJECT is not set (.env)")
    return genai.Client(vertexai=True, project=e.project, location=e.location)


@lru_cache
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


def model_for(role: str) -> str:
    model = settings().models.get(role)
    if not model or model.startswith("<"):
        raise LLMConfigError(f"settings.yaml models.{role} is not set (got {model!r}); run `cli.py check-llm`")
    return model


def supports_thinking_level(model: str) -> bool:
    """thinking_level exists from Gemini 3 on; 2.x models reject it (they use thinking_budget)."""
    m = re.match(r"(?:.*/)?gemini-(\d+)", model)
    return bool(m) and int(m.group(1)) >= 3


def build_config(prompt: Prompt | None, role: str, schema: type[BaseModel] | None,
                 tools: Sequence[Callable] | None, max_tool_calls: int | None, *,
                 max_output_tokens: int | None = None, timeout_s: float | None = None,
                 thinking_level: str | None = None, model: str | None = None) -> types.GenerateContentConfig:
    """`model`, when given, gates thinking_level (only sent to models that support it)."""
    kwargs: dict[str, Any] = {
        "http_options": types.HttpOptions(timeout=int((timeout_s or settings().budgets.llm_timeout_s) * 1000)),
    }
    if max_output_tokens is not None:
        kwargs["max_output_tokens"] = max_output_tokens
    if prompt is not None:
        kwargs["system_instruction"] = prompt.system
    if schema is not None and tools:
        raise ValueError("do not combine tools with response_json_schema (SPEC §8)")
    if schema is not None:
        kwargs["response_mime_type"] = "application/json"
        kwargs["response_json_schema"] = schema.model_json_schema()
    if tools:
        kwargs["tools"] = list(tools)
        kwargs["automatic_function_calling"] = types.AutomaticFunctionCallingConfig(
            maximum_remote_calls=max_tool_calls or settings().budgets.detective_max_tool_calls)
    levels = settings().thinking_level
    level = thinking_level or levels.get(role, levels.get("default"))
    if level and (model is None or supports_thinking_level(model)):
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


async def _generate(client: genai.Client, model: str, contents: list[Any], config: types.GenerateContentConfig,
                    sync: bool, timeout_s: float, max_attempts: int) -> types.GenerateContentResponse:
    async for attempt in AsyncRetrying(retry=retry_if_exception(_retryable), stop=stop_after_attempt(max_attempts),
                                       wait=wait_exponential(multiplier=2, max=30), reraise=True):
        with attempt:
            if sync:  # tool loop: the verified sync AFC path, off the event loop
                coro = asyncio.to_thread(client.models.generate_content, model=model, contents=contents, config=config)
            else:
                coro = client.aio.models.generate_content(model=model, contents=contents, config=config)
            return await asyncio.wait_for(coro, timeout=timeout_s)
    raise AssertionError("unreachable")


async def _generate_with_fallback(model: str, contents: list[Any], config: types.GenerateContentConfig,
                                  sync: bool, timeout_s: float | None = None, max_attempts: int = 4,
                                  failures: list[str] | None = None) -> tuple[types.GenerateContentResponse, str]:
    """Try Vertex, then the API key. Each client's error is appended to `failures` (so a Vertex error is not
    hidden behind the fallback's error in the log)."""
    last: Exception | None = None
    timeout = timeout_s or settings().budgets.llm_timeout_s
    for name, client in clients():
        try:
            return await _generate(client, model, contents, config, sync, timeout, max_attempts), name
        except FALLBACK_ERRORS as exc:
            if failures is not None:
                failures.append(f"{name}: {type(exc).__name__}: {exc}"[:500])
            last = exc
            if _retryable(exc):
                raise
    assert last is not None
    raise last


async def call(role: str, prompt_file: str | None, *, pano_id: str, text_parts: Sequence[str] = (),
               images: Sequence[ImageRef] = (), schema: type[BaseModel] | None = None,
               tools: Sequence[Callable] | None = None, max_tool_calls: int | None = None,
               model: str | None = None, max_output_tokens: int | None = None, timeout_s: float | None = None,
               max_attempts: int = 4, retry_invalid: bool = True, thinking_level: str | None = None) -> LLMResult:
    """One logged LLM call. `model` overrides `settings.models[role]` (used by the smoke test only).
    Cost limits for cheap tool calls: `max_output_tokens`, `timeout_s` (else budgets.llm_timeout_s),
    `max_attempts` (transport retries on 429/500/503), `retry_invalid=False` skips the JSON-repair call and
    returns parsed=None on invalid JSON,
    `thinking_level` overrides settings.thinking_level (thinking tokens count against max_output_tokens)."""
    model = model or model_for(role)
    prompt = load_prompt(prompt_file) if prompt_file else None
    config = build_config(prompt, role, schema, tools, max_tool_calls,
                          max_output_tokens=max_output_tokens, timeout_s=timeout_s, thinking_level=thinking_level,
                          model=model)
    img_parts, img_log = zip(*(image_part(p, lvl) for p, lvl in images)) if images else ((), ())
    contents: list[Any] = [*text_parts, *img_parts]
    log: dict[str, Any] = {
        "role": role, "model": model, "prompt_file": str(prompt.path.name) if prompt else None,
        "prompt_version": prompt.version if prompt else None, "knowledge": list(prompt.knowledge) if prompt else [],
        "text_parts": list(text_parts), "images": list(img_log), "schema": schema.__name__ if schema else None,
        "tools": [t.__name__ for t in tools or ()], "max_output_tokens": max_output_tokens,
    }
    t0 = time.perf_counter()
    failures: list[str] = []
    try:
        resp, backend = await _generate_with_fallback(model, contents, config, bool(tools), timeout_s, max_attempts,
                                                      failures)
        text, parsed, attempts = _response_text(resp), None, [_response_text(resp)]
        usage = [usage_dict(resp)]
        if schema is not None:
            try:
                parsed = schema.model_validate_json(text)
            except ValidationError as exc:
                if not retry_invalid:  # caller handles parsed=None; usage and raw text are kept
                    log["validation_error"] = str(exc)[:500]
                else:  # retry once with the error appended
                    retry_contents = [*contents, f"Your previous JSON failed validation:\n{exc}\nReturn corrected JSON only."]
                    resp, backend = await _generate_with_fallback(model, retry_contents, config, False, timeout_s,
                                                                  max_attempts, failures)
                    text = _response_text(resp)
                    attempts.append(text)
                    usage.append(usage_dict(resp))
                    parsed = schema.model_validate_json(text)
    except Exception as exc:
        log.update(latency_s=round(time.perf_counter() - t0, 3), error=f"{type(exc).__name__}: {exc}",
                   fallback_errors=failures)
        scribe.log_event(pano_id, "llm_call", role, log)
        scribe.log_event(pano_id, "error", role, {"where": "llm.call", "error": log["error"]})
        raise
    latency = time.perf_counter() - t0
    tool_calls = extract_tool_calls(resp)
    merged_usage = usage[-1] if len(usage) == 1 else {"attempts": usage}
    if failures:
        log["fallback_errors"] = failures
    log.update(backend=backend, latency_s=round(latency, 3), response_text=attempts if len(attempts) > 1 else text,
               parsed=parsed.model_dump(mode="json") if parsed else None, tool_calls=tool_calls, usage=merged_usage)
    scribe.log_event(pano_id, "llm_call", role, log)
    return LLMResult(text, parsed, tool_calls, merged_usage, latency, model, raw=resp)


_sync_loop: asyncio.AbstractEventLoop | None = None


def call_sync(*args: Any, **kwargs: Any) -> LLMResult:
    """Blocking wrapper for code that is not inside an event loop.

    Uses one persistent loop: the cached genai client binds its aio session to the first loop it runs on,
    so asyncio.run() per call fails from the second call on with "Event loop is closed"."""
    global _sync_loop
    if _sync_loop is None or _sync_loop.is_closed():
        _sync_loop = asyncio.new_event_loop()
    return _sync_loop.run_until_complete(call(*args, **kwargs))


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
