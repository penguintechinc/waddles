//! C# build recipe: a pinned, containerized `dotnet build -c Release`
//! against `componentize-dotnet`'s NativeAOT-LLVM toolchain (spec SS9.3),
//! mirroring `bundles/csharp/csping/Dockerfile`'s exact toolchain pins
//! (same base image digest, same pinned+sha256-verified WASI SDK, same
//! non-root build user).
//!
//! **Unlike `rust.rs`/`python.rs`/`js.rs` (still stubs), this recipe is
//! wired.** It shells out to `docker build`/`docker run` against a
//! generalized copy of that Dockerfile (`assets/csharp/Dockerfile`; see
//! its own header comment for exactly what was generalized and why),
//! rather than a locally-installed toolchain binary the way every other
//! `LanguageBuilder`'s doc comment describes ("shells out to its own
//! pinned toolchain binary, never a library call"): the .NET 10 SDK +
//! NativeAOT-LLVM + pinned WASI SDK this recipe needs has no minimal
//! standalone install the way `rustc`/`cargo-component` or
//! `python`/`componentize-py` do, so baking the whole toolchain into a
//! throwaway image per build -- the same approach
//! `scripts/verify-csping-fixture.sh` already exercises for this exact
//! bundle -- is what lets this crate (and this repo's CI/dev machines)
//! never need a host-installed .NET SDK (`rules/client.md` Build &
//! Distribution: "All client builds must use containerized toolchain").
//!
//! **Known production gap (tracked, not fixed by this wiring pass):** the
//! untrusted `build` Kubernetes container this crate's `run_build` runs
//! inside needs Docker (or a rootless equivalent -- Kaniko, sysbox)
//! available to actually invoke `docker build`/`docker run` from inside
//! it. Baking the .NET/NativeAOT-LLVM/WASI-SDK toolchain directly into
//! that container's own image (matching where the other three languages'
//! toolchains are headed) is the real production target.
//!
//! **Former upstream gap, now closed:** the pinned `penguin-bundle-host`
//! crate previously did not list `"csharp"` in its manifest-schema
//! `KNOWN_LANGUAGES` (V17), so a real `bundle.yaml` declaring
//! `language: csharp` could not pass `crate::manifest::parse_and_validate`.
//! `penguin-libs` PR #128 added `"csharp"` to `KNOWN_LANGUAGES` on
//! `release/rust-bundle-host/v0.1.x`; this crate now pins that commit, and
//! this module's own tests parse the real `bundles/csharp/csping/bundle.yaml`
//! through `parse_and_validate` rather than constructing a
//! [`BundleManifest`] by hand.

use super::LanguageBuilder;
use crate::errors::CompilerError;
use crate::manifest::BundleManifest;
use std::path::{Path, PathBuf};
use std::process::Command;
use walkdir::WalkDir;

/// Vendored copy of the normative WIT world every bundle (any language)
/// compiles against -- `wit/waddle-bundle/stage.wit`'s own header comment
/// says "every consumer ... references this exact file ... and never
/// inlines a copy." Embedded at compile time (rather than read from a
/// relative path at runtime) so this builder does not depend on the
/// `bundle-compiler` binary's working directory inside its own container
/// image -- the same reason `core/bundle_executor/src/engine.rs`'s
/// `bindgen!` macro reads it via a `path` relative to `CARGO_MANIFEST_DIR`
/// rather than an assumed runtime cwd.
const STAGE_WIT: &str = include_str!(concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../../wit/waddle-bundle/stage.wit"
));

/// Generalized copy of `bundles/csharp/csping/Dockerfile` -- see
/// `assets/csharp/Dockerfile`'s own header comment for exactly what was
/// generalized.
const DOCKERFILE: &str = include_str!("../../assets/csharp/Dockerfile");

/// Canonical `nuget.config` every C# bundle build uses, regardless of
/// what (if anything) the bundle source itself provides -- see
/// `assets/csharp/nuget.config`'s own header comment.
const NUGET_CONFIG: &str = include_str!("../../assets/csharp/nuget.config");

/// C# [`LanguageBuilder`]: compiles a `componentize-dotnet` C# bundle to a
/// WASI-p2 component via a pinned, containerized `dotnet build`. See this
/// module's doc comment for the full recipe and its known gaps.
pub struct CSharpBuilder;

impl LanguageBuilder for CSharpBuilder {
    fn build(
        &self,
        source_dir: &Path,
        _manifest: &BundleManifest,
        out_dir: &Path,
    ) -> Result<PathBuf, CompilerError> {
        let ctx = tempfile::tempdir()?;
        let ctx_path = ctx.path();

        // Recreate the exact repo-root-relative layout every bundle's own
        // `.csproj` already assumes (`csping.csproj`'s
        // `<Wit Include="../../../wit/waddle-bundle/stage.wit" .../>`,
        // the same convention `bundles/rust/ping/Cargo.toml`'s
        // `path = "../../../wit/waddle-bundle"` uses) so a bundle's own
        // project file resolves its WIT reference unmodified -- this
        // builder never generates or rewrites the bundle's `.csproj`.
        let wit_dir = ctx_path.join("wit/waddle-bundle");
        std::fs::create_dir_all(&wit_dir)?;
        std::fs::write(wit_dir.join("stage.wit"), STAGE_WIT)?;

        let bundle_dir = ctx_path.join("bundles/csharp/bundle");
        std::fs::create_dir_all(&bundle_dir)?;
        copy_dir_recursive(source_dir, &bundle_dir)?;
        // Enforced unconditionally -- see `NUGET_CONFIG`'s doc comment.
        std::fs::write(bundle_dir.join("nuget.config"), NUGET_CONFIG)?;

        let dockerfile_path = ctx_path.join("Dockerfile");
        std::fs::write(&dockerfile_path, DOCKERFILE)?;

        let tag = format!("waddles/bundle-csharp-build:{}", std::process::id());
        run_docker(
            Command::new("docker")
                .arg("build")
                .arg("-f")
                .arg(&dockerfile_path)
                .arg("-t")
                .arg(&tag)
                .arg(ctx_path),
            "docker build",
        )?;

        std::fs::create_dir_all(out_dir)?;
        let out_dir_abs = out_dir.canonicalize()?;
        let uid_gid = current_uid_gid()?;
        let mount = format!("{}:/out", out_dir_abs.display());
        let run_result = run_docker(
            Command::new("docker")
                .arg("run")
                .arg("--rm")
                .arg("--user")
                .arg(&uid_gid)
                .arg("-v")
                .arg(&mount)
                .arg(&tag),
            "docker run",
        );
        // Best-effort image cleanup regardless of whether the run
        // succeeded -- never masks `run_result`'s own error.
        let _ = Command::new("docker").arg("rmi").arg(&tag).status();
        run_result?;

        let component_path = out_dir.join("component.wasm");
        if !component_path.exists() {
            return Err(CompilerError::CompileFailed {
                language: "csharp".to_string(),
                message: "docker run completed but produced no component.wasm".to_string(),
            });
        }
        validate_component(&component_path)?;
        Ok(component_path)
    }
}

/// Runs `cmd`, mapping a spawn failure or non-zero exit to
/// `CompilerError::CompileFailed` tagged with `label` (`"docker build"` /
/// `"docker run"`) for a readable failure message. Inherits stdio so the
/// (potentially multi-minute, NuGet-restore-heavy) build's own output is
/// visible rather than silently buffered.
fn run_docker(cmd: &mut Command, label: &str) -> Result<(), CompilerError> {
    let status = cmd.status().map_err(|e| CompilerError::CompileFailed {
        language: "csharp".to_string(),
        message: format!("{label}: docker not runnable: {e}"),
    })?;
    if !status.success() {
        return Err(CompilerError::CompileFailed {
            language: "csharp".to_string(),
            message: format!("{label} exited with {status}"),
        });
    }
    Ok(())
}

/// Recursively copies every file under `src` into `dst`, preserving
/// relative structure. Used to place the untrusted bundle's own source
/// tree into the throwaway Docker build context at the fixed
/// `bundles/csharp/bundle` path `assets/csharp/Dockerfile` expects.
fn copy_dir_recursive(src: &Path, dst: &Path) -> Result<(), CompilerError> {
    for entry in WalkDir::new(src) {
        let entry = entry.map_err(std::io::Error::other)?;
        let rel = entry.path().strip_prefix(src).map_err(|e| {
            CompilerError::Config(format!(
                "{} is not under {}: {e}",
                entry.path().display(),
                src.display()
            ))
        })?;
        if rel.as_os_str().is_empty() {
            continue;
        }
        let target = dst.join(rel);
        if entry.file_type().is_dir() {
            std::fs::create_dir_all(&target)?;
        } else if entry.file_type().is_file() {
            if let Some(parent) = target.parent() {
                std::fs::create_dir_all(parent)?;
            }
            std::fs::copy(entry.path(), &target)?;
        }
    }
    Ok(())
}

/// The current process's `uid:gid`, shelled out via `id -u`/`id -g` rather
/// than an `unsafe` `libc::getuid()` call (this crate's `[lints.rust]
/// unsafe_code = "deny"`) -- passed to `docker run --user` so the
/// container writes `component.wasm` into the bind-mounted `out_dir` as
/// the caller, not the image's baked-in `builder` uid (10001); whichever
/// one doesn't match `out_dir`'s ownership fails the final `cp` with a
/// permission error, exactly as `bundles/csharp/csping/Dockerfile`'s own
/// header comment documents for the manual build path this mirrors.
fn current_uid_gid() -> Result<String, CompilerError> {
    Ok(format!("{}:{}", run_id_arg("-u")?, run_id_arg("-g")?))
}

/// Runs `id <arg>` and returns its trimmed stdout, or `CompileFailed` if
/// `id` cannot be spawned or exits non-zero.
fn run_id_arg(arg: &str) -> Result<String, CompilerError> {
    let out = Command::new("id")
        .arg(arg)
        .output()
        .map_err(|e| CompilerError::CompileFailed {
            language: "csharp".to_string(),
            message: format!("id {arg}: not runnable: {e}"),
        })?;
    if !out.status.success() {
        return Err(CompilerError::CompileFailed {
            language: "csharp".to_string(),
            message: format!("id {arg}: exited with {}", out.status),
        });
    }
    Ok(String::from_utf8_lossy(&out.stdout).trim().to_string())
}

/// Runs the same `wasm-tools validate --features component-model` check
/// `bundles/csharp/csping/README.md`'s "Validate" section documents doing
/// by hand -- README "What a CSharpBuilder would need" item 6: "run the
/// same ... check this spike ran by hand, as part of `run_build`'s
/// post-build verification." Fails closed (`CompileFailed`) if
/// `wasm-tools` cannot be spawned at all, matching every scanner in
/// `crate::scan::sast` treating a missing tool as a hard failure rather
/// than a silent skip.
fn validate_component(path: &Path) -> Result<(), CompilerError> {
    let path_str = path.to_string_lossy().into_owned();
    let out = Command::new("wasm-tools")
        .args(["validate", "--features", "component-model", &path_str])
        .output()
        .map_err(|e| CompilerError::CompileFailed {
            language: "csharp".to_string(),
            message: format!("wasm-tools not runnable: {e}"),
        })?;
    if !out.status.success() {
        return Err(CompilerError::CompileFailed {
            language: "csharp".to_string(),
            message: format!(
                "wasm-tools validate failed: {}",
                String::from_utf8_lossy(&out.stderr)
            ),
        });
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]
    use super::*;

    /// End-to-end: builds the real `bundles/csharp/csping` spike bundle
    /// through `CSharpBuilder::build` -- the "new arm" `builder_for`
    /// (`build/mod.rs`) now returns for `"csharp"` -- via the same pinned,
    /// rootless, checksum-verified Dockerfile approach
    /// `scripts/verify-csping-fixture.sh` already exercises, then asserts
    /// the produced component passes `wasm-tools validate` (`build`
    /// itself already runs that check before returning `Ok`, so a
    /// successful, non-empty result here is already proof it validated).
    ///
    /// The manifest comes from `crate::manifest::parse_and_validate`
    /// against the bundle's own real `bundle.yaml` -- not a hand-built
    /// [`BundleManifest`] -- now that `penguin-bundle-host`
    /// (`penguin-libs` PR #128) lists `"csharp"` in `KNOWN_LANGUAGES`.
    ///
    /// Ignored: needs `docker` + network (NuGet restore, ~1-2 minutes) --
    /// not run in the default `cargo test` / CI unit-test pass. Runnable
    /// via `make test-csharp-bundle-compile`.
    #[test]
    #[ignore = "needs docker + network; run via `make test-csharp-bundle-compile`"]
    fn builds_csping_via_docker() {
        let source_dir = Path::new(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/../../bundles/csharp/csping"
        ));
        let manifest = crate::manifest::parse_and_validate(
            &source_dir.join("bundle.yaml"),
            &crate::manifest::ManifestOptions::default(),
        )
        .expect("real csping bundle.yaml should validate now that csharp is a known language");
        let out_dir = tempfile::tempdir().expect("tempdir");
        let wasm_path = CSharpBuilder
            .build(source_dir, &manifest, out_dir.path())
            .expect("CSharpBuilder::build against the real csping bundle");
        assert_eq!(wasm_path, out_dir.path().join("component.wasm"));
        let metadata = std::fs::metadata(&wasm_path).expect("metadata");
        assert!(metadata.len() > 0, "produced component.wasm is empty");
    }
}
