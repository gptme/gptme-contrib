.PHONY: help test typecheck test-packages typecheck-packages check-names

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

test: test-packages test-integration  ## Run all tests

test-integration:  ## Run integration tests (git hooks, etc.)
	@echo "Running integration tests..."
	uv run --with pytest pytest tests/integration/ -v

typecheck: typecheck-packages  ## Run all type checks

test-packages:  ## Run tests for all packages
	@echo "Running tests for all packages..."
	@for pkg in packages/*/; do \
		if [ -f "$$pkg/Makefile" ]; then \
			echo "\n=== Testing $$(basename $$pkg) ==="; \
			$(MAKE) -C "$$pkg" test || failed=1; \
		fi \
	done; \
	if [ -n "$$failed" ]; then exit 1; fi

typecheck-packages:  ## Run mypy for all packages
	@echo "Running typecheck for all packages..."
	@for pkg in packages/*/; do \
		if [ -f "$$pkg/Makefile" ]; then \
			echo "\n=== Typechecking $$(basename $$pkg) ==="; \
			$(MAKE) -C "$$pkg" typecheck || failed=1; \
		fi \
	done; \
	if [ -n "$$failed" ]; then exit 1; fi

check-names:  ## Validate naming patterns (no instance names in template)
	@bash scripts/precommit/check-names.sh
