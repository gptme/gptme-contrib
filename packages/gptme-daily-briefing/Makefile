.PHONY: test typecheck

test:
	uv run pytest tests/ -v

typecheck:
	uv run --with mypy --with types-PyYAML mypy -p gptme_daily_briefing --ignore-missing-imports
