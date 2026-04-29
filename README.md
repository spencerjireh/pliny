# Pliny

Personal knowledge base aggregator. See `spec.md` for architecture, `BUILD.md` for toolchain, `user_stories.md` for product goals.

## Quick start

```
cp .env.example .env
make sync        # uv sync
make browsers    # uv run playwright install chromium (snapshot stage)
make up          # docker compose up -d (postgres + neo4j)
make migrate     # alembic upgrade head
uv run pliny api &
uv run pliny worker --pool fast &
uv run pliny worker --pool slow &
```

## Make targets

- `make sync` — `uv sync`
- `make browsers` — `uv run playwright install chromium` (one-time per dev machine; needed for snapshot stage)
- `make up` / `make down` / `make nuke` — docker compose lifecycle
- `make migrate` — `alembic upgrade head`
- `make test` — `pytest`
- `make lint` — `ruff check`, `ruff format --check`, `pyright`
- `make fmt` — `ruff format`, `ruff check --fix`
- `make regen-types` — regenerate `frontend/src/api/types.ts` from the running API's OpenAPI schema (requires `pliny api` listening on `localhost:8000`)

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

The first time you load the SPA, you'll be prompted for the API key (`API_KEY` env value); it's stored in `localStorage` and a 401 clears it.
