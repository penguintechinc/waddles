//! Runtime configuration, sourced entirely from environment variables (12
//! factor -- matches the env vars already wired into
//! `k8s/helm/waddlebot/templates/infrastructure/egress-proxy.yaml` by
//! feature/bundle-egress-gateway, plus the machine-JWT/limiter variables
//! this crate adds on top of that skeleton).

use std::net::IpAddr;
use std::time::Duration;

use ipnet::IpNet;

/// Every tunable this service reads at startup. `from_env` fails closed --
/// a missing required variable is a startup error, never a silent default
/// that would weaken the security posture (signing key path, JWKS URL).
#[derive(Debug, Clone)]
pub struct Config {
    pub listen_port: u16,
    pub metrics_port: u16,
    pub allowed_ports: Vec<u16>,
    pub deny_cidrs: Vec<IpNet>,
    pub deny_cluster_cidrs: Vec<IpNet>,
    pub machine_jwt_jwks_url: String,
    pub machine_jwt_audience: String,
    pub machine_jwt_trusted_issuers: Vec<String>,
    pub machine_jwt_required_scope: String,
    pub allowed_caller_services: Vec<String>,
    pub per_tenant_max_connections: u32,
    pub per_tenant_bandwidth_bytes_per_sec: u64,
    pub connect_timeout: Duration,
    pub assertion_max_ttl: Duration,
    /// Timeout for reading a client's request headers off the wire
    /// (`hyper::server::conn::http1::Builder::header_read_timeout`) --
    /// bounds a slow-loris-style caller that opens a connection and
    /// trickles headers in indefinitely.
    pub header_read_timeout: Duration,
    /// Inactivity timeout applied to each direction of a CONNECT tunnel's
    /// byte-copy loop: no bytes read within this window closes the
    /// tunnel, freeing the tenant's connection-limiter slot.
    pub tunnel_idle_timeout: Duration,
    /// Hard ceiling on a single CONNECT tunnel's total lifetime,
    /// regardless of activity -- bounds a connection an operator's
    /// destination keeps trickling just enough traffic through to dodge
    /// the idle timeout forever.
    pub tunnel_max_duration: Duration,
    /// Defense-in-depth, operator-controlled kill switch for the
    /// `PrivateIp` assertion category: even a validly-signed, correctly
    /// destination-matched private-IP grant is refused unless this
    /// deployment has explicitly opted in. Independent of (layered
    /// underneath) the assertion's own category -- a compromised/
    /// misconfigured signer minting private-IP grants is still contained
    /// by this being `false` by default. `EGRESS_PROXY_ALLOW_PRIVATE_IP`.
    pub allow_private_ip: bool,
}

#[derive(thiserror::Error, Debug)]
pub enum ConfigError {
    #[error("missing required env var {0}")]
    Missing(&'static str),
    #[error("invalid value for {0}: {1}")]
    Invalid(&'static str, String),
}

fn env_or(name: &str, default: &str) -> String {
    std::env::var(name).unwrap_or_else(|_| default.to_string())
}

fn parse_ports(raw: &str) -> Result<Vec<u16>, ConfigError> {
    raw.split(',')
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(|s| {
            s.parse::<u16>()
                .map_err(|_| ConfigError::Invalid("ALLOWED_PORTS", s.to_string()))
        })
        .collect()
}

fn parse_cidrs(raw: &str, var: &'static str) -> Result<Vec<IpNet>, ConfigError> {
    raw.split(',')
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(|s| {
            // Bare IPs (no `/prefix`, e.g. a single node IP) are accepted as
            // a /32 or /128 host route.
            if s.contains('/') {
                s.parse::<IpNet>()
                    .map_err(|_| ConfigError::Invalid(var, s.to_string()))
            } else {
                s.parse::<IpAddr>()
                    .map(IpNet::from)
                    .map_err(|_| ConfigError::Invalid(var, s.to_string()))
            }
        })
        .collect()
}

fn parse_list(raw: &str) -> Vec<String> {
    raw.split(',')
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(str::to_string)
        .collect()
}

impl Config {
    /// Loads config from the process environment. See module doc for the
    /// env-var/helm-skeleton correspondence.
    pub fn from_env() -> Result<Self, ConfigError> {
        let listen_port: u16 = env_or("PROXY_LISTEN_PORT", "8443")
            .parse()
            .map_err(|_| ConfigError::Invalid("PROXY_LISTEN_PORT", "not a u16".into()))?;
        let metrics_port: u16 = env_or("METRICS_PORT", "9090")
            .parse()
            .map_err(|_| ConfigError::Invalid("METRICS_PORT", "not a u16".into()))?;
        let allowed_ports = parse_ports(&env_or("ALLOWED_PORTS", "80,443,6697"))?;
        let deny_cidrs = parse_cidrs(&env_or("DENY_CIDRS", ""), "DENY_CIDRS")?;
        let deny_cluster_cidrs =
            parse_cidrs(&env_or("DENY_CLUSTER_CIDRS", ""), "DENY_CLUSTER_CIDRS")?;
        let machine_jwt_jwks_url = std::env::var("MACHINE_JWT_JWKS_URL")
            .map_err(|_| ConfigError::Missing("MACHINE_JWT_JWKS_URL"))?;
        let machine_jwt_audience = std::env::var("MACHINE_JWT_AUDIENCE")
            .map_err(|_| ConfigError::Missing("MACHINE_JWT_AUDIENCE"))?;
        let machine_jwt_trusted_issuers = parse_list(&env_or(
            "MACHINE_JWT_TRUSTED_ISSUERS",
            "spiffe://penguintech.io",
        ));
        let machine_jwt_required_scope = env_or("MACHINE_JWT_REQUIRED_SCOPE", "egress:connect");
        let allowed_caller_services = parse_list(&env_or(
            "ALLOWED_CALLER_SERVICES",
            "svc-ingest,svc-process,svc-action",
        ));
        let per_tenant_max_connections: u32 = env_or("PER_TENANT_MAX_CONNECTIONS", "50")
            .parse()
            .map_err(|_| ConfigError::Invalid("PER_TENANT_MAX_CONNECTIONS", "not a u32".into()))?;
        let per_tenant_bandwidth_bytes_per_sec: u64 =
            env_or("PER_TENANT_BANDWIDTH_BYTES_PER_SEC", "10485760")
                .parse()
                .map_err(|_| {
                    ConfigError::Invalid("PER_TENANT_BANDWIDTH_BYTES_PER_SEC", "not a u64".into())
                })?;
        let connect_timeout_secs: u64 = env_or("CONNECT_TIMEOUT_SECONDS", "10")
            .parse()
            .map_err(|_| ConfigError::Invalid("CONNECT_TIMEOUT_SECONDS", "not a u64".into()))?;
        let assertion_max_ttl_secs: u64 = env_or("ASSERTION_MAX_TTL_SECONDS", "60")
            .parse()
            .map_err(|_| ConfigError::Invalid("ASSERTION_MAX_TTL_SECONDS", "not a u64".into()))?;
        let header_read_timeout_secs: u64 = env_or("HEADER_READ_TIMEOUT_SECONDS", "10")
            .parse()
            .map_err(|_| ConfigError::Invalid("HEADER_READ_TIMEOUT_SECONDS", "not a u64".into()))?;
        let tunnel_idle_timeout_secs: u64 = env_or("TUNNEL_IDLE_TIMEOUT_SECONDS", "300")
            .parse()
            .map_err(|_| ConfigError::Invalid("TUNNEL_IDLE_TIMEOUT_SECONDS", "not a u64".into()))?;
        let tunnel_max_duration_secs: u64 = env_or("TUNNEL_MAX_DURATION_SECONDS", "3600")
            .parse()
            .map_err(|_| {
            ConfigError::Invalid("TUNNEL_MAX_DURATION_SECONDS", "not a u64".into())
        })?;
        let allow_private_ip: bool = env_or("EGRESS_PROXY_ALLOW_PRIVATE_IP", "false")
            .parse()
            .map_err(|_| {
                ConfigError::Invalid("EGRESS_PROXY_ALLOW_PRIVATE_IP", "not a bool".into())
            })?;

        Ok(Self {
            listen_port,
            metrics_port,
            allowed_ports,
            deny_cidrs,
            deny_cluster_cidrs,
            machine_jwt_jwks_url,
            machine_jwt_audience,
            machine_jwt_trusted_issuers,
            machine_jwt_required_scope,
            allowed_caller_services,
            per_tenant_max_connections,
            per_tenant_bandwidth_bytes_per_sec,
            connect_timeout: Duration::from_secs(connect_timeout_secs),
            assertion_max_ttl: Duration::from_secs(assertion_max_ttl_secs),
            header_read_timeout: Duration::from_secs(header_read_timeout_secs),
            tunnel_idle_timeout: Duration::from_secs(tunnel_idle_timeout_secs),
            tunnel_max_duration: Duration::from_secs(tunnel_max_duration_secs),
            allow_private_ip,
        })
    }
}
