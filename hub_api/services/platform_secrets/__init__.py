"""Platform secret store (T3): registry, repository, bootstrap, importer, cache, telemetry.

The platform's own generated secrets live sealed in `platform_vault.secrets` (migration 0109); this
package decides WHICH secrets exist (`registry`), reads and writes the tables (`repository`),
creates them exactly once at boot (`bootstrap`, inside hub-api's advisory lock), validates the
one-time import of values already in use (`importer`), serves them to hub-api (`cache`) and reports
on all of it without ever emitting a value (`telemetry`). All crypto stays in
`services.provider_connections.vault`.
"""
