"""
Engagement Module - Polls and Forms
Manages community polls and forms with visibility controls.

Stateless, clusterable container with:
- REST API for polls/forms CRUD
- Visibility-based access control
- PyDAL for database operations
"""
import asyncio
import hashlib
import logging
import logging.handlers
import sys
from datetime import datetime, timedelta
from enum import Enum
from typing import Dict, Any, Optional, List

from quart import Quart, request, jsonify
import jwt
from pydal import DAL, Field

from flask_core import install_security_headers
from flask_core.auth import DEFAULT_TENANT_SLUG
from flask_core.community_access import bind_shared_read_tables, install_community_scoped_auth
from flask_core.feature_flags import feature_enabled
from flask_core.http_rate_limit import install_rate_limiting

from config import Config


class VisibilityLevel(str, Enum):
    PUBLIC = "public"
    REGISTERED = "registered"
    COMMUNITY = "community"
    ADMINS = "admins"


class DefaultConfig(dict):
    def __init__(self, root_path=None, defaults=None):
        super().__init__()
        if defaults:
            self.update(defaults)
        self.setdefault('PROVIDE_AUTOMATIC_OPTIONS', True)
        self.setdefault('JSON_SORT_KEYS', False)
        self.setdefault('PROPAGATE_EXCEPTIONS', True)
        self.setdefault('MAX_CONTENT_LENGTH', 16 * 1024 * 1024)

    def __getitem__(self, key):
        try:
            return super().__getitem__(key)
        except KeyError:
            defaults = {
                'PROVIDE_AUTOMATIC_OPTIONS': True,
                'JSON_SORT_KEYS': False,
                'PROPAGATE_EXCEPTIONS': True,
                'MAX_CONTENT_LENGTH': 16 * 1024 * 1024,
            }
            if key in defaults:
                return defaults[key]
            raise


class PreConfiguredQuart(Quart):
    config_class = DefaultConfig


app = PreConfiguredQuart(__name__)
# security.md A05 hardening -- JSON-only service, default deny-everything CSP.
install_security_headers(app)
config = Config()

for key in dir(config):
    if key.isupper() and not key.startswith('_'):
        app.config[key] = getattr(config, key)


# Logging
def setup_logging():
    log_format = "[%(asctime)s] %(levelname)s %(name)s:%(funcName)s:%(lineno)d %(message)s"
    formatter = logging.Formatter(log_format)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, config.LOG_LEVEL.upper()))
    root_logger.addHandler(console_handler)


setup_logging()
logger = logging.getLogger(__name__)

# Database
db = DAL(
    config.DATABASE_URL,
    folder="databases",
    pool_size=config.DB_POOL_SIZE,
    # `migrate_enabled` is a DAL-level ceiling -- False blocks migration
    # for every table regardless of any per-`define_table` `migrate=`
    # argument. Every business table below still passes its own explicit
    # `migrate=False` (unchanged prod behavior); this only needs to be
    # `config.DB_MIGRATE`-controlled so `bind_shared_read_tables`'s own
    # `migrate=` argument (for the community-scoped-authz tables) can take
    # effect in tests against a throwaway sqlite DB.
    migrate_enabled=config.DB_MIGRATE,
)


class _SyncDalAsyncAdapter:
    """Minimal `.select_async()` shim over this module's own sync `db`.

    `install_community_scoped_auth`/`community_access` are written against
    `flask_core.database.AsyncDAL`'s async interface; this module predates
    that convention and calls pydal synchronously throughout (a pre-
    existing, separate issue, out of scope here). Rather than rewriting
    this module's DB layer, this adapter runs the one query
    `community_access` needs (`Set.select(*fields)`) in a worker thread --
    real async, just not `flask_core.database.AsyncDAL`.
    """

    def __init__(self, sync_dal) -> None:  # noqa: ANN001
        self.dal = sync_dal

    async def select_async(self, query_set, *fields):  # noqa: ANN001, ANN201
        return await asyncio.to_thread(
            query_set.select, *fields
        ) if fields else await asyncio.to_thread(query_set.select)


# SECURITY (C6, A01 -- unauthenticated access): every route on `app`
# (`/api/v1/polls`, `/api/v1/forms`, ...) had ZERO tenant/auth enforcement
# on GET routes, and the module-local `require_auth` decorator on the
# write routes referenced `config.JWT_SECRET_KEY`, an attribute that was
# never defined on `Config` -- every "protected" write crashed with
# AttributeError before ever checking a token (fail-open in effect: no
# request was ever actually authenticated). Standardized on the same
# `SECRET_KEY`-based JWT + tenant check every other core service uses
# (`flask_core.community_access`), applied once for the whole `app` since
# this module registers routes directly rather than via a `Blueprint`;
# `/health` stays exempt for K8s liveness/readiness probes.
app.config["dal"] = db
app.config["async_dal"] = _SyncDalAsyncAdapter(db)
bind_shared_read_tables(db, migrate=config.DB_MIGRATE)
install_community_scoped_auth(app, exempt_paths=frozenset({"/health"}))

# SECURITY (A04): every route on `app` had zero rate limiting -- shared
# global before_request hook, see flask_core.http_rate_limit module
# docstring. `/health` stays exempt (DEFAULT_EXEMPT_PATHS).
install_rate_limiting(app, namespace=config.MODULE_NAME, redis_url=config.REDIS_URL)


def init_database():
    """Initialize database tables."""

    db.define_table(
        'community_polls',
        Field('community_id', 'integer', notnull=True),
        Field('created_by', 'integer', notnull=True),
        Field('title', 'string', length=255, notnull=True),
        Field('description', 'text'),
        Field('view_visibility', 'string', length=20, default='community'),
        Field('submit_visibility', 'string', length=20, default='community'),
        Field('allow_multiple_choices', 'boolean', default=False),
        Field('max_choices', 'integer', default=1),
        Field('expires_at', 'datetime'),
        Field('is_active', 'boolean', default=True),
        Field('created_at', 'datetime', default=datetime.utcnow),
        Field('updated_at', 'datetime', update=datetime.utcnow),
        migrate=False
    )

    db.define_table(
        'poll_options',
        Field('poll_id', 'reference community_polls', notnull=True),
        Field('option_text', 'string', length=500, notnull=True),
        Field('sort_order', 'integer', default=0),
        Field('created_at', 'datetime', default=datetime.utcnow),
        migrate=False
    )

    db.define_table(
        'poll_votes',
        Field('poll_id', 'reference community_polls', notnull=True),
        Field('option_id', 'reference poll_options', notnull=True),
        Field('user_id', 'integer'),
        Field('ip_hash', 'string', length=64),
        Field('voted_at', 'datetime', default=datetime.utcnow),
        migrate=False
    )

    db.define_table(
        'community_forms',
        Field('community_id', 'integer', notnull=True),
        Field('created_by', 'integer', notnull=True),
        Field('title', 'string', length=255, notnull=True),
        Field('description', 'text'),
        Field('view_visibility', 'string', length=20, default='community'),
        Field('submit_visibility', 'string', length=20, default='community'),
        Field('allow_anonymous', 'boolean', default=False),
        Field('submit_once_per_user', 'boolean', default=True),
        Field('is_active', 'boolean', default=True),
        Field('created_at', 'datetime', default=datetime.utcnow),
        Field('updated_at', 'datetime', update=datetime.utcnow),
        migrate=False
    )

    db.define_table(
        'form_fields',
        Field('form_id', 'reference community_forms', notnull=True),
        Field('field_type', 'string', length=50, notnull=True),
        Field('label', 'string', length=255, notnull=True),
        Field('placeholder', 'string', length=255),
        Field('is_required', 'boolean', default=False),
        Field('options_json', 'json'),
        Field('validation_json', 'json'),
        Field('sort_order', 'integer', default=0),
        Field('created_at', 'datetime', default=datetime.utcnow),
        migrate=False
    )

    db.define_table(
        'form_submissions',
        Field('form_id', 'reference community_forms', notnull=True),
        Field('user_id', 'integer'),
        Field('ip_hash', 'string', length=64),
        Field('submitted_at', 'datetime', default=datetime.utcnow),
        migrate=False
    )

    db.define_table(
        'form_field_values',
        Field('submission_id', 'reference form_submissions', notnull=True),
        Field('field_id', 'reference form_fields', notnull=True),
        Field('value_text', 'text'),
        Field('value_json', 'json'),
        Field('created_at', 'datetime', default=datetime.utcnow),
        migrate=False
    )

    db.commit()
    logger.info("Database tables initialized")


# JWT Auth
def verify_jwt_token(token: str) -> Optional[Dict[str, Any]]:
    try:
        # SECURITY (C6): was `config.JWT_SECRET_KEY`, an attribute never
        # defined on `Config` -- every call crashed with AttributeError
        # before ever checking a token. `Config.JWT_SECRET` is the field
        # that's actually defined and validated (`Config.validate()`
        # requires it).
        return jwt.decode(token, config.JWT_SECRET, algorithms=[config.JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None


def require_auth(f):
    async def decorated(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return jsonify({"error": "Missing or invalid Authorization header"}), 401
        token = auth_header.split(" ", 1)[1]
        payload = verify_jwt_token(token)
        if not payload:
            return jsonify({"error": "Invalid or expired token"}), 401
        request.auth_payload = payload
        return await f(*args, **kwargs)
    decorated.__name__ = f.__name__
    return decorated


# Visibility helpers
def check_visibility(visibility: str, user_id: Optional[int], community_id: int, is_admin: bool) -> bool:
    if visibility == VisibilityLevel.PUBLIC.value:
        return True
    if visibility == VisibilityLevel.REGISTERED.value:
        return user_id is not None
    if visibility == VisibilityLevel.COMMUNITY.value:
        return user_id is not None  # Would check community membership via gRPC
    if visibility == VisibilityLevel.ADMINS.value:
        return is_admin
    return False


def hash_ip(ip: str) -> str:
    return hashlib.sha256(ip.encode()).hexdigest()


# Format helpers
def format_poll(row, options=None, vote_counts=None) -> Dict[str, Any]:
    result = {
        "id": row.id,
        "community_id": row.community_id,
        "title": row.title,
        "description": row.description,
        "view_visibility": row.view_visibility,
        "submit_visibility": row.submit_visibility,
        "allow_multiple_choices": row.allow_multiple_choices,
        "max_choices": row.max_choices,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        "is_active": row.is_active,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }
    if options:
        result["options"] = options
    if vote_counts:
        result["vote_counts"] = vote_counts
    return result


def format_form(row, fields=None) -> Dict[str, Any]:
    result = {
        "id": row.id,
        "community_id": row.community_id,
        "title": row.title,
        "description": row.description,
        "view_visibility": row.view_visibility,
        "submit_visibility": row.submit_visibility,
        "allow_anonymous": row.allow_anonymous,
        "submit_once_per_user": row.submit_once_per_user,
        "is_active": row.is_active,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }
    if fields:
        result["fields"] = fields
    return result


# Endpoints
@app.route("/health", methods=["GET"])
async def health():
    try:
        db.executesql("SELECT 1")
        return jsonify({
            "status": "healthy",
            "module": "engagement_module",
            "version": config.MODULE_VERSION,
            "timestamp": datetime.utcnow().isoformat()
        })
    except Exception as e:
        return jsonify({"status": "unhealthy", "error": str(e)}), 503


# Poll endpoints
@app.route("/api/v1/polls", methods=["POST"])
@require_auth
async def create_poll():
    try:
        data = await request.get_json()
        community_id = data.get("community_id")
        title = data.get("title")
        options = data.get("options", [])

        # v3 Feature gate (marketing.engagement / waddles.marketing.engagement,
        # free tier). The paid Marketing Features (marketing.scheduling,
        # marketing.publishing) follow this identical one-line guard at their
        # own handler entry points once wired.
        tenant_slug = request.auth_payload.get("tenant", DEFAULT_TENANT_SLUG)
        if not await feature_enabled(
            "waddles.marketing.engagement", tenant=tenant_slug, community=community_id
        ):
            return jsonify({"error": "engagement is not available"}), 404

        if not all([community_id, title, options]):
            return jsonify({"error": "community_id, title, and options required"}), 400

        if len(options) < 2:
            return jsonify({"error": "At least 2 options required"}), 400

        poll_id = db.community_polls.insert(
            community_id=community_id,
            created_by=request.auth_payload.get("user_id"),
            title=title,
            description=data.get("description"),
            view_visibility=data.get("view_visibility", "community"),
            submit_visibility=data.get("submit_visibility", "community"),
            allow_multiple_choices=data.get("allow_multiple_choices", False),
            max_choices=data.get("max_choices", 1),
            expires_at=data.get("expires_at"),
            is_active=True
        )

        for i, opt in enumerate(options):
            db.poll_options.insert(
                poll_id=poll_id,
                option_text=opt,
                sort_order=i
            )

        db.commit()
        poll = db.community_polls[poll_id]
        opts = db(db.poll_options.poll_id == poll_id).select(orderby=db.poll_options.sort_order)

        return jsonify({
            "success": True,
            "poll": format_poll(poll, [{"id": o.id, "text": o.option_text} for o in opts])
        }), 201

    except Exception as e:
        logger.error(f"Create poll failed: {e}", exc_info=True)
        db.rollback()
        return jsonify({"error": str(e)}), 500


@app.route("/api/v1/polls/<int:poll_id>", methods=["GET"])
async def get_poll(poll_id: int):
    try:
        poll = db.community_polls[poll_id]
        if not poll:
            return jsonify({"error": "Poll not found"}), 404

        opts = db(db.poll_options.poll_id == poll_id).select(orderby=db.poll_options.sort_order)
        options = [{"id": o.id, "text": o.option_text} for o in opts]

        # Get vote counts
        vote_counts = {}
        for opt in opts:
            count = db(db.poll_votes.option_id == opt.id).count()
            vote_counts[opt.id] = count

        return jsonify({
            "success": True,
            "poll": format_poll(poll, options, vote_counts)
        })

    except Exception as e:
        logger.error(f"Get poll failed: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/api/v1/polls/community/<int:community_id>", methods=["GET"])
async def list_polls(community_id: int):
    try:
        polls = db(
            (db.community_polls.community_id == community_id) &
            (db.community_polls.is_active == True)
        ).select(orderby=~db.community_polls.created_at)

        return jsonify({
            "success": True,
            "count": len(polls),
            "polls": [format_poll(p) for p in polls]
        })

    except Exception as e:
        logger.error(f"List polls failed: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/api/v1/polls/<int:poll_id>/vote", methods=["POST"])
@require_auth
async def vote(poll_id: int):
    try:
        data = await request.get_json()
        option_ids = data.get("option_ids", [])
        user_id = request.auth_payload.get("user_id")

        poll = db.community_polls[poll_id]
        if not poll or not poll.is_active:
            return jsonify({"error": "Poll not found or inactive"}), 404

        if poll.expires_at and poll.expires_at < datetime.utcnow():
            return jsonify({"error": "Poll has expired"}), 400

        if not poll.allow_multiple_choices and len(option_ids) > 1:
            return jsonify({"error": "Only one choice allowed"}), 400

        if poll.allow_multiple_choices and len(option_ids) > poll.max_choices:
            return jsonify({"error": f"Maximum {poll.max_choices} choices allowed"}), 400

        # Check if already voted
        existing = db(
            (db.poll_votes.poll_id == poll_id) &
            (db.poll_votes.user_id == user_id)
        ).select().first()

        if existing:
            return jsonify({"error": "Already voted"}), 409

        # Record votes
        for opt_id in option_ids:
            db.poll_votes.insert(
                poll_id=poll_id,
                option_id=opt_id,
                user_id=user_id
            )

        db.commit()
        return jsonify({"success": True, "message": "Vote recorded"})

    except Exception as e:
        logger.error(f"Vote failed: {e}", exc_info=True)
        db.rollback()
        return jsonify({"error": str(e)}), 500


# Form endpoints
@app.route("/api/v1/forms", methods=["POST"])
@require_auth
async def create_form():
    try:
        data = await request.get_json()
        community_id = data.get("community_id")
        title = data.get("title")
        fields = data.get("fields", [])

        if not all([community_id, title]):
            return jsonify({"error": "community_id and title required"}), 400

        form_id = db.community_forms.insert(
            community_id=community_id,
            created_by=request.auth_payload.get("user_id"),
            title=title,
            description=data.get("description"),
            view_visibility=data.get("view_visibility", "community"),
            submit_visibility=data.get("submit_visibility", "community"),
            allow_anonymous=data.get("allow_anonymous", False),
            submit_once_per_user=data.get("submit_once_per_user", True),
            is_active=True
        )

        for i, field in enumerate(fields):
            db.form_fields.insert(
                form_id=form_id,
                field_type=field.get("type", "text"),
                label=field.get("label"),
                placeholder=field.get("placeholder"),
                is_required=field.get("required", False),
                options_json=field.get("options"),
                validation_json=field.get("validation"),
                sort_order=i
            )

        db.commit()
        form = db.community_forms[form_id]

        return jsonify({
            "success": True,
            "form": format_form(form)
        }), 201

    except Exception as e:
        logger.error(f"Create form failed: {e}", exc_info=True)
        db.rollback()
        return jsonify({"error": str(e)}), 500


@app.route("/api/v1/forms/<int:form_id>", methods=["GET"])
async def get_form(form_id: int):
    try:
        form = db.community_forms[form_id]
        if not form:
            return jsonify({"error": "Form not found"}), 404

        fields = db(db.form_fields.form_id == form_id).select(orderby=db.form_fields.sort_order)
        field_list = [{
            "id": f.id,
            "type": f.field_type,
            "label": f.label,
            "placeholder": f.placeholder,
            "required": f.is_required,
            "options": f.options_json,
            "validation": f.validation_json
        } for f in fields]

        return jsonify({
            "success": True,
            "form": format_form(form, field_list)
        })

    except Exception as e:
        logger.error(f"Get form failed: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/api/v1/forms/community/<int:community_id>", methods=["GET"])
async def list_forms(community_id: int):
    try:
        forms = db(
            (db.community_forms.community_id == community_id) &
            (db.community_forms.is_active == True)
        ).select(orderby=~db.community_forms.created_at)

        return jsonify({
            "success": True,
            "count": len(forms),
            "forms": [format_form(f) for f in forms]
        })

    except Exception as e:
        logger.error(f"List forms failed: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/api/v1/forms/<int:form_id>/submit", methods=["POST"])
@require_auth
async def submit_form(form_id: int):
    try:
        data = await request.get_json()
        values = data.get("values", {})
        user_id = request.auth_payload.get("user_id")

        form = db.community_forms[form_id]
        if not form or not form.is_active:
            return jsonify({"error": "Form not found or inactive"}), 404

        # Check single submission
        if form.submit_once_per_user and user_id:
            existing = db(
                (db.form_submissions.form_id == form_id) &
                (db.form_submissions.user_id == user_id)
            ).select().first()
            if existing:
                return jsonify({"error": "Already submitted"}), 409

        # Create submission
        ip_hash = hash_ip(request.remote_addr or "unknown")
        submission_id = db.form_submissions.insert(
            form_id=form_id,
            user_id=user_id if not form.allow_anonymous else None,
            ip_hash=ip_hash
        )

        # Save field values
        for field_id, value in values.items():
            if isinstance(value, (dict, list)):
                db.form_field_values.insert(
                    submission_id=submission_id,
                    field_id=int(field_id),
                    value_json=value
                )
            else:
                db.form_field_values.insert(
                    submission_id=submission_id,
                    field_id=int(field_id),
                    value_text=str(value)
                )

        db.commit()
        return jsonify({"success": True, "submission_id": submission_id}), 201

    except Exception as e:
        logger.error(f"Submit form failed: {e}", exc_info=True)
        db.rollback()
        return jsonify({"error": str(e)}), 500


@app.route("/api/v1/forms/<int:form_id>/submissions", methods=["GET"])
@require_auth
async def get_submissions(form_id: int):
    try:
        form = db.community_forms[form_id]
        if not form:
            return jsonify({"error": "Form not found"}), 404

        submissions = db(db.form_submissions.form_id == form_id).select(
            orderby=~db.form_submissions.submitted_at
        )

        result = []
        for sub in submissions:
            values = db(db.form_field_values.submission_id == sub.id).select()
            result.append({
                "id": sub.id,
                "user_id": sub.user_id,
                "submitted_at": sub.submitted_at.isoformat() if sub.submitted_at else None,
                "values": {
                    str(v.field_id): v.value_json if v.value_json else v.value_text
                    for v in values
                }
            })

        return jsonify({
            "success": True,
            "count": len(result),
            "submissions": result
        })

    except Exception as e:
        logger.error(f"Get submissions failed: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


# Lifecycle
@app.before_serving
async def startup():
    logger.info(f"Starting engagement_module v{config.MODULE_VERSION}")
    try:
        config.validate()
    except ValueError as e:
        logger.error(f"Config error: {e}")
        sys.exit(1)
    try:
        init_database()
    except Exception as e:
        logger.error(f"Database init failed: {e}")
        sys.exit(1)
    logger.info(f"Engagement Module started on port {config.MODULE_PORT}")


@app.after_serving
async def shutdown():
    logger.info("Shutting down Engagement Module")
    db.close()


if __name__ == "__main__":
    app.run(host=config.MODULE_HOST, port=config.MODULE_PORT, debug=False)
