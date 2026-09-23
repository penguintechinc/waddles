//! `bundle-compiler` -- CLI entry point. `build` runs in the untrusted
//! `build` initContainer; `publish` runs in the trusted `publisher`
//! container. Never the same process, never the same binary invocation
//! (M2a plan Artifact & Digest Contract, spec SS4.6).

use bundle_compiler::build::run_build;
use bundle_compiler::errors::CompilerError;
use clap::{Parser, Subcommand};
use std::path::PathBuf;
use std::process::ExitCode;

/// `bundle-compiler` command-line interface.
#[derive(Parser)]
#[command(name = "bundle-compiler", version)]
struct Cli {
    /// Which half of the Job this invocation runs.
    #[command(subcommand)]
    command: Command,
}

/// The two subcommands, one per container of the bundle-build Job.
#[derive(Subcommand)]
enum Command {
    /// UNTRUSTED -- runs bundle-supplied code. Validates the manifest,
    /// scans the source, compiles it. Never touches the bucket, the DB,
    /// or hub-api; must never be given those credentials.
    Build {
        /// Path to the bundle source directory (or a prebuilt component
        /// file, when the manifest declares `artifact: prebuilt`).
        #[arg(long)]
        bundle: PathBuf,
        /// Path to the `bundle.yaml` v2 manifest.
        #[arg(long)]
        manifest: PathBuf,
        /// Output directory for `component.wasm` and `manifest.json`.
        #[arg(long)]
        out: PathBuf,
    },
    /// TRUSTED -- never executes bundle code. Re-validates the component
    /// `build` produced (or a directly-uploaded Tier 2 prebuilt one),
    /// computes both digests, signs, uploads, writes `app_versions`,
    /// notifies hub-api.
    Publish {
        /// Path to the candidate component produced by `build`.
        #[arg(long)]
        component: PathBuf,
        /// Path to the validated `manifest.json` `build` wrote.
        #[arg(long)]
        manifest: PathBuf,
        /// The manifest's declared language.
        #[arg(long)]
        language: String,
        /// `source` or `prebuilt`.
        #[arg(long, value_parser = ["source", "prebuilt"])]
        artifact_kind: String,
    },
}

fn main() -> ExitCode {
    bundle_compiler::logging::init_logging();
    let cli = Cli::parse();
    let result = match cli.command {
        Command::Build {
            bundle,
            manifest,
            out,
        } => run_build(&bundle, &manifest, &out),
        Command::Publish {
            component,
            manifest,
            language,
            artifact_kind,
        } => bundle_compiler::run_publish(&component, &manifest, &language, &artifact_kind),
    };
    match result {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            let exit_code = e.exit_code();
            report_failure(&e, exit_code);
            ExitCode::from(exit_code as u8)
        }
    }
}

/// Logs a top-level failure before the process exits with its mapped
/// code -- split out so `main` stays a thin dispatcher.
fn report_failure(e: &CompilerError, exit_code: i32) {
    tracing::error!(error = %e, exit_code, "bundle-compiler failed");
}
