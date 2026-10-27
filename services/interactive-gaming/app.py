"""
Interactive Gaming Service - Combined Quart Application

Merges 4 gaming interaction modules into a single service on port 8104:
1. LFG (Looking for Group matching) - /api/v1/lfg
2. Inventory management - /api/v1/inventory
3. Server Manager (RCON + enforcement) - /api/v1/server-manager
4. Server Status monitoring - /api/v1/server-status
"""
import asyncio
import os
import sys

_base_dir = os.path.dirname(__file__)


_COLLIDING_MODULES = frozenset({
    'services', 'controllers', 'config', 'validation_models', 'models',
})


def _setup_module_imports(module_name: str) -> None:
    """Flush cached packages that collide across modules and prioritize module dir."""
    for key in list(sys.modules):
        top = key.split('.', 1)[0]
        if top in _COLLIDING_MODULES:
            del sys.modules[key]
    sys.path.insert(0, os.path.join(_base_dir, module_name))


from quart import Quart
from flask_core import (
    create_health_blueprint,
    init_database,
    setup_aaa_logging,
)
from config import Config

# Import each module with isolated sys.path to avoid services/ collision
_setup_module_imports('lfg_interaction_module')
from lfg_interaction_module.app import (  # noqa: E402
    api_bp as lfg_api_bp,
)
from lfg_interaction_module.services.lfg_service import LfgService  # noqa: E402

_setup_module_imports('inventory_interaction_module')
from inventory_interaction_module.app import (  # noqa: E402
    api_bp as inventory_api_bp,
)

_setup_module_imports('server_manager_interaction_module')
from server_manager_interaction_module.app import (  # noqa: E402
    status_bp as manager_status_bp,
    manager_bp as manager_api_bp,
)
from server_manager_interaction_module.services.provider_service import (  # noqa: E402
    ProviderService,
)
from server_manager_interaction_module.services.status_service import (  # noqa: E402
    StatusService,
)
from server_manager_interaction_module.services.encryption_service import (  # noqa: E402
    EncryptionService,
)
from server_manager_interaction_module.services.rcon_service import (  # noqa: E402
    RconService,
)
from server_manager_interaction_module.services.enforcement_service import (  # noqa: E402
    EnforcementService,
)

_setup_module_imports('server_status_interaction_module')
from server_status_interaction_module.app import (  # noqa: E402
    api_bp as status_api_bp,
)
from server_status_interaction_module.services.provider_service import (  # noqa: E402
    ProviderService as StatusProviderService,
)
from server_status_interaction_module.services.status_service import (  # noqa: E402
    StatusService as StatusStatusService,
)

app = Quart(__name__)

# Register health/metrics endpoints
health_bp = create_health_blueprint(Config.MODULE_NAME, Config.MODULE_VERSION)
app.register_blueprint(health_bp)

logger = setup_aaa_logging(Config.MODULE_NAME, Config.MODULE_VERSION)

# Global service instances
dal = None
lfg_service = None
provider_service = None
status_service = None
encryption_service = None
rcon_service = None
enforcement_service = None
status_provider_service = None
status_status_service = None


@app.before_serving
async def startup():
    global dal, lfg_service, provider_service, status_service
    global encryption_service, rcon_service, enforcement_service
    global status_provider_service, status_status_service

    logger.system(
        "Starting interactive-gaming service",
        action="startup",
        version=Config.MODULE_VERSION,
    )

    dal = init_database(Config.DATABASE_URL)
    app.config['dal'] = dal

    # Initialize LFG service
    lfg_service = LfgService(dal, Config)

    # Initialize server manager services (shared)
    provider_service = ProviderService(Config)
    status_service = StatusService(dal, Config, provider_service)
    encryption_service = EncryptionService(Config)
    rcon_service = RconService(Config, encryption_service)
    enforcement_service = EnforcementService(
        dal, Config, rcon_service, encryption_service
    )

    # Initialize server status services
    status_provider_service = StatusProviderService(Config)
    status_status_service = StatusStatusService(dal, Config, status_provider_service)

    logger.system(
        "interactive-gaming service started",
        result="SUCCESS",
        version=Config.MODULE_VERSION,
    )


# Register all blueprints with their URL prefixes
# LFG module endpoints: /api/v1/lfg/*
app.register_blueprint(lfg_api_bp, name='lfg_api')

# Inventory module endpoints: /api/v1/inventory/*
# (Note: inventory module only has /api/v1/status endpoint)
inventory_api_bp.url_prefix = '/api/v1/inventory'
app.register_blueprint(inventory_api_bp, name='inventory_api')

# Server Manager endpoints:
# - /api/v1/server-status/* (backward compat with server_status module)
# - /api/v1/server-manager/* (new RCON/enforcement endpoints)
app.register_blueprint(manager_status_bp, name='mgr_status_api')
app.register_blueprint(manager_api_bp)

# Server Status endpoints: /api/v1/server-status/*
# Note: These overlap with manager_status_bp; manager_status_bp takes precedence
# for backward compatibility. Additional server_status endpoints are available here.
app.register_blueprint(status_api_bp, name='svr_status_api')


if __name__ == '__main__':
    import hypercorn.asyncio
    from hypercorn.config import Config as HyperConfig

    config = HyperConfig()
    config.bind = [f"0.0.0.0:{Config.MODULE_PORT}"]
    asyncio.run(hypercorn.asyncio.serve(app, config))
