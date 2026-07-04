# ACP — operator command interface.
# This Makefile is a THIN menu: every target is a one-line delegate to the
# `agentctl` Python CLI, to docker compose, or to a script under scripts/.
# No business logic lives here. `make` with no argument prints this help.
#
# Env overrides thread through without new targets, e.g.:
#   make up MODEL_BACKEND=claude
#   make eval SANDBOX_RUNNER=go

.DEFAULT_GOAL := help

# --- knobs (overridable on the command line) ---------------------------------
MODEL_BACKEND  ?= stub
SANDBOX_RUNNER ?= python
GATEWAY_PORT   ?= 8000
LIVE           ?= 0
TASK           ?=
SANDBOX_IMAGE  ?= acp-sandbox:latest
SOURCE         ?= ./sample_repo
PATCH          ?= good

COMPOSE := docker compose
# `env -u VIRTUAL_ENV` strips any inherited venv so poetry binds to THIS project's
# ./.venv (an inherited VIRTUAL_ENV on some machines otherwise shadows it).
RUN     := env -u VIRTUAL_ENV poetry run
# The ONE operator entrypoint to the `docker` CLI. All build/clean docker logic
# lives in scripts/sandbox.sh; the Makefile never calls `docker build/run/rm`
# inline (only `docker compose` for the service lifecycle). Invoked via `bash`
# so it needs no executable bit.
SANDBOX := bash scripts/sandbox.sh
# Trailing cleanup appended to every Docker-touching target. `; status=$$?` first
# captures the recipe's real exit code, THEN we clean leaked sandbox containers
# (best-effort, never fatal), THEN re-exit with the original status — so a failed
# test still fails, but never leaves leaked containers behind.
CLEAN_TRAP := ; status=$$?; $(SANDBOX) clean $(SANDBOX_IMAGE); exit $$status

# Export so both compose and agentctl see the selected backend/runner.
export ACP_MODEL_BACKEND  = $(MODEL_BACKEND)
export ACP_SANDBOX_RUNNER = $(SANDBOX_RUNNER)

.PHONY: help install sandbox-build sandbox-build-go sandbox-build-rust sandbox-build-ts \
        sandbox-clean sandbox-verify migrate seed \
        up up-claude down clean logs ps health fmt lint typecheck shell test \
        test-unit test-integration test-e2e test-smoke test-regression test-parity test-docker \
        test-coverage \
        eval eval-task eval-docker redteam demo-happy demo-happy-docker demo-resume demo-budget \
        metrics trace

help: ## Show this help (auto-generated from target comments)
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z0-9_-]+:.*?## / {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

# --- setup -------------------------------------------------------------------
install: ## Install deps (poetry) and build the sandbox runner image
	poetry install
	$(SANDBOX) build $(SANDBOX_IMAGE)

sandbox-build: ## Build just the sandbox runner image (Docker required)
	$(SANDBOX) build $(SANDBOX_IMAGE)

sandbox-build-go: ## Build the Go sandbox runner image (Phase 8)
	$(SANDBOX) build acp-sandbox-go:latest

sandbox-build-rust: ## Build the Rust sandbox runner image (Phase 8)
	$(SANDBOX) build acp-sandbox-rust:latest

sandbox-build-ts: ## Build the TypeScript sandbox runner image (Phase 8)
	$(SANDBOX) build acp-sandbox-ts:latest

sandbox-clean: ## Remove leaked acp-sandbox containers (best-effort; safe if Docker is down)
	$(SANDBOX) clean $(SANDBOX_IMAGE)

sandbox-verify: ## Verify a patch in the sandbox: make sandbox-verify SOURCE=./sample_repo PATCH=good
	@$(RUN) agentctl sandbox verify --source $(SOURCE) --patch $(PATCH) $(CLEAN_TRAP)

migrate: ## Apply SQLite schema/migrations (WAL)
	$(RUN) agentctl migrate

seed: ## Load demo user + hashed API key (prints token once)
	$(RUN) agentctl seed

# --- lifecycle ---------------------------------------------------------------
up: ## Boot the whole stack in stub mode, keyless (hard requirement #8)
	$(COMPOSE) up --build

up-claude: ## Boot the stack with the Model Gateway pointed at Claude (needs .env key)
	$(COMPOSE) up --build

down: ## Tear down, preserve volumes
	$(COMPOSE) down

clean: ## Tear down + wipe volumes/artifacts + leaked sandbox containers (fresh state)
	$(COMPOSE) down -v
	$(SANDBOX) clean $(SANDBOX_IMAGE)
	rm -rf var artifacts

logs: ## Open the observability dashboard (LIVE=1 for auto-refresh); tail logs
	@echo "Dashboard: http://localhost:$(GATEWAY_PORT)/dashboard$(if $(filter 1,$(LIVE)),?live=1,)"
	@if command -v open >/dev/null 2>&1; then \
	  open "http://localhost:$(GATEWAY_PORT)/dashboard$(if $(filter 1,$(LIVE)),?live=1,)"; \
	elif command -v xdg-open >/dev/null 2>&1; then \
	  xdg-open "http://localhost:$(GATEWAY_PORT)/dashboard$(if $(filter 1,$(LIVE)),?live=1,)"; \
	fi
	$(COMPOSE) logs -f gateway

ps: ## Show service status
	$(COMPOSE) ps

health: ## Hit /healthz and /readyz on the running gateway
	@curl -fsS http://localhost:$(GATEWAY_PORT)/healthz && echo
	@curl -fsS http://localhost:$(GATEWAY_PORT)/readyz && echo

# --- develop -----------------------------------------------------------------
fmt: ## Format code (ruff format)
	$(RUN) ruff format src tests

lint: ## Lint (ruff)
	$(RUN) ruff check src tests

typecheck: ## Type-check (mypy)
	$(RUN) mypy

shell: ## Dev shell into the control-plane container
	$(COMPOSE) run --rm gateway /bin/bash

# --- test (full pyramid, individually runnable) ------------------------------
test: ## Run the entire test suite (auto-cleans leaked sandbox containers after)
	@$(RUN) pytest $(CLEAN_TRAP)

test-unit: ## Unit tests only
	$(RUN) pytest -m unit

test-integration: ## Integration tests only (auto-cleans leaked sandbox containers after)
	@$(RUN) pytest -m integration $(CLEAN_TRAP)

test-e2e: ## End-to-end tests only (auto-cleans leaked sandbox containers after)
	@$(RUN) pytest -m e2e $(CLEAN_TRAP)

test-smoke: ## Smoke tests only (health endpoints)
	$(RUN) pytest -m smoke

test-regression: ## Regression tests only
	$(RUN) pytest -m regression

test-parity: ## Phase-8 polyglot parity tests (requires sandbox images for go/rust/ts)
	@$(RUN) pytest -m parity $(CLEAN_TRAP)

test-docker: sandbox-build ## Docker-gated tests, forced (A8): FAIL — not skip — if a buildable image is missing
	@ACP_REQUIRE_DOCKER=1 $(RUN) pytest -m docker $(CLEAN_TRAP)

test-coverage: ## Run the suite with coverage and print the number (no fail-under gate)
	$(RUN) pytest --cov=src/acp --cov-report=term-missing

# --- eval & demo (the proof surface) -----------------------------------------
eval: ## Run all eval tasks in stub mode against oracles (FAKE sandbox — see eval-docker for the real proof)
	$(RUN) agentctl eval

eval-task: ## Run a single eval task: make eval-task TASK=<id>
	$(RUN) agentctl eval --task $(TASK)

eval-docker: sandbox-build ## Run all eval tasks against the REAL Docker sandbox (the true build+test proof, A8)
	@ACP_EVAL_SANDBOX=docker $(RUN) agentctl eval $(CLEAN_TRAP)

redteam: ## Run injection + secret-exfil defense tasks
	$(RUN) agentctl redteam

demo-happy: ## Scripted happy-path run (walkthrough; FAKE sandbox)
	$(RUN) agentctl demo-happy

demo-happy-docker: sandbox-build ## Happy-path run against the REAL Docker sandbox (A8)
	@ACP_EVAL_SANDBOX=docker $(RUN) agentctl demo-happy $(CLEAN_TRAP)

demo-resume: ## Kill mid-task, show crash-resume with no double-charge
	$(RUN) agentctl demo-resume

demo-budget: ## Trigger budget exhaustion, show clean stop + partial
	$(RUN) agentctl demo-budget

# --- operate -----------------------------------------------------------------
metrics: ## Dump /metrics from the running gateway
	@curl -fsS http://localhost:$(GATEWAY_PORT)/metrics

trace: ## Print a run's journal: make trace TASK=<id>
	$(RUN) agentctl trace $(TASK)
