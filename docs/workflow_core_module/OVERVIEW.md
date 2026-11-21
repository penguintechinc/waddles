# Workflow Core Module

> Sophisticated workflow engine enabling visual workflow creation, validation, and execution with support for complex control flow, loops, conditionals, and external integrations.

## Purpose

The Workflow Core Module is a powerful automation engine that enables communities and administrators to design and execute complex, event-driven workflows without code. It supports visual workflow creation with multiple node types (action, condition, loop, webhook, module integration), provides expression-based data flow through workflow contexts, validates workflows before execution to prevent runtime errors, and schedules workflows using cron expressions or event triggers. The module integrates with external modules through gRPC, manages workflow permissions and license-gated features, and maintains execution history for auditing and debugging.

## Key Capabilities

- Visual workflow builder with DAG (Directed Acyclic Graph) execution
- Multiple node types (action, condition, loop, webhook, module calls)
- Expression engine for dynamic data flow and variable interpolation
- Workflow validation and syntax checking
- Scheduled execution with cron expressions
- Event-driven triggers and webhooks
- Module integration via gRPC and REST APIs
- Permission-based access control
- License-gated advanced features
- Execution history and debugging tools

## Documentation Index

| Document | Description |
|---|---|
| [USAGE.md](USAGE.md) | Getting started, running locally, common workflows |
| [API.md](API.md) | Endpoints, request/response formats, error codes |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Internal design, data flows, component breakdown |
| [CONFIGURATION.md](CONFIGURATION.md) | Environment variables, setup, feature flags |
| [TESTING.md](TESTING.md) | Test strategy, mock data, how to run tests |
| [TROUBLESHOOTING.md](TROUBLESHOOTING.md) | Common issues, debug steps, FAQ |
| [RELEASE_NOTES.md](RELEASE_NOTES.md) | Version history, migrations |

## Quick Reference

| Item | Value |
|---|---|
| Source | core/workflow_core_module |
| Language | Python |
| Port | 8070 (REST) |
| gRPC Port | 50070 |
| Maintained by | Penguin Tech Inc |
