PY ?= python3
PIP ?= $(PY) -m pip

.PHONY: update install test

# Rebuild after pulling changes from another machine.
update:
	git pull --ff-only
	$(PIP) install -e .
	$(MAKE) test

# Editable install: code changes are live, this only refreshes metadata/entry points.
install:
	$(PIP) install -e .

test:
	$(PY) -m unittest discover -s tests -q
