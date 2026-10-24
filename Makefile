.PHONY: install preflight up down ps logs shell pull upgrade-native help

# The compose file lives in config/, not at the repo root, and its env file
# sits beside it. Every target goes through these two flags so that `make`
# works from the repo root — a bare `docker compose` there finds no file.
COMPOSE := docker compose -f config/docker-compose.yml --env-file config/.env

## install: Run preflight checks, then pull and start the container.
install:
	./bin/preflight.sh && $(COMPOSE) pull && $(COMPOSE) up -d

## preflight: Run host state checks without starting anything.
preflight:
	./bin/preflight.sh

## up: Start (or recreate) the container in the background.
up:
	$(COMPOSE) up -d

## down: Stop the container, letting agents finalize their sessions first.
down:
	$(COMPOSE) down

## ps: Show container status.
ps:
	$(COMPOSE) ps

## logs: Follow the container log.
logs:
	$(COMPOSE) logs -f

## shell: Open a shell inside the running container.
shell:
	$(COMPOSE) exec karakos bash

## pull: Fetch a newer image without starting anything.
pull:
	$(COMPOSE) pull

## upgrade-native: (native mode) fast-forward the checkout, refresh venv and dashboard, restart the units. KARAKOS_REF=vX.Y.Z picks a tag.
upgrade-native:
	./bin/native-upgrade.sh

## help: List available targets.
help:
	@grep -E '^## ' Makefile | sed 's/^## //'
