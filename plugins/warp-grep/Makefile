.PHONY: help test typecheck

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

test:  ## Run tests
	uv run pytest tests/ -v

typecheck:  ## Run type checking
	uv run mypy src/ --ignore-missing-imports
