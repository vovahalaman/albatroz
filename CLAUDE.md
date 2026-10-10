# CLAUDE.md — Navegador build status

Read [SPEC.md](SPEC.md) first; it is the source of truth. This file tracks status only.
Working rules: SPEC §2 (hard rules) and §16. Update this file after every phase.

## Naming

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

**Phase: P0b baseline path — done (branch `feature/baseline-pipeline`). `cli.py solve` / `batch` solve real
training panoramas end to end in baseline mode (Gemini on Vertex + Nominatim). UI still a stub.**

Implemented and tested (`uv run pytest` → 94 passed, 1 skipped):

| Module | What works |
|---|---|
| `config.py` | Loads `.env`, `settings.yaml`, `regions.yaml` into validated Pydantic models. `Regions.contains/clamp`. `NAVEGADOR_RUNS_DIR` / `NAVEGADOR_CACHE_DIR` overrides. |
| `schemas.py` | All §5 contracts + §10 agent outputs (`BaselineResult`, `ReaderResult`, `GeographerResult`, `MCQPhrasing`, `CueGuardResult`, `AdvocateResult`) + `View`, `SunObs`, `CueResult`, `ExcludedZone`. R2: `Hypothesis.model_validate(data, context={"registry": ids})` rejects unregistered provenance; `"estimated"` is allowed. |
| `core/state.py` | `State` model, `new_state/load/save` (atomic write), `parse_timestamp`, `pano_id_for`, `run_dir`. |
| `core/scribe.py` | `log_event` (append-only `log.jsonl`, event names enforced, bytes logged as sha256), `register_tool_call` → `call_id` + `tool_registry` (R2), `add_penalty` / `penalty_factor`, `export` → `log.json` (with git commit, prompt hashes, knowledge hash, models) + `report.md`. |
| `llm.py` | `call()` per §8: prompt + knowledge front-matter, per-part `media_resolution`, structured output with one validation retry, AFC tool loop (sync path in a thread), thinking level, tenacity retries on 429/500/503, timeout, Vertex → API-key fallback, one `llm_call` event per call. `smoke_test()` behind `cli.py check-llm`. |
| `tools/strategist.py` | `haversine_m`, `theta`, `score`, `expected_theta` (MC), `best_point` (centres + grid around top 3, fixed seed), `clusters` (single linkage), `submit` (`ev` / `hedge`, region clamping, fallback). Matches all §1 test vectors and the 9 630 / 8 230 examples. |
| `tools/cache.py` | `lookup` (hit/miss, a cached `None` is a hit), `get`, `put` (atomic), `@cached(ns)`. `cache/<ns>/<sha256>.json`, no expiry. |
| `tools/geo.py` | §9.3: `geocode`, `reverse` (Nominatim), `overpass_near`, `overpass_named`, `wiki_coords`. Every call: disk cache → policy check → HTTP with timeout + tenacity retry (429/5xx/connection) → `scribe.register_tool_call` (`call_id` on every result, `cached` flag). Failures log an `error` event and raise `GeoError`; errors are never cached. Nominatim: 1.1 s process-wide throttle (cache hits skip it), User-Agent from settings, `max_geocode_calls_per_pano` counted from the run's log. Calls must run inside `geo.use_run(pano_id, actor)`. Tests: `tests/test_geo.py` (mocked HTTP, fake clock). |
| `tools/magnifier.py` | §9.1 `views()` (8 h + 4 g + 4 u + overview; reused via `views.json` when pano + settings unchanged), `zoom()` (input clamped, cached by pano content hash). Settings in `settings.yaml → magnifier:`. |
| `core/pipeline.py` | Baseline mode: `start_run` (S0: state, EXIF/XMP probe, `run_start`), `advance` (S1 views → BASELINE → S7 submit → S8 report; every stage timed, logged, saved; a failing stage logs `error` and the run still submits), `force_submit`. `autonomous`/`assisted` run the baseline stages for now (logged). `answer_mcq`, `add_cue` still P1 stubs. |
| `agents/baseline.py` | One `llm.call` (overview LOW + h000…h315 MEDIUM + label/timestamp/image order) → `baseline.json`; top 3 places geocoded (model's country first, then pt,es,fr), results outside `regions.yaml` dropped, R2-validated Hypotheses (weight = confidence, radius 300 m / 8 km). Country-box fallback when nothing geocodes. |
| `core/export.py` | `validate` (R1), `to_text`, `to_json`, `to_csv`, `to_albatroz` (= JSON until confirmed). S7 writes `submission.json/.txt/.csv`. |
| `core/exif.py` | S0 probe: EXIF tags, GPS, XMP (GPano). GPS only used with `flags.use_exif_gps`. The training panos carry no EXIF at all. |
| `cli.py` | `check-llm`, `geo-check`, `solve` (prints country probs, places → coordinates, clues, errors, the 10 pairs), `batch` (labels CSV `file|filename,label`; optional `--truth eval/labels.csv` → distance + pseudo-score; summary CSV with git commit + `baseline.md` hash in `runs/_batch/`). `learn` is P2. |
| Content | `prompts/baseline.md` (Appendix B), `prompts/check_llm.md`, knowledge seed (Appendix A). |

Stubbed (signatures + docstrings, raise `NotImplementedError`): `core/navigator.py`, `pipeline.answer_mcq/add_cue`,
all agents except baseline, `tools/astrolabe.py` (on `aatu-sundetection`, not merged), `ui/app.py`, `eval/*`.
Agent prompts other than baseline are skeletons. `test_astrolabe.py` is a skipped placeholder.

## Known issues

- **Structured output deviates from SPEC §8 (`structured_output: json_mode`).** With `response_json_schema`
  and 9 images, Gemini's constrained decoding wrote runaway floats (`"pt": 0.98500000000000010214…`) until the
  output limit: 2/4 calls broken, ~95 s each. `response_schema` (SDK-converted) still broke ~1/4; a prompt rule
  "two decimals" did not help. `json_mode` = `response_mime_type=application/json` without a schema, the schema
  appended as a text part (`llm.schema_instruction`), Pydantic validates (one retry): 6/6 valid. Switch back in
  `settings.yaml` if a later model fixes it.
- **`llm.py` clients were `lru_cache`d** → the second `asyncio.run` in a process failed with "Event loop is closed"
  (broke `batch` and would break Streamlit). Clients are now built per call.
- **`budgets.llm_max_output_tokens: 8192`** (added): caps runaway output. It includes thinking tokens
  (thinking alone used up to ~4 000), so do not set it much lower.
- **AI Studio fallback key has no credits** (402 RESOURCE_EXHAUSTED). Vertex works; the fallback only hides
  the real Vertex error (llm.py raises the last client's error).
- **Baseline latency:** ~25–45 s per pano (default thinking), ~12 k tokens. `batch` is sequential.
- **`settings.yaml → baseline:`** (added): `top_places`, `geocode_limit`, `radius_m`. Rule 8 (×0.5 text location)
  is not applied to geocoded place names (open question 5).
- **Geo context.** SPEC §9.3 signatures have no `pano_id` (they are exposed to the model as tools), so the
  pano comes from the `geo.use_run(pano_id, actor)` context manager (contextvars; propagates through
  `asyncio.to_thread`, not through bare threads). Calling a geo tool outside it raises `RuntimeError`.
- **Nominatim throttle is per process.** It cannot coordinate the four laptops; the shared policy relies on
  the cache and `max_geocode_calls_per_pano`. Geo refuses network calls while the User-Agent has no contact
  address (`TEAM_EMAIL` in `.env`).
- **Additions to SPEC §6 `geo:`** `http_timeout_s`, `overpass_timeout_s`, `overpass_max_results`, `retries`
  and the three endpoint URLs (defaults in `GeoSettings`). `overpass_named` without `bbox` searches the
  `regions.yaml` boxes, not the whole planet. `geo.py` is ~260 lines (soft limit ~250).
- **`check-llm` not yet run against GCP.** `.env` is empty and no ADC credentials exist on this laptop.
  The `settings.yaml` model IDs are still placeholders; any role call raises `LLMConfigError` until set.
- **Timestamp regex vs. real filenames.** SPEC §1 says `(\d{14})`, but the training files are named
  `IMG_YYYYMMDD_hhmmss_00_NNN.jpg`, which that does not match. `state.parse_timestamp` tries `(\d{14})`
  first and then `(\d{8})_(\d{6})`.
- **Panorama folder.** The training panoramas live in `data/train1`, `data/train2` (with
  `data/panoramas.csv` from `make_csv.py`), not in `panos/` as in SPEC §7. Both are git-ignored.
- **`settings.yaml → magnifier:`** (not in SPEC §6) holds the magnifier render settings. `schemas.ZoomRequest` now wraps yaw and clamps pitch/FOV instead of rejecting the whole Reader answer; the bounds stay in the JSON schema, NaN/inf is still rejected.
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
- `.env` was tracked in git (empty). Now untracked via `git rm --cached`, as SPEC §15 requires.

## Open questions

Checked against the organizers' brief (`Albatroz Challenge - JunctionX Lisbon 2026.pdf`, 2026-10-10).

Answered by the brief:

- **MCQs may name geographic zones** (old Q6): yes. The brief says MCQs "can be about features,
  about 'geographic zones', etc." and must be generated by the agent, not suggested by the team.
- **Penalty scope** (old Q5): rule 9 says cue/MCQ/text factors apply "to all subsequent candidate
  plausible locations". The current default (every penalty applies to all 10 ranks,
  `flags.commit_then_ask: false`) is the safe lower bound. Committing candidates *before* asking could
  avoid some penalties; that needs organizer confirmation before using it.
- **Prepared knowledge / prompts are allowed**: the team "may prepare the software or AI agents or
  whatever means it finds useful". During the run only text cues and MCQ clicks may be fed in, so
  `knowledge/` and `prompts/` must be frozen before Sunday (tag `freeze-sunday`) and must stay general
  (never "panorama X is in Y").

[CONFIRM] still open for the organizers (ask on Discord):

1. **Submission format** for the 10 coordinates. Default: `export.to_albatroz` = `to_json`.
2. **JSON log format.** Default: our `log.json` (§11). Post a sample during the trial.
3. **Timestamp local or UTC?** Not stated in the brief. Default: local (`flags.timestamp_is_local: true`).
4. **EXIF/XMP GPS/heading allowed?** Not stated. Default: logged but not used (`flags.use_exif_gps: false`).
5. **Rule 8 (text localization ×0.5) vs. automatic geocoding.** The baseline agent returns place
   *names* and code geocodes them. Does that count as a text localization (×0.5)? Our reading: no,
   because rule 8 describes the *team* defining a coordinate. Unconfirmed; this is the biggest rules
   risk for baseline mode. The Detective (agent outputs coordinates via tools) is the safer design.
6. **Fixed knowledge file**: confirm that a prepared, unchanging notes file about regional features
   is acceptable (the brief implies yes).

Spec vs. brief differences:

- **Islands:** the brief only says "mainly Portugal but also Spain and France"; the Azores, Madeira,
  Canaries, Balearics and Corsica come from SPEC §1. Keeping them in `regions.yaml` is harmless.
- **Ties are broken by time** (brief rule 12), so per-panorama speed matters.
- Brief distance = gis.tools Haversine; Strategist uses Haversine with R = 6 371 008.8 m (difference negligible).

Team questions:

7. Should the timestamp fallback regex (see Known issues) stay, or will scored-run filenames be pure 14 digits?
8. Move `data/train*` into `panos/`, or point the tooling at `data/`?
9. The organizers' PDF is in the repo root and not git-ignored. Push it or ignore it?

## Next steps

1. Prompt work: `uv run python cli.py batch data --labels data/panoramas.csv` after each `baseline.md` change;
   compare the summary CSVs (they record the prompt hash). Fill `eval/labels.csv` with reviewed locations to get
   pseudo-scores and distances.
2. Merge `feature/geo-tools`. P1 · C: `detective.py` wraps the geo tools inside `geo.use_run(...)` and turns
   `GeoError` into a tool error message for the model.
3. Commit, push to `main`, then start P0b in parallel (SPEC §15):
   - A: `pipeline.py` (baseline) + `cli.py solve/batch`
   - B: `magnifier.py` + `astrolabe.py` + tests
   - C: `geo.py` + `cache.py` + `agents/baseline.py`
   - D: `ui/app.py` + `export.py`