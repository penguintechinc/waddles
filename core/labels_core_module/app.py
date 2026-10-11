"""
Labels Core Module for Waddles - Flask/Quart Implementation
Universal label management system supporting any entity type
"""
import asyncio
import os
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from quart import Blueprint, Quart, request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), 'libs'))

from config import Config  # noqa: E402
from flask_core import (  # noqa: E402
    async_endpoint,
    bind_community_read_tables,
    create_health_blueprint,
    error_response,
    init_database,
    install_community_scoped_auth,
    install_rate_limiting,
    install_security_headers,
    setup_aaa_logging,
    success_response,
)

app = Quart(__name__)
# security.md A05 hardening -- JSON-only service, default deny-everything CSP.
install_security_headers(app)

# Register health/metrics endpoints
health_bp = create_health_blueprint(Config.MODULE_NAME, Config.MODULE_VERSION)
app.register_blueprint(health_bp)

api_bp = Blueprint('api', __name__, url_prefix='/api/v1')
logger = setup_aaa_logging(Config.MODULE_NAME, Config.MODULE_VERSION)

# SECURITY (C6, A01 -- unauthenticated access): every api_bp route had ZERO
# authentication -- any caller reaching this service's network address
# could read/write ANY community's labels. Registered once for the whole
# blueprint so a route added later can't ship without this check by
# omission -- see flask_core.community_access module docstring.
install_community_scoped_auth(api_bp)

# SECURITY (A04): every api_bp route had zero rate limiting -- shared
# global before_request hook, see flask_core.http_rate_limit module
# docstring.
install_rate_limiting(app, namespace=Config.MODULE_NAME)

dal = None

# Supported entity types - extensible for any item type
ENTITY_TYPES = [
    'user',           # User labels
    'module',         # Module labels
    'community',      # Community labels
    'entityGroup',    # Entity group labels
    'item',           # Generic items (inventory, resources, etc.)
    'event',          # Calendar events
    'memory',         # Memories (quotes, urls, notes)
    'playlist',       # Music playlists
    'browser_source', # Browser sources
    'command',        # Custom commands
    'alias',          # Command aliases
]

# Label limits per entity type
LABEL_LIMITS = {
    'community': 5,
    'user': 5,        # Per community
    'item': 10,       # Items can have more labels
    'event': 5,
    'memory': 10,
    'default': 5,
}


@dataclass
class LabelInfo:
    """Label information dataclass"""
    id: int
    name: str
    category: str
    description: str
    color: str
    icon: Optional[str]
    is_system: bool
    created_by: str
    created_at: str
    is_active: bool


@dataclass
class EntityLabelInfo:
    """Entity label assignment information"""
    id: int
    entity_id: str
    entity_type: str
    label_id: int
    label_name: str
    label_color: str
    label_icon: Optional[str]
    applied_by: str
    applied_at: str
    expires_at: Optional[str]
    metadata: Dict[str, Any]


def define_tables(db):
    """Define database tables for labels system"""
    # Labels table - stores label definitions
    db.define_table(
        'labels',
        db.Field('name', 'string', length=100, notnull=True),
        db.Field('category', 'string', length=50, notnull=True),
        db.Field('description', 'text'),
        db.Field('color', 'string', length=7, default='#6366f1'),
        db.Field('icon', 'string', length=50),
        db.Field('is_system', 'boolean', default=False),
        db.Field('created_by', 'string', length=100, notnull=True),
        db.Field('created_at', 'datetime', default=datetime.utcnow),
        db.Field('updated_at', 'datetime', update=datetime.utcnow),
        db.Field('is_active', 'boolean', default=True),
        migrate=False
    )

    # Entity labels table - stores label assignments to entities
    db.define_table(
        'entity_labels',
        db.Field('entity_id', 'string', length=255, notnull=True),
        db.Field('entity_type', 'string', length=50, notnull=True),
        db.Field('label_id', 'reference labels', notnull=True),
        db.Field('community_id', 'string', length=100),
        db.Field('applied_by', 'string', length=100, notnull=True),
        db.Field('applied_at', 'datetime', default=datetime.utcnow),
        db.Field('expires_at', 'datetime'),
        db.Field('metadata', 'json'),
        db.Field('is_active', 'boolean', default=True),
        migrate=False
    )

    db.commit()
    return db


@app.before_serving
async def startup():
    """Initialize database on startup"""
    global dal
    logger.system("Starting labels_core_module", action="startup")
    dal = init_database(Config.DATABASE_URL)
    define_tables(dal.dal)
    # SECURITY (C6): `async_dal` (this AsyncDAL wrapper) and `dal` (the
    # real pydal DAL it proxies attribute access to) are stored under the
    # two config keys `install_community_scoped_auth`/`community_access`
    # expect -- matches `core/svc_streaming/app.py`'s established
    # convention.
    app.config['async_dal'] = dal
    app.config['dal'] = dal.dal
    bind_community_read_tables(app.config['dal'], migrate=Config.DB_MIGRATE)
    logger.system("labels_core_module started", result="SUCCESS")


# ============================================================================
# Label Management Endpoints
# ============================================================================

@api_bp.route('/labels', methods=['GET'])
@async_endpoint
async def list_labels():
    """
    List all labels with optional filtering.
    Query params: category, is_system, search
    """
    try:
        category = request.args.get('category')
        is_system = request.args.get('is_system')
        search = request.args.get('search', '').strip()

        query = dal.labels.is_active == True  # noqa: E712

        if category:
            if category not in ENTITY_TYPES:
                return error_response(f"Invalid category. Must be one of: {ENTITY_TYPES}", 400)
            query &= dal.labels.category == category

        if is_system is not None:
            query &= dal.labels.is_system == (is_system.lower() == 'true')

        if search:
            query &= dal.labels.name.contains(search)

        labels = dal(query).select(orderby=dal.labels.category | dal.labels.name)

        label_list = []
        for label in labels:
            label_info = LabelInfo(
                id=label.id,
                name=label.name,
                category=label.category,
                description=label.description or '',
                color=label.color or '#6366f1',
                icon=label.icon,
                is_system=label.is_system,
                created_by=label.created_by,
                created_at=label.created_at.isoformat() if label.created_at else '',
                is_active=label.is_active
            )
            label_list.append(asdict(label_info))

        return success_response({
            'labels': label_list,
            'total': len(label_list),
            'supported_types': ENTITY_TYPES
        })

    except Exception as e:
        logger.error(f"Error listing labels: {e}")
        return error_response(f"Error listing labels: {str(e)}", 500)


@api_bp.route('/labels', methods=['POST'])
@async_endpoint
async def create_label():
    """
    Create a new label.
    Body: name, category, description?, color?, icon?, created_by
    """
    try:
        data = await request.get_json()
        if not data:
            return error_response("No data provided", 400)

        name = data.get('name', '').strip()
        category = data.get('category', '').strip()
        description = data.get('description', '').strip()
        color = data.get('color', '#6366f1').strip()
        icon = data.get('icon', '').strip() or None
        created_by = data.get('created_by', '').strip()

        if not all([name, category, created_by]):
            return error_response("Missing required fields: name, category, created_by", 400)

        if category not in ENTITY_TYPES:
            return error_response(f"Invalid category. Must be one of: {ENTITY_TYPES}", 400)

        # Check if label already exists
        existing = dal(
            (dal.labels.name == name) &
            (dal.labels.category == category) &
            (dal.labels.is_active == True)  # noqa: E712
        ).select().first()

        if existing:
            return error_response(f"Label '{name}' already exists in category '{category}'", 409)

        # Create label
        label_id = dal.labels.insert(
            name=name,
            category=category,
            description=description,
            color=color,
            icon=icon,
            created_by=created_by
        )
        dal.commit()

        logger.audit(
            action="create_label",
            user=created_by,
            community="",
            result="SUCCESS",
            detail=f"Label created: {name}"
        )

        return success_response({
            'message': f"Label '{name}' created successfully",
            'label_id': label_id
        }, 201)

    except Exception as e:
        logger.error(f"Error creating label: {e}")
        return error_response(f"Error creating label: {str(e)}", 500)


@api_bp.route('/labels/<int:label_id>', methods=['GET'])
@async_endpoint
async def get_label(label_id: int):
    """Get a specific label by ID"""
    try:
        label = dal(dal.labels.id == label_id).select().first()
        if not label:
            return error_response("Label not found", 404)

        label_info = LabelInfo(
            id=label.id,
            name=label.name,
            category=label.category,
            description=label.description or '',
            color=label.color or '#6366f1',
            icon=label.icon,
            is_system=label.is_system,
            created_by=label.created_by,
            created_at=label.created_at.isoformat() if label.created_at else '',
            is_active=label.is_active
        )

        return success_response({'label': asdict(label_info)})

    except Exception as e:
        logger.error(f"Error getting label: {e}")
        return error_response(f"Error getting label: {str(e)}", 500)


@api_bp.route('/labels/<int:label_id>', methods=['PUT'])
@async_endpoint
async def update_label(label_id: int):
    """Update an existing label"""
    try:
        data = await request.get_json()
        if not data:
            return error_response("No data provided", 400)

        label = dal(dal.labels.id == label_id).select().first()
        if not label:
            return error_response("Label not found", 404)

        if label.is_system:
            return error_response("Cannot modify system labels", 403)

        update_data = {}
        if 'name' in data:
            update_data['name'] = data['name'].strip()
        if 'description' in data:
            update_data['description'] = data['description'].strip()
        if 'color' in data:
            update_data['color'] = data['color'].strip()
        if 'icon' in data:
            update_data['icon'] = data['icon'].strip() or None

        if update_data:
            dal(dal.labels.id == label_id).update(**update_data)
            dal.commit()

        return success_response({'message': 'Label updated successfully'})

    except Exception as e:
        logger.error(f"Error updating label: {e}")
        return error_response(f"Error updating label: {str(e)}", 500)


@api_bp.route('/labels/<int:label_id>', methods=['DELETE'])
@async_endpoint
async def delete_label(label_id: int):
    """Soft delete a label"""
    try:
        label = dal(dal.labels.id == label_id).select().first()
        if not label:
            return error_response("Label not found", 404)

        if label.is_system:
            return error_response("Cannot delete system labels", 403)

        # Soft delete label and all assignments
        dal(dal.labels.id == label_id).update(is_active=False)
        dal(dal.entity_labels.label_id == label_id).update(is_active=False)
        dal.commit()

        return success_response({'message': 'Label deleted successfully'})

    except Exception as e:
        logger.error(f"Error deleting label: {e}")
        return error_response(f"Error deleting label: {str(e)}", 500)


# ============================================================================
# Entity Label Assignment Endpoints
# ============================================================================

@api_bp.route('/labels/apply', methods=['POST'])
@async_endpoint
async def apply_label():
    """
    Apply a label to any entity.
    Body: entity_id, entity_type, label_id, applied_by, community_id?, expires_at?, metadata?
    """
    try:
        data = await request.get_json()
        if not data:
            return error_response("No data provided", 400)

        # Support bulk operations
        if isinstance(data, list):
            return await apply_labels_bulk(data)

        entity_id = str(data.get('entity_id', '')).strip()
        entity_type = data.get('entity_type', '').strip()
        label_id = data.get('label_id')
        applied_by = data.get('applied_by', '').strip()
        community_id = data.get('community_id', '').strip() or None
        expires_at = data.get('expires_at')
        metadata = data.get('metadata', {})

        if not all([entity_id, entity_type, label_id, applied_by]):
            return error_response("Missing required fields: entity_id, entity_type, label_id, applied_by", 400)

        if entity_type not in ENTITY_TYPES:
            return error_response(f"Invalid entity_type. Must be one of: {ENTITY_TYPES}", 400)

        # Check if label exists
        label = dal(dal.labels.id == label_id).select().first()
        if not label:
            return error_response("Label not found", 404)

        # Check label limit for entity type
        limit = LABEL_LIMITS.get(entity_type, LABEL_LIMITS['default'])
        current_count = dal(
            (dal.entity_labels.entity_id == entity_id) &
            (dal.entity_labels.entity_type == entity_type) &
            (dal.entity_labels.is_active == True)  # noqa: E712
        ).count()

        if current_count >= limit:
            return error_response(f"{entity_type} can have maximum {limit} labels", 400)

        # Parse expires_at if provided
        expires_datetime = None
        if expires_at:
            try:
                expires_datetime = datetime.fromisoformat(expires_at.replace('Z', '+00:00'))
            except ValueError:
                return error_response("Invalid expires_at format. Use ISO 8601", 400)

        # Check if label is already applied
        existing = dal(
            (dal.entity_labels.entity_id == entity_id) &
            (dal.entity_labels.entity_type == entity_type) &
            (dal.entity_labels.label_id == label_id) &
            (dal.entity_labels.is_active == True)  # noqa: E712
        ).select().first()

        if existing:
            return error_response("Label already applied to this entity", 409)

        # Apply label
        entity_label_id = dal.entity_labels.insert(
            entity_id=entity_id,
            entity_type=entity_type,
            label_id=label_id,
            community_id=community_id,
            applied_by=applied_by,
            expires_at=expires_datetime,
            metadata=metadata
        )
        dal.commit()

        logger.audit(
            action="apply_label",
            user=applied_by,
            community=str(community_id),
            result="SUCCESS",
            detail=f"Label applied: {label.name} to {entity_type}:{entity_id}"
        )

        return success_response({
            'message': 'Label applied successfully',
            'entity_label_id': entity_label_id
        }, 201)

    except Exception as e:
        logger.error(f"Error applying label: {e}")
        return error_response(f"Error applying label: {str(e)}", 500)


async def apply_labels_bulk(label_applications: List[Dict]) -> tuple:
    """Apply multiple labels in bulk"""
    try:
        if len(label_applications) > 1000:
            return error_response("Bulk operation limited to 1000 items", 400)

        results = []
        successful = 0
        failed = 0

        for app in label_applications:
            try:
                entity_id = str(app.get('entity_id', '')).strip()
                entity_type = app.get('entity_type', '').strip()
                label_id = app.get('label_id')
                applied_by = app.get('applied_by', '').strip()

                if not all([entity_id, entity_type, label_id, applied_by]):
                    results.append({'success': False, 'error': 'Missing required fields', 'input': app})
                    failed += 1
                    continue

                if entity_type not in ENTITY_TYPES:
                    results.append({'success': False, 'error': 'Invalid entity_type', 'input': app})
                    failed += 1
                    continue

                # Check existing
                existing = dal(
                    (dal.entity_labels.entity_id == entity_id) &
                    (dal.entity_labels.entity_type == entity_type) &
                    (dal.entity_labels.label_id == label_id) &
                    (dal.entity_labels.is_active == True)  # noqa: E712
                ).select().first()

                if existing:
                    results.append({'success': False, 'error': 'Label already applied', 'input': app})
                    failed += 1
                    continue

                # Apply
                entity_label_id = dal.entity_labels.insert(
                    entity_id=entity_id,
                    entity_type=entity_type,
                    label_id=label_id,
                    community_id=app.get('community_id'),
                    applied_by=applied_by,
                    expires_at=app.get('expires_at'),
                    metadata=app.get('metadata', {})
                )

                results.append({'success': True, 'entity_label_id': entity_label_id, 'input': app})
                successful += 1

            except Exception as e:
                results.append({'success': False, 'error': str(e), 'input': app})
                failed += 1

        dal.commit()

        return success_response({
            'message': f"Bulk operation completed: {successful} successful, {failed} failed",
            'results': results,
            'summary': {'total': len(label_applications), 'successful': successful, 'failed': failed}
        })

    except Exception as e:
        logger.error(f"Error in bulk label application: {e}")
        return error_response(f"Error in bulk label application: {str(e)}", 500)


@api_bp.route('/labels/remove', methods=['POST'])
@async_endpoint
async def remove_label():
    """
    Remove a label from an entity.
    Body: entity_id, entity_type, label_id
    """
    try:
        data = await request.get_json()
        if not data:
            return error_response("No data provided", 400)

        entity_id = str(data.get('entity_id', '')).strip()
        entity_type = data.get('entity_type', '').strip()
        label_id = data.get('label_id')

        if not all([entity_id, entity_type, label_id]):
            return error_response("Missing required fields: entity_id, entity_type, label_id", 400)

        # Find and deactivate the assignment
        assignment = dal(
            (dal.entity_labels.entity_id == entity_id) &
            (dal.entity_labels.entity_type == entity_type) &
            (dal.entity_labels.label_id == label_id) &
            (dal.entity_labels.is_active == True)  # noqa: E712
        ).select().first()

        if not assignment:
            return error_response("Label assignment not found", 404)

        dal(dal.entity_labels.id == assignment.id).update(is_active=False)
        dal.commit()

        return success_response({'message': 'Label removed successfully'})

    except Exception as e:
        logger.error(f"Error removing label: {e}")
        return error_response(f"Error removing label: {str(e)}", 500)


@api_bp.route('/entity/<entity_type>/<entity_id>/labels', methods=['GET'])
@async_endpoint
async def get_entity_labels(entity_type: str, entity_id: str):
    """
    Get all labels for a specific entity.
    """
    try:
        if entity_type not in ENTITY_TYPES:
            return error_response(f"Invalid entity_type. Must be one of: {ENTITY_TYPES}", 400)

        # Join entity_labels with labels to get label details
        query = (
            (dal.entity_labels.entity_id == entity_id) &
            (dal.entity_labels.entity_type == entity_type) &
            (dal.entity_labels.is_active == True)  # noqa: E712
        )

        entity_labels = dal(query).select(
            dal.entity_labels.ALL,
            dal.labels.name,
            dal.labels.color,
            dal.labels.icon,
            dal.labels.description,
            left=dal.labels.on(dal.labels.id == dal.entity_labels.label_id)
        )

        label_list = []
        for el in entity_labels:
            label_info = EntityLabelInfo(
                id=el.entity_labels.id,
                entity_id=el.entity_labels.entity_id,
                entity_type=el.entity_labels.entity_type,
                label_id=el.entity_labels.label_id,
                label_name=el.labels.name,
                label_color=el.labels.color or '#6366f1',
                label_icon=el.labels.icon,
                applied_by=el.entity_labels.applied_by,
                applied_at=el.entity_labels.applied_at.isoformat() if el.entity_labels.applied_at else '',
                expires_at=el.entity_labels.expires_at.isoformat() if el.entity_labels.expires_at else None,
                metadata=el.entity_labels.metadata or {}
            )
            label_list.append(asdict(label_info))

        return success_response({
            'entity_id': entity_id,
            'entity_type': entity_type,
            'labels': label_list,
            'total': len(label_list),
            'limit': LABEL_LIMITS.get(entity_type, LABEL_LIMITS['default'])
        })

    except Exception as e:
        logger.error(f"Error getting entity labels: {e}")
        return error_response(f"Error getting entity labels: {str(e)}", 500)


# ============================================================================
# Search Endpoints
# ============================================================================

@api_bp.route('/labels/search', methods=['GET'])
@async_endpoint
async def search_by_labels():
    """
    Search entities by labels.
    Query params: entity_type, labels (comma-separated), community_id?, match_all?
    """
    try:
        entity_type = request.args.get('entity_type', '').strip()
        label_names = request.args.get('labels', '').strip()
        community_id = request.args.get('community_id')
        match_all = request.args.get('match_all', 'false').lower() == 'true'
        limit = min(int(request.args.get('limit', '100')), 1000)

        if not entity_type or not label_names:
            return error_response("Missing required parameters: entity_type, labels", 400)

        if entity_type not in ENTITY_TYPES:
            return error_response(f"Invalid entity_type. Must be one of: {ENTITY_TYPES}", 400)

        label_list = [l.strip() for l in label_names.split(',') if l.strip()]

        # Get label IDs
        label_rows = dal(
            (dal.labels.name.belongs(label_list)) &
            (dal.labels.is_active == True)  # noqa: E712
        ).select(dal.labels.id, dal.labels.name)

        if not label_rows:
            return success_response({
                'entity_type': entity_type,
                'searched_labels': label_list,
                'results': [],
                'total': 0
            })

        label_ids = [r.id for r in label_rows]

        # Find entities with these labels
        query = (
            (dal.entity_labels.label_id.belongs(label_ids)) &
            (dal.entity_labels.entity_type == entity_type) &
            (dal.entity_labels.is_active == True)  # noqa: E712
        )

        if community_id:
            query &= dal.entity_labels.community_id == community_id

        entity_labels = dal(query).select(
            dal.entity_labels.entity_id,
            dal.entity_labels.label_id,
            dal.labels.name,
            dal.labels.color,
            left=dal.labels.on(dal.labels.id == dal.entity_labels.label_id)
        )

        # Group by entity
        entity_results = {}
        for el in entity_labels:
            eid = el.entity_labels.entity_id
            if eid not in entity_results:
                entity_results[eid] = []
            entity_results[eid].append({
                'label_id': el.entity_labels.label_id,
                'label_name': el.labels.name,
                'label_color': el.labels.color
            })

        # Filter for match_all if requested
        if match_all and len(label_list) > 1:
            filtered = {}
            for eid, labels in entity_results.items():
                found_names = {l['label_name'] for l in labels}
                if all(ln in found_names for ln in label_list):
                    filtered[eid] = labels
            entity_results = filtered

        # Build results list
        results = []
        for eid, labels in list(entity_results.items())[:limit]:
            results.append({
                'entity_id': eid,
                'entity_type': entity_type,
                'labels': labels,
                'match_count': len(labels)
            })

        return success_response({
            'entity_type': entity_type,
            'searched_labels': label_list,
            'match_all': match_all,
            'results': results,
            'total': len(results)
        })

    except Exception as e:
        logger.error(f"Error searching by labels: {e}")
        return error_response(f"Error searching by labels: {str(e)}", 500)


@api_bp.route('/status')
@async_endpoint
async def status():
    """Status endpoint"""
    return success_response({
        'status': 'operational',
        'module': Config.MODULE_NAME,
        'version': Config.MODULE_VERSION,
        'supported_entity_types': ENTITY_TYPES,
        'label_limits': LABEL_LIMITS
    })


app.register_blueprint(api_bp)

if __name__ == '__main__':
    import hypercorn.asyncio
    from hypercorn.config import Config as HyperConfig
    hyper_config = HyperConfig()
    hyper_config.bind = [f"0.0.0.0:{Config.MODULE_PORT}"]
    asyncio.run(hypercorn.asyncio.serve(app, hyper_config))
