.PHONY: install dev test demo attack-demo eval keys clean

# Resolve Python: prefer the venv interpreter so we sidestep the Microsoft
# Store `python` alias on Windows.
ifneq ("$(wildcard .venv/Scripts/python.exe)","")
PY := .venv/Scripts/python.exe
else ifneq ("$(wildcard .venv/bin/python)","")
PY := .venv/bin/python
else
PY := python3
endif

install:
	$(PY) -m pip install -e ".[dev]"

keys:
	$(PY) -c "from approval.tokens import generate_keypair; generate_keypair()"

dev:
	$(PY) -m uvicorn api.main:app --reload --port 8000

test:
	$(PY) -m pytest -v

demo:
	bash scripts/demo.sh

attack-demo:
	bash scripts/attack_demo.sh

eval:
	$(PY) tests/eval/run_eval.py

clean:
	rm -rf data/ __pycache__ .pytest_cache .ruff_cache
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
