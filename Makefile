setup:
	uv sync

lint:
	uv run ruff check .
