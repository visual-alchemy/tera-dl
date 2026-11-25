PY ?= python3
VENV ?= .venv
PIP ?= $(PY) -m pip

.PHONY: update install test venv

# Rebuild after pulling changes from another machine.
update:
	git pull --ff-only
	$(PIP) install -e .
	$(MAKE) test

# Editable install: code changes are live, this only refreshes metadata/entry points.
install:
	$(PIP) install -e .

# Use this when the system Python is not writable (Debian/Ubuntu, PEP 668).
venv:
	$(PY) -m venv $(VENV)
	$(VENV)/bin/python -m pip install -U pip setuptools wheel
	$(VENV)/bin/python -m pip install -e .
	@echo "Now run: source $(VENV)/bin/activate"

test:
	$(PY) -m unittest discover -s tests -q
