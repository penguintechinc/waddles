//! [`BucketComponentSource`]: the real [`crate::invoke::ComponentSource`]
//! this binary wires in production (`crate::run`), replacing
//! [`crate::invoke::UnimplementedBucketSource`] -- spec
//! `docs/superpowers/specs/2026-09-14-rust-data-plane-design.md` SS7.6
//! ("On `load`, the executor: ... `GET`s `bundles/{app_id}/{version}/
//! {sha256}.json` (the sidecar) and `bundles/{app_id}/{version}/
//! {sha256}.wasm`") and SS12.7's `BUNDLE_BUCKET_*` env vars.
//!
//! **Key convention.** `{app_id}`/`{version}`/`{sha256}` are folded into
//! `component_key`/`sidecar_key` by the *stage* side, not here --
//! `core/svc_action/src/distribution.rs::bucket_keys` (the one place this
//! convention is actually produced today) computes
//! `bundles/{app_id}/{version}/{sha256_hex}.wasm` /`.json` from the
//! distribution API's `app_id`/`version`/`digest` fields, stripping the
//! `sha256:` prefix first. This module is purely the *consumer* of
//! whatever `component_key` a `load` frame carries -- it does not
//! reconstruct or validate that convention itself, matching
//! [`crate::invoke::ComponentSource::fetch`]'s existing contract.
//!
//! **Why not `object_store`** (the crate `core/svc_streaming` already uses
//! for its own S3-compatible uploads, and the obvious first guess for
//! "reuse an existing S3 client"): `object_store 0.14.1`'s `aws` feature
//! (and every other cloud feature: `azure`/`gcp`/`http`) unconditionally
//! pulls in `reqwest` --
//! `["aws-base", "reqwest", "reqwest/rustls", "aws-lc-rs"]` in its own
//! published `Cargo.toml` `[features]` table, confirmed against the exact
//! `=0.14.1` version pinned in `core/svc_streaming/Cargo.toml` via
//! `cargo tree -p object_store -e features`. `reqwest` is explicitly
//! banned for this specific binary by both `deny.toml`'s `[bans] deny`
//! list and `tests/dependency_policy.rs` (spec SS4.5/SS14.6 test 16: "The
//! executor binary links a networking or database crate" must fail the
//! build -- "this credential-less, network-egress-restricted process
//! ... must never link a crate that could talk to ... an arbitrary HTTP
//! endpoint"). Pulling `object_store` in here would either reintroduce
//! exactly the crate that test exists to keep out, or require weakening a
//! deliberate, documented security assertion -- neither is this change's
//! call to make silently. Instead this module hand-rolls the one HTTP
//! verb it needs (a single unsigned-body `GET`, AWS SigV4-signed) over the
//! `tokio`/`rustls`/`tokio-rustls` this binary already depends on, plus
//! `hmac` (RustCrypto, no networking dependency at all) for the signing
//! math -- the bucket GET is a *sanctioned, purpose-built* egress path
//! (`deny.toml`'s own comment: "this process's only egress is the
//! host-API port and the bucket"), not the free-form client the ban
//! targets.
//!
//! **Config convention reused verbatim from spec SS12.7**:
//! `BUNDLE_BUCKET_ENDPOINT` / `_NAME` / `_REGION` (default
//! `http://minio.waddles.svc.cluster.local:9000` / `waddles-bundles` /
//! `us-east-1`) and `BUNDLE_BUCKET_ACCESS_KEY_ID` / `_SECRET_ACCESS_KEY`
//! ("required, env only"). `BUNDLE_BUCKET_CA_FILE` is this module's own
//! addition (not in the spec's env table): required whenever
//! `BUNDLE_BUCKET_ENDPOINT` is `https://`, mirroring `crate::tls`'s
//! existing `HOST_API_CA_FILE` file-based-CA pattern rather than trusting
//! the system root store (this is a closed bucket endpoint, not the
//! public internet) or, worse, skipping verification.

use std::path::PathBuf;
use std::sync::Arc;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use hmac::{Hmac, Mac};
use rustls::{ClientConfig, RootCertStore};
use rustls_pki_types::ServerName;
use sha2::{Digest, Sha256};
use tokio::io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt};
use tokio::net::TcpStream;
use tokio_rustls::TlsConnector;

use crate::config::CliConfig;
use crate::error::ExecutorError;
use crate::invoke::ComponentSource;

/// SHA-256 of the empty byte string -- every request this module sends is
/// a body-less `GET`, so this constant (verified in
/// `sha256_hex_of_empty_input_matches_the_well_known_constant` below
/// rather than trusted blind) is the `x-amz-content-sha256` value on
/// every call.
const EMPTY_BODY_SHA256: &str = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";

/// Wraps a SigV4 secret access key so it can be held in [`BucketConfig`]
/// without an accidental `{:?}`/log exposure -- `Debug` redacts, matching
/// `core/svc_streaming`'s `crate::config::Secret` precedent (Token &
/// Secret Hygiene: "Log masked").
#[derive(Clone)]
pub struct SecretAccessKey(String);

impl SecretAccessKey {
    fn expose(&self) -> &str {
        &self.0
    }
}

impl std::fmt::Debug for SecretAccessKey {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("SecretAccessKey(***redacted***)")
    }
}

/// Resolved configuration for [`BucketComponentSource`] -- see the module
/// doc for the `BUNDLE_BUCKET_*` env var convention this is built from.
#[derive(Debug, Clone)]
pub struct BucketConfig {
    pub endpoint: String,
    pub bucket: String,
    pub region: String,
    pub access_key_id: String,
    pub secret_access_key: SecretAccessKey,
    pub ca_file: Option<PathBuf>,
    pub fetch_timeout: Duration,
    pub max_component_bytes: u64,
}

impl BucketConfig {
    /// Reads the `BUNDLE_BUCKET_*` fields `CliConfig` parsed from env/CLI
    /// (spec SS12.7). Fails closed -- a config error, not a panic or a
    /// silently-empty fetch -- when a required field is unset, or when
    /// `BUNDLE_BUCKET_ENDPOINT` is `https://` without a
    /// `BUNDLE_BUCKET_CA_FILE` (this binary never disables TLS
    /// verification, `rules/security.md`).
    pub fn from_cli(cfg: &CliConfig) -> Result<Self, ExecutorError> {
        let endpoint = cfg
            .bundle_bucket_endpoint
            .clone()
            .filter(|s| !s.trim().is_empty())
            .ok_or_else(|| {
                ExecutorError::Config("BUNDLE_BUCKET_ENDPOINT must be set".to_string())
            })?;
        let bucket = cfg
            .bundle_bucket_name
            .clone()
            .filter(|s| !s.trim().is_empty())
            .ok_or_else(|| ExecutorError::Config("BUNDLE_BUCKET_NAME must be set".to_string()))?;
        let access_key_id = cfg
            .bundle_bucket_access_key_id
            .clone()
            .filter(|s| !s.trim().is_empty())
            .ok_or_else(|| {
                ExecutorError::Config("BUNDLE_BUCKET_ACCESS_KEY_ID must be set".to_string())
            })?;
        let secret_access_key = cfg
            .bundle_bucket_secret_access_key
            .clone()
            .filter(|s| !s.trim().is_empty())
            .ok_or_else(|| {
                ExecutorError::Config("BUNDLE_BUCKET_SECRET_ACCESS_KEY must be set".to_string())
            })?;
        if endpoint.starts_with("https://") && cfg.bundle_bucket_ca_file.is_none() {
            return Err(ExecutorError::Config(
                "BUNDLE_BUCKET_CA_FILE must be set for an https:// BUNDLE_BUCKET_ENDPOINT"
                    .to_string(),
            ));
        }
        Ok(Self {
            endpoint,
            bucket,
            region: cfg.bundle_bucket_region.clone(),
            access_key_id,
            secret_access_key: SecretAccessKey(secret_access_key),
            ca_file: cfg.bundle_bucket_ca_file.clone(),
            fetch_timeout: Duration::from_secs(cfg.bundle_fetch_timeout_s),
            max_component_bytes: cfg.bundle_max_component_bytes,
        })
    }
}

/// The production [`ComponentSource`]: SigV4-signs and issues one `GET`
/// per `fetch` call against `config.endpoint`'s `config.bucket`, path-style
/// (`/{bucket}/{key}`), returning the component bytes on `200`.
///
/// **Scope note** (matching this crate's existing "never fake a host
/// call, but a realistic-scope item may stay a documented TODO"
/// convention): only `component_key` is fetched. `sidecar_key` is still
/// accepted (the trait signature is unchanged) but not fetched -- nothing
/// in `crate::invoke::on_load` consumes sidecar bytes today; the spec
/// SS7.6 step 4 Ed25519 signature check over the sidecar is a separate,
/// not-yet-wired verification step this change does not add.
pub struct BucketComponentSource {
    config: BucketConfig,
}

impl BucketComponentSource {
    pub fn new(config: BucketConfig) -> Self {
        Self { config }
    }

    /// Builds a source directly from `CliConfig`'s `BUNDLE_BUCKET_*`
    /// fields (`BucketConfig::from_cli`) -- the constructor `crate::run`
    /// uses.
    pub fn from_cli(cfg: &CliConfig) -> Result<Self, ExecutorError> {
        Ok(Self::new(BucketConfig::from_cli(cfg)?))
    }
}

impl ComponentSource for BucketComponentSource {
    async fn fetch(
        &self,
        component_key: &str,
        _sidecar_key: &str,
    ) -> Result<Vec<u8>, ExecutorError> {
        tokio::time::timeout(
            self.config.fetch_timeout,
            get_object(&self.config, component_key),
        )
        .await
        .map_err(|_| {
            ExecutorError::BucketFetch(format!(
                "bucket GET for {component_key:?} timed out after {:?}",
                self.config.fetch_timeout
            ))
        })?
    }
}

struct Endpoint {
    tls: bool,
    host: String,
    port: u16,
}

/// Parses `BUNDLE_BUCKET_ENDPOINT` (`scheme://host[:port]`, spec SS12.7's
/// documented shape -- no path component) into its connection parts.
fn parse_endpoint(raw: &str) -> Result<Endpoint, ExecutorError> {
    let (tls, rest) = if let Some(r) = raw.strip_prefix("https://") {
        (true, r)
    } else if let Some(r) = raw.strip_prefix("http://") {
        (false, r)
    } else {
        return Err(ExecutorError::Config(format!(
            "BUNDLE_BUCKET_ENDPOINT {raw:?} must start with http:// or https://"
        )));
    };
    // Defensive: the documented shape has no path, but strip one if present
    // rather than folding it into the host.
    let rest = rest.split('/').next().unwrap_or(rest);
    let (host, port) = match rest.rsplit_once(':') {
        Some((h, p)) => {
            let port: u16 = p.parse().map_err(|_| {
                ExecutorError::Config(format!("invalid port in BUNDLE_BUCKET_ENDPOINT {raw:?}"))
            })?;
            (h.to_string(), port)
        }
        None => (rest.to_string(), if tls { 443 } else { 80 }),
    };
    if host.is_empty() {
        return Err(ExecutorError::Config(format!(
            "BUNDLE_BUCKET_ENDPOINT {raw:?} has no host"
        )));
    }
    Ok(Endpoint { tls, host, port })
}

/// AWS URI-encoding for a canonical-request path segment: unreserved
/// characters (`A-Za-z0-9-_.~`) and the `/` separator pass through
/// untouched; everything else becomes `%XY` (uppercase hex, per the SigV4
/// spec).
fn uri_encode(input: &str) -> String {
    let mut out = String::with_capacity(input.len());
    for byte in input.bytes() {
        match byte {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' | b'/' => {
                out.push(byte as char);
            }
            _ => out.push_str(&format!("%{byte:02X}")),
        }
    }
    out
}

fn sha256_hex(data: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(data);
    format!("{:x}", hasher.finalize())
}

type HmacSha256 = Hmac<Sha256>;

fn hmac_sha256(key: &[u8], data: &[u8]) -> Result<[u8; 32], ExecutorError> {
    let mut mac = <HmacSha256 as Mac>::new_from_slice(key)
        .map_err(|e| ExecutorError::Config(format!("invalid HMAC key: {e}")))?;
    mac.update(data);
    let result = mac.finalize().into_bytes();
    let mut out = [0u8; 32];
    out.copy_from_slice(&result);
    Ok(out)
}

/// SigV4's four-step signing-key derivation (`AWS4<secret>` -> date ->
/// region -> service -> `aws4_request`), service fixed to `"s3"`.
fn derive_signing_key(
    secret: &str,
    date_stamp: &str,
    region: &str,
) -> Result<[u8; 32], ExecutorError> {
    let k_date = hmac_sha256(format!("AWS4{secret}").as_bytes(), date_stamp.as_bytes())?;
    let k_region = hmac_sha256(&k_date, region.as_bytes())?;
    let k_service = hmac_sha256(&k_region, b"s3")?;
    hmac_sha256(&k_service, b"aws4_request")
}

/// Howard Hinnant's `civil_from_days` (public domain,
/// <http://howardhinnant.github.io/date_algorithms.html>) -- converts days
/// since the Unix epoch into a proleptic-Gregorian `(year, month, day)`.
/// Used instead of adding a date/time crate dependency purely to format
/// the two SigV4 timestamp strings: this binary's dependency-set
/// minimalism is a stated security property (`Cargo.toml`'s header
/// comment), and this ~10-line computation is independently verified
/// against known dates in `civil_from_days_matches_known_dates` below.
fn civil_from_days(z: i64) -> (i64, u32, u32) {
    let z = z + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = (z - era * 146_097) as u64; // [0, 146096]
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365; // [0, 399]
    let y = yoe as i64 + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100); // [0, 365]
    let mp = (5 * doy + 2) / 153; // [0, 11]
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32; // [1, 31]
    let m = if mp < 10 { mp + 3 } else { mp - 9 } as u32; // [1, 12]
    let y = if m <= 2 { y + 1 } else { y };
    (y, m, d)
}

/// Formats `now` as SigV4's `(x-amz-date, date_stamp)` pair:
/// `YYYYMMDDTHHMMSSZ` and `YYYYMMDD`.
fn amz_timestamp(now: SystemTime) -> Result<(String, String), ExecutorError> {
    let secs = now
        .duration_since(UNIX_EPOCH)
        .map_err(|e| ExecutorError::Config(format!("system clock before UNIX epoch: {e}")))?
        .as_secs();
    let days = (secs / 86_400) as i64;
    let secs_of_day = secs % 86_400;
    let (year, month, day) = civil_from_days(days);
    let hour = secs_of_day / 3600;
    let minute = (secs_of_day % 3600) / 60;
    let second = secs_of_day % 60;
    Ok((
        format!("{year:04}{month:02}{day:02}T{hour:02}{minute:02}{second:02}Z"),
        format!("{year:04}{month:02}{day:02}"),
    ))
}

/// Builds the SigV4 `Authorization` header value for a body-less `GET` at
/// `canonical_uri` against `host_header`.
fn build_authorization(
    config: &BucketConfig,
    host_header: &str,
    canonical_uri: &str,
    amz_date: &str,
    date_stamp: &str,
) -> Result<String, ExecutorError> {
    let canonical_request = format!(
        "GET\n{canonical_uri}\n\nhost:{host_header}\nx-amz-content-sha256:{EMPTY_BODY_SHA256}\nx-amz-date:{amz_date}\n\nhost;x-amz-content-sha256;x-amz-date\n{EMPTY_BODY_SHA256}"
    );
    let credential_scope = format!("{date_stamp}/{}/s3/aws4_request", config.region);
    let hashed_canonical_request = sha256_hex(canonical_request.as_bytes());
    let string_to_sign =
        format!("AWS4-HMAC-SHA256\n{amz_date}\n{credential_scope}\n{hashed_canonical_request}");
    let signing_key = derive_signing_key(
        config.secret_access_key.expose(),
        date_stamp,
        &config.region,
    )?;
    let signature_bytes = hmac_sha256(&signing_key, string_to_sign.as_bytes())?;
    let signature = signature_bytes.iter().fold(String::new(), |mut acc, b| {
        acc.push_str(&format!("{b:02x}"));
        acc
    });
    Ok(format!(
        "AWS4-HMAC-SHA256 Credential={}/{credential_scope}, SignedHeaders=host;x-amz-content-sha256;x-amz-date, Signature={signature}",
        config.access_key_id
    ))
}

fn build_bucket_tls_config(config: &BucketConfig) -> Result<ClientConfig, ExecutorError> {
    let ca_path = config.ca_file.as_ref().ok_or_else(|| {
        ExecutorError::Config(
            "BUNDLE_BUCKET_CA_FILE must be set for an https:// BUNDLE_BUCKET_ENDPOINT".to_string(),
        )
    })?;
    let mut roots = RootCertStore::empty();
    for cert in crate::tls::load_certs(ca_path)? {
        roots
            .add(cert)
            .map_err(|e| ExecutorError::Config(format!("invalid CA certificate: {e}")))?;
    }
    Ok(ClientConfig::builder()
        .with_root_certificates(roots)
        .with_no_client_auth())
}

/// Issues the signed `GET` and returns the response body's bytes.
async fn get_object(config: &BucketConfig, key: &str) -> Result<Vec<u8>, ExecutorError> {
    let endpoint = parse_endpoint(&config.endpoint)?;
    let canonical_uri = format!("/{}/{}", uri_encode(&config.bucket), uri_encode(key));
    let host_header = format!("{}:{}", endpoint.host, endpoint.port);
    let (amz_date, date_stamp) = amz_timestamp(SystemTime::now())?;
    let authorization =
        build_authorization(config, &host_header, &canonical_uri, &amz_date, &date_stamp)?;

    let request = format!(
        "GET {canonical_uri} HTTP/1.1\r\nHost: {host_header}\r\nx-amz-date: {amz_date}\r\nx-amz-content-sha256: {EMPTY_BODY_SHA256}\r\nAuthorization: {authorization}\r\nConnection: close\r\n\r\n"
    );

    let tcp = TcpStream::connect((endpoint.host.as_str(), endpoint.port))
        .await
        .map_err(ExecutorError::Io)?;

    if endpoint.tls {
        let tls_config = build_bucket_tls_config(config)?;
        let connector = TlsConnector::from(Arc::new(tls_config));
        let server_name = ServerName::try_from(endpoint.host.clone()).map_err(|e| {
            ExecutorError::Config(format!("invalid bucket host {:?}: {e}", endpoint.host))
        })?;
        let mut stream = connector
            .connect(server_name, tcp)
            .await
            .map_err(ExecutorError::Io)?;
        send_and_read(&mut stream, request.as_bytes(), config.max_component_bytes).await
    } else {
        let mut stream = tcp;
        send_and_read(&mut stream, request.as_bytes(), config.max_component_bytes).await
    }
}

/// Finds the index of the header/body separator (`\r\n\r\n`), if the
/// buffer contains one yet.
fn find_header_end(buf: &[u8]) -> Option<usize> {
    buf.windows(4).position(|w| w == b"\r\n\r\n")
}

/// Parses the status line and `Content-Length` header out of a raw
/// HTTP/1.1 header block.
fn parse_status_and_length(header_text: &str) -> Result<(u16, Option<u64>), ExecutorError> {
    let mut lines = header_text.split("\r\n");
    let status_line = lines
        .next()
        .filter(|l| !l.is_empty())
        .ok_or_else(|| ExecutorError::BucketFetch("empty response from bucket".to_string()))?;
    let status: u16 = status_line
        .split_whitespace()
        .nth(1)
        .ok_or_else(|| {
            ExecutorError::BucketFetch(format!("malformed status line {status_line:?}"))
        })?
        .parse()
        .map_err(|_| {
            ExecutorError::BucketFetch(format!("malformed status line {status_line:?}"))
        })?;
    let mut content_length = None;
    for line in lines {
        if let Some((k, v)) = line.split_once(':') {
            if k.trim().eq_ignore_ascii_case("content-length") {
                content_length = v.trim().parse::<u64>().ok();
            }
        }
    }
    Ok((status, content_length))
}

/// Sends `request` and reads a full HTTP/1.1 response, enforcing
/// `max_bytes` against the advertised `Content-Length` before reading a
/// single body byte -- never buffers unbounded attacker/misconfiguration
/// data.
async fn send_and_read<T>(
    stream: &mut T,
    request: &[u8],
    max_bytes: u64,
) -> Result<Vec<u8>, ExecutorError>
where
    T: AsyncRead + AsyncWrite + Unpin,
{
    stream.write_all(request).await.map_err(ExecutorError::Io)?;

    let mut buf = Vec::new();
    let mut chunk = [0u8; 4096];
    let header_end = loop {
        let n = stream.read(&mut chunk).await.map_err(ExecutorError::Io)?;
        if n == 0 {
            return Err(ExecutorError::BucketFetch(
                "bucket connection closed before headers completed".to_string(),
            ));
        }
        buf.extend_from_slice(&chunk[..n]);
        if let Some(pos) = find_header_end(&buf) {
            break pos;
        }
        if buf.len() > 64 * 1024 {
            return Err(ExecutorError::BucketFetch(
                "bucket response headers exceeded 64KiB".to_string(),
            ));
        }
    };

    let header_text = String::from_utf8_lossy(&buf[..header_end]).into_owned();
    let (status, content_length) = parse_status_and_length(&header_text)?;
    if status != 200 {
        return Err(ExecutorError::BucketFetch(format!(
            "bucket GET returned HTTP {status}"
        )));
    }
    let content_length = content_length.ok_or_else(|| {
        ExecutorError::BucketFetch("bucket response had no Content-Length".to_string())
    })?;
    if content_length > max_bytes {
        return Err(ExecutorError::BucketFetch(format!(
            "component size {content_length} exceeds BUNDLE_MAX_COMPONENT_BYTES {max_bytes}"
        )));
    }

    let body_start = header_end + 4; // "\r\n\r\n"
    let mut body = buf[body_start..].to_vec();
    while (body.len() as u64) < content_length {
        let n = stream.read(&mut chunk).await.map_err(ExecutorError::Io)?;
        if n == 0 {
            return Err(ExecutorError::BucketFetch(format!(
                "bucket connection closed after {} of {content_length} body bytes",
                body.len()
            )));
        }
        body.extend_from_slice(&chunk[..n]);
    }
    body.truncate(content_length as usize);
    Ok(body)
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::*;
    use tokio::net::TcpListener;

    fn test_bucket_config(endpoint: &str) -> BucketConfig {
        BucketConfig {
            endpoint: endpoint.to_string(),
            bucket: "waddles-bundles".to_string(),
            region: "us-east-1".to_string(),
            access_key_id: "AKIAIOSFODNN7EXAMPLE".to_string(),
            secret_access_key: SecretAccessKey(
                "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY".to_string(),
            ),
            ca_file: None,
            fetch_timeout: Duration::from_secs(5),
            max_component_bytes: 33_554_432,
        }
    }

    fn cli_with_bucket_env() -> CliConfig {
        use clap::Parser;
        let mut cfg = CliConfig::try_parse_from([
            "bundle-executor",
            "--stage-host-api-addr",
            "svc-process:8301",
        ])
        .expect("static test args always parse");
        cfg.bundle_bucket_endpoint = Some("http://127.0.0.1:9000".to_string());
        cfg.bundle_bucket_name = Some("waddles-bundles".to_string());
        cfg.bundle_bucket_access_key_id = Some("AKIAIOSFODNN7EXAMPLE".to_string());
        cfg.bundle_bucket_secret_access_key =
            Some("wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY".to_string());
        cfg
    }

    #[test]
    fn secret_access_key_debug_is_redacted() {
        let secret = SecretAccessKey("super-secret-value".to_string());
        let debug = format!("{secret:?}");
        assert!(!debug.contains("super-secret-value"));
        assert!(debug.contains("redacted"));
    }

    #[test]
    fn from_cli_requires_every_bundle_bucket_field() {
        let cfg = CliConfig::for_healthcheck();
        assert!(matches!(
            BucketConfig::from_cli(&cfg),
            Err(ExecutorError::Config(_))
        ));
    }

    #[test]
    fn from_cli_rejects_https_without_a_ca_file() {
        let mut cfg = cli_with_bucket_env();
        cfg.bundle_bucket_endpoint = Some("https://minio.example.com:9000".to_string());
        let err = BucketConfig::from_cli(&cfg);
        assert!(matches!(err, Err(ExecutorError::Config(_))));
    }

    #[test]
    fn from_cli_builds_a_config_from_valid_env() -> Result<(), ExecutorError> {
        let cfg = cli_with_bucket_env();
        let bucket_cfg = BucketConfig::from_cli(&cfg)?;
        assert_eq!(bucket_cfg.endpoint, "http://127.0.0.1:9000");
        assert_eq!(bucket_cfg.bucket, "waddles-bundles");
        assert_eq!(bucket_cfg.region, "us-east-1");
        Ok(())
    }

    #[test]
    fn parse_endpoint_parses_http_with_explicit_port() -> Result<(), ExecutorError> {
        let ep = parse_endpoint("http://minio.waddles.svc.cluster.local:9000")?;
        assert!(!ep.tls);
        assert_eq!(ep.host, "minio.waddles.svc.cluster.local");
        assert_eq!(ep.port, 9000);
        Ok(())
    }

    #[test]
    fn parse_endpoint_defaults_https_port_to_443() -> Result<(), ExecutorError> {
        let ep = parse_endpoint("https://bucket.example.com")?;
        assert!(ep.tls);
        assert_eq!(ep.port, 443);
        Ok(())
    }

    #[test]
    fn parse_endpoint_rejects_a_missing_scheme() {
        assert!(matches!(
            parse_endpoint("minio.waddles.svc.cluster.local:9000"),
            Err(ExecutorError::Config(_))
        ));
    }

    #[test]
    fn parse_endpoint_rejects_an_empty_host() {
        assert!(matches!(
            parse_endpoint("http://:9000"),
            Err(ExecutorError::Config(_))
        ));
    }

    #[test]
    fn parse_endpoint_rejects_an_unparseable_port() {
        assert!(matches!(
            parse_endpoint("http://example.com:not-a-port"),
            Err(ExecutorError::Config(_))
        ));
    }

    #[test]
    fn uri_encode_leaves_unreserved_characters_and_slashes_untouched() {
        assert_eq!(
            uri_encode("bundles/waddles.test.app/1/abc-DEF_123~.wasm"),
            "bundles/waddles.test.app/1/abc-DEF_123~.wasm"
        );
    }

    #[test]
    fn uri_encode_escapes_reserved_characters() {
        assert_eq!(uri_encode("a b+c"), "a%20b%2Bc");
    }

    /// Ground truth for the empty-string SHA-256 digest -- verified rather
    /// than trusted as a hardcoded magic string, since `EMPTY_BODY_SHA256`
    /// is on every signed request's canonical request.
    #[test]
    fn sha256_hex_of_empty_input_matches_the_well_known_constant() {
        assert_eq!(sha256_hex(b""), EMPTY_BODY_SHA256);
    }

    /// RFC 4231 test case 1 (independently confirmed via Python's stdlib
    /// `hmac`/`hashlib`, not recalled from memory) -- an external oracle
    /// for `hmac_sha256`, not a tautological self-check.
    #[test]
    fn hmac_sha256_matches_rfc_4231_test_case_1() -> Result<(), ExecutorError> {
        let key = [0x0bu8; 20];
        let data = b"Hi There";
        let mac = hmac_sha256(&key, data)?;
        let hex = mac.iter().fold(String::new(), |mut acc, b| {
            acc.push_str(&format!("{b:02x}"));
            acc
        });
        assert_eq!(
            hex,
            "b0344c61d8db38535ca8afceaf0bf12b881dc200c9833da726e9376c2e32cff7"
        );
        Ok(())
    }

    #[test]
    fn civil_from_days_matches_known_dates() {
        assert_eq!(civil_from_days(0), (1970, 1, 1));
        assert_eq!(civil_from_days(19_723), (2024, 1, 1));
        assert_eq!(civil_from_days(19_782), (2024, 2, 29)); // leap day
        assert_eq!(civil_from_days(20_089), (2025, 1, 1));
    }

    #[test]
    fn amz_timestamp_formats_a_known_instant() -> Result<(), ExecutorError> {
        let instant = UNIX_EPOCH + Duration::from_secs(1_369_353_600); // 2013-05-24T00:00:00Z
        let (amz_date, date_stamp) = amz_timestamp(instant)?;
        assert_eq!(amz_date, "20130524T000000Z");
        assert_eq!(date_stamp, "20130524");
        Ok(())
    }

    /// AWS's own published SigV4 GET-object example (`GET
    /// https://examplebucket.s3.amazonaws.com/test.txt`), cross-checked
    /// independently via a clean-room Python `hmac`/`hashlib`
    /// reimplementation of the same publicly-documented algorithm rather
    /// than recalled from memory -- two independent implementations of the
    /// same spec agreeing is the external oracle here.
    #[test]
    fn build_authorization_matches_the_aws_get_object_example() -> Result<(), ExecutorError> {
        let config = BucketConfig {
            endpoint: "https://examplebucket.s3.amazonaws.com".to_string(),
            bucket: "unused-for-this-test".to_string(),
            region: "us-east-1".to_string(),
            access_key_id: "AKIAIOSFODNN7EXAMPLE".to_string(),
            secret_access_key: SecretAccessKey(
                "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY".to_string(),
            ),
            ca_file: None,
            fetch_timeout: Duration::from_secs(5),
            max_component_bytes: 33_554_432,
        };
        let authorization = build_authorization(
            &config,
            "examplebucket.s3.amazonaws.com",
            "/test.txt",
            "20130524T000000Z",
            "20130524",
        )?;
        assert_eq!(
            authorization,
            "AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request, \
             SignedHeaders=host;x-amz-content-sha256;x-amz-date, \
             Signature=df548e2ce037944d03f3e68682813b093763996d597cf890ca3d9037fd231eb4"
        );
        Ok(())
    }

    #[test]
    fn parse_status_and_length_reads_status_and_content_length() -> Result<(), ExecutorError> {
        let headers = "HTTP/1.1 200 OK\r\nContent-Length: 42\r\nServer: minio\r\n";
        let (status, len) = parse_status_and_length(headers)?;
        assert_eq!(status, 200);
        assert_eq!(len, Some(42));
        Ok(())
    }

    #[test]
    fn parse_status_and_length_reports_missing_content_length() -> Result<(), ExecutorError> {
        let (status, len) = parse_status_and_length("HTTP/1.1 200 OK\r\nServer: minio\r\n")?;
        assert_eq!(status, 200);
        assert_eq!(len, None);
        Ok(())
    }

    #[test]
    fn parse_status_and_length_rejects_an_empty_response() {
        assert!(matches!(
            parse_status_and_length(""),
            Err(ExecutorError::BucketFetch(_))
        ));
    }

    #[test]
    fn find_header_end_locates_the_separator() {
        let buf = b"HTTP/1.1 200 OK\r\n\r\nbody";
        assert_eq!(find_header_end(buf), Some(15));
        assert_eq!(find_header_end(b"no separator here"), None);
    }

    /// Drives `get_object` against a real local TCP listener that speaks
    /// just enough HTTP/1.1 to stand in for MinIO: reads the request line,
    /// asserts it is a signed `GET` for the expected path, and replies
    /// with a fixed body -- proving the request-building, sending, and
    /// response-parsing halves of the real (non-stub) fetch path, not just
    /// the signing math in isolation.
    #[tokio::test]
    async fn get_object_fetches_bytes_from_a_real_local_http_server(
    ) -> Result<(), Box<dyn std::error::Error>> {
        let listener = TcpListener::bind("127.0.0.1:0").await?;
        let addr = listener.local_addr()?;
        let body = b"hello from the bucket".to_vec();
        let body_for_server = body.clone();

        let server = tokio::spawn(async move {
            let (mut socket, _) = listener.accept().await.expect("accept");
            let mut buf = vec![0u8; 4096];
            let n = socket.read(&mut buf).await.expect("read request");
            let request = String::from_utf8_lossy(&buf[..n]).into_owned();
            // The key contains only unreserved chars and `/`, so nothing
            // should actually be percent-encoded -- assert the exact
            // expected request line and a well-formed SigV4 Authorization
            // header, proving this isn't just "any GET succeeds".
            assert!(request.starts_with(
                "GET /waddles-bundles/bundles/waddles.test.app/1/abc.wasm HTTP/1.1\r\n"
            ));
            assert!(request
                .contains("Authorization: AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/"));
            assert!(request.contains("x-amz-content-sha256: e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"));

            let response = format!(
                "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                body_for_server.len()
            );
            socket
                .write_all(response.as_bytes())
                .await
                .expect("write status");
            socket
                .write_all(&body_for_server)
                .await
                .expect("write body");
            socket.shutdown().await.expect("shutdown");
        });

        let config = test_bucket_config(&format!("http://{addr}"));
        let fetched = get_object(&config, "bundles/waddles.test.app/1/abc.wasm").await?;
        assert_eq!(fetched, body);

        server.await?;
        Ok(())
    }

    #[tokio::test]
    async fn get_object_surfaces_a_non_200_status_as_a_bucket_fetch_error(
    ) -> Result<(), Box<dyn std::error::Error>> {
        let listener = TcpListener::bind("127.0.0.1:0").await?;
        let addr = listener.local_addr()?;

        let server = tokio::spawn(async move {
            let (mut socket, _) = listener.accept().await.expect("accept");
            let mut buf = vec![0u8; 4096];
            let _ = socket.read(&mut buf).await.expect("read request");
            socket
                .write_all(
                    b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
                )
                .await
                .expect("write 404");
            socket.shutdown().await.expect("shutdown");
        });

        let config = test_bucket_config(&format!("http://{addr}"));
        let err = get_object(&config, "bundles/never-loaded/1/x.wasm").await;
        assert!(matches!(err, Err(ExecutorError::BucketFetch(_))));

        server.await?;
        Ok(())
    }

    #[tokio::test]
    async fn get_object_rejects_a_content_length_over_the_configured_cap(
    ) -> Result<(), Box<dyn std::error::Error>> {
        let listener = TcpListener::bind("127.0.0.1:0").await?;
        let addr = listener.local_addr()?;

        let server = tokio::spawn(async move {
            let (mut socket, _) = listener.accept().await.expect("accept");
            let mut buf = vec![0u8; 4096];
            let _ = socket.read(&mut buf).await.expect("read request");
            socket
                .write_all(
                    b"HTTP/1.1 200 OK\r\nContent-Length: 999999999\r\nConnection: close\r\n\r\n",
                )
                .await
                .expect("write oversized header");
            socket.shutdown().await.expect("shutdown");
        });

        let mut config = test_bucket_config(&format!("http://{addr}"));
        config.max_component_bytes = 1024;
        let err = get_object(&config, "bundles/too-big/1/x.wasm").await;
        assert!(matches!(err, Err(ExecutorError::BucketFetch(_))));

        server.await?;
        Ok(())
    }

    #[tokio::test]
    async fn fetch_times_out_against_a_server_that_never_responds(
    ) -> Result<(), Box<dyn std::error::Error>> {
        let listener = TcpListener::bind("127.0.0.1:0").await?;
        let addr = listener.local_addr()?;

        let server = tokio::spawn(async move {
            // Accept and hold the connection open without ever writing a
            // response -- `BucketComponentSource::fetch`'s
            // `tokio::time::timeout` must still return.
            let (_socket, _) = listener.accept().await.expect("accept");
            tokio::time::sleep(Duration::from_secs(5)).await;
        });

        let mut config = test_bucket_config(&format!("http://{addr}"));
        config.fetch_timeout = Duration::from_millis(50);
        let source = BucketComponentSource::new(config);
        let result = source
            .fetch("bundles/slow/1/x.wasm", "bundles/slow/1/x.json")
            .await;
        assert!(matches!(result, Err(ExecutorError::BucketFetch(_))));

        server.abort();
        Ok(())
    }

    /// The task-level proof: `Executor::on_load` fetching a REAL component
    /// through the REAL (non-stub, non-`FixtureSource`) `fetch` -- a
    /// SigV4-signed GET against a real local TCP listener -- verifying its
    /// digest and compiling it, then `Executor::on_invoke` running a real
    /// export on the instantiated component. Before this change,
    /// production wiring (`crate::run`) used
    /// `crate::invoke::UnimplementedBucketSource`, which fails every fetch
    /// by construction; no bundle could ever load. `event_type:
    /// "memory-hog"` is used for the invoke because it is the fixture's
    /// only `transform` branch that completes without a WIT host-call
    /// (see `tests/fixtures/README.md`), so this test needs no stage-side
    /// host-call responder to reach a clean, deterministic `Ok` -- proving
    /// instantiation and export dispatch, not host-call bridging (already
    /// covered by `tests/host_bridge_integration.rs`).
    #[tokio::test]
    async fn bucket_component_source_loads_and_invokes_a_real_component_end_to_end(
    ) -> Result<(), Box<dyn std::error::Error>> {
        use crate::wire::RequestHandler as _;
        use penguin_bundle_host::wire::{ExportKind, InvokeBody, LoadBody, LoadLimits};

        const APP_ID: &str = "waddles.test.bucket-e2e";
        const FIXTURE_WASM: &[u8] = include_bytes!("../tests/fixtures/hostile_fixture.wasm");

        fn fixture_digest() -> String {
            let mut hasher = Sha256::new();
            hasher.update(FIXTURE_WASM);
            format!("sha256:{:x}", hasher.finalize())
        }

        let digest = fixture_digest();
        let sha256_hex = digest.trim_start_matches("sha256:").to_string();
        let component_key = format!("bundles/{APP_ID}/1/{sha256_hex}.wasm");
        let expected_request_line = format!("GET /waddles-bundles/{component_key} HTTP/1.1\r\n");

        let listener = TcpListener::bind("127.0.0.1:0").await?;
        let addr = listener.local_addr()?;
        let server = tokio::spawn(async move {
            let (mut socket, _) = listener.accept().await.expect("accept");
            let mut buf = vec![0u8; 8192];
            let n = socket.read(&mut buf).await.expect("read request");
            let request = String::from_utf8_lossy(&buf[..n]).into_owned();
            assert!(
                request.starts_with(&expected_request_line),
                "unexpected request line: {request}"
            );
            assert!(request
                .contains("Authorization: AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/"));
            let header = format!(
                "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                FIXTURE_WASM.len()
            );
            socket
                .write_all(header.as_bytes())
                .await
                .expect("write header");
            socket.write_all(FIXTURE_WASM).await.expect("write body");
            socket.shutdown().await.expect("shutdown");
        });

        let mut cfg = cli_with_bucket_env();
        cfg.bundle_bucket_endpoint = Some(format!("http://{addr}"));
        let source = BucketComponentSource::from_cli(&cfg)?;
        let executor = crate::invoke::Executor::new(&cfg, source)?;

        let loaded = executor
            .on_load(LoadBody {
                app_id: APP_ID.to_string(),
                version: "1".to_string(),
                digest: digest.clone(),
                component_key: component_key.clone(),
                sidecar_key: format!("bundles/{APP_ID}/1/{sha256_hex}.json"),
                capabilities: vec![],
                limits: LoadLimits {
                    timeout_ms: 2000,
                    // Comfortably above the fixture's 64 MiB `memory-hog`
                    // allocation plus the component's own baseline runtime
                    // footprint and dlmalloc/page-growth overhead (128 MiB
                    // was observed to still trip the cap by one 64KiB wasm
                    // page), so this proves a clean success path through
                    // the REAL bucket-fetched component rather than
                    // exercising the (separately, already-tested in
                    // `crate::invoke`) memory-cap trap. 256 is also this
                    // crate's own `EXECUTOR_MAX_MEMORY_LIMIT_MB` default
                    // ceiling, so this is the most headroom a `load` can
                    // request at all.
                    memory_mb: 256,
                },
            })
            .await
            .map_err(|e| format!("on_load against the real bucket fetch failed: {e:?}"))?;
        assert_eq!(loaded.digest, digest);
        assert_eq!(
            loaded.exports,
            vec!["transform".to_string(), "dispatch".to_string()]
        );

        let (tx, _rx) = tokio::sync::mpsc::unbounded_channel();
        let connection = crate::wire::Connection::new(tx);
        let result = executor
            .on_invoke(
                InvokeBody {
                    app_id: APP_ID.to_string(),
                    digest: digest.clone(),
                    export: ExportKind::Transform,
                    payload: serde_json::json!({
                        "platform": "test",
                        "event_type": "memory-hog",
                        "actor": null,
                        "payload_json": "{}",
                        "occurred_at": "2026-09-25T00:00:00.000Z",
                    }),
                    deadline_ms: 10_000,
                    trace: None,
                },
                1,
                connection,
            )
            .await;
        assert!(
            result.is_ok(),
            "on_invoke against the real bucket-loaded component failed: {result:?}"
        );

        server.await?;
        Ok(())
    }
}
