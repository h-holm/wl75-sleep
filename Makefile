# Install, inspect and remove the LaunchAgent that runs `wl75 tick`, and run all checks.

LABEL := local.wl75-sleep
TEMPLATE := launchd/$(LABEL).plist.template
PLIST := $(HOME)/Library/LaunchAgents/$(LABEL).plist
DOMAIN := gui/$(shell id -u)
PYTHON := $(CURDIR)/.venv/bin/python
LOG_DIR := $(HOME)/Library/Logs/wl75-sleep
LOG_FILE := $(LOG_DIR)/wl75-sleep.log

CONFIG ?= $(CURDIR)/config.toml
STATE ?= $(HOME)/Library/Application Support/wl75-sleep/state.json
TICK_SECONDS ?= 300

.PHONY: install uninstall status logs check

install:
	uv sync --frozen --inexact  # --inexact keeps the development tools in the same .venv.
	mkdir -p "$(LOG_DIR)" "$(dir $(PLIST))"
	sed -e 's|@PYTHON@|$(PYTHON)|g' \
	    -e 's|@REPOSITORY@|$(CURDIR)|g' \
	    -e 's|@CONFIG@|$(CONFIG)|g' \
	    -e 's|@STATE@|$(STATE)|g' \
	    -e 's|@LOG_FILE@|$(LOG_FILE)|g' \
	    -e 's|@LOG_DIR@|$(LOG_DIR)|g' \
	    -e 's|@TICK_SECONDS@|$(TICK_SECONDS)|g' \
	    "$(TEMPLATE)" > "$(PLIST)"
	plutil -lint "$(PLIST)"
	-launchctl bootout "$(DOMAIN)/$(LABEL)" 2>/dev/null
	launchctl bootstrap "$(DOMAIN)" "$(PLIST)"
	@echo "Installed: a tick every $(TICK_SECONDS) s. Check it with: uv run src/wl75_sleep/main.py doctor"

uninstall:
	-launchctl bootout "$(DOMAIN)/$(LABEL)"
	rm -f "$(PLIST)"

status:
	@launchctl print "$(DOMAIN)/$(LABEL)" | grep -E '^\s*(state|runs|last exit code|run interval|program) =' \
	    || echo "$(LABEL) is not loaded."

logs:
	@tail -n 50 "$(LOG_FILE)"

check:
	uv run --group lint ruff check
	uv run --group lint ruff format --check
	uv run --group lint ty check
	uv run --group test pytest
