# WaddleBot Flask Core Library

Shared utilities and components for all WaddleBot Flask/Quart modules.

## Components

### Database (`database.py`)
- **AsyncDAL**: Async wrapper around PyDAL for non-blocking database operations
- Connection pooling with configurable pool size
- Read replica support for query distribution
- Transaction management with context managers
- Bulk operations support
- **Redacted error logging** (`db_errors.py`): DB failures are logged as operation +
  exception type + SQLSTATE/driver code + a fixed category label -- never the driver
  message (see [Database error logging](#database-error-logging))
- **Redacted validation logging** (`validation_errors.py`): validation failures are logged as
  error count + declared field locations + error types -- never the client's `input`/`msg`
  (see [Validation error logging](#validation-error-logging))

### Authentication (`auth.py`)
- **Flask-Security-Too** integration for user management
- Multi-provider OAuth (Twitch, Discord, Slack)
- JWT token generation and validation
- API key management with secure hashing
- Role-based access control (RBAC)

### Data Models (`datamodels.py`)
- Python 3.13 optimized dataclasses with `slots=True`
- Shared data structures across all modules
- Type-safe with full type hints
- Memory-efficient and immutable where appropriate

### Logging (`logging_config.py`)
- Comprehensive AAA (Authentication, Authorization, Audit) logging
- Structured log format with consistent fields
- Console, file, and optional syslog output
- Log rotation with configurable size and backup count
- Performance logging with execution time tracking

### API Utilities (`api_utils.py`)
- Standardized API response formatting
- Error handling decorators
- Authentication decorators
- Rate limiting decorators
- Request validation
- CORS headers support
- Pagination utilities

## Installation

```bash
cd /home/penguin/code/WaddleBot/libs/flask_core
pip install -e .
```

## Usage

### Database

```python
from libs.flask_core import AsyncDAL, init_database

# Initialize database
dal = init_database(
    uri='postgresql://user:pass@host/db',
    pool_size=10,
    read_replica_uri='postgresql://user:pass@replica/db'
)

# Define tables
users = dal.define_table('users',
    dal.Field('username', 'string'),
    dal.Field('email', 'string')
)

# Async operations
user_id = await dal.insert_async(users, username='john', email='john@example.com')
rows = await dal.select_async(users.id == user_id)
await dal.update_async(users.id == user_id, email='newemail@example.com')
```

#### Database error logging

**SECURITY (PII in logs):** a DB driver error's message routinely embeds the *bound
values* of the failed statement (psycopg2 `DETAIL: Key (email)=(...) already exists`,
`invalid input syntax for type uuid: "..."`, pydal's inlined INSERT text, SQLAlchemy's
`[parameters: (...)]`). flask_core therefore **never logs the raw driver message** --
not in the log line, not via `exc_info`/traceback rendering, not in `extra`.

Every `AsyncDAL` operation, `db_operation()`, the `install_db_resilience()` teardown
hook, `ReadReplicaManager`/`ReadReplicaRouter`, `ChannelShardManager` and the
`async_endpoint` decorator log through `flask_core.db_errors` instead:

```
ERROR flask_core.database ExecuteSQL error: type=psycopg2.errors.UniqueViolation \
      sqlstate=23505 category=unique_violation constraint=users_email_key table=users
DEBUG flask_core.database ExecuteSQL error: sanitized traceback
      (frames only -- file/line/function/source, no exception text)
```

| Emitted | Never emitted |
|---|---|
| operation label, exception type | exception message / `args` |
| SQLSTATE (`pgcode`/`sqlstate`), sqlite error name, MySQL errno | SQL text, bound parameters |
| fixed category label looked up from the SQLSTATE | `DETAIL`/`CONTEXT`/`LINE n:` echoes |
| constraint/table/column names (regex-validated identifiers only) | anything failing validation (dropped) |

The allowlist fails closed: inside DB wrappers even non-driver exceptions are logged
type-only (pydal casts values before the driver sees them, so its own `ValueError` can
echo one). The exception is still re-raised unchanged. Failures outside DB wrappers
(`async_endpoint`, teardown) are redacted only when a DB driver error is in the cause
chain; other errors keep their full message and traceback.

Services should use the same helpers instead of `logger.error(f"... {e}")` around DB calls:

```python
from flask_core import log_db_error

try:
    await dal.executesql_async(sql, params)
except Exception as exc:
    log_db_error(logger, "load widgets failed", exc)
    raise
```

Note: re-raised driver errors that reach Quart's own `Exception on request` handler are
logged by Quart, outside flask_core -- catch/translate them at the service boundary if
that log stream is in scope for PII controls.

### Request validation

`flask_core.validation` provides `validate_json` / `validate_query` / `validate_form`
(Pydantic) decorators and `validate_data` for programmatic use.

#### Validation error logging

**SECURITY (PII in logs):** a Pydantic `ValidationError` entry carries the client's `input`,
and its `msg`/`ctx` and `loc` can echo it too (a custom `ValueError(f"bad {v}")`, a UUID parse
error naming the bad character, an `extra_forbidden` error whose `loc` is a client-chosen key).
The decorators therefore **never log the raw errors**. They log through
`flask_core.validation_errors.describe_validation_errors`:

```
WARNING flask_core.validation AUTHZ validation_failed endpoint=signup model=Signup \
        errors=3 fields=age:int_parsing,meta.<key>:int_parsing,<key>:extra_forbidden
```

| Emitted | Never emitted |
|---|---|
| error count | `input` value |
| field location, only if a declared field name/alias of the model | `msg`, `ctx`, `url` |
| list index / dict key / extra key -> `[]` / `<key>` placeholder | client-chosen key names, index numbers |
| Pydantic error type (regex-validated token) | anything failing validation (`unknown`) |

Exceptions raised *inside* a decorated endpoint also reach the decorators' `except Exception`;
those are logged type-only (plus SQLSTATE/category for DB driver errors) via
`flask_core.db_errors.log_db_error`, with a frames-only traceback at DEBUG -- never `str(e)`.
The 400 response body is unchanged (it goes back to the same client that sent the data).
Use `describe_validation_errors(exc, Model)` instead of `logger.error(f"... {exc.errors()}")`
in service code.

### Authentication

```python
from libs.flask_core import setup_auth, create_jwt_token
from quart import Quart

app = Quart(__name__)
oauth = setup_auth(app, dal, config={
    'TWITCH_CLIENT_ID': 'your_client_id',
    'TWITCH_CLIENT_SECRET': 'your_secret'
})

# Create JWT token
token = create_jwt_token(
    user_id='123',
    username='john',
    email='john@example.com',
    roles=['user', 'moderator'],
    secret_key=app.config['SECRET_KEY']
)
```

### API Endpoints

```python
from libs.flask_core import async_endpoint, auth_required, success_response, error_response
from quart import Blueprint

api = Blueprint('api', __name__)

@api.route('/protected', methods=['GET'])
@auth_required
@async_endpoint
async def protected_route():
    user = request.current_user
    return success_response({'user': user})

@api.route('/data', methods=['POST'])
@async_endpoint
async def create_data():
    data = await request.get_json()
    # Process data
    return success_response(data, status_code=201)
```

### Logging

```python
from libs.flask_core import setup_aaa_logging

# Setup logging
logger = setup_aaa_logging(
    module_name='my_module',
    version='1.0.0',
    log_level='INFO'
)

# Log events
logger.auth(action='login', user='john', result='SUCCESS')
logger.authz(action='view_community', user='john', community='my_community', result='ALLOWED')
logger.audit(action='update_settings', user='john', community='my_community', result='SUCCESS')
logger.error('Something went wrong', user='john', action='process_data')
logger.performance(action='process_batch', execution_time=150)
```

### Data Models

```python
from libs.flask_core import CommandRequest, CommandResult, MessageType, Platform

# Create command request
request = CommandRequest(
    entity_id='twitch:channel:12345',
    user_id='user123',
    message='!help',
    message_type=MessageType.CHAT_MESSAGE,
    platform=Platform.TWITCH,
    username='john_doe'
)

# Create command result
result = CommandResult(
    execution_id='exec_123',
    command_id=1,
    success=True,
    processing_time_ms=45
)
```

## Python 3.13 Optimizations

This library utilizes Python 3.13 features:

- **`slots=True`** in dataclasses for 40-50% memory reduction
- **Structural pattern matching** for cleaner conditional logic
- **Type aliases** for better type hints
- **TaskGroup** for structured concurrency (in modules using this library)

## License

Copyright © 2024 WaddleBot Team
