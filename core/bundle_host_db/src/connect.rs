//! Production Postgres connection factory for the bundle `db` capability --
//! connects as the least-privilege, read-write `waddles_bundle_runtime`
//! role (`alembic/versions/0030_bundle_app_schemas.py` provisions the role
//! and its `app_core`/`app_community` grants; this crate is DML-only and
//! never issues DDL, see `crate::lib`'s module doc).
//!
//! Mirrors `core/bundle_active_set::reader`'s `ReaderConfig`/`connect`
//! shape byte-for-byte (own small config struct, password passed
//! separately to [`connect`] rather than carried on the struct -- Token &
//! Secret Hygiene), with one deliberate difference: that crate's factory is
//! **read-only** (`options[default_transaction_read_only]=on`, a
//! SELECT-only loader); this one is **read-write**, since `insert`/
//! `update`/`delete` are core `db` capability operations, not just reads.
//!
//! Each of `core/svc_process`/`core/svc_action`'s own `lib.rs` startup
//! wiring calls [`connect`] once at process start (never per-invoke) and
//! wraps the resulting [`sea_orm::DatabaseConnection`] in
//! [`crate::backend::PostgresBackend`] -- see those crates' own
//! `BUNDLE_DB_*` env var docs for the exact Helm-values knobs that make
//! this connection, and therefore the `storage.tables` capability itself,
//! actually usable in a given deployment.

use std::time::Duration;

use sea_orm::{ConnectOptions, Database, DatabaseConnection, DbErr, RuntimeErr};

/// Non-secret connection settings for the bundle-runtime endpoint --
/// `BUNDLE_DB_*` in each service's own `CliConfig`. The password is never
/// carried on this struct; callers pass it directly to [`connect`].
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ConnectConfig {
    pub host: String,
    pub port: u16,
    pub name: String,
    /// Expected to be `waddles_bundle_runtime` in every real deployment
    /// (the role `0030_bundle_app_schemas.py` provisions) -- kept
    /// configurable rather than hardcoded only so a test/alpha deployment
    /// can point this at a differently-named role without a code change.
    pub user: String,
}

/// Percent-encodes `raw` for the userinfo part of a URL (RFC 3986): only the
/// unreserved set (`A-Za-z0-9-._~`) passes through. Without this, a generated
/// password containing `/`, `?`, `#` or `%` either makes the whole
/// connection string unparsable or is silently decoded into a different
/// password -- and a role name is encoded for the same reason.
fn encode_userinfo(raw: &str) -> String {
    const HEX: &[u8; 16] = b"0123456789ABCDEF";
    let mut out = String::with_capacity(raw.len());
    for b in raw.bytes() {
        match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'.' | b'_' | b'~' => {
                out.push(char::from(b));
            }
            _ => {
                out.push('%');
                // A nibble is always < 16, so both lookups are in bounds.
                out.push(char::from(HEX[usize::from(b >> 4)]));
                out.push(char::from(HEX[usize::from(b & 0x0f)]));
            }
        }
    }
    out
}

fn connection_url(cfg: &ConnectConfig, password: &str) -> String {
    format!(
        "postgres://{user}:{password}@{host}:{port}/{name}",
        user = encode_userinfo(&cfg.user),
        password = encode_userinfo(password),
        host = cfg.host,
        port = cfg.port,
        name = cfg.name,
    )
}

/// Masks every occurrence of `password` (raw and URL-encoded form) in a
/// connection error. SeaORM/sqlx embed the *whole* connection string --
/// password included -- in the "cannot be parsed" error, and callers log
/// whatever [`connect`] returns at startup, so the password must never
/// survive into the returned [`DbErr`] (Token & Secret Hygiene). Errors that
/// don't carry the password are returned untouched so callers can still
/// match on their variant (e.g. a pool timeout).
fn redact_password(err: DbErr, password: &str) -> DbErr {
    if password.is_empty() {
        return err;
    }
    let message = err.to_string();
    let encoded = encode_userinfo(password);
    if !message.contains(password) && !message.contains(&encoded) {
        return err;
    }
    // Replace the encoded form first: it may itself contain the raw form.
    let masked = message.replace(&encoded, "****").replace(password, "****");
    DbErr::Conn(RuntimeErr::Internal(masked))
}

/// Opens a pooled, read-write connection under `cfg.user` (production:
/// `waddles_bundle_runtime`). `sqlx_logging(false)` -- never echo the
/// password-bearing URL at DEBUG (same rationale as
/// `core/bundle_active_set::reader::connect`). Pool sized for a
/// shared-per-process connection serving every concurrent bundle `db`
/// invocation on this instance (unlike that crate's 4-connection reader
/// pool, which serves at most one query per poll tick).
pub async fn connect(cfg: &ConnectConfig, password: &str) -> Result<DatabaseConnection, DbErr> {
    connect_with_acquire_timeout(cfg, password, ACQUIRE_TIMEOUT).await
}

/// How long the pool keeps retrying the initial connection before giving up.
/// This is sqlx's own default made explicit (so it is pinned here rather than
/// inherited); [`connect_with_acquire_timeout`] exists so tests can shorten it.
const ACQUIRE_TIMEOUT: Duration = Duration::from_secs(30);

/// [`connect`]'s body with the pool acquire timeout injectable, so the
/// network-failure path is testable in milliseconds instead of 30 seconds.
async fn connect_with_acquire_timeout(
    cfg: &ConnectConfig,
    password: &str,
    acquire_timeout: Duration,
) -> Result<DatabaseConnection, DbErr> {
    let mut opts = ConnectOptions::new(connection_url(cfg, password));
    opts.max_connections(10)
        .min_connections(1)
        .acquire_timeout(acquire_timeout)
        .sqlx_logging(false);
    Database::connect(opts)
        .await
        .map_err(|err| redact_password(err, password))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample() -> ConnectConfig {
        ConnectConfig {
            host: "waddles-pg.internal".to_string(),
            port: 5432,
            name: "waddlebot".to_string(),
            user: "waddles_bundle_runtime".to_string(),
        }
    }

    #[test]
    fn connection_url_includes_configured_host_and_role() {
        let url = connection_url(&sample(), "s3cret");
        assert_eq!(
            url,
            "postgres://waddles_bundle_runtime:s3cret@waddles-pg.internal:5432/waddlebot"
        );
    }

    /// Unlike `bundle_active_set::reader`'s factory, this one must NOT
    /// carry the read-only startup option -- `insert`/`update`/`delete`
    /// need a genuinely read-write session.
    #[test]
    fn connection_url_never_carries_the_read_only_startup_option() {
        let url = connection_url(&sample(), "s3cret");
        assert!(
            !url.contains("default_transaction_read_only"),
            "bundle db capability needs read-write connections"
        );
    }

    #[test]
    fn connection_url_is_a_well_formed_postgres_url() {
        let parsed = url::Url::parse(&connection_url(&sample(), "s3cret"))
            .expect("connection_url must always produce a valid URL");
        assert_eq!(parsed.scheme(), "postgres");
        assert_eq!(parsed.host_str(), Some("waddles-pg.internal"));
        assert_eq!(parsed.username(), "waddles_bundle_runtime");
    }

    /// Decodes a percent-encoded URL component the same way sqlx does when
    /// it reads the userinfo back out of the connection string.
    fn percent_decode(encoded: &str) -> String {
        url::form_urlencoded::parse(format!("k={encoded}").as_bytes())
            .next()
            .map(|(_, v)| v.into_owned())
            .unwrap_or_default()
    }

    #[test]
    fn encode_userinfo_passes_the_unreserved_set_through() {
        assert_eq!(encode_userinfo("aZ09-._~"), "aZ09-._~");
        assert_eq!(encode_userinfo(""), "");
    }

    #[test]
    fn encode_userinfo_escapes_every_reserved_and_non_ascii_byte() {
        assert_eq!(encode_userinfo("@/:?#%"), "%40%2F%3A%3F%23%25");
        assert_eq!(encode_userinfo("a b+c"), "a%20b%2Bc");
        // Multi-byte UTF-8 is escaped byte by byte.
        assert_eq!(encode_userinfo("\u{e9}"), "%C3%A9");
    }

    /// regression: a generated password containing a URL delimiter used to be
    /// spliced into the connection string raw -- `/`, `?`, `#` made the URL
    /// unparsable and `%41` silently decoded to a *different* password.
    #[test]
    fn connection_url_round_trips_a_password_full_of_url_delimiters() {
        let password = "p/ss?w#rd@:%41 +\u{e9}";
        let url = connection_url(&sample(), password);
        let parsed = url::Url::parse(&url).expect("encoded URL must parse");

        assert_eq!(parsed.host_str(), Some("waddles-pg.internal"));
        assert_eq!(parsed.port(), Some(5432));
        assert_eq!(parsed.path(), "/waddlebot");
        assert_eq!(
            parsed.query(),
            None,
            "a '?' in the password must not open a query"
        );
        assert_eq!(
            parsed.fragment(),
            None,
            "a '#' in the password must not open a fragment"
        );
        assert_eq!(
            percent_decode(parsed.password().unwrap_or_default()),
            password
        );
    }

    #[test]
    fn connection_url_encodes_the_role_name_too() {
        let mut cfg = sample();
        cfg.user = "role with/odd:chars".to_string();
        let parsed = url::Url::parse(&connection_url(&cfg, "pw")).expect("encoded URL must parse");
        assert_eq!(parsed.host_str(), Some("waddles-pg.internal"));
        assert_eq!(percent_decode(parsed.username()), "role with/odd:chars");
    }

    #[test]
    fn connect_config_is_cloneable_comparable_and_debuggable() {
        let cfg = sample();
        assert_eq!(cfg.clone(), cfg);
        let mut other = sample();
        other.port = 5433;
        assert_ne!(cfg, other);
        let debug = format!("{cfg:?}");
        assert!(debug.contains("waddles-pg.internal"));
        assert!(debug.contains("waddles_bundle_runtime"));
    }

    #[test]
    fn redact_password_masks_the_raw_and_the_encoded_form() {
        let err = DbErr::Conn(RuntimeErr::Internal(
            "cannot parse 'postgres://u:p/ss@h:1/db' (raw p/ss)".to_string(),
        ));
        let masked = redact_password(err, "p/ss").to_string();
        assert!(!masked.contains("p/ss"), "raw password leaked: {masked}");
        assert!(
            !masked.contains("p%2Fss"),
            "encoded password leaked: {masked}"
        );
        assert!(masked.contains("****"));

        let encoded_only = DbErr::Conn(RuntimeErr::Internal("url u:p%2Fss@h".to_string()));
        let masked = redact_password(encoded_only, "p/ss").to_string();
        assert!(
            !masked.contains("p%2Fss"),
            "encoded password leaked: {masked}"
        );
    }

    #[test]
    fn redact_password_leaves_unrelated_errors_and_empty_passwords_alone() {
        let untouched = redact_password(DbErr::Custom("pool timed out".to_string()), "s3cret");
        assert_eq!(untouched, DbErr::Custom("pool timed out".to_string()));

        // An empty password must not turn every character boundary into "****".
        let err = DbErr::Custom("anything".to_string());
        assert_eq!(redact_password(err.clone(), ""), err);
    }

    /// regression: sqlx echoes the whole connection string -- password and
    /// all -- in its "cannot be parsed" error, and callers log whatever
    /// `connect` returns at startup.
    #[tokio::test]
    async fn connect_never_leaks_the_password_when_the_url_cannot_be_parsed() {
        let mut cfg = sample();
        cfg.host = "bad host".to_string();
        let err = connect(&cfg, "hunter2-s3cret")
            .await
            .expect_err("a host with a space is not a valid connection string");
        let text = format!("{err} / {err:?}");
        assert!(!text.contains("hunter2-s3cret"), "password leaked: {text}");
        assert!(!text.contains("hunter2"), "password leaked: {text}");
        assert!(matches!(err, DbErr::Conn(_)), "got {err:?}");
    }

    #[tokio::test]
    async fn connect_rejects_an_empty_host_without_leaking_the_password() {
        let mut cfg = sample();
        cfg.host = String::new();
        let err = connect(&cfg, "hunter2-s3cret")
            .await
            .expect_err("empty host");
        assert!(!format!("{err} / {err:?}").contains("hunter2"));
    }

    /// A password full of URL delimiters must reach the driver as a
    /// well-formed URL (it used to fail parsing outright): the only failure
    /// left against a closed port is the pool giving up, never a parse
    /// error. 127.0.0.1:1 refuses every connection, so no database is
    /// needed; the shortened acquire timeout keeps the test fast.
    #[tokio::test]
    async fn connect_with_a_delimiter_heavy_password_fails_only_at_the_network() {
        let mut cfg = sample();
        cfg.host = "127.0.0.1".to_string();
        cfg.port = 1;
        let err = connect_with_acquire_timeout(&cfg, "p/ss?w#rd@:%41", Duration::from_millis(300))
            .await
            .expect_err("nothing listens on 127.0.0.1:1");
        assert!(
            matches!(err, DbErr::Conn(RuntimeErr::SqlxError(_))),
            "expected a driver/pool error (URL parsed fine), got {err:?}"
        );
        let text = format!("{err} / {err:?}");
        assert!(
            !text.contains("p/ss") && !text.contains("p%2Fss"),
            "leak: {text}"
        );
    }
}
