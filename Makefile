.PHONY: install preflight up down ps logs shell pull upgrade-native install-upgrade-agent uninstall-upgrade-agent agent-check help

# The compose file lives in config/, not at the repo root, and its env file
# sits beside it. Every target goes through these two flags so that `make`
# works from the repo root — a bare `docker compose` there finds no file.
COMPOSE := docker compose -f config/docker-compose.yml --env-file config/.env

## install: Run preflight checks, pull and start the container, then install the host upgrade agent.
install:
	./bin/preflight.sh && $(COMPOSE) pull && $(COMPOSE) up -d
	@$(MAKE) --no-print-directory install-upgrade-agent || echo "warning: host upgrade agent not installed; run 'make install-upgrade-agent' later"

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

# Host cron entry for bin/upgrade-agent.sh (the host half of bin/self-upgrade.sh).
# The line is matched by its "cd <checkout> && ./bin/upgrade-agent.sh" part, so
# install is idempotent per checkout and uninstall removes only this checkout's line.
UPGRADE_AGENT_CMD := cd $(CURDIR) && ./bin/upgrade-agent.sh
UPGRADE_AGENT_LINE := * * * * * $(UPGRADE_AGENT_CMD) >/dev/null 2>&1

## install-upgrade-agent: Add the per-minute host cron job that lets agents upgrade this install (bin/self-upgrade.sh). Idempotent.
install-upgrade-agent:
	@command -v crontab >/dev/null 2>&1 || { echo "crontab not found; run '$(UPGRADE_AGENT_CMD)' every minute from your own scheduler" >&2; exit 1; }
	@if crontab -l 2>/dev/null | grep -qF -- '$(UPGRADE_AGENT_CMD) '; then \
		echo "host upgrade agent already installed in this user's crontab"; \
	else \
		{ crontab -l 2>/dev/null || true; echo '$(UPGRADE_AGENT_LINE)'; } | crontab - && \
		echo "installed host upgrade agent: $(UPGRADE_AGENT_LINE)"; \
	fi

## uninstall-upgrade-agent: Remove the host cron job added by install-upgrade-agent.
uninstall-upgrade-agent:
	@command -v crontab >/dev/null 2>&1 || { echo "crontab not found; nothing to remove" >&2; exit 1; }
	@if crontab -l 2>/dev/null | grep -qF -- '$(UPGRADE_AGENT_CMD) '; then \
		crontab -l 2>/dev/null | grep -vF -- '$(UPGRADE_AGENT_CMD) ' | crontab - && \
		echo "removed host upgrade agent from this user's crontab"; \
	else \
		echo "host upgrade agent not installed; nothing to remove"; \
	fi

## agent-check: Run the capability manifest's programmatic checks (tests/capabilities); exit 1 on any FAIL.
agent-check:
	python3 bin/agent-check.py

## help: List available targets.
help:
	@grep -E '^## ' Makefile | sed 's/^## //'
