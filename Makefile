PYTHON ?= python3
VENV ?= .venv
PACKAGE = lucid_component_knx

.PHONY: help setup-venv test build clean

help:
	@echo "lucid-component-knx"
	@echo "  make setup-venv  - Create .venv, install project + deps"
	@echo "  make test        - Run unit tests"
	@echo "  make build       - Build wheel and sdist"
	@echo "  make clean       - Remove build artifacts"

setup-venv:
	@test -d $(VENV) || ($(PYTHON) -m venv $(VENV) && echo "Created $(VENV).")
	@$(VENV)/bin/pip install -q -e ".[dev]"
	@$(VENV)/bin/pip install -q build pytest-cov
	@echo "Ready. Run 'make test' or 'make build'."

test:
	@$(VENV)/bin/python -m pytest tests/ -v -q

build:
	@test -d $(VENV) || (echo "Run 'make setup-venv' first." && exit 1)
	@$(VENV)/bin/python -m build

clean:
	@rm -rf build/ dist/ *.egg-info src/*.egg-info
	@rm -rf .pytest_cache .coverage htmlcov/
