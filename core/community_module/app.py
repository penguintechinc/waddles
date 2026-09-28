"""
Community management - Quart Application
"""
import asyncio
import os
import sys

from quart import Quart, Blueprint

from flask_core import (  # noqa: E402
    setup_aaa_logging, init_database, async_endpoint, success_response,
    create_health_blueprint, install_rate_limiting, install_security_headers,
    bind_community_read_tables, install_community_scoped_auth
)
from config import Config  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), 'libs'))

app = Quart(__name__)
# security.md A05 hardening -- JSON-only service, default deny-everything CSP.
install_security_headers(app)

# Register health/metrics endpoints
health_bp = create_health_blueprint(Config.MODULE_NAME, Config.MODULE_VERSION)
app.register_blueprint(health_bp)

# SECURITY (A04): shared global before_request rate-limit hook -- see
# flask_core.http_rate_limit module docstring.
install_rate_limiting(app, namespace=Config.MODULE_NAME)

api_bp = Blueprint('api', __name__, url_prefix='/api/v1')
logger = setup_aaa_logging(Config.MODULE_NAME, Config.MODULE_VERSION)

# SECURITY (C6, A01 -- BOLA/unauthenticated access): this blueprint had
# ZERO tenant/community-membership enforcement on any route -- harmless
# today since `/status` is its only route, but the next CRUD route added
# (e.g. one taking a `community_id` path parameter) would ship with no
# auth by omission. Registered once for the whole blueprint (not
# per-route), matching `core/security_core_module/app.py`'s convention, so
# a route added later can't skip this check by omission -- see
# flask_core.community_access module docstring. Scoped to `api_bp` (not
# `app`) so the separate `health_bp` (`/health`, `/healthz`, `/metrics` --
# K8s probes) is never touched by this hook; `/api/v1/status` (this
# module's only current business route, always public) is carved out via
# `exempt_paths`. `dal_key='raw_dal'` keeps `app.config['dal']` pointing at
# the `AsyncDAL` wrapper other code in this module already expects, while
# this hook gets the raw pydal `DAL` it needs for
# `tenants`/`communities`/`community_members` queries.
install_community_scoped_auth(
    api_bp,
    dal_key='raw_dal',
    exempt_paths=frozenset({'/api/v1/status'}),
)

dal = None


@app.before_serving
async def startup():
    global dal
    logger.system("Starting community_module", action="startup")
    dal = init_database(Config.DATABASE_URL)
    app.config['dal'] = dal
    app.config['async_dal'] = dal
    # Read-only tenants/communities/community_members subset
    # `install_community_scoped_auth` needs -- owned by hub-api's own
    # migrations, never created here (migrate=False in prod).
    app.config['raw_dal'] = dal.dal
    bind_community_read_tables(app.config['raw_dal'], migrate=Config.DB_MIGRATE)
    logger.system("community_module started", result="SUCCESS")


@api_bp.route('/status')
@async_endpoint
async def status():
    return success_response({"status": "operational", "module": Config.MODULE_NAME})

app.register_blueprint(api_bp)

if __name__ == '__main__':
    import hypercorn.asyncio
    from hypercorn.config import Config as HyperConfig
    config = HyperConfig()
    config.bind = [f"0.0.0.0:{Config.MODULE_PORT}"]
    asyncio.run(hypercorn.asyncio.serve(app, config))
