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
* BuildKit cache mounts (not in image layers): cargo registry/git (shared), one cargo `target/` per Rust
  service (`waddles-target-<image>`), pip, npm. They live in the BuildKit pod and are capped by its GC
  policy (`exec.cachemount` kept 6h, 40GB total; admin repo `k8s/dal2alpha/runners/20-buildkit.yaml`).
* Seeder: one `bundle-<name>` stage per Rust bundle over a shared `rust-bundle-base`, so a bundle change
  rebuilds only that bundle.

## Pruning

`scripts/ci/prune_alpha_images.py` (`make prune-alpha-images`) is a DRY RUN unless `--apply`; `--apply`
also needs `--deployed <plan.json>`. It keeps the newest N (default 10) alpha epochs per image plus anything
deployed, never touches untagged versions, and is not scheduled anywhere (a schedule needs owner approval).
