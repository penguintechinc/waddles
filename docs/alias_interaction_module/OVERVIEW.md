# Alias Interaction Module

> Linux-style command alias management system with variable substitution support for interactive workflow automation.

## Purpose

The Alias Interaction Module provides a sophisticated alias management system that enables communities to define custom command shortcuts with dynamic variable substitution. This module implements a Linux-inspired alias system where administrators can create reusable command templates that can be invoked with user-specific parameters, args substitution, and context variables. The module stores aliases in a persistent database, tracks usage statistics, and supports community-specific isolation.

The module is designed for interactive scenarios where users might invoke common workflows repeatedly—such as reporting issues, running diagnostics, scheduling meetings, or executing multi-step processes. Instead of typing full commands every time, users can define aliases once and reuse them with different parameters. The system supports intelligent variable substitution including user context, positional arguments, and aggregated argument lists.

This module integrates seamlessly with the WaddleBot ecosystem through the Flask core library, providing async/await support with Quart, database abstraction via PyDAL, and standardized health check and logging capabilities that align with enterprise observability requirements.

## Key Capabilities

- **Alias Creation and Management**: Create, list, and delete community-specific aliases with full CRUD operations
- **Variable Substitution**: Support for {user}, {args}, {arg1}, {arg2}, and {all_args} placeholders with intelligent replacement
- **Community Isolation**: Aliases are scoped to specific communities, preventing cross-community contamination
- **Usage Tracking**: Automatic tracking of alias execution counts for analytics and debugging
- **Soft Delete Support**: Aliases are soft-deleted (is_active flag) to preserve historical data
- **Async/Await Architecture**: Fully asynchronous request handling using Quart and Hypercorn
- **Health Monitoring**: Built-in health check and metrics endpoints for operational visibility
- **Database Abstraction**: PyDAL-based data access layer supporting multiple database backends

## Documentation Index

| Document | Description |
|---|---|
| [USAGE.md](USAGE.md) | Getting started, running locally, Docker setup, common workflows |
| [API.md](API.md) | REST endpoints, request/response formats, error handling, examples |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Internal design, data flows, service components, database schema |
| [CONFIGURATION.md](CONFIGURATION.md) | Environment variables, setup options, feature flags, defaults |
| [TESTING.md](TESTING.md) | Test strategy, unit tests, mock data, testing procedures |
| [TROUBLESHOOTING.md](TROUBLESHOOTING.md) | Common issues, debug commands, FAQ, resolution steps |
| [RELEASE_NOTES.md](RELEASE_NOTES.md) | Version history, breaking changes, migration guides |

## Quick Reference

| Item | Value |
|---|---|
| **Source Directory** | action/interactive/alias_interaction_module |
| **Language** | Python 3.12 |
| **Framework** | Quart (async Flask) |
| **Default Port** | 8010 (HTTP) |
| **Database** | PostgreSQL (PyDAL abstraction) |
| **Container Runtime** | Hypercorn with 4 workers |
| **Module Version** | 2.0.0 |
| **Maintained by** | Penguin Tech Inc |

---

## Module Structure

```
action/interactive/alias_interaction_module/
├── app.py                    # Quart application, endpoints, startup
├── config.py                 # Configuration management, credentials
├── requirements.txt          # Python dependencies
├── Dockerfile                # Container build definition
└── services/
    ├── __init__.py
    └── alias_service.py      # AliasService class with business logic
```

## Core Components

### app.py
The Quart application entry point defining HTTP endpoints, startup/shutdown lifecycle, blueprint registration, and request routing. Key endpoints include:

- `GET /api/v1/status` - Service status check
- `GET /api/v1/aliases` - List community aliases
- `POST /api/v1/aliases` - Create new alias
- `DELETE /api/v1/aliases/<alias_id>` - Delete alias
- `POST /api/v1/aliases/execute` - Execute alias with substitution

### config.py
Configuration management including environment variable loading, credential handling with optional Redis support, and database connection parameters. Implements:

- Database URL configuration (PostgreSQL)
- Router service URL for integration
- Logging setup and log level control
- Credential caching with background listener support
- Thread-safe credential state management

### alias_service.py
Business logic for alias operations including:

- `create_alias()` - Insert new alias records
- `list_aliases()` - Query aliases by community
- `delete_alias()` - Soft delete with is_active flag
- `execute_alias()` - Process aliases with variable substitution

## Integration Points

The module integrates with:

- **Flask Core Library** (`libs/flask_core`) - Provides async decorators, health checks, database init, logging
- **Router Service** - Optional integration via `ROUTER_API_URL` configuration
- **PostgreSQL Database** - Primary data store via PyDAL
- **Redis** (optional) - Credential refresh notifications
- **Health Check Endpoints** - Standard WaddleBot monitoring integration

## Getting Started

To quickly start using this module:

1. **Read [USAGE.md](USAGE.md)** - Local development setup and running instructions
2. **Review [API.md](API.md)** - Understand available endpoints and request formats
3. **Check [CONFIGURATION.md](CONFIGURATION.md)** - Configure required environment variables
4. **Refer to [ARCHITECTURE.md](ARCHITECTURE.md)** - Deep dive into internal design

For troubleshooting issues, see [TROUBLESHOOTING.md](TROUBLESHOOTING.md). For test procedures, see [TESTING.md](TESTING.md).
