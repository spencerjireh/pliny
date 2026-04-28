# Pliny

Personal knowledge base aggregator. See `spec.md` for architecture, `BUILD.md` for toolchain, `user_stories.md` for product goals.

## Quick start

```
cp .env.example .env
make sync        # uv sync
make up          # docker compose up -d (postgres)
make migrate     # alembic upgrade head
uv run pliny api &
uv run pliny worker --pool fast &
```

## Make targets

- `make sync` — `uv sync`
- `make up` / `make down` / `make nuke` — docker compose lifecycle
- `make migrate` — `alembic upgrade head`
- `make test` — `pytest`
- `make lint` — `ruff check`, `ruff format --check`, `pyright`
- `make fmt` — `ruff format`, `ruff check --fix`
