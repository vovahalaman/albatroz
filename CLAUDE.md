# CLAUDE.md — Navegador build status

Read [SPEC.md](SPEC.md) first; it is the source of truth. This file tracks status only.
Working rules: SPEC §2 (hard rules) and §16. Update this file after every phase.

## Name mapping (SPEC.md → code)

SPEC.md uses Portuguese crew names; the code uses English file, class and config-key names.
When SPEC mentions a name on the left, edit the file on the right.

| SPEC name | Code |
|---|---|
| Navegador (orchestrator) | `core/navigator.py`, `prompts/navigator.md`, `models.navigator` |
| Escrivão | `core/scribe.py` |
| Estratega | `tools/strategist.py`, `settings.yaml → strategist:` |
| Lupa | `tools/magnifier.py` |
| Astrolábio | `tools/astrolabe.py` |
| Leitor | `agents/reader.py`, `prompts/reader.md`, `ReaderResult` |
| Geógrafo | `agents/geographer.py`, `prompts/geographer.md`, `GeographerResult` |
| Detetive | `agents/detective.py`, `prompts/detective.md`, `models.detective`, `budgets.detective_max_tool_calls` |
| Advogado | `agents/advocate.py`, `prompts/advocate.md`, `AdvocateResult`, `flags.advocate_enabled` |

The package and project name `navegador` is unchanged.

## Status

**Phase: P0a scaffold — done, except the live `check-llm` run (needs GCP credentials).**
**P0b · B Astrolábio (`tools/astrolabe.py`) — done, tuned on the 20 training panoramas. Deliberately conservative, low recall.**

Implemented and tested (`uv run pytest` → 69 passed, 2 skipped, 1 failing in `test_llm.py`, see Known issues):

| Module | What works |
|---|---|
| `config.py` | Loads `.env`, `settings.yaml`, `regions.yaml` into validated Pydantic models. `Regions.contains/clamp`. `NAVEGADOR_RUNS_DIR` / `NAVEGADOR_CACHE_DIR` overrides. |
| `schemas.py` | All §5 contracts + §10 agent outputs (`BaselineResult`, `ReaderResult`, `GeographerResult`, `MCQPhrasing`, `CueGuardResult`, `AdvocateResult`) + `View`, `SunObs`, `CueResult`, `ExcludedZone`. R2: `Hypothesis.model_validate(data, context={"registry": ids})` rejects unregistered provenance; `"estimated"` is allowed. |
| `core/state.py` | `State` model, `new_state/load/save` (atomic write), `parse_timestamp`, `pano_id_for`, `run_dir`. |
| `core/scribe.py` | `log_event` (append-only `log.jsonl`, event names enforced, bytes logged as sha256), `register_tool_call` → `call_id` + `tool_registry` (R2), `add_penalty` / `penalty_factor`, `export` → `log.json` (with git commit, prompt hashes, knowledge hash, models) + `report.md`. |
| `llm.py` | `call()` per §8: prompt + knowledge front-matter, per-part `media_resolution`, structured output with one validation retry, AFC tool loop (sync path in a thread), thinking level, tenacity retries on 429/500/503, timeout, Vertex → API-key fallback, one `llm_call` event per call. `smoke_test()` behind `cli.py check-llm`. |
| `tools/strategist.py` | `haversine_m`, `theta`, `score`, `expected_theta` (MC), `best_point` (centres + grid around top 3, fixed seed), `clusters` (single linkage), `submit` (`ev` / `hedge`, region clamping, fallback). Matches all §1 test vectors and the 9 630 / 8 230 examples. |
| `tools/astrolabe.py` | `detect(pano)` → `SunObs | None`, **conservative / low recall: a wrong sun is worse than no sun.** Bright low-saturation glare blobs in the upper half (seam-aware); the sun is located from the saturated core (min(R,G,B) ≥ 252), uncertainty = core radius + tilt margin. Rejected if too small/large/extended, core not round (circularity of the saturated core < 0.4), weak core (radius < 4° or < 30% of glare), below 3° elevation, above the date's max noon sun at 27.5°N (+ tilt), low ring contrast, or touching the top edge; `None` when unsure (score < 0.3) or ambiguous. Training set, blob detection only (`--no-vlm`): **4/12 suns, 0 false**. With the Gemini fallback/verifier (`tools/astrolabe_vlm.py`, `models.perception`, `astrolabe.vlm_fallback`): detected suns are verified ("no" vetoes), otherwise the VLM may point at the sun; the point must be locally bright and within min elevation/date ceiling; result `source=vlm`, confidence 0.3, uncertainty 8°. **10/12 suns (clear 6/7, cloud 4/5), 0 false, max residual 9.2° (194, 683: VLM points on bright cloud near the glare)**. Cost safeguards: disk cache by image hash (invalid answers too), ≤25 calls/process, sky image ≤1520×380, 512 output tokens (incl. thinking), 30 s, 1 retry, refuses `pro` models; `thinking_level` is only sent to Gemini 3+ models. `tools/astrolabe_summary.py`: `sun_summary()` for the agent (clock hypotheses, compass with image yaw per N…NW, compass uncertainty, plain note). Labels for evaluation: `eval/sun_labels.csv`; `sun_debug` prints suns found x/12, false suns, residual flags, VLM calls/tokens. `sun_check(lat, lng, ts, sun_yaw, sun_elev)`: evaluates **all** clock hypotheses (`astrolabe.time_hypotheses`: local tz, UTC, Europe/Lisbon), per-hypothesis expected elev/azimuth (pvlib), residual, `north_yaw_deg`; best fit, consistency 0–1, `ambiguous`. Matches the §9.2 reference table. `scripts/sun_debug.py <folder>` writes annotated PNGs + a table for tuning. `band()` is P2. |
| `cli.py` | `check-llm` works; `solve`, `batch`, `learn` are stubs. |
| Content | `prompts/baseline.md` (Appendix B), `prompts/check_llm.md`, knowledge seed (Appendix A). |

Stubbed (signatures + docstrings, raise `NotImplementedError`): `core/pipeline.py`, `core/navigator.py`,
`core/export.py`, all `agents/*`, `tools/magnifier.py` (except `pixel_to_angles`),
`tools/geo.py`, `tools/cache.py`, `ui/app.py`, `eval/*`. Agent prompts other than baseline are skeletons.
`tests/test_magnifier.py`, `test_pipeline_baseline.py` are skipped placeholders.

## Known issues

- **`check-llm` not yet run against GCP.** `.env` is empty and no ADC credentials exist on this laptop.
  The `settings.yaml` model IDs are still placeholders; any role call raises `LLMConfigError` until set.
- **Timestamp regex vs. real filenames.** SPEC §1 says `(\d{14})`, but the training files are named
  `IMG_YYYYMMDD_hhmmss_00_NNN.jpg`, which that does not match. `state.parse_timestamp` tries `(\d{14})`
  first and then `(\d{8})_(\d{6})`.
- **Panorama folder.** The training panoramas live in `data/train1`, `data/train2` (with
  `data/panoramas.csv` from `make_csv.py`), not in `panos/` as in SPEC §7. Both are git-ignored.
- **Additions to SPEC §6 `settings.yaml`:** a `scoring:` block (rank decay, special-one ×3,
  10 m zero distance, θ scales, penalty factors ×0.8/×0.95/×0.5), so no Python hardcodes them.
- `strategist.submit(state, fallback=None)`: the pipeline passes the geocoded Geographer top-region
  centroid as `fallback`, so Strategist stays I/O-free.
- `llm.call` has an extra keyword `model=` (overrides `settings.models[role]`). Only the smoke test uses it.
- `llm.py` is ~300 lines (soft limit ~250) because the smoke test lives there; R5 keeps SDK calls in this file.
- Strategist MC samples are allocated by weight (stratified) rather than drawn. This removes mixture
  noise; with random draws the 9 630 example came out at 9 737.
- Legacy `requirements.txt` (adds geopy, astral, geoclip) and `make_csv.py` remain from before the
  scaffold. Dependencies are now managed by `pyproject.toml` + uv.
- **Python version:** uv defaults to Python 3.15 if installed, where `h5py` (a pvlib dependency) has no wheel and
  fails to build. Use 3.13 (SPEC): `export UV_PYTHON=3.13` before `uv sync`. Consider committing `.python-version`.
- **Astrolábio ignores `flags.timestamp_is_local`.** The camera clock is unreliable (local, UTC or Lisbon time,
  even in ES/FR), so `sun_check` always evaluates every clock in `astrolabe.time_hypotheses`. Clocks that give
  the same instant are merged (e.g. local = Lisbon in PT). The azimuth cannot separate clocks (unknown heading),
  so several may fit → `ambiguous: true`.
- **Additions for Astrolábio:** `astrolabe:` block in `settings.yaml` + `AstrolabeSettings` in `config.py`;
  `SunObs` gained `uncertainty_deg` and `pixel_xy` (full-res pixels); `scipy` is now a direct dependency.
- **Astrolábio recall is low by design** (4 of ~6 visible suns on the training set, 0 false positives). With
  `min_confidence: 0.3` and core circularity ≥ 0.4 the margin is thin: the highest non-sun candidate scores 0.24
  (064, a gap between pillars), the weakest real sun 0.31 (695); 685's real sun passes the checks but scores 0.27.
  If a false sun ever appears, raise `min_confidence` (0.4 keeps only 749 and 754). Re-check with
  `scripts/sun_debug.py` when new panoramas arrive. Ring contrast is on luminance. This camera clips white at ~253, not 255 (`core_min`).
  `astrolabe.py` is ~330 lines (soft limit ~250); VLM and summary code live in `astrolabe_vlm.py` / `astrolabe_summary.py`.
- **VLM token cap:** `gemini-3.8-flash` thinks 137–192 tokens even at `thinking_level: LOW` (`MINIMAL` is rejected,
  `thinking_budget` is ignored); at 200 tokens 7/20 answers were cut off, at 512 only 1 (218) failed.
- **Committed `models.*` still say `gemini-2.5-flash`**, which returns 404 "no longer available to new users" on our
  project. Locally `gemini-3.8-flash` is used; the model-ID change is not committed yet (team decision). With 2.5,
  VLM calls fail (404, not cached) and detection falls back to blob-only.
- **`llm.py` additions (backward compatible):** `call(..., max_output_tokens, timeout_s, max_attempts, retry_invalid,
  thinking_level)`; `retry_invalid=False` returns `parsed=None` instead of raising. `call_sync` now reuses one event
  loop (with `asyncio.run` per call the cached genai client failed from the 2nd call on with "Event loop is closed").
- `tests/conftest.py` blocks real LLM requests in every test (`llm._generate` raises).
- `tests/test_llm.py::test_unset_model_raises` fails since master set real model IDs in `settings.yaml`
  (the test expects placeholders). Not owned by Astrolábio; left as is.
- `.env` was tracked in git (empty). Now untracked via `git rm --cached`, as SPEC §15 requires.

## Open questions

[CONFIRM] items for the organizers (SPEC Appendix C). Defaults are implemented and configurable:

1. **Submission format** for the 10 coordinates. Default: `export.to_albatroz` = `to_json`.
2. **JSON log format.** Default: our `log.json` (§11). Post a sample on Discord during the trial.
3. **Timestamp local or UTC?** Default: local (`flags.timestamp_is_local: true`).
4. **EXIF/XMP GPS/heading allowed?** Default: logged but not used (`flags.use_exif_gps: false`).
5. **Penalty scope (rule 9):** only candidates committed after the MCQ/cue? Default: every penalty
   issued before submission applies to all 10 ranks (`flags.commit_then_ask: false`).
6. **May MCQ options name geographic zones** (e.g. "Alentejo")? Default: yes (reverse-geocoded zone names).

Team questions:

7. Should the timestamp fallback regex (see Known issues) stay, or will scored-run filenames be pure 14 digits?
8. Move `data/train*` into `panos/`, or point the tooling at `data/`?

## Next steps

1. Fill `.env`, run `gcloud auth application-default login`, then `uv run python cli.py check-llm`.
   Put the chosen model IDs into `config/settings.yaml`.
2. Commit, push to `main`, then start P0b in parallel (SPEC §15):
   - A: `pipeline.py` (baseline) + `cli.py solve/batch`
   - B: `magnifier.py` + tests (`astrolabe.py` done; re-tune with `scripts/sun_debug.py data/train1` on new data)
   - C: `geo.py` + `cache.py` + `agents/baseline.py`
   - D: `ui/app.py` + `export.py`
3. Pipeline S2 stores `astrolabe.detect()` in `state.sun`; the Detective gets `sun_check` as a tool and passes
   `state.sun.yaw/elevation/uncertainty_deg`.
