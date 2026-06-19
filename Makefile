# =============================================================================
# Shopify Order Download
#
# Default targets (`make up`, `make down`, ...) use `.env`. The Makefile
# inspects POSTGRES_HOST in that env file:
#   - POSTGRES_HOST=postgres  -> layer in docker-compose.local-pg.yml
#                                (bundled postgres container)
#   - anything else (e.g. RDS endpoint) -> only the base compose runs
#
# Prod targets (`make up-prod`, `make down-prod`, ...) use `.env.prod`.
# =============================================================================

ENV ?= .env
COMPOSE_FILES := -f docker-compose.yml

# If the env file asks for the bundled `postgres` service hostname, layer the
# local-pg override on top. Tolerates trailing whitespace and CR.
LOCAL_PG := $(shell grep -E '^[[:space:]]*POSTGRES_HOST[[:space:]]*=[[:space:]]*postgres[[:space:]]*$$' $(ENV) 2>/dev/null)
ifneq ($(strip $(LOCAL_PG)),)
  COMPOSE_FILES += -f docker-compose.local-pg.yml
endif

DC := docker compose --env-file $(ENV) $(COMPOSE_FILES)

# Default service for `make logs SERVICE=...` etc.
SERVICE ?=

.PHONY: help up down restart build logs ps stats \
        up-prod down-prod restart-prod build-prod logs-prod ps-prod stats-prod \
        nuke nuke-prod env-check

help:
	@echo "Local (uses .env — auto-detects Postgres host):"
	@echo "  make up        - build and start the stack"
	@echo "  make down      - stop the stack (volumes kept)"
	@echo "  make restart   - restart all services"
	@echo "  make build     - rebuild images without starting"
	@echo "  make logs      - tail logs (SERVICE=puller for a single service)"
	@echo "  make ps        - show running containers"
	@echo "  make stats     - hit /stats on the local API"
	@echo "  make nuke      - down + delete all named volumes (DESTRUCTIVE)"
	@echo ""
	@echo "Prod (uses .env.prod, always with RDS):"
	@echo "  make up-prod / down-prod / restart-prod / build-prod"
	@echo "  make logs-prod / ps-prod / stats-prod / nuke-prod"
	@echo ""
	@echo "Diagnostics:"
	@echo "  make env-check - print which env file and compose files would be used"

env-check:
	@echo "ENV file       : $(ENV)"
	@echo "Compose files  : $(COMPOSE_FILES)"
	@if [ -n "$(LOCAL_PG)" ]; then \
	  echo "Postgres mode  : bundled container (local-pg override active)"; \
	else \
	  echo "Postgres mode  : external host (e.g. RDS) — no local-pg override"; \
	fi
	@echo "POSTGRES_HOST  : $$(grep -E '^[[:space:]]*POSTGRES_HOST[[:space:]]*=' $(ENV) 2>/dev/null | head -1 | cut -d= -f2-)"

# ---------- local ----------------------------------------------------------

up:
	$(DC) up -d --build

down:
	$(DC) down

restart:
	$(DC) restart $(SERVICE)

build:
	$(DC) build

logs:
	$(DC) logs -f --tail=200 $(SERVICE)

ps:
	$(DC) ps

stats:
	@curl -s http://localhost:8000/stats | python3 -m json.tool || echo "API not reachable on localhost:8000"

nuke:
	$(DC) down -v

# ---------- prod (RDS, .env.prod) -----------------------------------------

up-prod:
	$(MAKE) up ENV=.env.prod

down-prod:
	$(MAKE) down ENV=.env.prod

restart-prod:
	$(MAKE) restart ENV=.env.prod SERVICE=$(SERVICE)

build-prod:
	$(MAKE) build ENV=.env.prod

logs-prod:
	$(MAKE) logs ENV=.env.prod SERVICE=$(SERVICE)

ps-prod:
	$(MAKE) ps ENV=.env.prod

stats-prod:
	$(MAKE) stats ENV=.env.prod

nuke-prod:
	$(MAKE) nuke ENV=.env.prod
