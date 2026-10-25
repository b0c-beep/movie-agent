.PHONY: install dev-backend dev-frontend lint typecheck test test-e2e check

install:
	cd backend && uv sync --all-groups
	cd frontend && npm ci

dev-backend:
	cd backend && uv run uvicorn movie_agent.main:app --reload --port 8000 --env-file .env

dev-frontend:
	cd frontend && npm run dev

lint:
	cd backend && uv run ruff check .
	cd backend && uv run ruff format --check .
	cd frontend && npm run lint

typecheck:
	cd backend && uv run mypy
	cd frontend && npm run typecheck

test:
	cd backend && uv run pytest
	cd frontend && npm test

test-e2e:
	cd frontend && npm run test:e2e

check: lint typecheck test
