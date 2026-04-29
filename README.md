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

## Configuration

`.env` (copied from `.env.example`) holds every secret and tunable. Inline comments in `.env.example` explain each variable. The two integrations that need an external setup step are OpenAI and Telegram.

### OpenAI

Pliny calls OpenAI for summarize, chunk-embed, entities, and image extract (vision). Without `OPENAI_API_KEY` the pipeline will fail at those stages.

1. Create a key at <https://platform.openai.com/api-keys>. Paste it into `OPENAI_API_KEY=`.
2. Match `OPENAI_RPM` / `OPENAI_TPM` to your account-level rate limits (see <https://platform.openai.com/account/limits>). The LLM wrapper enforces these as a token bucket so concurrent workers don't bust the limit.
3. `OPENAI_DAILY_USD_CAP` is a hard daily spend cap. Each call estimates its cost from token counts and a pinned price; when the day's spend hits the cap, calls raise `CostCapExceeded` and the worker reschedules them past midnight UTC. In-flight calls finish. Default is $20 — adjust to taste; resets implicitly each UTC day.
4. `CURRENT_EMBEDDING_MODEL` selects the active embedding model. Renaming this kicks off a new column in the embeddings table for the new model; see user story S16 for the migration ritual (rerun canned queries against both indexes, top-10 overlap ≥ 0.8, then flip).

### Telegram

The bot is a long-polling adapter: forward a message (text, link, photo, document, voice/video/audio) to your bot, it replies with an item id, edits the message with a summary once the pipeline finishes, and follows a `redirect_to` if a snapshot-time merge happened.

1. Create a bot via [@BotFather](https://t.me/BotFather): `/newbot`, pick a name, pick a username ending in `bot`. BotFather hands you a token like `123456:ABC-DEF...`. Paste it into `TELEGRAM_BOT_TOKEN=`.
2. Find your numeric Telegram user ID by messaging [@userinfobot](https://t.me/userinfobot) (it replies with `Id: 12345678`). Put that into `TELEGRAM_ALLOWED_USER_IDS=12345678`. Multiple users: comma-separated.
3. In compose mode, the bot container is already configured to reach the API at `http://api:8000`. In host mode, leave `PLINY_API_BASE_URL=http://localhost:8000` and run `uv run pliny bot` alongside the api process.
4. Once running, message the bot. You should see a reply with `Captured! item_id=…` within a few seconds, then an edit with the summary as the pipeline finishes. The bot rejects any user not in the allowlist.

## Verifying the setup

After `docker compose up -d --build`:

1. `curl http://localhost/healthz` returns `{"ok": true}`.
2. `curl http://localhost/metrics` returns Prometheus exposition with `pliny_processing_jobs{...}` lines.
3. Open `http://localhost`, paste your `API_KEY` when prompted, and you land on the search screen with no items.
4. POST a quick item: `curl -X POST -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" http://localhost/v1/items -d '{"text": "hello pliny", "source": "api", "source_ref": "smoke-1"}'` — returns 202 with an item id.
5. Refresh the SPA — the item appears, and within ~30s the title and summary are filled in. Expand it for matching chunks.
6. Send a message to your Telegram bot — you should see an ack reply with the item id, an edit with the summary shortly after, and the same item visible in the SPA.

## Operations

- **`GET /metrics`** — Prometheus exposition (queue depth by pool, jobs by status, stage lag, error counts). Unauthenticated, mounted at root. Restrict via reverse-proxy IP allowlist if you expose it publicly.
- **Admin endpoints** (all require the bearer key, prefixed `/v1/admin/`):
  - `GET /jobs?status=&stage=&limit=` — recent jobs, failures first.
  - `POST /jobs/:id/retry` — reset a failed job to pending.
  - `POST /reprocess?stage=X` — bulk reset for items below the current `STAGE_VERSIONS` constant. Bump the constant in code first, then call this.
  - `POST /items/:id/reprocess?stage=X` — re-run one stage on one item (no version bump).
  - `POST /rebuild_graph` — drop and rebuild Neo4j from Postgres.
