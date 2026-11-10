using System.Text.Json.Serialization;

namespace WaddleBundleSuperpenguinChannelpoints;

/// <summary>The `reward` object of a Twitch EventSub channel-point redemption (only the fields this bundle reads).</summary>
public sealed record RewardInfo(
    [property: JsonPropertyName("id")] string? Id,
    [property: JsonPropertyName("title")] string? Title,
    [property: JsonPropertyName("cost")] long Cost);

/// <summary>
/// A Twitch EventSub `channel.channel_points_custom_reward_redemption.add` event body as the host forwards it in
/// `payload_json`. Only the fields the policy needs are modelled; everything else is ignored.
/// </summary>
public sealed record RedemptionEvent(
    [property: JsonPropertyName("id")] string? Id,
    [property: JsonPropertyName("status")] string? Status,
    [property: JsonPropertyName("user_input")] string? UserInput,
    [property: JsonPropertyName("broadcaster_user_login")] string? BroadcasterLogin,
    [property: JsonPropertyName("broadcaster_user_id")] string? BroadcasterId,
    [property: JsonPropertyName("reward")] RewardInfo? Reward);

/// <summary>One step of a redemption action: `award` (Amount), `say` (Text template) or `alert` (Text template).</summary>
public sealed record RedeemStep(
    [property: JsonPropertyName("k")] string Kind,
    [property: JsonPropertyName("n")] long Amount,
    [property: JsonPropertyName("t")] string Text);

/// <summary>
/// A configured reward-to-action mapping. `Key` is `id.&lt;guid&gt;` or `title.&lt;hash&gt;`; `Match` is the human-readable reward
/// reference shown by `!redeem list`; `Enabled` is the per-mapping switch.
/// </summary>
public sealed record RedeemMapping(
    [property: JsonPropertyName("key")] string Key,
    [property: JsonPropertyName("match")] string Match,
    [property: JsonPropertyName("on")] bool Enabled,
    [property: JsonPropertyName("steps")] List<RedeemStep> Steps);

/// <summary>The community's whole mapping table (one kv document).</summary>
public sealed record RedeemMappings([property: JsonPropertyName("m")] List<RedeemMapping> Items);

/// <summary>
/// What one redemption resolved to, recorded under the redemption id BEFORE any money moves so a redelivery repeats exactly the
/// same steps and reply (the host de-duplicates the credit on its own key).
/// </summary>
public sealed record RedeemOutcome(
    [property: JsonPropertyName("actor")] string Actor,
    [property: JsonPropertyName("match")] string Match,
    [property: JsonPropertyName("title")] string Title,
    [property: JsonPropertyName("cost")] long Cost,
    [property: JsonPropertyName("input")] string Input,
    [property: JsonPropertyName("steps")] List<RedeemStep> Steps);

/// <summary>This bundle's own source-generated JSON context (no reflection JSON, ever).</summary>
[JsonSerializable(typeof(RedemptionEvent))]
[JsonSerializable(typeof(RedeemMappings))]
[JsonSerializable(typeof(RedeemOutcome))]
public sealed partial class ChannelpointsJsonContext : JsonSerializerContext;
