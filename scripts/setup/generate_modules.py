#!/usr/bin/env python3
"""
Generate Flask modules from template
Systematically creates all remaining Waddles modules
"""

import os
from pathlib import Path

# Module definitions
MODULES = {
    # Interaction Modules
    'alias_interaction_module_flask': {'port': 8010, 'desc': 'Linux-style command aliases'},
    'shoutout_interaction_module_flask': {'port': 8011, 'desc': 'Platform user shoutouts'},
    'inventory_interaction_module_flask': {'port': 8024, 'desc': 'Inventory management'},
    'memories_interaction_module_flask': {'port': 8031, 'desc': 'Community memories'},
    'youtube_music_interaction_module_flask': {'port': 8025, 'desc': 'YouTube Music integration'},
    'spotify_interaction_module_flask': {'port': 8026, 'desc': 'Spotify integration'},

    # Core Modules
    'labels_core_module_flask': {'port': 8023, 'desc': 'Label management system'},
    'browser_source_core_module_flask': {'port': 8027, 'desc': 'Browser source for OBS'},
    'identity_core_module_flask': {'port': 8050, 'desc': 'Identity linking system'},
    'marketplace_module_flask': {'port': 8001, 'desc': 'Module marketplace'},
    'portal_module_flask': {'port': 8080, 'desc': 'Community portal'},

    # Collectors
    'twitch_module_flask': {'port': 8002, 'desc': 'Twitch collector'},
    'discord_module_flask': {'port': 8003, 'desc': 'Discord collector'},
    'slack_module_flask': {'port': 8004, 'desc': 'Slack collector'},

    # Supporting
    'community_module_flask': {'port': 8020, 'desc': 'Community management'},
    'reputation_module_flask': {'port': 8021, 'desc': 'Reputation tracking'},
}

APP_TEMPLATE = '''"""
{desc} - Quart Application
"""
import os, sys
from quart import Quart, Blueprint, request
from datetime import datetime
import asyncio

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), 'libs'))
from flask_core import setup_aaa_logging, init_database, async_endpoint, success_response, error_response
from config import Config

app = Quart(__name__)
api_bp = Blueprint('api', __name__, url_prefix='/api/v1')
logger = setup_aaa_logging(Config.MODULE_NAME, Config.MODULE_VERSION)

dal = None

@app.before_serving
async def startup():
    global dal
    logger.system("Starting {module_name}", action="startup")
    dal = init_database(Config.DATABASE_URL)
    app.config['dal'] = dal
    logger.system("{module_name} started", result="SUCCESS")

@app.route('/health')
async def health():
    return {{"status": "healthy", "module": Config.MODULE_NAME, "version": Config.MODULE_VERSION}}, 200

@api_bp.route('/status')
@async_endpoint
async def status():
    return success_response({{"status": "operational", "module": Config.MODULE_NAME}})

app.register_blueprint(api_bp)

if __name__ == '__main__':
    import hypercorn.asyncio
    from hypercorn.config import Config as HyperConfig
    config = HyperConfig()
    config.bind = [f"0.0.0.0:{{Config.MODULE_PORT}}"]
    asyncio.run(hypercorn.asyncio.serve(app, config))
'''

CONFIG_TEMPLATE = '''"""Configuration for {module_name}"""
import os
from dotenv import load_dotenv
from flask_core.secrets import require_secret_key

load_dotenv()

class Config:
    MODULE_NAME = '{module_name}'
    MODULE_VERSION = '2.0.0'
    MODULE_PORT = int(os.getenv('MODULE_PORT', '{port}'))
    DATABASE_URL = os.getenv('DATABASE_URL', 'postgresql://waddlebot:password@localhost:5432/waddlebot')
    CORE_API_URL = os.getenv('CORE_API_URL', 'http://router-service:8000')
    ROUTER_API_URL = os.getenv('ROUTER_API_URL', 'http://router-service:8000/api/v1/router')
    LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')
    # C1 security fix: fails closed (raises InsecureSecretError) rather than
    # silently booting with a publicly known JWT signing key when this
    # value is unset/placeholder in a production-like environment.
    SECRET_KEY = require_secret_key()
'''

REQUIREMENTS = '''quart>=0.19.0
hypercorn>=0.16.0
-e ../libs/flask_core
httpx>=0.26.0
python-dotenv>=1.0.0
'''

DOCKERFILE = '''FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN mkdir -p /var/log/waddlebotlog
CMD ["hypercorn", "app:app", "--bind", "0.0.0.0:{port}", "--workers", "4"]
'''


def create_module(module_name, port, desc):
    """Create a complete module structure"""
    base_path = Path(f'/home/penguin/code/WaddleBot/{module_name}')
    base_path.mkdir(exist_ok=True)
    (base_path / 'services').mkdir(exist_ok=True)

    # Create app.py
    with open(base_path / 'app.py', 'w') as f:
        f.write(APP_TEMPLATE.format(
            desc=desc,
            module_name=module_name.replace('_flask', '')
        ))

    # Create config.py
    with open(base_path / 'config.py', 'w') as f:
        f.write(CONFIG_TEMPLATE.format(
            module_name=module_name.replace('_flask', ''),
            port=port
        ))

    # Create requirements.txt
    with open(base_path / 'requirements.txt', 'w') as f:
        f.write(REQUIREMENTS)

    # Create Dockerfile
    with open(base_path / 'Dockerfile', 'w') as f:
        f.write(DOCKERFILE.format(port=port))

    # Create __init__.py for services
    with open(base_path / 'services' / '__init__.py', 'w') as f:
        f.write(f'"""Services for {module_name}"""\n')

    print(f"✓ Created {module_name}")


if __name__ == '__main__':
    print("Generating all Waddles Flask modules...")
    for module_name, config in MODULES.items():
        create_module(module_name, config['port'], config['desc'])
    print(f"\\n✓ Successfully generated {len(MODULES)} modules!")
