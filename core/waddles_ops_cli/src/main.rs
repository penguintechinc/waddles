//! `waddles-ops` entry point. Keys come from env only; secrets never appear in argv.

use std::io::Read;
use std::process::ExitCode;

use clap::{Parser, Subcommand};
use waddles_ops_cli::signing::{
    parse_public_keys, verify_sidecar, PlatformSigner, PUBLIC_KEYS_ENV,
};

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
}

fn run(cli: Cli) -> Result<(), String> {
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
            Ok(())
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
            Ok(())
        }
        Command::PublicKey => {
            let signer = PlatformSigner::from_env().map_err(|e| e.to_string())?;
            println!("{}", signer.public_key_b64());
            Ok(())
        }
    }
}

fn main() -> ExitCode {
    match run(Cli::parse()) {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("error: {e}");
            ExitCode::from(3)
        }
    }
}
