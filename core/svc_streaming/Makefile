# svc-streaming (Rust) -- local dev targets.
# Not wired into the root Makefile; invoke directly from this directory
# (`make -C core/svc_streaming <target>`) until the Python alpha retires.

.PHONY: build test lint fmt fmt-check clippy test-security coverage docker-build image-smoke clean

build:
	cargo build --all-targets

test:
	cargo test

lint: fmt-check clippy

fmt:
	cargo fmt

fmt-check:
	cargo fmt --check

clippy:
	cargo clippy --all-targets -- -D warnings

# Advisories (RustSec), license policy, banned crates, source allowlist.
# `cargo-deny` must be pre-installed (`cargo install cargo-deny --locked`).
test-security:
	cargo deny check

# Line coverage gate -- see rules/critical-rules.md Coverage (90% minimum).
# `cargo-llvm-cov` must be pre-installed (`cargo install cargo-llvm-cov --locked`).
coverage:
	cargo llvm-cov --fail-under-lines 90

# Image variants (cpu|amd|nvidia) -- see the Container section of README.md.
# Builds one variant; tag is alpha-<epoch>-<variant>. Usage: make docker-build VARIANT=amd
VARIANT ?= cpu

docker-build:
	docker build -f Dockerfile.rust --target $(VARIANT) -t localhost:32000/waddlebot/svc-streaming:alpha-$$(date +%s)-$(VARIANT) .

# Smoke-tests a built variant image: encoders, driver wiring, CPU fallback. Usage:
#   make image-smoke VARIANT=nvidia IMAGE=localhost:32000/waddlebot/svc-streaming:alpha-1700000000-nvidia
image-smoke:
	bash scripts/image-smoke.sh $(IMAGE) $(VARIANT)

clean:
	cargo clean
