set shell := ["bash", "-uc"]

setup:
    uv sync --all-extras
    cd web && pnpm install

api:
    uv run uvicorn shadow_mdc.api:app --reload --host 127.0.0.1 --port 8000

web:
    cd web && pnpm dev

dev:
    just --parallel api web

test:
    uv run pytest

lint:
    uv run ruff check src tests
    uv run mypy

check: lint test
    cd web && pnpm check

build:
    cd web && pnpm build
