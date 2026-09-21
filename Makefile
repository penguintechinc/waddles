.PHONY: dev test test-unit test-integration test-e2e test-functional test-security \
        smoke-test lint build docker-build docker-push deploy-dev deploy-prod \
        seed-mock-data clean pre-commit run-ai-local check-docs check-bundle-dal grpc-dev-certs

# Dev-only self-signed CA + server/client cert pair for the gRPC transport
# TLS required by every service in docker-compose.yml (security audit A02).
# Idempotent -- skips regeneration if certs/grpc-dev is already populated.
grpc-dev-certs:
	@bash scripts/setup/generate_dev_grpc_certs.sh

dev: grpc-dev-certs
	docker-compose up

build:
	docker-compose build

docker-build: build

docker-push:
	$(error docker-push is CI-only — beta/prod images built by GitHub Actions from release branches)

lint:
	@bash scripts/lint.sh

check-docs:
	@bash scripts/check-doc-refs.sh

# M1.5 gate (docs/superpowers/specs/2026-09-14-rust-data-plane-design.md §16):
# zero flask_core.database/pydal occurrences under the App Bundle directories.
# Expected to fail until the parallel svc_action/svc_process DAL migration
# branches land -- a gate that cannot fail is not a gate.
check-bundle-dal:
	@bash scripts/check-bundle-dal-imports.sh

test:
	@$(MAKE) test-unit

test-unit:
	@echo "Running unit tests..."
	@bash tests/k8s/alpha/05-unit-tests.sh

test-integration:
	@echo "Running integration tests..."
	@test -d tests/integration || { echo "tests/integration directory not found" >&2; exit 1; }
	@bash scripts/test-api-all.sh

test-e2e:
	@echo "Running e2e tests..."
	@test -f scripts/e2e-test-alpha.sh || { echo "scripts/e2e-test-alpha.sh not found" >&2; exit 1; }
	@bash scripts/e2e-test-alpha.sh

test-functional:
	$(error test-functional is not yet implemented — add pytest tests/functional/ -v after creating tests/functional directory)

test-security:
	@bash scripts/security-scan.sh

smoke-test:
	@echo "Running smoke tests..."
	@test -f tests/alpha-smoke-test.sh || { echo "tests/alpha-smoke-test.sh not found" >&2; exit 1; }
	@bash tests/alpha-smoke-test.sh

seed-mock-data:
	@echo "Seeding mock data..."
	@test -f scripts/seed-admin.sh || { echo "scripts/seed-admin.sh not found" >&2; exit 1; }
	@bash scripts/seed-admin.sh

clean:
	docker-compose down -v
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete 2>/dev/null || true

deploy-dev:
	@echo "Deploy to dev/alpha environment..."
	@test -f scripts/deploy-alpha.sh || { echo "scripts/deploy-alpha.sh not found" >&2; exit 1; }
	@bash scripts/deploy-alpha.sh

deploy-prod:
	$(error deploy-prod requires CI — tag a release to trigger the production pipeline)

run-ai-local: ## Run ai_interaction_module container locally (standalone, 1 worker)
	docker build -f action/interactive/ai_interaction_module/Dockerfile -t waddlebot/ai-interaction:local . && \
	docker run --rm \
	  --name ai-interaction-local \
	  --add-host=host.docker.internal:host-gateway \
	  --env-file action/interactive/ai_interaction_module/.env.local \
	  -e HYPERCORN_WORKERS=1 \
	  -p 8005:8005 \
	  waddlebot/ai-interaction:local

pre-commit:
	@echo "=== Pre-commit checks ==="
	@$(MAKE) lint
	@$(MAKE) test-security
	@$(MAKE) test
	@echo "=== Pre-commit complete ==="
