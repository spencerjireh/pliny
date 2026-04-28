.PHONY: up down nuke migrate test lint fmt sync

sync:
	uv sync

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
