# Module RTC Dockerfile
# Multi-stage Go build

FROM golang:1.26-bookworm@sha256:9fdc884aacc3bec89b20ffc69f4bb369c78210e3e4f600387b5128b12c199f81 AS builder

WORKDIR /build

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Copy go mod files first for layer caching (build context is project root)
COPY core/module_rtc/go.mod core/module_rtc/go.sum* ./
RUN go mod download

# Copy module source code
COPY core/module_rtc/ .

# Build binary
RUN CGO_ENABLED=0 GOOS=linux go build -ldflags="-w -s" -o /build/module_rtc ./cmd/server

# Production stage
FROM debian:bookworm-slim@sha256:f06537653ac770703bc45b4b113475bd402f451e85223f0f2837acbf89ab020a

WORKDIR /app

# Install runtime dependencies
# CVE-2026-103111 (libpcre2): upgrade to 10.42-1+deb12u2 from security updates.
RUN apt-get update && apt-get install -y --no-install-recommends --only-upgrade libpcre2-8-0 \
    && apt-get install -y --no-install-recommends \
    ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN groupadd -r waddlebot && useradd -r -g waddlebot waddlebot

# Copy binary from builder
COPY --from=builder /build/module_rtc /app/module_rtc

# Set ownership
RUN chown -R waddlebot:waddlebot /app

USER waddlebot

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8093/health || exit 1

EXPOSE 8093 50067

CMD ["/app/module_rtc"]
