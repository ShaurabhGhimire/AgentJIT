.PHONY: install mongo seed fixtures dev run test demo dashboard stream eval watchers

PY ?= python

install:
	pip install -r requirements.txt && pip install -e .

# Local single-node replica set (transactions + change streams). Atlas works too: set MONGODB_URI.
mongo:
	docker run -d --name agentjit-atlas -p 27017:27017 mongodb/mongodb-atlas-local:latest

seed:
	$(PY) scripts/seed_atlas.py

fixtures:
	$(PY) scripts/load_fixtures.py

# Mock world HTTP surface (tools + drift knobs)
dev:
	uvicorn agentjit.runtime.mockapi:app --reload --port 8001

run:
	uvicorn agentjit.runtime.mockapi:app --port 8001

test:
	$(PY) -m pytest -q tests

# Pre-warm the demo prefix, then serve the dashboard on :8000
demo:
	DB_PREFIX=demo $(PY) scripts/seed_demo.py demo
	DB_PREFIX=demo uvicorn agentjit.dashboard.app:app --port 8000

dashboard:
	uvicorn agentjit.dashboard.app:app --port 8000

stream:
	$(PY) scripts/run_stream.py --fresh

eval:
	$(PY) -m agentjit.eval.run --n 600 --out eval_results

watchers:
	$(PY) -m agentjit.compileplane.watchers
