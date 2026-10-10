# Bundle `supported_platforms`

A bundle manifest (`bundle.yaml` schema v2) can declare which platforms it supports. Waddles then refuses to bind the bundle to a source stream on any other platform. Introduced for issue #685.

## Declare it

Add a top-level list to the manifest:

```yaml
supported_platforms:
  - discord
```

| Rule | Detail |
|---|---|
| Optional | Absent means the bundle supports every platform. Existing bundles are unaffected. |
| Allowed values | `discord`, `twitch`, `slack`, `youtube`, `kick`, or `custom:name` for a custom platform registered to the tenant. |
| Non-empty | An empty list is rejected. |
| Duplicates | Ignored. |
| Must cover `consumes` | Every platform in a `consumes` rule must appear in `supported_platforms`. |

## What Waddles does with it

| When | Behavior |
|---|---|
| Manifest parse | Validates the list. Failures carry the reason `invalid_supported_platforms` (bad type, empty, unknown value, or unregistered `custom:` name) or `consumes_platform_unsupported` (a `consumes` rule names a platform not in the list). |
| Bundle approval | Source bindings are created only for consumed platforms the bundle supports. A skipped platform gets no source stream grant, so the bundle receives no events from it. Each skip logs a warning with app id, tenant, community and platform, and no user data. |
| Storage | The field is stored with the manifest. No database migration was needed. |

## Check it worked

1. Submit a manifest that lists `supported_platforms: [discord]` and a `consumes` rule for `twitch`. Expect `consumes_platform_unsupported`.
2. Remove the `twitch` consume rule, approve the bundle, and confirm bindings exist for Discord only.

## Limits

- The check runs when bindings are created. A runtime check in the process service is a planned follow-up and does not exist yet.
- The first-party command bundles in [Command Bundles Reference](command-bundles.md) do not declare the field; they consume Twitch and Discord only because of their `consumes` rules.

## Source

`hub_api/services/bundle_manifest_v2.py` (parse and validate), `hub_api/services/app_source_binding_service.py` (binding gate).
