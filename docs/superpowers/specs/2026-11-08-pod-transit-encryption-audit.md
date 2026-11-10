# Pod-to-Pod Transit Encryption Audit (v3.0.x)

Date: 2026-11-08. Base: `origin/release/v3.0.X`. Scope: chart `k8s/helm/waddlebot` + `values-dal2alpha.yaml`. Read-only audit, no cluster changes.

**Requirement (user, 2026-11-08):** all traffic between pods is encrypted, even with one shared key across tenants for v3.0.x. Per-tenant transit separation is v4.0.x.

## Verdict

- 23 in-cluster connection classes inventoried: **8 encrypted+verified, 14 plaintext, 1 unverified**. Another 3 (license server, OTel, SPIRE) are n/a.
- Of the 14 plaintext, **11 are live on dal2alpha** (3 are disabled there: PostHog x2, Ollama/Qdrant).
- Encrypted today: Valkey durable/KV, hub-api gRPC, host-API mTLS, svc gRPC. Plaintext: Postgres, cache Valkey, all hub-api HTTP, S3, webui/Gateway hops.
- Cluster layer: Cilium transparent encryption is **OFF** on `dal2alpha`. Read-only `kubectl --context dal2alpha -n kube-system get cm cilium-config` has no `enable-wireguard`/`enable-ipsec` key. So plaintext hops cross the node wire in clear.

Paths are relative to `k8s/helm/waddlebot/` unless noted. "Verified" = client validates the server cert chain against a pinned CA (not system roots, not insecure).

## Inventory

| # | Client -> Server | TLS today | Verified | Evidence | Gap / minimal fix |
|---|---|---|---|---|---|
| 1 | Rust svc-process/action/ingest -> durable Valkey (spine) | mTLS-less TLS (server-auth) | Yes (CA) | `infrastructure/valkey-durable.yaml:150-157` (`--port 0`, `--tls-replication yes`); `secrets.yaml:393`; `svc-process-rust.yaml:218-249` | none |
| 2 | Rust svc-process/action -> Valkey KV | TLS (server-auth) | Yes (CA) | `infrastructure/valkey-kv.yaml:111`; `secrets.yaml:545-546`; `svc-process-rust.yaml:234-238` | none |
| 3 | hub-api + seeder -> durable Valkey | TLS | Chain yes; hostname no | `hub-api.yaml:278-291` (`VALKEY_DURABLE_URL_TLS_AUTH`), `secrets.yaml:394` (`ssl_cert_reqs=required&ssl_ca_certs`) | redis-py `ssl_check_hostname` defaults false: add `ssl_check_hostname=true` once SANs cover the svc names (SANs in `infrastructure/valkey-tls-secret.yaml`) |
| 4 | Valkey durable primary <-> replica | TLS | Yes | `valkey-durable.yaml:157` | none |
| 5 | Python modules (core-data, reputation, collectors) -> **cache Valkey `infra-redis`** | **No** | n/a | `secrets.yaml:351,356` (`redis://:pw@`); `infrastructure/redis.yaml:98-110` TLS port is added *alongside* plaintext `6379`, never replacing it | Plaintext listener + password on wire. Set `--port 0` when TLS on (as valkey-kv does), move `REDIS_URL` to `rediss://...?ssl_cert_reqs=required&ssl_ca_certs=` |
| 6 | **All Postgres clients** (hub-api, core-data, migrations Job, seeder, cronjobs, reputation) -> Postgres | **No** | n/a | `infrastructure/postgres.yaml` has 0 `ssl`/`tls` hits; `secrets.yaml:220` `postgresql://` no `sslmode`; `hub_api/config.py:303-312` no ssl param | DB creds + all data in clear. Server `ssl=on` + cert; clients `sslmode=verify-full` (SAN must match svc name) |
| 7 | Rust svc-* -> hub-api gRPC `:50204` | TLS (server-auth) | Yes (`HUB_API_GRPC_CA_FILE`) | `svc-process-rust.yaml:165` (`https://`); `core/hub_client/src/lib.rs:216-232` (`ca_certificate`); `_helpers.tpl:738-741`; server `hub_api/grpc_internal/server.py:59-76` | Client cert not required: `GRPC_TLS_CLIENT_CA_PATH` unset (`_helpers.tpl:711-714`). Mutual auth is optional; JWT still authenticates |
| 8 | Python router -> core-data/core-identity/interactive-productivity/action-serverless gRPC | mTLS | Yes | `values-dal2alpha.yaml:39-45` (cert-manager, `requireClientCert: true`, `insecureDev: false`); `_helpers.tpl:586-600`; `libs/flask_core/flask_core/grpc_tls.py:120-143`; CA `k8s/dal2alpha/internal-ca.yaml` | router disabled on dal2alpha (`modules.router`); chart-ready only |
| 9 | bundle-executor -> svc-process/svc-action host-API | mTLS | Yes (pinned identity) | `core/bundle_executor/src/tls.rs:277-293`; `core/svc_action/src/host_api.rs:181-192`; `core/svc_process/src/host_api.rs:227-238`; `_helpers.tpl:651-676`; `values-dal2alpha.yaml:46-50` | none |
| 10 | bundle-executor (connector) -> svc-ingest host-API | mTLS | Yes (pinned) | `core/svc_ingest/src/connector/host_api_listener.rs:183-191`; `bundle-executor.yaml:215-221`; `_connector_runtime.tpl:29-31,159` | none (connector runtime off on dal2alpha) |
| 11 | svc-ingest/process/action/presentation, egress-proxy -> hub-api **service-token** `/internal/service-token` (JWT mint) | **No** | n/a | `svc-ingest-rust.yaml:144`; `svc-action-rust.yaml:160`; `svc-process-rust.yaml:167`; `svc-presentation-rust.yaml:129` | Machine JWTs issued over cleartext HTTP. Highest-value HTTP hop |
| 12 | svc-presentation, egress-proxy -> hub-api **JWKS** | **No** | n/a | `svc-presentation-rust.yaml:100`; `infrastructure/egress-proxy.yaml:112` | Integrity of trust keys: MITM can swap JWKS |
| 13 | Rust svc-ingest/process, seeder, cronjobs, waitForHubApi -> hub-api REST `HUB_API_URL` | **No** | n/a | `configmap.yaml:148`; `svc-ingest-rust.yaml:137`; `svc-process-rust.yaml:156`; `_helpers.tpl:467` | hub-api HTTPS listener (see F3) |
| 14 | hub-webui (Node proxy) -> hub-api `/api` | **No** | n/a | `hub-webui.yaml:67-68`; header `hub-webui.yaml:9` | Carries user sessions/PII; HTTPS + `NODE_EXTRA_CA_CERTS` |
| 15 | Cilium Gateway (`gateway-system`) -> hub-webui `:8080` | **No** (TLS ends at Gateway) | n/a | `values-dal2alpha.yaml:549-559` (HTTPRoute, port 8080); comment `:543-545` | Re-encrypt: `BackendTLSPolicy` + webui HTTPS, or WireGuard (F0) |
| 16 | hub-api -> svc-streaming (RTC control) | **No** | n/a | `hub-api.yaml:138` | svc-streaming rustls listener |
| 17 | hub-api -> core-data analytics proxy | **No** | n/a | `hub-api.yaml:201` (`http://...core-data...`) | core-data already has gRPC mTLS (#8); use HTTPS or gRPC |
| 18 | bundle-executor(s) -> SeaweedFS S3 (bundle .wasm) | **No** | n/a | `bundle-executor.yaml:184`; `bundle-executor-action.yaml:117`; `_helpers.tpl:1132`. Code *supports* https+CA: `core/bundle_executor/src/bucket.rs:52,117` | Integrity of signed-bundle fetch path; chart passes `http://` |
| 19 | svc-streaming, svc-presentation, hub-api, seeder -> SeaweedFS S3 | **No** | n/a | `svc-streaming.yaml:195`; `svc-presentation-rust.yaml:106`; `configmap.yaml:35-37`; `core/svc_presentation/src/images/store.rs:88` (`allow_http`) | S3 keys + media in clear. Enable weed S3 `-cert.file/-key.file` (`infrastructure/seaweedfs.yaml:258`); `https://` endpoints + CA |
| 20 | App modules -> PostHog `posthog-web` (when `posthog.enabled`) | **No** | n/a | `infrastructure/posthog/_helpers.tpl:44-46` (`http://posthog-web`); `configmap.yaml:113` | Disabled on dal2alpha (`values-dal2alpha.yaml:175`); on dal2alpha POSTHOG_HOST falls to external `https://license.penguintech.io` |
| 21 | PostHog web/worker -> its postgres/redis/clickhouse/kafka | **No** | n/a | `infrastructure/posthog/_helpers.tpl:63-64` (`redis://posthog-redis`); no TLS in `posthog/*.yaml` | Disabled on dal2alpha; fix for beta+ or rely on F0 |
| 22 | ai-researcher / interactive-ai -> Ollama + Qdrant | **No** | n/a | `configmap.yaml:50,54`; `values.yaml:3009` (`http://ollama:11434`); `infrastructure/ollama.yaml:63` | Disabled on dal2alpha (`values-dal2alpha.yaml:137-140`); front with TLS sidecar or F0 |
| 23 | Workloads -> egress-proxy (HTTP proxy hop) | **Unknown** | **Unverified** | `infrastructure/egress-proxy.yaml:75-84`; no `*_PROXY`/proxy-URL env found in any consumer template | Wiring not visible in chart (likely in bundle host code `core/bundle_host_http/src/egress.rs`); confirm proxy port is TLS or covered by F0 |
| - | workloads -> `license.penguintech.io` (PostHog/licensing) | HTTPS to public host | System roots | `values.yaml:3032`; `configmap.yaml:113` | n/a, leaves cluster |
| - | workloads -> OTel collector | none exists | n/a | `network-policies-egress.yaml:168-175` ("not yet deployed"); `values.yaml:2014` (`otlpEndpoint: ""`); svc-presentation only emitter wiring `svc-presentation-rust.yaml:147-150` | n/a today; when collector lands: OTLP/gRPC TLS + CA |
| - | SPIRE server/agent | own mTLS | n/a | `values-dal2alpha.yaml:597` `spire.enabled: false` (#437) | n/a |

Notes:
- `k8s/dal2alpha/internal-ca.yaml` is real and wired for rows 8-10 (cert-manager `waddlebot-internal-ca` ClusterIssuer). It carries critical X.509 `nameConstraints` (`internal-ca.yaml:17-41`): only `*.waddlebot.svc.cluster.local` FQDNs plus 7 listed bare names; **no IP SANs; no `<svc>.waddlebot.svc` short forms**. Chart URLs use the bare short name (`waddlebot-hub-api-v3`), so any new cert for hub-api/postgres/seaweedfs/webui/core-data/svc-streaming must either (a) be dialed by FQDN, or (b) add the bare names and re-issue the CA.
- Valkey (rows 1-4) and hub-api gRPC (7) use **separate alpha/local self-generated CAs** (`valkey-tls-secret.yaml:89`, `hub-api-grpc-tls-secret.yaml:78`, Helm `genCA`, 365 d), not the internal CA. They are shared across tenants, which fits the v3.0.x requirement. Rotation outside alpha needs ExternalSecret/cert-manager.
- Valkey servers use `tls-auth-clients no` (server-auth + password), not mTLS (`valkey-durable.yaml:155`, `redis.yaml:~108`). Acceptable under the stated requirement; mTLS is a v4 hardening.
- Not inventoried: kubelet HTTP probes and `localhost` valkey-cli `--insecure` probes (`valkey-kv.yaml:153`), which are node-local; Kubernetes API calls from Jobs (TLS by client-go default).

## Cluster-level backstop (F0)

Enable Cilium transparent encryption (WireGuard, node-to-node key per node pair, auto-rotated) on `dal2alpha`. One operator change encrypts every cross-node pod hop above at once, including all 14 plaintext rows, with no chart changes. Limits: same-node pod traffic does not traverse the wire, so it is not encrypted; it is not certificate-verified identity; Hubble L7 visibility of encrypted flows is reduced. Treat as the immediate coverage layer, not a substitute for app-level TLS (rows 5, 6, 11 carry credentials and should still get TLS). Requires user approval (cluster change; not done here).

## Parallelizable fix list

File ownership is disjoint per item unless noted ("shared": serialize or rebase). Gate: each flagged OFF by `{product}.disable-transit-tls` kill-switch semantics per `critical-rules.md` core-mechanism rule; each ships a regression test asserting the plaintext listener/URL is gone.

| ID | Fix | Owns (files) | Depends on |
|---|---|---|---|
| F0 | Cilium WireGuard on dal2alpha (operator action, user-approved) | cluster `cilium-config` / `k8s/dal2alpha/APPLY.md` | none |
| F1 | Postgres TLS: server `ssl=on` + cert; `sslmode=verify-full` clients | `templates/infrastructure/postgres.yaml`, `templates/secrets.yaml` (DATABASE_URL, line 220), `_helpers.tpl` dbEnv, `hub_api/config.py` (`_build_db_url`), shared db helper in `libs/` | F8 |
| F2 | Cache Valkey TLS-only + `rediss://` `REDIS_URL` | `templates/infrastructure/redis.yaml`, `templates/secrets.yaml` (351,356), `libs/flask_core` redis client | none (shared `secrets.yaml` with F1: separate hunks) |
| F3a | hub-api HTTPS listener (hypercorn certfile/keyfile) + cert-manager Certificate | `templates/hub-api.yaml`, new `templates/hub-api-tls-certificate.yaml`, `hub_api/` entrypoint | F8 |
| F3b | Rust HTTP clients to https + CA (service-token, JWKS, REST) | `core/hub_client/`, `core/svc_ingest`, `core/svc_process`, `core/svc_action`, `core/svc_presentation`; `svc-*-rust.yaml`, `configmap.yaml:148` | F3a |
| F3c | hub-webui proxy trusts hub-api CA (`NODE_EXTRA_CA_CERTS`) + `https://` upstream; webui serves TLS | `templates/hub-webui.yaml`, webui server code | F3a |
| F3d | Python callers (cronjobs, seeder, waitForHubApi, egress-proxy JWKS) https + CA | `templates/*-cronjob.yaml`, `core-bundle-seeder-job.yaml`, `_helpers.tpl:467`, `infrastructure/egress-proxy.yaml` | F3a |
| F4 | Gateway -> webui re-encryption (`BackendTLSPolicy`) | `templates/httproute.yaml`, `values-dal2alpha.yaml` httproute block | F3c |
| F5 | SeaweedFS S3 TLS + `https://` endpoints + CA mounts | `templates/infrastructure/seaweedfs.yaml`, `bundle-executor*.yaml`, `svc-streaming.yaml`, `svc-presentation-rust.yaml`, `core/svc_presentation/src/images/store.rs`, `configmap.yaml:35-37` | F8 |
| F6 | hub-api -> core-data and -> svc-streaming over TLS | `hub-api.yaml:138,201` (shared with F3a: rebase), `templates/core/core-data.yaml`, `templates/svc-streaming.yaml`, `core/svc_streaming/` | F8 |
| F7 | Mutual auth + hostname checks: set `GRPC_TLS_CLIENT_CA_PATH`; `ssl_check_hostname=true` on Valkey URLs | `_helpers.tpl:711-714`, `secrets.yaml:394,546`, `hub_api/grpc_internal/server.py` | none |
| F8 | CA prerequisites: dial by FQDN or extend nameConstraints (+ re-issue CA) | `k8s/dal2alpha/internal-ca.yaml`, new `templates/*-tls-certificate.yaml` | none, **land first** |
| F9 | Non-alpha-live hops: PostHog (web + deps), Ollama/Qdrant, OTel collector design TLS | `templates/infrastructure/posthog/*`, `ollama.yaml`, `qdrant.yaml`, `network-policies-egress.yaml` | none |
| F10 | Guardrail: CI render check failing on any in-cluster `http://`/`redis://` URL not allow-listed | `scripts/`, `Makefile`, `.github/workflows/` (via github-ops) | after F1-F6 |

### Alpha priority order

1. **F0** (immediate blanket coverage, one operator action)
2. **F8** (unblocks certs, tiny)
3. **F1 Postgres** (credentials + all data)
4. **F2 cache Valkey** (password in clear, quick change)
5. **F3a-d hub-api HTTPS** (service-token JWT mint, JWKS, REST, webui)
6. **F5 S3** (bundle fetch integrity + media)
7. **F4, F6** (Gateway/webui, core-data/streaming)
8. **F7** (verification hardening)
9. **F9, F10** (not live on alpha / guardrail)

Parallel batches: [F0, F8, F9, F7] then [F1, F2, F3a, F5, F6] then [F3b, F3c, F3d, F4] then [F10].
