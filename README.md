# Navegador

Multi-agent geolocation of 360° panoramas · Albatroz Challenge · JunctionX Lisbon 2026.
Design and contracts: [SPEC.md](SPEC.md). Build status and open questions: [CLAUDE.md](CLAUDE.md).

## Setup

Requires Python 3.13 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync                                     # creates .venv, installs deps + dev group (pytest)
cp .env.example .env                        # then fill in GOOGLE_CLOUD_PROJECT and TEAM_EMAIL
gcloud auth application-default login       # Vertex AI credentials (ADC)
gcloud auth application-default set-quota-project "$GOOGLE_CLOUD_PROJECT"
```

`.env`:

| Variable | Purpose |
|---|---|
| `GOOGLE_CLOUD_PROJECT` | GCP project with Vertex AI enabled (required) |
| `GOOGLE_CLOUD_LOCATION` | defaults to `global` |
| `GEMINI_API_KEY` | optional AI Studio fallback if Vertex fails |
| `TEAM_EMAIL` | contact address in the Nominatim User-Agent |

## Pick the models

```bash
uv run python cli.py check-llm              # lists Gemini models, does one text + one image call
uv run python cli.py check-llm --model gemini-2.5-flash
```

Copy the model IDs you want into `config/settings.yaml → models`. Python never hardcodes model IDs;
any role still set to a `<placeholder>` raises a clear error when called.

## Run

```bash
uv run pytest                               # unit tests
uv run streamlit run ui/app.py              # UI (P0b)
uv run python cli.py solve panos/<file>.jpg --label urban --mode baseline   # (P0b)
```

Panoramas go in `panos/` (git-ignored). Every run writes to `runs/<pano_id>/`:
`state.json`, `log.jsonl` (append-only event log), and on export `log.json` + `report.md`.

## Layout

```
config/      settings.yaml (models, thresholds, flags) · regions.yaml (allowed bounding boxes)
prompts/     one .md per agent; YAML front-matter `knowledge: [...]`
knowledge/   regional cues, confusion pairs, lessons (appended to prompts)
src/navegador/
  config.py schemas.py llm.py
  core/      state.py pipeline.py navegador.py escrivao.py export.py
  agents/    baseline leitor geografo detetive advogado professor cue_guard
  tools/     lupa astrolabio geo estratega cache
ui/app.py    Streamlit shell        cli.py   CLI shell
eval/        labels.csv run_batch.py compare.py
tests/
```

## House rules (from SPEC §2, §16)

- Every LLM call goes through `llm.py`; every HTTP call through `tools/` (cache + timeout + log).
- Prompt text only in `prompts/`, regional knowledge only in `knowledge/`.
- `src/navegador/**` never imports `streamlit`.
- No coordinates from model memory: hypotheses need a `provenance` from this run's tool registry.
