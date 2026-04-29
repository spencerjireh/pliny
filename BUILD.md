# Pliny — Build Setup

Companion to `spec.md`. Pins the toolchain, repo layout, and ops choices the spec deliberately leaves out. Update this file when any of these change.

## Toolchain

| Concern | Choice |
|---|---|
| Python | 3.13 |
| Package manager | `uv` (lockfile committed: `uv.lock`) |
| Lint + format | `ruff` (replaces black/isort/flake8) |
| Type checker | `pyright` (strict mode on `pliny/`) |
| Test runner | `pytest` + `pytest-asyncio` + `httpx` |
| Integration tests | `testcontainers` (real Postgres+pgvector, Neo4j, RustFS per session) |
| Pre-commit | `pre-commit` running `ruff check`, `ruff format --check`, `pyright` |
| Migrations | Alembic |
| DB driver | `psycopg[binary,pool]` (async) |
| Telegram | direct `httpx` long-polling client (no `aiogram`/`python-telegram-bot` dep) |
| Tokenizer | `tiktoken` (`cl100k_base`) |
| pHash | `imagehash` + `Pillow` |
| HTML extract | `trafilatura` |
| PDF extract | deferred — PDFs are snapshotted/stored; in-pipeline text extraction not yet wired |
| Snapshot | `playwright` + SingleFile CLI |
| Probe | deferred — `ffprobe` not yet bundled; audio/video metadata extraction not wired |

## Frontend toolchain

| Concern | Choice |
|---|---|
| Build tool | Vite |
| Language | TypeScript (strict) |
| Framework | React 18+ |
| Styling | Tailwind |
| API types | `openapi-typescript` codegen from FastAPI's `/openapi.json`; checked into repo, regen verified in CI |
| API client | Hand-written fetch wrapper that consumes the generated types |
| State | React state + URL params; no global store needed for one screen |
| Lint | `eslint` + `@typescript-eslint` |
| Format | `prettier` |

## Repo layout

Single repo, single Python package, multiple CLI entrypoints. The frontend lives alongside.

```
scrollz/
  pyproject.toml              # uv-managed; declares all entrypoints
  uv.lock
  .env.example
  .pre-commit-config.yaml
  pliny/
    __init__.py
    config.py                 # env loading
    cli.py                    # exposes: api / worker --pool {fast,slow} / bot
    api/
      app.py                  # FastAPI app, /v1/ router mount
      routes/
        items.py
        search.py
        admin.py
        health.py
      deps.py                 # auth, db sessions
    workers/
      pool.py                 # claim loop, NOTIFY/LISTEN
      runner.py               # per-job execution
      retry.py                # backoff policy
      sweeper.py              # resets stuck-running jobs past STAGE_TIMEOUT_SECONDS
    pipeline/
      stages.py               # stage handler registry
      context.py              # per-job execution context
      snapshot/handler.py
      wayback_fallback/handler.py
      extract/
        dispatcher.py         # routes by item type
        text.py
        url_html.py
        image.py              # vision OCR via LLM
      summarize/handler.py
      chunk/                  # handler.py + chunker.py (token-window splitter)
      embed/handler.py
      entities/handler.py
      graph_sync/handler.py
    bot/
      runner.py               # entrypoint: `pliny bot`
      dispatcher.py           # routes Telegram updates → handlers
      poller.py               # long-poll loop + status-poll → message edit
      telegram_api.py         # direct httpx wrapper (no aiogram)
      pliny_client.py         # bot → API HTTP client
      config.py               # parse_allowed_user_ids etc.
    snapshot/                 # snapshot subsystem (used by pipeline/snapshot stage)
      base.py                 # Snapshotter protocol
      playwright_impl.py      # playwright + SingleFile
      classifier.py           # url → snapshot kind
      merge.py                # pHash-based dedupe
    llm/
      base.py                 # LLM protocol
      openai_impl.py
      rate_limit.py           # token bucket (RPM/TPM)
      cost_cap.py             # daily $ counter (Postgres-backed)
    prompts/
      summarize.py            # PROMPT + VERSION
      entities.py
      vision_ocr.py
    schemas/
      metadata.py             # per-type Pydantic metadata models
      api.py                  # request/response models
      search.py
      admin.py
    storage/
      blob.py                 # BlobStore interface
      rustfs.py
      filesystem.py           # local impl for tests
    db/
      models.py               # SQLAlchemy / table definitions
      queries.py
      neo4j.py                # Neo4j async driver wrapper
    graph/
      schema.py               # Cypher constraints/indexes
      sync.py                 # incremental upserts from graph_sync stage
      rebuild.py              # admin endpoint: drop+rebuild from Postgres
    canonicalize.py
    media_hosts.py
    logging.py                # structured JSON logger
  migrations/                 # Alembic
    env.py
    versions/
  frontend/
    package.json
    vite.config.ts
    vitest.config.ts
    tsconfig.json
    nginx.conf                # production reverse proxy (compose mode)
    src/
      main.tsx
      App.tsx
      auth.ts                 # API-key bearer storage in localStorage
      state.ts                # screen state (URL params + React state)
      styles.css              # Tailwind v4 entry
      api/
        client.ts             # fetch wrapper consuming the generated types
        types.ts              # generated by openapi-typescript — do not edit
      components/
      __tests__/              # vitest
  tests/
    conftest.py               # testcontainers fixtures
    integration/
    unit/
  docker/
    api.Dockerfile
    workers.Dockerfile
    bot.Dockerfile
    frontend.Dockerfile
  docker-compose.yml          # local dev: postgres, neo4j, rustfs, api, workers, bot, frontend
  Makefile                    # nuke / up / migrate / test / regen-types / regen-types-static
  .github/
    workflows/
      ci.yml                  # ruff, pyright, pytest, frontend build, openapi diff check
      # nightly-live.yml      # deferred until a live OpenAI suite exists
  spec.md
  user_stories.md
  BUILD.md
  README.md
```

CLI entrypoints declared in `pyproject.toml` map to `pliny.cli:main`:

```
pliny api               # uvicorn pliny.api.app:create_app (factory=True)
pliny worker --pool fast
pliny worker --pool slow
pliny bot
```

Each runs as its own container/service in production.

## Local dev loop

`docker compose up` brings the full stack up. `Makefile` targets:

```
make up                 # docker compose up -d (postgres, neo4j, rustfs, api, workers, bot, frontend)
make nuke               # compose down -v && up && alembic upgrade head
make migrate            # alembic upgrade head
make test               # pytest
make lint               # ruff + pyright
make regen-types        # curl http://localhost:8000/openapi.json | npx openapi-typescript -o frontend/src/api/types.ts
make regen-types-static # same, but extracts the OpenAPI doc via create_app().openapi() — no live server needed
```

CI runs `regen-types-static` and fails if `frontend/src/api/types.ts` would change — keeps the FE in sync without booting uvicorn.

## Configuration

Single `.env` file at repo root (gitignored). `.env.example` is committed and documents every variable.

Required env vars (initial set; expand as needed):

```
# API
API_KEY=                          # bearer token for /v1/*
API_BIND_HOST=0.0.0.0
API_BIND_PORT=8000

# Postgres
DATABASE_URL=postgresql+psycopg://pliny:pliny@postgres:5432/pliny

# Neo4j
NEO4J_URI=bolt://neo4j:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=

# RustFS (S3-compatible)
RUSTFS_ENDPOINT=http://rustfs:9000
RUSTFS_ACCESS_KEY=
RUSTFS_SECRET_KEY=
RUSTFS_BUCKET=pliny

# OpenAI
OPENAI_API_KEY=
OPENAI_RPM=60
OPENAI_TPM=90000
OPENAI_DAILY_USD_CAP=20

# Embeddings
CURRENT_EMBEDDING_MODEL=text-embedding-3-small

# Telegram
TELEGRAM_BOT_TOKEN=
TELEGRAM_ALLOWED_USER_IDS=        # comma-separated

# Workers
FAST_WORKER_CONCURRENCY=4
SLOW_WORKER_CONCURRENCY=2
STAGE_TIMEOUT_SECONDS=900

# Embedded services (API process)
EMBED_FAST_WORKER=true            # API process runs the fast worker pool in-process
EMBED_BOT=true                    # API process runs the Telegram bot poller in-process
```

In production these are managed through Coolify's env-var UI; locally they live in `.env`.

## Process model

- **API**: one FastAPI/uvicorn process. `/v1/` prefix on every route. By default also runs (a) the fast worker pool as `FAST_WORKER_CONCURRENCY` asyncio tasks against `pool='fast'`, sharing a single SQLAlchemy/`psycopg` async connection pool, and (b) the Telegram bot's long-poll loop. Both are gated by `EMBED_FAST_WORKER` / `EMBED_BOT`; set either to `false` to run them as standalone processes via `pliny worker --pool fast` / `pliny bot`.
- **Slow worker pool**: separate process, `pool='slow'`, default concurrency 2. Stays in its own container so Playwright/snapshot work is isolated.
- **Bot**: see API. Standalone via `pliny bot` only when `EMBED_BOT=false`. In-memory `(item_id → message_ref)` map; restart loses pending edits (acceptable per spec).
- **Notify channels**: `job_pool_fast`, `job_pool_slow`. Each pool process holds one dedicated connection for `LISTEN`.

Each top-level process (api, slow-worker, frontend) is a separate Coolify service. Restart policy: `unless-stopped`.

## Ingest idempotency

`item_sources` has a partial unique index on `(source, source_ref) WHERE source_ref IS NOT NULL`. A retried Telegram message ID with the same `source` is a no-op insert. API ingest without `source_ref` continues to append (no idempotency key in v1).

## LLM cost cap

Postgres table `llm_spend_daily (date primary key, usd_spent numeric not null default 0)`. Every LLM call estimates its cost (prompt+completion tokens × pinned price) and `INSERT ... ON CONFLICT DO UPDATE SET usd_spent = usd_spent + excluded.usd_spent`. A pre-call check reads the row; if `>= OPENAI_DAILY_USD_CAP`, the wrapper raises `CostCapExceeded` and the worker treats it as a retryable error scheduled past midnight UTC. In-flight calls finish.

## Logging

Structured JSON to stdout. Every line includes:

```
ts, level, event, stage, item_id, attempt, latency_ms, error, claim_token
```

Shape is Sentry-ingestable (`level`, `event`, `error`) and Prometheus-friendly (latency_ms parseable). No tracing in v1.

## CI (GitHub Actions)

`.github/workflows/ci.yml` runs three jobs in parallel on push to `main` and on PRs:

1. **python**: `uv sync`, `ruff check`, `ruff format --check`, `pyright`, `pytest` (testcontainers spin up PG/Neo4j on the runner's Docker).
2. **frontend**: `npm ci`, `npm run lint`, `npm run build`, `npm run test`.
3. **openapi-drift**: `make regen-types-static` (extracts OpenAPI via `create_app().openapi()` directly — no uvicorn boot, no live DB), then `git diff --exit-code frontend/src/api/types.ts`.

`nightly-live.yml` is deferred until a live OpenAI test suite exists; running an empty cron workflow that fails every night is worse than not having one.

## Deployment (Coolify)

Coolify on a single VPS handles TLS, reverse proxy, env-var management, and scheduled Postgres backups. Two viable shapes:

- **Compose mode**: commit `docker-compose.yml` describing all services; Coolify wraps it. Closest to spec's "compose up" recipe; recommended.
- **App-per-service mode**: each Dockerfile becomes a Coolify application. Slightly more UI work; cleaner per-service deploys.

Reverse proxy routes:
- `/api/*` → API container (mapped to `/v1/*` server-side or app reads requests at `/api/v1/`; pick one and stick to it)
- `/*` → frontend static container (nginx serving Vite `dist/`)

Persistent volumes (Coolify-managed):
- `postgres_data` → `/var/lib/postgresql/data`
- `neo4j_data` → `/data`
- `rustfs_data` → RustFS data dir

Backups: enable Coolify's scheduled `pg_dump` once you start capturing data you'd miss. RustFS bucket replication can be added later via `rclone` cron in a sidecar; until then, raw bytes live only on the VPS volume.

## Open items (non-blocking)

- "Validated" criterion for embedding-model migration (S16) — manual: rerun ~10 canned queries against both indexes, compare top-10 overlap ≥ 0.8, then flip `CURRENT_EMBEDDING_MODEL`.
- Public-internet API rate limit — deferred; fine while access is over Tailscale or behind Coolify's auth. Add a per-IP token bucket if exposing publicly.
- Documentation discipline — README covers setup; spec.md is the architecture; this file is the toolchain. ADRs only when a decision genuinely warrants one.
