.PHONY: dev test lint up down

dev:
	uv run uvicorn agent_runtime.api.main:app --reload

test:
	uv run pytest -q

lint:
	uv run ruff check .

up:
	docker compose up --build

down:
	docker compose down -v
