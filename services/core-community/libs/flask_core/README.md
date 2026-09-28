# WaddleBot Flask Core Library

Shared utilities and components for all WaddleBot Flask/Quart modules.

## Components

### Database (`database.py`)
- **AsyncDAL**: Async wrapper around penguin-dal for non-blocking database operations
- Connection pooling with configurable pool size
- Read replica support for query distribution
- Transaction management with context managers
- Bulk operations support

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
