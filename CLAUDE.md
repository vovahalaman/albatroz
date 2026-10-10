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

`baseline`, `professor` and `cue_guard` are unchanged. The package and project name `navegador` is unchanged.

## Status

**Phase: P0a scaffold — done, except the live `check-llm` run (needs GCP credentials).**
**P0b · C (branch `feature/geo-tools`): `tools/geo.py` + `tools/cache.py` done; live `geo-check` passed.**

Implemented and tested (`uv run pytest` → 55 passed, 3 skipped, 1 failing; see Known issues):

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
| `cli.py` | `check-llm` and `geo-check` work; `solve`, `batch`, `learn` are stubs. |
| Content | `prompts/baseline.md` (Appendix B), `prompts/check_llm.md`, knowledge seed (Appendix A). |

Stubbed (signatures + docstrings, raise `NotImplementedError`): `core/pipeline.py`, `core/navigator.py`,
`core/export.py`, all `agents/*`, `tools/magnifier.py` (except `pixel_to_angles`), `tools/astrolabe.py`,
`ui/app.py`, `eval/*`. Agent prompts other than baseline are skeletons.
`tests/test_magnifier.py`, `test_astrolabe.py`, `test_pipeline_baseline.py` are skipped placeholders.

## Known issues

- **`test_llm.py::test_unset_model_raises` fails** because `settings.yaml` now has real model IDs
  (`gemini-3.8-flash`) instead of `<…>` placeholders. The test needs updating (llm owner), not the config.
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
2. Merge `feature/geo-tools`. P1 · C: `detective.py` wraps the geo tools inside `geo.use_run(...)` and turns
   `GeoError` into a tool error message for the model.
3. Commit, push to `main`, then start P0b in parallel (SPEC §15):
   - A: `pipeline.py` (baseline) + `cli.py solve/batch`
   - B: `magnifier.py` + `astrolabe.py` + tests
   - C: ~~`geo.py` + `cache.py`~~ (done) + `agents/baseline.py`
   - D: `ui/app.py` + `export.py`
