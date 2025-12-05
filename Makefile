.PHONY: help test typecheck test-packages typecheck-packages check-names

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

test: test-packages  ## Run all tests

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

check-names:
	@# If we are in gptme-contrib, we should have no agent/instance/user-specific names, and vice versa
	@# Exclusions:
	@#   - Makefile, fork.sh, fork.py: forking scripts need agent names
	@#   - analyze-lesson-usage.py: needs configurable agent name list
	@if [ "$(shell basename $(CURDIR))" = "gptme-contrib" ]; then \
		! git grep -i "bob\|alice\|erik@|@gmail" -- ':!Makefile' ':!fork.sh' ':!scripts/fork.py' ':!packages/lessons/src/lessons/analyze-lesson-usage.py'; \
	fi
