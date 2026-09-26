.PHONY: dev run fixtures install test

install:
	pip install -r requirements.txt

dev:
	uvicorn agentjit.runtime.mockapi:app --reload --port 8001

run:
	uvicorn agentjit.runtime.mockapi:app --port 8001

fixtures:
	python scripts/load_fixtures.py

test:
	pytest
