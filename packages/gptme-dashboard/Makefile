.PHONY: test typecheck

test:
	uv run pytest tests/ -v

typecheck:
	uv run --with mypy --with types-PyYAML mypy src/ --ignore-missing-imports
