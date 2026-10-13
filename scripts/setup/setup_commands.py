#!/usr/bin/env python3
"""
Setup script to add reputation and community commands to the router
"""

import os
import sys
from datetime import datetime

# Add the router module to the Python path
sys.path.insert(0, '/workspaces/WaddleBot/router_module')

from models import db


def setup_commands():
    """Add reputation and community commands to the router"""

    # Commands to add
    commands = [
        # Community commands (local container)
        {
            'command': 'community',
            'prefix': '!',
            'description': 'Community management commands - create, join, manage communities',
            'location_url': 'http://community-module:8000/community',
            'location': 'internal',
            'type': 'container',
            'method': 'POST',
            'timeout': 30,
            'headers': {'Content-Type': 'application/json'},
            'auth_required': False,
            'rate_limit': 10,  # 10 requests per minute
            'is_active': True,
            'module_type': 'local',
            'module_id': 'community',
            'version': '1.0.0'
        },

        # Reputation commands (local container)
        {
            'command': 'reputation',
            'prefix': '!',
            'description': 'View reputation scores and statistics',
            'location_url': 'http://reputation-module:8000/reputation',
            'location': 'internal',
            'type': 'container',
            'method': 'POST',
            'timeout': 15,
            'headers': {'Content-Type': 'application/json'},
            'auth_required': False,
            'rate_limit': 20,  # 20 requests per minute
            'is_active': True,
            'module_type': 'local',
            'module_id': 'reputation',
            'version': '1.0.0'
        },

        # Score command (alias for reputation)
        {
            'command': 'score',
            'prefix': '!',
            'description': 'View your reputation score',
            'location_url': 'http://reputation-module:8000/reputation/score',
            'location': 'internal',
            'type': 'container',
            'method': 'POST',
            'timeout': 15,
            'headers': {'Content-Type': 'application/json'},
            'auth_required': False,
            'rate_limit': 30,  # 30 requests per minute
            'is_active': True,
            'module_type': 'local',
            'module_id': 'reputation',
            'version': '1.0.0'
        },

        # Leaderboard command
        {
            'command': 'leaderboard',
            'prefix': '!',
            'description': 'View reputation leaderboard for current community',
            'location_url': 'http://reputation-module:8000/reputation/leaderboard',
            'location': 'internal',
            'type': 'container',
            'method': 'POST',
            'timeout': 20,
            'headers': {'Content-Type': 'application/json'},
            'auth_required': False,
            'rate_limit': 15,  # 15 requests per minute
            'is_active': True,
            'module_type': 'local',
            'module_id': 'reputation',
            'version': '1.0.0'
        }
    ]

    try:
        for cmd in commands:
            # Check if command already exists
            existing_cmd = db(
                (db.commands.command == cmd['command']) &
                (db.commands.prefix == cmd['prefix']) &
                (db.commands.is_active == True)
            ).select().first()

            if existing_cmd:
                print(f"Command '{cmd['prefix']}{cmd['command']}' already exists, skipping...")
                continue

            # Insert new command
            cmd_id = db.commands.insert(
                command=cmd['command'],
                prefix=cmd['prefix'],
                description=cmd['description'],
                location_url=cmd['location_url'],
                location=cmd['location'],
                type=cmd['type'],
                method=cmd['method'],
                timeout=cmd['timeout'],
                headers=cmd['headers'],
                auth_required=cmd['auth_required'],
                rate_limit=cmd['rate_limit'],
                is_active=cmd['is_active'],
                module_type=cmd['module_type'],
                module_id=cmd['module_id'],
                version=cmd['version'],
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow()
            )

            print(f"Added command '{cmd['prefix']}{cmd['command']}' with ID {cmd_id}")

        db.commit()
        print("✅ All commands added successfully!")

    except Exception as e:
        print(f"❌ Error adding commands: {str(e)}")
        db.rollback()
        raise


def setup_entities():
    """Add default entities for testing"""

    # Default entities to add
    entities = [
        {
            'entity_id': 'discord:test_guild:general',
            'platform': 'discord',
            'server_id': 'test_guild',
            'channel_id': 'general',
            'owner': 'test_user',
            'is_active': True,
            'config': {'test': True}
        },
        {
            'entity_id': 'slack:test_team:general',
            'platform': 'slack',
            'server_id': 'test_team',
            'channel_id': 'general',
            'owner': 'test_user',
            'is_active': True,
            'config': {'test': True}
        },
        {
            'entity_id': 'twitch:test_channel:',
            'platform': 'twitch',
            'server_id': 'test_channel',
            'channel_id': '',
            'owner': 'test_user',
            'is_active': True,
            'config': {'test': True}
        }
    ]

    try:
        for entity in entities:
            # Check if entity already exists
            existing_entity = db(
                (db.entities.entity_id == entity['entity_id']) &
                (db.entities.is_active == True)
            ).select().first()

            if existing_entity:
                print(f"Entity '{entity['entity_id']}' already exists, skipping...")
                continue

            # Insert new entity
            entity_id = db.entities.insert(
                entity_id=entity['entity_id'],
                platform=entity['platform'],
                server_id=entity['server_id'],
                channel_id=entity['channel_id'],
                owner=entity['owner'],
                is_active=entity['is_active'],
                config=entity['config'],
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow()
            )

            print(f"Added entity '{entity['entity_id']}' with ID {entity_id}")

        db.commit()
        print("✅ All entities added successfully!")

    except Exception as e:
        print(f"❌ Error adding entities: {str(e)}")
        db.rollback()
        raise


if __name__ == "__main__":
    print("Setting up Waddles commands and entities...")

    # Setup commands
    setup_commands()

    # Setup entities
    setup_entities()

    print("🎉 Setup complete!")
