# Module RTC Dockerfile
# Multi-stage Go build

FROM golang:1.26-bookworm@sha256:dc9ad6c05acc7a88e5b71bde60a5fe3bd4b9f0db209011711b464107438a8107 AS builder

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
FROM debian:bookworm-slim@sha256:7c7b2c966bc9ee8cedfeef67e0e279108992c77681fa595db4a9d65c06ccc587

WORKDIR /app

# Install runtime dependencies. Full apt-get upgrade (not just libpcre2-8-0)
# clears every fixable HIGH/CRITICAL OS-package CVE the pinned
# debian:bookworm-slim digest still ships (perl/perl-base, libcap,
# libgnutls, libpcre2-8-0, ...).
RUN apt-get update && apt-get upgrade -y --no-install-recommends \
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
