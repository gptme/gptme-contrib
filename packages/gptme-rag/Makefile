test:
	uv run pytest tests/ -v -m "not slow"

test-all:
	uv run pytest tests/ -v

typecheck:
	uv run --with mypy mypy src/ --ignore-missing-imports
