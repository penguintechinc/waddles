# In-cluster HTTP transit TLS (audit F3 / F4 / F6)

Implements items F3, F4 and F6 of `docs/superpowers/specs/2026-11-08-pod-transit-encryption-audit.md`
(PR #1161): every in-cluster HTTP hop to hub-api, hub-webui (from the Gateway), core-data and
svc-streaming is TLS with CA verification. Switch: `global.internalTransitTls.enabled`
(on in `values-dal2alpha.yaml`, off by default for clusters without cert-manager). When on there is
no plaintext path: a failed handshake or bad config fails loud.

## Dependency on the CA work

Certificates are issued from the internal-CA ClusterIssuer `waddlebot-internal-ca`
(`k8s/dal2alpha/internal-ca.yaml`, being extended by `feature/transit-tls-core`). The issuer
name/kind are values (`global.internalTransitTls.certManager.*`), so a rename in that PR is a
one-line change here. Constraints inherited from the CA: SANs are the service **FQDN only**
(`<fullname>-<svc>.<ns>.svc.cluster.local`, no short names, no IP SANs), so every URL below is dialed by FQDN.
Certs are ECDSA P-256 (SHA-256 signed by the P-256 CA), 90d, auto-renewed.

## Hops

| Hop | Server | Client |
|---|---|---|
| svc-* -> hub-api (REST `HUB_API_URL`, service-token mint, JWKS) | hub-api hypercorn `--certfile/--keyfile` on `:8204` | `core/service_auth` `internal_tls` (Rust), `flask_core.internal_tls` (Python), webui `tls.js` |
| hub-webui `/api` proxy -> hub-api | same | `proxy.js` keep-alive `https.Agent` pinned to the CA |
| `wait-for-hub-api` init containers | same | stdlib `ssl` with `cadata` from the CA |
| Gateway -> hub-webui | `server.js` `https.createServer` | `BackendTLSPolicy` (CA ConfigMap; operator step in `k8s/dal2alpha/APPLY.md`) |
| hub-api -> core-data analytics | core-data hypercorn TLS on `:8040` | `analytics_proxy.py` pooled client |
| hub-api -> svc-streaming RTC | new rustls listener `STREAMING_TLS_PORT` (`:8448`, `core/svc_streaming/src/tls.rs`) | `calls_proxy.py` pooled client |
| svc-streaming -> hub-api token ledger | hub-api | `svc_streaming::tls::internal_client` |

Client contract (all languages): `INTERNAL_CA_PEM` (injected from the hub-api cert Secret's `ca.crt`
via `secretKeyRef`, no volume) or `INTERNAL_CA_FILE` pins trust to the internal CA (public roots
disabled); `INTERNAL_TLS_REQUIRED=true` refuses any `http://` URL and requires a CA. Unreadable or
non-PEM CA, `REQUIRED` without a CA, or a plaintext URL is a hard error.

## Latency profile

Deliberately just above the modern bar, not maximal: TLS 1.3 preferred, 1.2 floor; ECDSA P-256 certs;
X25519 / P-256 key exchange; AES-128-GCM or ChaCha20-Poly1305 suites (rustls listener and Node pin
exactly these; hypercorn uses `ECDHE+AESGCM:ECDHE+CHACHA20` for TLS 1.2); session resumption on
(rustls stateless tickets, OpenSSL/Node defaults); **one pooled keep-alive client per process**
(`service_auth::internal_tls::shared_client`, `flask_core.internal_tls.PooledClient` per event loop,
one `https.Agent` in the webui proxy, one `Arc` ledger client in svc-streaming) with HTTP/2
multiplexing negotiated by ALPN where both ends speak it. Tests assert N requests -> 1 connection.

**Stronger protection** (per-flow encryption beyond this, identity-independent coverage, same-node
traffic) is a CNI-layer choice: enable Cilium transparent encryption (WireGuard) -- audit item F0 --
rather than raising per-connection handshake cost here.

## gRPC migration candidates

Until penguinRPC (v4.0.x) internal calls should prefer gRPC/HTTP2 over TLS. Hops with a gRPC surface
already use it (hub-api gRPC `:50204`, svc gRPC, host-API mTLS) and are untouched. These hops are
REST-only today, so they stay HTTP (now HTTPS + HTTP/2 + connection reuse) and are migration candidates:

- hub-api -> core-data analytics proxy (core-data's gRPC `:50051` has no analytics RPC)
- hub-api -> svc-streaming RTC control (`calls_proxy`)
- svc-streaming -> hub-api token-debit ledger
- svc-* -> hub-api service-token mint and JWKS (machine-JWT bootstrap)
- svc-ingest -> hub-api `resolve-scope`, DEK provider, connection resolver REST calls
- hub-webui -> hub-api `/api` (browser-facing REST proxy; stays REST)

No new gRPC services are introduced here.

## Not covered

Postgres, cache Valkey, SeaweedFS S3, PostHog/Ollama and the egress-proxy hop (audit F1, F2, F5, F9)
are separate items. Legacy Python `svc_*` callers of `HUB_API_URL` are not deployed on dal2alpha and
are not wired; the Rust pods and hub-api are.
