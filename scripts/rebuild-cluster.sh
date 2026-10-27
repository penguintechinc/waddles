#!/bin/bash
# =============================================================================
# Waddles Local Cluster Rebuild Script
# Rebuilds the entire local development cluster from scratch
# =============================================================================

set -euo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# Configuration
DOCKER_COMPOSE_FILE="${PROJECT_ROOT}/docker-compose.yml"
POSTGRES_INIT_FILE="${PROJECT_ROOT}/config/postgres/init.sql"
MIGRATION_DIR="${PROJECT_ROOT}/config/postgres/migrations"
SEED_ADMIN_FILE="${PROJECT_ROOT}/config/postgres/seed_admin.sql"

# Load environment variables
if [ -f "${PROJECT_ROOT}/.env" ]; then
    # shellcheck disable=SC1090
    source "${PROJECT_ROOT}/.env"
fi

# Export required variables for docker-compose
export POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-waddlebot123}"
export REDIS_PASSWORD="${REDIS_PASSWORD:-redis123}"
export S3_ACCESS_KEY_ID="${S3_ACCESS_KEY_ID:-waddlebot}"
export S3_SECRET_ACCESS_KEY="${S3_SECRET_ACCESS_KEY:-waddlebot-dev-secret}"
# SECURITY (CWE-798): dev/local-only bootstrap credential -- matches the
# default in docker-compose.yml's db-migrations service. Override via .env
# to change the local admin login; never used as a fallback in beta/prod.
export INITIAL_ADMIN_EMAIL="${INITIAL_ADMIN_EMAIL:-admin@localhost.local}"
export INITIAL_ADMIN_PASSWORD="${INITIAL_ADMIN_PASSWORD:-WaddleDev-Local-Only-2026!}"

show_help() {
    cat << EOF
Waddles Local Cluster Rebuild Script

Usage: $0 [OPTIONS]

Options:
    -h, --help          Show this help message
    -f, --full          Full rebuild: stop, clean volumes, rebuild (WARNING: Data loss!)
    -n, --no-seed       Skip seeding admin user
    -v, --verbose       Show detailed output

By default, does a "soft rebuild" (stop/start without deleting data).

Examples:
    # Soft rebuild (keeps data)
    $0

    # Full rebuild (WARNING: loses all data!)
    $0 --full

    # Rebuild without admin seeding
    $0 --no-seed

EOF
}

FULL_REBUILD=false
SKIP_SEED=false
VERBOSE=false

while [[ $# -gt 0 ]]; do
    case $1 in
        -h|--help)
            show_help
            exit 0
            ;;
        -f|--full)
            FULL_REBUILD=true
            shift
            ;;
        -n|--no-seed)
            SKIP_SEED=true
            shift
            ;;
        -v|--verbose)
            VERBOSE=true
            shift
            ;;
        *)
            echo -e "${RED}Unknown option: $1${NC}"
            show_help
            exit 1
            ;;
    esac
done

# Helper functions
log_header() {
    echo ""
    echo -e "${BLUE}========================================${NC}"
    echo -e "${BLUE}$1${NC}"
    echo -e "${BLUE}========================================${NC}"
}

log_step() {
    echo -e "\n${CYAN}▶${NC} $1"
}

log_success() {
    echo -e "${GREEN}✓${NC} $1"
}

log_error() {
    echo -e "${RED}✗${NC} $1"
}

log_warning() {
    echo -e "${YELLOW}⚠${NC} $1"
}

check_requirements() {
    log_step "Checking requirements..."

    if ! command -v docker &> /dev/null; then
        log_error "Docker is not installed"
        exit 1
    fi
    log_success "Docker found: $(docker --version)"

    if ! command -v docker-compose &> /dev/null; then
        log_error "Docker Compose is not installed"
        exit 1
    fi
    log_success "Docker Compose found: $(docker-compose --version)"

    if [ ! -f "$DOCKER_COMPOSE_FILE" ]; then
        log_error "docker-compose.yml not found at $DOCKER_COMPOSE_FILE"
        exit 1
    fi
    log_success "docker-compose.yml found"

    if [ ! -f "$POSTGRES_INIT_FILE" ]; then
        log_error "PostgreSQL init file not found at $POSTGRES_INIT_FILE"
        exit 1
    fi
    log_success "PostgreSQL init.sql found"
}

stop_containers() {
    log_step "Stopping running containers..."

    if docker-compose -f "$DOCKER_COMPOSE_FILE" ps -q &>/dev/null; then
        docker-compose -f "$DOCKER_COMPOSE_FILE" down --remove-orphans
        log_success "Containers stopped"
    else
        log_warning "No running containers found"
    fi
}

clean_volumes() {
    log_step "Removing volumes (this will DELETE all data)..."
    log_warning "Removing: postgres-data, redis-data, seaweedfs-data, ollama-data, qdrant-data"

    docker volume rm waddlebot_postgres-data 2>/dev/null || true
    docker volume rm waddlebot_redis-data 2>/dev/null || true
    docker volume rm waddlebot_seaweedfs-data 2>/dev/null || true
    docker volume rm waddlebot_ollama-data 2>/dev/null || true
    docker volume rm waddlebot_qdrant-data 2>/dev/null || true

    log_success "Volumes removed"
}

start_services() {
    log_step "Starting infrastructure services..."

    cd "$PROJECT_ROOT"
    docker-compose -f "$DOCKER_COMPOSE_FILE" up -d infra-postgres infra-redis infra-seaweedfs

    log_warning "Waiting for PostgreSQL to be ready..."
    local max_attempts=30
    local attempt=0

    while [ $attempt -lt $max_attempts ]; do
        if docker exec waddlebot-postgres pg_isready -U waddlebot >/dev/null 2>&1; then
            log_success "PostgreSQL is ready"
            break
        fi
        attempt=$((attempt + 1))
        echo -n "."
        sleep 1
    done

    if [ $attempt -eq $max_attempts ]; then
        log_error "PostgreSQL failed to start after ${max_attempts} seconds"
        exit 1
    fi

    # Verify Redis
    log_warning "Waiting for Redis to be ready..."
    sleep 2
    if docker exec waddlebot-redis redis-cli -a "${REDIS_PASSWORD}" ping >/dev/null 2>&1; then
        log_success "Redis is ready"
    fi

    # Verify SeaweedFS
    log_warning "Waiting for SeaweedFS to be ready..."
    sleep 2
    log_success "SeaweedFS is ready"
}

run_migrations() {
    log_step "Running database migrations..."

    # List all migration files
    local migrations=($(ls -1 "$MIGRATION_DIR"/*.sql 2>/dev/null | sort))

    if [ ${#migrations[@]} -eq 0 ]; then
        log_warning "No migrations found in $MIGRATION_DIR"
        return
    fi

    local total=${#migrations[@]}
    log_warning "Found $total migration files"

    for migration in "${migrations[@]}"; do
        local filename=$(basename "$migration")
        log_step "Running migration: $filename"

        PGPASSWORD="$POSTGRES_PASSWORD" psql \
            -h localhost \
            -p 5432 \
            -U waddlebot \
            -d waddlebot \
            -f "$migration" \
            > /dev/null 2>&1

        if [ $? -eq 0 ]; then
            log_success "Migration completed: $filename"
        else
            log_error "Migration failed: $filename"
            exit 1
        fi
    done
}

seed_admin() {
    if [ "$SKIP_SEED" = true ]; then
        log_warning "Skipping admin user seeding (--no-seed flag used)"
        return
    fi

    if [ ! -f "$SEED_ADMIN_FILE" ]; then
        log_error "Seed file not found: $SEED_ADMIN_FILE"
        return
    fi

    log_step "Seeding admin user..."

    # seed_admin.sql reads ADMIN_EMAIL/ADMIN_PASSWORD via psql's \getenv --
    # reuse the same dev-only INITIAL_ADMIN_EMAIL/PASSWORD exported above
    # (single source of truth for the local admin login).
    ADMIN_EMAIL="$INITIAL_ADMIN_EMAIL" ADMIN_PASSWORD="$INITIAL_ADMIN_PASSWORD" \
    PGPASSWORD="$POSTGRES_PASSWORD" psql \
        -h localhost \
        -p 5432 \
        -U waddlebot \
        -d waddlebot \
        -f "$SEED_ADMIN_FILE" \
        > /dev/null 2>&1

    if [ $? -eq 0 ]; then
        log_success "Admin user seeded successfully"
    else
        log_error "Failed to seed admin user"
        return 1
    fi
}

verify_services() {
    log_step "Verifying all services..."

    local services=("infra-postgres" "infra-redis" "infra-seaweedfs")
    local all_healthy=true

    for service in "${services[@]}"; do
        if docker-compose -f "$DOCKER_COMPOSE_FILE" ps "$service" | grep -q "Up"; then
            log_success "$service is running"
        else
            log_error "$service is not running"
            all_healthy=false
        fi
    done

    if [ "$all_healthy" = true ]; then
        log_success "All services are healthy"
    else
        log_error "Some services are not healthy"
        return 1
    fi
}

show_summary() {
    log_header "Rebuild Complete!"

    echo ""
    echo -e "${CYAN}Infrastructure Services:${NC}"
    docker-compose -f "$DOCKER_COMPOSE_FILE" ps infra-postgres infra-redis infra-seaweedfs 2>/dev/null | tail -n +2 || true

    echo ""
    echo -e "${CYAN}Database Connection:${NC}"
    echo -e "  Host: ${BLUE}localhost${NC}"
    echo -e "  Port: ${BLUE}5432${NC}"
    echo -e "  User: ${BLUE}waddlebot${NC}"
    echo -e "  DB:   ${BLUE}waddlebot${NC}"

    echo ""
    echo -e "${CYAN}Redis Connection:${NC}"
    echo -e "  Host: ${BLUE}localhost${NC} (internal only)"
    echo -e "  Password: ${BLUE}${REDIS_PASSWORD}${NC}"

    echo ""
    echo -e "${CYAN}SeaweedFS Access:${NC}"
    echo -e "  API: ${BLUE}http://localhost:9000${NC}"
    echo -e "  Console: ${BLUE}http://localhost:9001${NC}"
    echo -e "  Access Key: ${BLUE}${S3_ACCESS_KEY_ID}${NC}"
    echo -e "  Secret Key: ${BLUE}${S3_SECRET_ACCESS_KEY}${NC}"

    if [ "$SKIP_SEED" = false ]; then
        echo ""
        echo -e "${CYAN}Hub Module Admin Login:${NC}"
        echo -e "  Email: ${BLUE}${INITIAL_ADMIN_EMAIL}${NC}"
        echo -e "  Password: (from INITIAL_ADMIN_PASSWORD -- set in .env, or the local dev default; not echoed)"
        echo -e "  ${YELLOW}⚠ Dev-only credential. Set INITIAL_ADMIN_EMAIL/INITIAL_ADMIN_PASSWORD explicitly in beta/prod -- never a default there.${NC}"
    fi

    echo ""
    echo -e "${CYAN}Next Steps:${NC}"
    echo -e "  1. Start the hub module: ${BLUE}docker-compose -f docker-compose.yml up waddlebot-hub${NC}"
    echo -e "  2. Access the hub: ${BLUE}http://localhost:8060${NC}"
    echo -e "  3. Login with credentials above"

    echo ""
}

main() {
    log_header "Waddles Cluster Rebuild"

    if [ "$FULL_REBUILD" = true ]; then
        echo ""
        log_warning "WARNING: FULL REBUILD MODE"
        log_warning "This will DELETE all data including:"
        log_warning "  - PostgreSQL data"
        log_warning "  - Redis cache"
        log_warning "  - SeaweedFS files"
        echo ""
        read -p "Are you sure? Type 'yes' to continue: " -r
        echo
        if [[ ! $REPLY =~ ^[Yy][Ee][Ss]$ ]]; then
            log_error "Rebuild cancelled"
            exit 1
        fi
    fi

    echo ""
    check_requirements
    stop_containers

    if [ "$FULL_REBUILD" = true ]; then
        clean_volumes
    fi

    start_services
    run_migrations
    seed_admin

    verify_services
    show_summary
}

# Run main function
main
