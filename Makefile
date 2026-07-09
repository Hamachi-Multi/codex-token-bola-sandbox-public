PYTHON ?= python3

.PHONY: test compile ci lint doctor build serve pipeline playwright-install ui-check ui-check-live release-export-guard private-repo-hygiene public-repo-bootstrap-check sandbox-dry-run-readiness production-public-readiness production-public-dry-run production-public-rulesets

compile:
	$(PYTHON) -m py_compile hooks/token-usage.py scripts/*.py

test:
	$(PYTHON) -m unittest discover -s tests -v

ci: compile test ui-check

lint:
	$(PYTHON) -m ruff check hooks scripts tests

doctor:
	$(PYTHON) scripts/codex_token_usage.py doctor

build:
	$(PYTHON) scripts/codex_token_usage.py build

pipeline:
	$(PYTHON) scripts/codex_token_usage.py pipeline

release-export-guard:
	@test -n "$(EXPORT_DIR)" || { echo "EXPORT_DIR is required" >&2; exit 2; }
	@$(PYTHON) scripts/private_export_guard.py --repo-root . --manifest release/export-manifest.json --export-dir "$(EXPORT_DIR)"

private-repo-hygiene:
	$(PYTHON) scripts/private_repo_hygiene.py --repo-root . --manifest release/export-manifest.json

public-repo-bootstrap-check:
	$(PYTHON) scripts/public_repo_bootstrap.py --bootstrap-root release/public-bootstrap

sandbox-dry-run-readiness:
	$(PYTHON) scripts/sandbox_dry_run_readiness.py --repo-root . --config release/sandbox-dry-run.example.json

production-public-readiness:
	$(PYTHON) scripts/production_public_readiness.py --repo-root . --config release/production-public-readiness.example.json

production-public-dry-run:
	$(PYTHON) scripts/production_public_dry_run.py --repo-root . --config release/production-public-readiness.example.json

production-public-rulesets:
	$(PYTHON) scripts/production_public_rulesets.py --config release/production-public-rulesets.example.json

playwright-install:
	$(PYTHON) -m playwright install chromium

ui-check:
	$(PYTHON) scripts/playwright_dashboard_check.py

ui-check-live:
	$(PYTHON) scripts/playwright_dashboard_check.py --url http://127.0.0.1:8766

serve:
	$(PYTHON) scripts/codex_token_usage.py serve --host 127.0.0.1 --port 8766
