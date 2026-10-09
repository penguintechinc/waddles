# svc-presentation (Rust) -- local dev targets.
# Not wired into the root Makefile; invoke directly from this directory
# (`make -C core/svc_presentation <target>`) until the Python alpha retires.

.PHONY: build test lint fmt fmt-check clippy test-security coverage docker-build clean

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

# Context = core/ (parent of this directory) -- see Dockerfile.rust's own
# header comment for why (same-repo path dependencies on overlay_schema/
# overlay_auth/service_auth).
docker-build:
	docker build -f Dockerfile.rust -t localhost:32000/waddlebot/svc-presentation:alpha-$$(date +%s) ..

clean:
	cargo clean
