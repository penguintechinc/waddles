# In-cluster transit encryption (v3.0.x)

Pod-to-pod traffic for the hops below is encrypted and the server identity is verified. Per-tenant transit
separation (distinct keys per tenant) is v4.0.x; v3.0.x uses one shared CA/key set for all tenants.

| Hop | Mechanism | Client verification |
|---|---|---|
| Any client -> Postgres | `ssl=on`, TLS 1.2+, `pg_hba` is `hostssl` + `hostnossl ... reject` | `PGSSLMODE=verify-full`, `PGSSLROOTCERT=/etc/waddles/transit-ca/ca.crt`, dial the FQDN |
| Python modules -> cache Valkey | `--port 0` (no plaintext listener), TLS port 6380 only | `rediss://` + `ssl_cert_reqs=required` + `ssl_ca_certs` + `ssl_check_hostname=true` |
| Rust / hub-api / seeder -> durable + kv Valkey | TLS only (unchanged) | CA pinned (`valkey-ca.crt`), hostname checked |
| Any client -> SeaweedFS S3 | `weed server -s3.cert.file/-s3.key.file` | `https://<fqdn>:8333`, CA pinned (`BUNDLE_BUCKET_CA_FILE` / `S3_CA_FILE` / `AWS_CA_BUNDLE`), system roots off |

## Algorithms and performance

TLS 1.3 preferred, 1.2 minimum. ECDSA P-256 certificates signed `ecdsa-with-SHA256`, P-256 key exchange,
AES-128-GCM or ChaCha20-Poly1305 only (Postgres `ssl_ciphers`, Valkey `tls-ciphers`/`tls-ciphersuites`; SeaweedFS uses
the Go defaults, which are a superset of those suites). Session resumption is on (Valkey `tls-session-caching yes`,
OpenSSL/Go tickets elsewhere). Every client keeps a pooled keep-alive connection (sqlx / SQLAlchemy / pydal pools,
redis-py `ConnectionPool`, redis-rs connection manager, `object_store` / boto3 HTTP pools), so a handshake happens per
connection, never per request.

## One trust root (F8)

- **cert-manager on** (`global.transitTls.certManager.enabled`: dal2alpha, beta, gamma, production): the Postgres, SeaweedFS
  and Valkey server certs are issued by the shared `waddlebot-internal-ca` ClusterIssuer (the same issuer as the host-API/gRPC
  mTLS and hub-api HTTPS certs; `global.internalTransitTls.certManager.issuerName`, when set, overrides it). ECDSA P-256,
  `ecdsa-with-SHA256`, 90d auto-renew. SANs are FQDN-only because that CA's critical nameConstraints permit only
  `*.<ns>.svc.cluster.local`; every client dials the FQDN. Secrets: `<release>-postgres-tls`, `<release>-seaweedfs-tls`,
  `<release>-valkey-tls`. Every client mounts `ca.crt` from the one `<release>-postgres-tls` Secret (the same CA bytes for all
  three hops). No self-generated CA exists in this mode; `global.valkeyTls.externalSecret` is ignored (cert-manager owns it).
- **cert-manager off** (local, values-alpha): `templates/transit-tls-secret.yaml` self-generates one ECDSA CA plus Postgres and
  SeaweedFS leaves into `<release>-transit-tls` (CA key never stored; pre-install/pre-upgrade hook so the migration Jobs can
  mount it); the legacy Valkey cert keeps its own (now ECDSA) CA. beta/gamma/production never use this path.

The nameConstrained CA is why nothing here relies on short Service names: `infra-postgres.waddlebot.svc.cluster.local`, etc.

Rotation: cert-manager renews 15 days before expiry; pods pick up the new leaf on restart (Postgres/SeaweedFS/Valkey read
the files at start). In the self-generated mode delete the Secret and `helm upgrade`.

## First rollout onto an existing release (dal2alpha)

The db-migrate Job is a pre-upgrade hook and now connects with `verify-full`, but the running Postgres is still the
pre-TLS pod until the upgrade applies. For the one upgrade that introduces TLS, bring Postgres up first:
`helm upgrade ... --set pipeline.migrationsJob.enabled=false --set pipeline.hubAdminSeedJob.enabled=false`, wait for
`postgres`, `redis` and `seaweedfs` to roll (they restart: `Recreate`), then run the normal `helm upgrade`. Fresh installs
need nothing special. With cert-manager on, the old self-generated `<release>-valkey-tls` Secret is replaced by the cert-manager one of the same
name: delete the old Secret before the upgrade (cert-manager will not adopt a Secret it did not create).

## Failure behaviour

No plaintext fallback exists: `verify-full`, `hostnossl reject`, `--port 0`, `https://` endpoints with the CA pinned. A
bad CA path or PEM is a hard error in the Rust S3 clients; a hostname/CA mismatch fails the handshake.

## Not covered here

SeaweedFS master/filer/volume ports (admin UI `9333`), hub-api HTTP/gRPC client-cert mutual auth, PostHog, Ollama/Qdrant and
the OTel collector are tracked separately in the pod transit-encryption audit (F3, F4, F6, F7 gRPC part, F9).

## Optional: stronger protection via the CNI

Customers who need protection beyond application-level TLS (for example every pod hop, including the ones above not yet
covered, plus defence in depth against a mis-issued certificate) can additionally enable CNI transparent encryption, for
instance Cilium WireGuard (`encryption.enabled=true`, `encryption.type=wireguard`). It is independent of this chart,
encrypts node-to-node pod traffic with per-node-pair keys, and is the cluster-operator's choice; same-node traffic does
not cross the wire.
