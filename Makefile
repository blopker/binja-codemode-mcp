BN_USER_DIR ?= $(HOME)/Library/Application Support/Binary Ninja
PLUGINS_DIR := $(BN_USER_DIR)/plugins
LINK        := $(PLUGINS_DIR)/binja_codemode_mcp
DRIVER_DIR  := scratch/driver
SOURCE      := $(CURDIR)/src/binja_codemode_mcp

.DEFAULT_GOAL := help
.PHONY: help setup test lint fmt typecheck install uninstall status driver clean

help: ## Show this help
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk -F':.*?## ' '{printf "  \033[1m%-12s\033[0m %s\n", $$1, $$2}'

setup: ## Install dev dependencies
	uv sync

test: lint typecheck ## Everything that gates a commit
	uv run pytest -v

lint: ## Check formatting and lint rules
	uv run ruff check .
	uv run ruff format --check .

fmt: ## Apply autofixes and format
	uv run ruff check --fix .
	uv run ruff format .

typecheck: ## Type-check against the real Binary Ninja API
	uv run ty check src tests

install: ## Symlink the plugin into Binary Ninja
	@mkdir -p "$(PLUGINS_DIR)"
	@if [ -e "$(LINK)" ] && [ ! -L "$(LINK)" ]; then \
		echo "Refusing to replace $(LINK): it exists and is not a symlink."; \
		exit 1; \
	fi
	@ln -sfn "$(SOURCE)" "$(LINK)"
	@echo "Linked $(LINK) -> $(SOURCE)"
	@echo "Restart Binary Ninja, then: Plugins > Code Mode MCP > Start Server"

uninstall: ## Remove the symlink
	@if [ -L "$(LINK)" ]; then rm "$(LINK)" && echo "Removed $(LINK)"; \
	else echo "Not linked: $(LINK)"; fi

status: ## Show install state and whether the server is reachable
	@if [ -L "$(LINK)" ]; then echo "linked:   $$(readlink "$(LINK)")"; \
	else echo "linked:   no  (run 'make install')"; fi
	@if curl -sf -o /dev/null -X POST http://127.0.0.1:42069/mcp \
		-H 'Content-Type: application/json' -d '{}' 2>/dev/null; then \
		echo "endpoint: responding on 127.0.0.1:42069"; \
	elif curl -s -o /dev/null -w '%{http_code}' -X POST \
		http://127.0.0.1:42069/mcp 2>/dev/null | grep -q 401; then \
		echo "endpoint: up (401 without a token, as expected)"; \
	else \
		echo "endpoint: not responding — is the server started in Binary Ninja?"; \
	fi

driver: ## Run the live driver plan in a fresh Claude session
	@# Setup the test folder
	@rm -rf "$(DRIVER_DIR)" && mkdir -p "$(DRIVER_DIR)"
	@cp /bin/ls "$(DRIVER_DIR)/ls-a"
	@cp /bin/ls "$(DRIVER_DIR)/ls-b"
	@echo "Prepared $(DRIVER_DIR)/ls-a and ls-b. Close, then open both in Binary Ninja."
	@# Low effort: the plan states every case, so the work is following it rather
	@# than deciding what to do, and a run is dozens of calls long.
	claude --effort low "Run the live driver plan in @tests/driver_plan.md"

clean: ## Remove caches
	@find . -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
	@rm -rf .pytest_cache .ruff_cache
	@echo "Cleaned."
