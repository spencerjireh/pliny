# Pliny

Personal knowledge base aggregator. See `spec.md` for architecture, `BUILD.md` for toolchain, `user_stories.md` for product goals.

## Quick start (compose mode — recommended)

Brings up the whole stack — postgres, neo4j, rustfs, api, fast/slow workers, bot, and frontend nginx — in containers:

```
cp .env.example .env             # edit secrets: API_KEY, NEO4J_PASSWORD, OPENAI_API_KEY, TELEGRAM_BOT_TOKEN
docker compose up -d --build     # `make up` skips --build; use --build first time
```

The frontend serves on `http://localhost`, reverse-proxying `/v1/*`, `/healthz`, and `/metrics` to the api container. Migrations run automatically on api startup.

## Quick start (host mode — for active development)

Run only the data services in docker, run the Python processes on the host:

```
cp .env.example .env
make sync        # uv sync
make browsers    # uv run playwright install chromium (snapshot stage)
docker compose up -d postgres neo4j rustfs
make migrate     # alembic upgrade head
uv run pliny api &
uv run pliny worker --pool fast &
uv run pliny worker --pool slow &
uv run pliny bot &
cd frontend && npm install && npm run dev    # http://localhost:5173
```

## Make targets

- `make sync` — `uv sync`
- `make browsers` — `uv run playwright install chromium` (one-time per dev machine; needed for snapshot stage)
- `make up` / `make down` / `make nuke` — docker compose lifecycle (full stack)
- `make migrate` — `alembic upgrade head`
- `make test` — `pytest`
- `make lint` — `ruff check`, `ruff format --check`, `pyright`
- `make fmt` — `ruff format`, `ruff check --fix`
- `make regen-types` — regenerate `frontend/src/api/types.ts` from a running API's OpenAPI schema (requires `pliny api` listening on `localhost:8000`)
- `make regen-types-static` — same regeneration via `create_app().openapi()` directly; no running server needed. CI uses this path and fails on a diff against the committed copy.

## Frontend (dev)

The React viewer lives in `frontend/`. Tooling: Vite + React 18 + TypeScript (strict) + Tailwind v4.

```
cd frontend
npm install
npm run dev          # http://localhost:5173, proxies /v1/* to localhost:8000
npm run lint         # eslint
npm run build        # tsc + vite build
npm run test         # vitest
```

In dev, the Vite server proxies `/v1/*` to `http://localhost:8000`, so the SPA never makes a cross-origin request and CORS stays unconfigured. To call the API directly from the browser instead (e.g. when running on a non-local API host), set `CORS_ALLOWED_ORIGINS=http://localhost:5173` in the API's environment.

In production (compose mode), nginx in the `frontend` container serves the built SPA on port 80 and reverse-proxies `/v1`, `/healthz`, and `/metrics` to the `api` service on the compose network — same-origin, CORS unconfigured.

The first time you load the SPA, you'll be prompted for the API key (`API_KEY` env value); it's stored in `localStorage` and a 401 clears it.

## Operations

- **`GET /metrics`** — Prometheus exposition (queue depth by pool, jobs by status, stage lag, error counts). Unauthenticated, mounted at root. Restrict via reverse-proxy IP allowlist if you expose it publicly.
- **Admin endpoints** (all require the bearer key, prefixed `/v1/admin/`):
  - `GET /jobs?status=&stage=&limit=` — recent jobs, failures first.
  - `POST /jobs/:id/retry` — reset a failed job to pending.
  - `POST /reprocess?stage=X` — bulk reset for items below the current `STAGE_VERSIONS` constant. Bump the constant in code first, then call this.
  - `POST /items/:id/reprocess?stage=X` — re-run one stage on one item (no version bump).
  - `POST /rebuild_graph` — drop and rebuild Neo4j from Postgres.
