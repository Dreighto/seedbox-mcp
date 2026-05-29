set shell := ["zsh", "-cu"]

setup:
    uv sync

run:
    uv run python -m whatbox_media_mcp.server

test:
    uv run --extra dev pytest

lint:
    uv run --extra dev ruff check .

format:
    uv run --extra dev ruff format .

check:
    uv run --extra dev ruff check .
    uv run --extra dev mypy src
    uv run --extra dev pytest

smoke:
    scripts/healthcheck.sh
