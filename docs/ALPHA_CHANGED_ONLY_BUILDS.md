# Alpha changed-only builds (deploy-dal2alpha)

`.github/workflows/deploy-dal2alpha.yml` builds and deploys only the images whose inputs changed.

```
prepare -> plan -> build (BUILD set, per BuildKit lane) ----\
                \-> retag (REUSE set, by digest) -------------> deploy (helm, per-image tags)
```

## How an image is judged

| Step | Source |
|------|--------|
| What is deployed | `helm get values waddlebot` -> `global.imageTags[<image>]`, else `global.imageTag` |
| When it was built | that image's `org.opencontainers.image.revision` label in ghcr |
| What changed | `git diff <that revision> HEAD`, mapped by `scripts/ci/image-inputs.yaml` |

Anything uncertain **builds** (fail safe): no Helm release, image or label missing, base sha not in the
clone, empty-but-different diff, a changed path that matches no image and no ignore entry, a change
to the map/scripts/deploy workflow, or `force_all=true`.

## Escape hatch

`workflow_dispatch` with `force_all=true` rebuilds and redeploys every image.

## Keeping the map honest

* `make changed-images-audit` fails if any tracked file is neither an image input nor in `ignore`.
* `scripts/ci/tests/test_image_inputs.py` also fails on an unmapped new top-level entry and when a
  Dockerfile `COPY` source is not covered by that image's inputs.
* New image: add it to `image-inputs.yaml` and to the chart (`global.imageTags` key = image name).

## Deploying only what changed

The chart accepts `global.imageTags` (image name -> tag). The deploy writes one entry per image:
rebuilt images get the new `alpha-<epoch>`, reused images keep their previous tag, so their pod template
is unchanged and Helm does not restart them. `make check-alpha-image-pins` proves this by rendering the
chart. Reused images are also retagged to the new alpha tag by digest (digest equality verified).

## BuildKit lanes

Each image has a `lane` (`a` primary, `b` optional second pod). Set the repo variable `BUILDKIT_HOST_B`
(e.g. `tcp://buildkitd-1.buildkit.svc:1234`) once the second pod exists. Unset, or unreachable at plan
time (TCP probe, warning emitted) -> lane `b` builds on lane `a`, identical to the single-endpoint flow.
The remote-driver bootstrap in each job fails loudly if a chosen endpoint has no workers.

## Caching and its bounds

* Registry layer cache: exactly one ref per image, `ghcr.io/penguintechinc/waddles/buildcache/<image>:alpha`,
  `mode=max`, overwritten every build (never per-run tags).
* BuildKit cache mounts (not in image layers): per image, never shared between images --
  `waddles-cargo-registry-<image>-v<epoch>`, `waddles-cargo-git-<image>-v<epoch>`,
  `waddles-target-<image>-v<epoch>` (Rust), `waddles-pip-<image>-...`, `waddles-npm-<image>-...`, all
  `sharing=locked`. They live in the BuildKit pod and are capped by its GC
  policy (`exec.cachemount` kept 6h, 40GB total; admin repo `k8s/dal2alpha/runners/20-buildkit.yaml`).
* Seeder: one `bundle-<name>` stage per Rust bundle over a shared `rust-bundle-base`, so a bundle change
  rebuilds only that bundle.

## Pruning

`scripts/ci/prune_alpha_images.py` (`make prune-alpha-images`) is a DRY RUN unless `--apply`; `--apply`
also needs `--deployed <plan.json>`. It keeps the newest N (default 10) alpha epochs per image plus anything
deployed, never touches untagged versions, and is not scheduled anywhere (a schedule needs owner approval).

## Cache trust boundary and flushing

BuildKit cache mounts (cargo registry/git, per-image `target/`, pip, npm) persist on the shared BuildKit
pod and are trusted by every later build that mounts the same id, so they are only used where poisoning
needs a prerequisite we already control:

* **Who can reach the pod:** only the deploy runner scale set `dal2-alpha` (CiliumNetworkPolicy
  `buildkitd-access`, namespace `github-runners`) and the `buildkitd-prune` cronjob. PR-code runners
  (`dal2-alpha-ci`) cannot, and `deploy-dal2alpha.yml` has no `pull_request*` trigger. `alpha_plan.py plan`
  refuses to run unless the event is `push`/`workflow_dispatch` and the ref is `refs/heads/release/*`
  (`test_cache_trust_boundary.py` also fails if any other workflow reaches the shared pod). One trust level:
  release-branch code. buildkitd listens on plaintext `tcp://0.0.0.0:1234` today (no mTLS): keep
  `buildkitd-access` restricted, and add client-cert mTLS (admin repo hand-off in the PR).
* **What may enter a cache:** only integrity-verified installs. `scripts/ci/check-dockerfile-cache-mounts.py`
  (run by `test_cache_trust_boundary.py`) fails any cache-mounted `pip install` without `--require-hashes`,
  `npm install` (use `npm ci`), or cargo step without `--locked`. Cargo re-verifies every registry crate
  against the checksum in `Cargo.lock`; `npm ci` checks lockfile integrity; pip checks hashes. The unhashed
  `pip install /app/libs/flask_core` step in `hub_api/Dockerfile` is deliberately NOT cache-mounted
  (`--no-cache-dir`). Known gap (pre-existing, not introduced here): that step resolves flask_core's declared
  dependencies without hashes; hash-locking them is a follow-up.
* **`target/` mounts:** the cargo fingerprint cache is not content-addressed, so it is the one cache whose
  correctness rests on the single trust level above. Cost of dropping it: egress-proxy rebuild after a one-line
  change 69s instead of 26s (measured), more for larger crates.

**Flushing every cache:** every cache-mount id carries `-v${CACHE_EPOCH}` (also part of the RUN layer cache
key). Set the repo variable `ALPHA_CACHE_EPOCH` to a new number and dispatch `force_all=true`; the old
mounts are abandoned and garbage-collected. To reclaim immediately:
`buildctl --addr tcp://buildkitd.buildkit.svc:1234 prune --filter type==exec.cachemount` (the existing
`buildkitd-prune` cronjob already prunes hourly with `--keep-duration=24h`).

## Failure mode: corrupted cargo git cache ("revision ... not found")

Deploy run 43637937508 failed `Build svc-action` in 1.1s with `failed to fetch into
/usr/local/cargo/git/db/penguin-libs-...` / `revision <sha> not found`, while the same revision had built
fine before. Cause: the first version of the cache mounts used ONE registry/git cache id shared by all
seven Rust images with `sharing=shared`; the parallel builds fetched the same `penguin-libs` git db at the
same time. Cargo's package-cache lock (`$CARGO_HOME/.package-cache`) lives outside the mounted
`registry/` and `git/` directories, so concurrent builds on one shared mount do not exclude each other and a
partial fetch corrupts the db.

Fix and rules (enforced by `scripts/ci/check-dockerfile-cache-mounts.py`, run in `scripts/ci/tests`):

* every cache id is per image (`...-<image>-v${CACHE_EPOCH}`), no id is used by two Dockerfiles;
* every cache mount is `sharing=locked`; `sharing=shared` and the default are rejected.

Recovery if a cache is ever corrupted: set the repo variable `ALPHA_CACHE_EPOCH` to a new number and dispatch
`deploy-dal2alpha` with `force_all=true` (a fresh empty cache per image is created; the old one is garbage
collected), or flush now with `buildctl ... prune --filter type==exec.cachemount`.
