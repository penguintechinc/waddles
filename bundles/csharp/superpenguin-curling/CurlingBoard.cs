namespace WaddleBundleSuperpenguinCurling;

/// <summary>Which screen edge the strip sits on; upstream's `CurlingStripPosition`. Bottom/Top run horizontally, Left/Right vertically.</summary>
public enum StripPosition
{
    Bottom = 0,
    Top = 1,
    Left = 2,
    Right = 3,
}

/// <summary>How a resting stone is scored; upstream's `CurlingScoringType`: concentric house rings or the shuffleboard zones.</summary>
public enum ScoringType
{
    CurlingHouse = 0,
    Shuffleboard = 1,
}

/// <summary>The sheet condition; upstream's `CurlingIceCondition`. It scales friction (see <see cref="CurlingSimulation.DragMultiplier"/>).</summary>
public enum IceCondition
{
    Normal = 0,
    Slick = 1,
    HeavyFrost = 2,
    CrossDrift = 3,
}

/// <summary>
/// The sheet configuration. Defaults are upstream's `CurlingGameSettings` defaults (bottom strip, left-to-right, curling-house
/// scoring, target at 84% of the strip, 7-point power variance, default power drawn from 65..85) except where the README lists a
/// deviation (the ice condition is rolled per shot from the seed because a single wagered shot has no round to roll it for).
/// Only the default sheet is reachable from chat; there is no streamer settings UI yet.
/// </summary>
public sealed record CurlingConfig(
    StripPosition Strip,
    SlideDirection Direction,
    ScoringType Scoring,
    bool KitchenPenalty,
    double TargetDistancePercent,
    bool DynamicIce,
    IceCondition FixedIce,
    double PowerVariancePercent,
    int DefaultPowerMin,
    int DefaultPowerMax,
    bool ViewerPowerControl)
{
    /// <summary>The default sheet, as `CurlingGameSettings` ships it (dynamic ice on: see the README deviation note).</summary>
    public static CurlingConfig Default { get; } = new(
        StripPosition.Bottom, SlideDirection.LeftToRight, ScoringType.CurlingHouse, false, 84.0, true, IceCondition.Normal, 7.0, 65, 85, true);

    /// <summary>The config with upstream's setting clamps applied (target 60..95, power range 1..100 ordered); variance is capped at 25 points.</summary>
    public CurlingConfig Clamped()
    {
        var min = Math.Clamp(DefaultPowerMin, 1, 100);
        var max = Math.Clamp(DefaultPowerMax, 1, 100);
        if (min > max)
        {
            (min, max) = (max, min);
        }

        return this with
        {
            TargetDistancePercent = Math.Clamp(TargetDistancePercent, 60.0, 95.0),
            PowerVariancePercent = Math.Clamp(PowerVariancePercent, 0.0, 25.0),
            DefaultPowerMin = min,
            DefaultPowerMax = max,
        };
    }

    /// <summary>True when the strip runs along the horizontal screen axis (Bottom/Top).</summary>
    public bool IsHorizontal => Strip is StripPosition.Bottom or StripPosition.Top;
}

/// <summary>
/// The deterministic result of one slide: the powers, the ice, the geometry (start/rest/lateral along and across the strip, all
/// normalised 0..1), the scoring outcome and the keyframes. <see cref="WallHit"/> is true when the stone ran into the back board
/// (upstream clamps the rest position to 0.02..0.98) instead of stopping on its own.
/// </summary>
public sealed record CurlingResult(
    double RawPower,
    double EffectivePower,
    IceCondition Ice,
    bool IsReversed,
    double TargetRatio,
    double StartPos,
    double RestPos,
    double Lateral,
    int TotalTicks,
    bool WallHit,
    int Score,
    bool IsBullseye,
    bool IsWin,
    IReadOnlyList<SlidePoint> Keyframes);
