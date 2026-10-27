#!/usr/bin/env bash
# Seed UI mock content (communities, members, leaderboard, overlays, chat,
# bundle activations) so marketing screenshots are not empty-state.
# Runs config/postgres/seed_ui_mock.sql; idempotent. Run seed-admin.sh first
# (login account) and let the bundle seeder populate app_catalog.
#
# Usage: POSTGRES_PASSWORD=... scripts/seed-ui-mock-data.sh [--docker]
# Env:   POSTGRES_HOST/PORT/DB/USER/PASSWORD (same as seed-admin.sh)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SEED_FILE="${SCRIPT_DIR}/../config/postgres/seed_ui_mock.sql"
DB_HOST="${POSTGRES_HOST:-localhost}"
DB_PORT="${POSTGRES_PORT:-5432}"
DB_NAME="${POSTGRES_DB:-waddlebot}"
DB_USER="${POSTGRES_USER:-waddlebot}"
DB_PASSWORD="${POSTGRES_PASSWORD:-}"
USE_DOCKER=false

for arg in "$@"; do
    case "${arg}" in
        --docker) USE_DOCKER=true ;;
        -h|--help) sed -n '2,10p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "Unknown option: ${arg}" >&2; exit 2 ;;
    esac
done

[[ -f "${SEED_FILE}" ]] || { echo "ERROR: ${SEED_FILE} not found" >&2; exit 1; }

if [[ "${USE_DOCKER}" == true ]]; then
    container="$(docker ps --filter "name=postgres" --format '{{.Names}}' | head -n1)"
    [[ -n "${container}" ]] || { echo "ERROR: no running postgres container" >&2; exit 1; }
    docker exec -i "${container}" psql -U "${DB_USER}" -d "${DB_NAME}" -v ON_ERROR_STOP=1 < "${SEED_FILE}"
else
    [[ -n "${DB_PASSWORD}" ]] || { echo "ERROR: set POSTGRES_PASSWORD (or use --docker)" >&2; exit 2; }
    PGPASSWORD="${DB_PASSWORD}" psql -h "${DB_HOST}" -p "${DB_PORT}" -U "${DB_USER}" -d "${DB_NAME}" \
        -v ON_ERROR_STOP=1 -f "${SEED_FILE}"
fi
echo "UI mock data seeded: 4 communities (ids 9001-9004), 6 demo members, leaderboard, overlays, chat, bundle activations"
