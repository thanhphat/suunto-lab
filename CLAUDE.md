# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Self-hosted store and analysis for Suunto (later Garmin/Coros) FIT files: a FastAPI backend + single-page web dashboard, a queue worker that parses FIT files and computes metrics, and a read-only MCP server so AI agents can analyse the data. Postgres 16 + TimescaleDB. README is in Vietnamese (`suunto-lab-architecture.md` is an identical copy); user-facing strings (API errors, activity names, worker error messages) are Vietnamese too — keep them that way.

## Commands

```bash
# Full stack (db, api :8000, worker, mcp :8001); needs a .env with POSTGRES_PASSWORD, API_TOKEN, MCP_TOKEN
docker compose up -d --build
docker compose --profile tunnel up -d          # also start Cloudflare Tunnel (CLOUDFLARE_TUNNEL_TOKEN)

# Local dev (Postgres/Timescale at localhost:5432)
uv sync                                        # Python 3.15 (.python-version) + deps into .venv
export DATABASE_URL=postgresql://suunto:suunto@localhost:5432/suunto API_TOKEN=dev MCP_TOKEN=dev
uv run uvicorn app.api:app --reload --port 8000       # web + API, docs at /api/docs
uv run python -m app.worker                           # processing queue
uv run python -m app.mcp_server --http 8001           # MCP over streamable HTTP at /mcp (or --stdio)

# Smoke-test the pipeline and an MCP tool
curl -H "Authorization: Bearer dev" -F "files=@samples/easy-run.fit" localhost:8000/api/upload
MCP_TOKEN=dev scripts/mcp_call.sh get_overview
MCP_TOKEN=dev scripts/mcp_call.sh list_activities '{"sport":"running"}' 1000
```

Dependencies are managed with uv: `pyproject.toml` + committed `uv.lock` (no `requirements.txt`). Use `uv add <pkg>` / `uv lock --upgrade`, never pip. The project is not an installable package (`tool.uv.package = false`); code runs as `app.*` modules from the repo root. The Docker image installs with `uv sync --locked` into `/srv/.venv`, which is on `PATH`, so compose commands call `python`/`uvicorn` directly.

There is no test suite, linter config, or `.env.example` in the repo (README references one). `samples/*.fit` are synthetic files for manual testing.

## Architecture

**Pipeline (upload → queue → derive):**
- `ingest.store_upload` content-addresses the bytes (SHA-256) to `FIT_STORAGE_DIR/<athlete>/<sha[:2]>/<sha>.fit`, inserts a `fit_files` row (status `pending`), and `NOTIFY fit_files_pending`. Same-sha uploads are no-ops.
- `worker.py` LISTENs and also polls; claims rows with `FOR UPDATE SKIP LOCKED` (multiple workers are safe), max 3 attempts, re-queues rows stuck in `processing` >10 min.
- `processing.process_fit_file`: `fit_parser.decode` (Garmin FIT SDK → vendor-neutral `Parsed` with aligned numpy arrays) → `metrics.compute` (pure numpy functions) → writes `activities`, `laps`, `samples` (via COPY), `rr_intervals`, `activity_metrics`, `splits` in one transaction, then rebuilds `daily_load` for the whole athlete history. Parse errors mark the file `failed` permanently; a second file with the same `(athlete, start_time, sport)` is marked `duplicate`.

**Data layers (lower layer is the source of truth; upper layers must stay recomputable from it):**
raw `fit_files` + files on disk → canonical `activities/laps/samples/rr_intervals` → derived `activity_metrics/splits/daily_load` → views `v_activities/v_weekly`.
- Reprocessing (`POST /api/reprocess-all`, `/api/files/{id}/reprocess`) just sets files back to `pending`. When reprocessing the same file, the existing activity id is reused — keep ids stable.
- `activity_metrics.hr_hist` stores seconds-per-bpm so HR zone/TRIMP/CTL changes (`PUT /api/settings` → `recompute_zones`) don't need the raw samples.
- `samples` is a Timescale hypertable (compressed after 60 days) — always filter by `activity_id`.

**Schema/migrations:** `app/schema.sql` is idempotent (`IF NOT EXISTS`, `CREATE OR REPLACE`) and is applied by `db.migrate()` at startup of api/worker/mcp under an advisory lock. There is no migration tool — schema changes must be written so re-running the whole file is safe on an existing DB. `migrate()` also ensures one athlete row exists; the app is effectively single-athlete (`athlete_id` exists for future multi-user).

**Read side:** `queries.py` is the shared read model used by both `api.py` and `mcp_server.py`; put new read logic there rather than in either entrypoint. `queries.clean` makes DB values JSON-safe.

**MCP server (`mcp_server.py`):**
- Uses the `mcp` 2.x `MCPServer` API; all tools are read-only (`RO` annotations).
- `INSTRUCTIONS` embeds the `app/metrics.py` module docstring — that docstring is the canonical definition of every metric (moving, grade, GAP/Minetti, VAM, zones, Edwards TRIMP, decoupling) and is sent to agents. Update it whenever metric logic changes.
- `query_sql` connects as Postgres role `agent_ro` (granted in `schema.sql`: SELECT only, no `fit_files`, read-only transactions, 8 s timeout); only a single SELECT/WITH is accepted. New tables/views agents should see need an explicit `GRANT SELECT ... TO agent_ro` in `schema.sql`, and `describe_schema` should be updated.
- `TokenGate` auth: `Authorization: Bearer <MCP_TOKEN>` or secret path `/mcp/<MCP_TOKEN>` (for claude.ai). `MCP_ALLOWED_HOSTS` enables DNS-rebinding protection.

**Web/API:** `api.py` serves `app/static/index.html` (single self-contained file, no build step) at `/`. All `/api/*` routes depend on `auth`, a single shared `API_TOKEN` (Bearer or `X-API-Token`); `/api/upload/raw` takes the raw body for the iOS Shortcut.

**Config:** everything comes from env vars in `app/config.py` (`DATABASE_URL`, `API_TOKEN`, `MCP_TOKEN`, `AGENT_DB_PASSWORD`, `FIT_STORAGE_DIR`, `DEFAULT_TZ` = Asia/Ho_Chi_Minh, etc.). Dates like `local_date`/`daily_load.day` use the athlete's timezone from `athlete_settings`.
