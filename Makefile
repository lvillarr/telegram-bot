.PHONY: test test-unit test-smoke install-dev

PYTHON := python3.10

install-dev:
	$(PYTHON) -m pip install -q -r requirements-dev.txt

test-unit:
	$(PYTHON) -m pytest tests/test_exports.py tests/test_token_limits.py -v

test-smoke:
	@echo "Requiere servidor corriendo. Usar BOT_BASE_URL=https://<dominio> para Railway."
	$(PYTHON) -m pytest tests/test_smoke.py -v

test: install-dev test-unit
	@echo ""
	@echo "Para smoke tests (necesita servidor): make test-smoke"
