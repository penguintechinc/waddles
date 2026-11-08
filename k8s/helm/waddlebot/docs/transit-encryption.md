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

## CA and certificates (F8)

`templates/transit-tls-secret.yaml` renders `<release>-transit-tls`: `ca.crt` (the CA certificate only, no CA key),
`postgres.crt/.key`, `seaweedfs.crt/.key`. SANs: `<svc>`, `<svc>.<ns>`, `<svc>.<ns>.svc`, `<svc>.<ns>.svc.cluster.local`;
clients always dial the FQDN. The chart-owned CA has no nameConstraints. The cert-manager `waddlebot-internal-ca`
ClusterIssuer (`k8s/dal2alpha/internal-ca.yaml`) is not used for these hops: its critical nameConstraints permit only the
`waddlebot.svc.cluster.local` suffix plus 7 bare names, so short-name dials can never validate against it. It is already
ECDSA P-256 / SHA-256.

Provisioning: alpha/local self-generate (kept across upgrades while the SAN identity is unchanged); beta/gamma/production
must supply `global.transitTls.*` or set `global.transitTls.externalSecret=true`; otherwise the render fails closed. The
Secret is a pre-install/pre-upgrade hook so the migration/seed hook Jobs can mount the CA.

Rotation: delete the Secret and `helm upgrade` (alpha), or rotate via the ExternalSecret. Server pods roll on a SAN change
(`checksum/transit-tls`); a rotated CA/leaf with unchanged SANs needs `kubectl rollout restart` of Postgres, SeaweedFS and
the clients.

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
