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

**Phase: P0a scaffold — done. `check-llm` passes on Vertex AI.**

Implemented and tested (`uv run pytest` → 35 passed, 3 skipped):

| Module | What works |
|---|---|
| `config.py` | Loads `.env`, `settings.yaml`, `regions.yaml` into validated Pydantic models. `Regions.contains/clamp`. `NAVEGADOR_RUNS_DIR` / `NAVEGADOR_CACHE_DIR` overrides. |
| `schemas.py` | All §5 contracts + §10 agent outputs (`BaselineResult`, `ReaderResult`, `GeographerResult`, `MCQPhrasing`, `CueGuardResult`, `AdvocateResult`) + `View`, `SunObs`, `CueResult`, `ExcludedZone`. R2: `Hypothesis.model_validate(data, context={"registry": ids})` rejects unregistered provenance; `"estimated"` is allowed. |
| `core/state.py` | `State` model, `new_state/load/save` (atomic write), `parse_timestamp`, `pano_id_for`, `run_dir`. |
| `core/scribe.py` | `log_event` (append-only `log.jsonl`, event names enforced, bytes logged as sha256), `register_tool_call` → `call_id` + `tool_registry` (R2), `add_penalty` / `penalty_factor`, `export` → `log.json` (with git commit, prompt hashes, knowledge hash, models) + `report.md`. |
| `llm.py` | `call()` per §8: prompt + knowledge front-matter, per-part `media_resolution`, structured output with one validation retry, AFC tool loop (sync path in a thread), thinking level, tenacity retries on 429/500/503, timeout, Vertex → API-key fallback, one `llm_call` event per call. `smoke_test()` behind `cli.py check-llm`. |
| `tools/strategist.py` | `haversine_m`, `theta`, `score`, `expected_theta` (MC), `best_point` (centres + grid around top 3, fixed seed), `clusters` (single linkage), `submit` (`ev` / `hedge`, region clamping, fallback). Matches all §1 test vectors and the 9 630 / 8 230 examples. |
| `cli.py` | `check-llm` works; `solve`, `batch`, `learn` are stubs. |
| Content | `prompts/baseline.md` (Appendix B), `prompts/check_llm.md`, knowledge seed (Appendix A). |

Stubbed (signatures + docstrings, raise `NotImplementedError`): `core/pipeline.py`, `core/navigator.py`,
`core/export.py`, all `agents/*`, `tools/magnifier.py` (except `pixel_to_angles`), `tools/astrolabe.py`,
`tools/geo.py`, `tools/cache.py`, `ui/app.py`, `eval/*`. Agent prompts other than baseline are skeletons.
`tests/test_magnifier.py`, `test_astrolabe.py`, `test_pipeline_baseline.py` are skipped placeholders.

## Known issues

- **`check-llm` passes on Vertex AI (2026-10-10).** Models chosen: `gemini-3.8-flash` (perception, detective,
  baseline, advocate) and `gemini-3.5-flash-lite` (navigator, cue_guard). `gemini-2.5-flash` returns 404
  ("no longer available to new users"). The AI Studio key fallback works but its prepaid credits are depleted.
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

1. Fill `.env`, run `gcloud auth application-default login`, then `uv run python cli.py check-llm`.
   Put the chosen model IDs into `config/settings.yaml`.
2. Commit, push to `main`, then start P0b in parallel (SPEC §15):
   - A: `pipeline.py` (baseline) + `cli.py solve/batch`
   - B: `magnifier.py` + `astrolabe.py` + tests
   - C: `geo.py` + `cache.py` + `agents/baseline.py`
   - D: `ui/app.py` + `export.py`
