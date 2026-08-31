# Convenience wrapper so the correct invocation is the easy one.
#
# The virtualenv deliberately lives OUTSIDE the project directory. This project
# sits under ~/Documents, which macOS syncs to iCloud Drive, and a venv there
# gets damaged: iCloud evicts and restores thousands of small files, and the
# result is broken symlinks, missing executables and minute-long start-ups.
# Keeping the environment at ~/.venvs/fin-asst avoids all of it.

VENV ?= $(HOME)/.venvs/fin-asst
PY   := $(VENV)/bin/python
BIN  := $(VENV)/bin/finasst
export UV_PROJECT_ENVIRONMENT = $(VENV)

.PHONY: help sync serve test check import-samples doctor clean-venv

help:
	@echo "make sync      install dependencies into $(VENV)"
	@echo "make serve     run the web app on http://127.0.0.1:8000"
	@echo "make test      run the test suite"
	@echo "make check     coverage report: how much of your money the app can see"
	@echo "make doctor    diagnose a broken environment"
	@echo "make clean-venv  delete and rebuild the environment from scratch"

sync:
	uv sync

serve: sync
	$(BIN) serve

test: sync
	$(VENV)/bin/pytest -q

check: sync
	$(BIN) coverage
	$(BIN) summary

doctor:
	@echo "project:      $(CURDIR)"
	@echo "venv:         $(VENV)"
	@echo -n "interpreter:  "; [ -x "$(PY)" ] && $(PY) -V || echo "MISSING -- run: make clean-venv"
	@echo -n "finasst:      "; $(PY) -c "import finasst; print(finasst.__file__)" 2>&1 | tail -1
	@echo -n "database:     "; $(PY) -c "from finasst import config; print(config.DB_PATH, '(exists)' if config.DB_PATH.exists() else '(not created yet)')" 2>&1 | tail -1
	@if [ -d .venv ]; then echo "WARNING: a .venv exists inside the project (iCloud will damage it). Remove it: rm -rf .venv"; fi

clean-venv:
	rm -rf "$(VENV)" .venv
	uv sync
	@$(MAKE) doctor
