.PHONY: install run test coverage eval export schema-check lint typecheck audit verify clean

PYTHON ?= python

install:
	$(PYTHON) -m pip install -r requirements-dev.txt

run:
	$(PYTHON) -m uvicorn app.main:app --reload

test:
	$(PYTHON) -m pytest

coverage:
	$(PYTHON) -m pytest --cov=app --cov=evals --cov-branch --cov-fail-under=80

eval:
	$(PYTHON) evals/run_reference_evals.py

export:
	$(PYTHON) scripts/export_contracts.py

schema-check:
	$(PYTHON) scripts/export_contracts.py --check

lint:
	$(PYTHON) -m ruff check app evals tests scripts

typecheck:
	$(PYTHON) -m mypy

audit:
	$(PYTHON) -m pip_audit -r requirements.txt --progress-spinner off

verify: lint typecheck schema-check coverage audit

clean:
	rm -rf .pytest_cache htmlcov .coverage customer_service.db data
