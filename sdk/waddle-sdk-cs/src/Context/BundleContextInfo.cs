namespace WaddleSdk.Context;

/// <summary>
/// Idiomatic mirror of `waddle:bundle/context.bundle-context`
/// (`wit/waddle-bundle/stage.wit`) -- the immutable, always-granted per-call
/// scope every stage invocation carries.
/// </summary>
public sealed record BundleContextInfo(
    string Tenant,
    string? Community,
    string AppId,
    string Feature,
    string Version,
    string MessageId,
    string ConfigJson);
