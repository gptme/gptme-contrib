.PHONY: test typecheck install

test:
	uv run --extra dev pytest tests/ -v

typecheck:
	uv run mypy src/

install:
	uv pip install -e .
