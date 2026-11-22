.PHONY: help install test lint fmt bench serve sdk-ts docker

help:            ## Show targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-10s %s\n", $$1, $$2}'

install:         ## Install for development
	pip install -e ".[dev,fast]"

test:            ## Run the Python test suite
	pytest -q

lint:            ## Lint
	ruff check .

fmt:             ## Auto-fix lint issues
	ruff check --fix .

bench:           ## Reproduce the README performance numbers
	python bench/bench.py

serve:           ## Run a local server (data in ~/.agentcloud)
	agentcloud up

sdk-ts:          ## Build and test the TypeScript SDK against a throwaway server on :8799
	agentcloud up --data /tmp/ac-sdk --port 8799 --workers 1 & \
	  for i in $$(seq 1 50); do curl -sf localhost:8799/healthz >/dev/null && break; sleep 0.2; done; \
	  cd sdk/typescript && npx -y -p typescript@5.6.3 tsc && node test/e2e.mjs; \
	  status=$$?; kill $$(python -c "import json;print(json.load(open('/tmp/ac-sdk/server.json'))['pid'])"); exit $$status

docker:          ## Build the container image
	docker build -t agentcloud .
