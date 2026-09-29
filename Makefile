.PHONY: dev test test-unit test-integration test-e2e test-functional test-security \
        smoke-test lint build docker-build docker-push deploy-dev deploy-prod \
        seed-mock-data clean pre-commit run-ai-local check-docs check-bundle-dal grpc-dev-certs \
        verify-csping-fixture test-waddle-sdk-cs test-superpenguin-roll \
        build-superpenguin-roll-bundle verify-ping-bundle-reproducible \
        generate-minio-kms-key alpha-deploy

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

# C#-toolchain spike (spec S18 R13): rebuilds bundles/csharp/csping from
# source in its pinned, checksum-verified, rootless Dockerfile and asserts
# byte-identical output against the committed
# core/bundle_executor/tests/fixtures/csping.wasm + .sha256 -- makes that
# committed binary auditable instead of a trust-me blob.
verify-csping-fixture:
	@bash scripts/verify-csping-fixture.sh

# waddle-sdk-cs: shared C# SDK for Waddles app bundles (sdk/waddle-sdk-cs) --
# xUnit tests run in the same pinned containerized .NET SDK image as every
# other C# build in this repo (see scripts/test-waddle-sdk-cs.sh).
test-waddle-sdk-cs:
	@bash scripts/test-waddle-sdk-cs.sh

# superpenguin-roll: first C# bundle built on waddle-sdk-cs, ported from
# PenguinTwitchBot's PastyGames/Roll.cs (MIT, used with permission -- see
# bundles/csharp/superpenguin-roll/bundle.yaml `notice`).
test-superpenguin-roll:
	@bash scripts/test-superpenguin-roll.sh

# Rebuilds bundles/csharp/superpenguin-roll to a real .wasm component and
# reports its size/sha256 -- see scripts/verify-superpenguin-roll-fixture.sh
# for why no committed fixture is byte-identity-checked here (unlike
# verify-csping-fixture, this component is not committed to
# core/bundle_executor/tests/fixtures/).
build-superpenguin-roll-bundle:
	@bash scripts/verify-superpenguin-roll-fixture.sh

# Proves bundles/rust/ping's WASI 0.2 component build (bundles/Dockerfile.core-bundles's
# rust-bundle-builder stage) is byte-reproducible -- two independent --no-cache builds must
# produce an identical sha256. See scripts/verify-ping-bundle-reproducible.sh for why.
verify-ping-bundle-reproducible:
	@bash scripts/verify-ping-bundle-reproducible.sh

# Generates a MinIO static KMS key and applies it as a Secret so at-rest
# encryption (security.md Encryption: Storage) works outside alpha -- see
# k8s/helm/waddlebot's infrastructure.minio.kms.secretName fail guard.
# Usage: make generate-minio-kms-key KUBE_CONTEXT=dal2-beta [NAMESPACE=waddlebot]
generate-minio-kms-key:
	@test -n "$(KUBE_CONTEXT)" || { echo "ERROR: KUBE_CONTEXT is required, e.g. make generate-minio-kms-key KUBE_CONTEXT=dal2-beta" >&2; exit 1; }
	@bash scripts/generate-minio-kms-key.sh --context "$(KUBE_CONTEXT)" $(if $(NAMESPACE),--namespace "$(NAMESPACE)",)

# Builds+pushes images at HEAD's SHA (Rust svc-ingest/svc-process/svc-action into their
# own "*-rust" repositories), then `helm upgrade --install` with only the image tag set --
# no secret/TLS material, ever: k8s/helm/waddlebot self-provisions everything alpha needs
# (fix/helm-alpha-self-provisioning). Waits on migrations + rollout, re-runs the seeder,
# verifies. Requires kube context local-alpha or microk8s (validated by the script,
# rejects any other KUBE_CONTEXT before build/push/helm run). Usage: make alpha-deploy [ARGS="--skip-build"]
alpha-deploy:
	@bash scripts/alpha-deploy.sh $(ARGS)

pre-commit:
	@echo "=== Pre-commit checks ==="
	@$(MAKE) lint
	@$(MAKE) test-security
	@$(MAKE) test
	@echo "=== Pre-commit complete ==="
