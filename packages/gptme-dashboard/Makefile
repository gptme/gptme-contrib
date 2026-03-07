.PHONY: test typecheck

test:
	uv run --with flask pytest tests/ -v

typecheck:
	uv run mypy src/ --ignore-missing-imports
