//! `waddles-ops` entry point. Keys, DB and S3 credentials come from env only; secrets never
//! appear in argv, stdout or logs.

use std::io::Read;
use std::process::ExitCode;

use clap::{Parser, Subcommand};
use waddles_ops_cli::pystr::py_int;
use waddles_ops_cli::signing::{
    parse_public_keys, verify_sidecar, PlatformSigner, PUBLIC_KEYS_ENV,
};
use waddles_ops_cli::suppression_cli::{self, Command as SuppCmd};
use waddles_ops_cli::{audit_cli, sign_backfill, storage};

/// Waddles operator tooling.
#[derive(Parser)]
#[command(name = "waddles-ops", version)]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Sign one (app_id, version, digest, approval_id); key from BUNDLE_SIGNING_PRIVATE_KEY/_KEY_ID.
    /// Prints the signed sidecar JSON to stdout.
    Sign {
        #[arg(long)]
        app_id: String,
        #[arg(long)]
        version: String,
        #[arg(long)]
        digest: String,
        #[arg(long)]
        approval_id: i64,
    },
    /// Verify a signed sidecar (stdin, or --file) against BUNDLE_SIGNING_PUBLIC_KEYS.
    Verify {
        #[arg(long)]
        file: Option<std::path::PathBuf>,
    },
    /// Print the active signer's public key (base64) for provisioning verifiers.
    PublicKey,
    /// Backfill: sign every approved-but-unsigned app_versions row and upload its sidecar.
    SignApprovedBundles,
    /// Verify the tamper-evident audit chains from the database (exit 0/1/2/3).
    VerifyAuditChain {
        /// Verify only this chain id (repeatable).
        #[arg(long)]
        chain: Vec<String>,
        /// Pin a chain head: CHAIN=SEQ:HASH (repeatable).
        #[arg(long)]
        expect: Vec<String>,
        /// Exit 0 when there are no records at all.
        #[arg(long)]
        allow_empty: bool,
    },
    /// Ban-evasion suppression list tooling (lift / check / purge / stats).
    Suppression {
        #[command(subcommand)]
        command: Suppression,
    },
}

#[derive(Subcommand)]
enum Suppression {
    /// Delete one identity's row in one tenant (id from stdin).
    Lift {
        #[arg(long, value_parser = parse_tenant)]
        tenant_id: i128,
        #[arg(long)]
        platform: String,
        /// Read the platform user id from the first line of stdin (never argv).
        #[arg(long)]
        platform_user_id_stdin: bool,
    },
    /// Is there a live row for this identity in this tenant? (id from stdin)
    Check {
        #[arg(long, value_parser = parse_tenant)]
        tenant_id: i128,
        #[arg(long)]
        platform: String,
        #[arg(long)]
        platform_user_id_stdin: bool,
    },
    /// Delete every expired row now.
    Purge,
    /// Live/expired counts per tenant, restriction type and key version.
    Stats,
}

/// Python `int()`-compatible tenant id (sign/underscores/whitespace accepted).
fn parse_tenant(s: &str) -> Result<i128, String> {
    py_int(s).ok_or_else(|| format!("invalid int value: {s:?}"))
}

fn init_tracing() {
    // LOG_LEVEL mirrors hub-api's env; logs go to stderr so stdout stays machine-parseable.
    let level = match std::env::var("LOG_LEVEL") {
        Ok(v) => v,
        Err(std::env::VarError::NotPresent) => "info".to_string(),
        Err(std::env::VarError::NotUnicode(_)) => {
            eprintln!("waddles-ops: LOG_LEVEL is not valid unicode; using \"info\"");
            "info".to_string()
        }
    };
    let filter = match tracing_subscriber::EnvFilter::try_new(&level) {
        Ok(f) => f,
        Err(e) => {
            eprintln!("waddles-ops: invalid LOG_LEVEL {level:?} ({e}); using \"info\"");
            tracing_subscriber::EnvFilter::new("info")
        }
    };
    let _ = tracing_subscriber::fmt()
        .with_env_filter(filter)
        .with_writer(std::io::stderr)
        .try_init();
}

fn run_async<F: std::future::Future<Output = u8>>(fut: F) -> u8 {
    match tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
    {
        Ok(rt) => rt.block_on(fut),
        Err(e) => {
            eprintln!("error: cannot start async runtime: {e}");
            3
        }
    }
}

fn sign_backfill_cmd() -> u8 {
    run_async(async {
        let pool = match waddles_ops_cli::db::connect_from_env().await {
            Ok(p) => p,
            Err(e) => {
                eprintln!("error: sign-approved-bundles could not run: {e}");
                return 3;
            }
        };
        let (store, _bucket) = match storage::bundle_store_from_env() {
            Ok(s) => s,
            Err(e) => {
                eprintln!("error: sign-approved-bundles could not run: {e}");
                return 3;
            }
        };
        match sign_backfill::run(&pool, store.as_ref()).await {
            Ok(summary) => {
                println!(
                    "TOTAL examined={} signed={} failures={}",
                    summary.examined, summary.signed, summary.failures
                );
                summary.exit_code()
            }
            Err(e) => {
                eprintln!("error: sign-approved-bundles could not run: {e}");
                3
            }
        }
    })
}

fn suppression_cmd(command: Suppression) -> u8 {
    let (cmd, needs_stdin, flag) = match command {
        Suppression::Lift {
            tenant_id,
            platform,
            platform_user_id_stdin,
        } => (
            SuppCmd::Lift {
                tenant_id,
                platform,
            },
            true,
            platform_user_id_stdin,
        ),
        Suppression::Check {
            tenant_id,
            platform,
            platform_user_id_stdin,
        } => (
            SuppCmd::Check {
                tenant_id,
                platform,
            },
            true,
            platform_user_id_stdin,
        ),
        Suppression::Purge => (SuppCmd::Purge, false, false),
        Suppression::Stats => (SuppCmd::Stats, false, false),
    };
    let mut platform_user_id = String::new();
    if needs_stdin {
        if !flag {
            eprintln!("error: this command needs --platform-user-id-stdin (ids never go in argv)");
            return suppression_cli::EXIT_BAD_ARGS;
        }
        match suppression_cli::read_platform_user_id(std::io::stdin().lock()) {
            Ok(id) => platform_user_id = id,
            Err(_) => {
                eprintln!("error: stdin is not valid UTF-8 text");
                return suppression_cli::EXIT_BAD_ARGS;
            }
        }
    }
    run_async(suppression_cli::run(cmd, platform_user_id))
}

fn run(cli: Cli) -> Result<u8, String> {
    match cli.command {
        Command::Sign {
            app_id,
            version,
            digest,
            approval_id,
        } => {
            let signer = PlatformSigner::from_env().map_err(|e| e.to_string())?;
            let doc = signer
                .sign(&app_id, &version, &digest, approval_id)
                .map_err(|e| e.to_string())?;
            println!("{}", doc.to_canonical_json().map_err(|e| e.to_string())?);
            Ok(0)
        }
        Command::Verify { file } => {
            let mut buf = Vec::new();
            match file {
                Some(p) => {
                    buf = std::fs::read(&p).map_err(|e| format!("read {}: {e}", p.display()))?
                }
                None => {
                    std::io::stdin()
                        .read_to_end(&mut buf)
                        .map_err(|e| format!("read stdin: {e}"))?;
                }
            }
            let raw = std::env::var(PUBLIC_KEYS_ENV)
                .map_err(|_| format!("{PUBLIC_KEYS_ENV} must be set"))?;
            let keys = parse_public_keys(&raw).map_err(|e| e.to_string())?;
            if keys.is_empty() {
                return Err(format!(
                    "{PUBLIC_KEYS_ENV} has no keys; zero keys fails closed"
                ));
            }
            let sc = verify_sidecar(&buf, &keys).map_err(|e| e.to_string())?;
            println!(
                "verified app_id={} version={} key_id={} approval_id={}",
                sc.app_id, sc.version, sc.key_id, sc.approval_id
            );
            Ok(0)
        }
        Command::PublicKey => {
            let signer = PlatformSigner::from_env().map_err(|e| e.to_string())?;
            println!("{}", signer.public_key_b64());
            Ok(0)
        }
        Command::SignApprovedBundles => Ok(sign_backfill_cmd()),
        Command::VerifyAuditChain {
            chain,
            expect,
            allow_empty,
        } => Ok(run_async(audit_cli::run(chain, expect, allow_empty))),
        Command::Suppression { command } => Ok(suppression_cmd(command)),
    }
}

fn main() -> ExitCode {
    init_tracing();
    match run(Cli::parse()) {
        Ok(code) => ExitCode::from(code),
        Err(e) => {
            eprintln!("error: {e}");
            ExitCode::from(3)
        }
    }
}
