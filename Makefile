PYTHON ?= python3

.PHONY: test compile ci lint doctor build serve pipeline playwright-install ui-check ui-check-live

compile:
	$(PYTHON) -m py_compile codex_token_bola/*.py scripts/*.py

test:
	$(PYTHON) -m unittest discover -s tests -v

ci: lint compile test ui-check

lint:
	$(PYTHON) -m ruff check codex_token_bola scripts tests

doctor:
	$(PYTHON) -m codex_token_bola doctor

build:
	$(PYTHON) -m codex_token_bola build

pipeline:
	$(PYTHON) -m codex_token_bola pipeline

playwright-install:
	$(PYTHON) -m playwright install chromium

ui-check:
	$(PYTHON) scripts/playwright_dashboard_check.py

ui-check-live:
	$(PYTHON) scripts/playwright_dashboard_check.py --url http://127.0.0.1:8766

serve:
	$(PYTHON) -m codex_token_bola serve --host 127.0.0.1 --port 8766
