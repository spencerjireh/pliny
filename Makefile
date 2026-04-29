.PHONY: up down nuke migrate test lint fmt sync browsers regen-types regen-types-static

sync:
	uv sync

browsers:
	uv run playwright install chromium

regen-types:
	@curl -fsS http://localhost:8000/openapi.json \
	  | npx --yes openapi-typescript@^7 -o frontend/src/api/types.ts

# CI-friendly variant: extracts the OpenAPI document directly from the FastAPI
# app via Python. Doesn't need a running server or a live database.
regen-types-static:
	@uv run python -c "import json; from pliny.api.app import create_app; print(json.dumps(create_app().openapi()))" \
	  | npx --yes openapi-typescript@^7 -o frontend/src/api/types.ts

up:
	docker compose up -d

down:
	docker compose down

nuke:
	docker compose down -v
	docker compose up -d
	$(MAKE) migrate

migrate:
	uv run alembic upgrade head

test:
	uv run pytest

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run pyright

fmt:
	uv run ruff format .
	uv run ruff check --fix .
