using SuperPenguin.DropGameCore;

namespace WaddleBundleSuperpenguinPlinko;

/// <summary>One recorded puck position: time in ms since release and the position in 1/10000 of the board (y is negative above the board).</summary>
public readonly record struct PlinkoKeyframe(int TimeMs, int X, int Y);

/// <summary>One peg contact: time in ms since release and the peg's row/column on the board.</summary>
public readonly record struct PlinkoPegHit(int TimeMs, int Row, int Col);

/// <summary>
/// The deterministic result of one drop: where the puck was released, the bucket it settled in, the keyframe path and the peg contacts.
/// <see cref="TimedOut"/> is true only when the 25 s watchdog (upstream's "emergency timeout") ended a trapped puck.
/// </summary>
public sealed record PlinkoResult(
    double StartX,
    int BucketIndex,
    BucketOutcome Outcome,
    IReadOnlyList<PlinkoKeyframe> Keyframes,
    IReadOnlyList<PlinkoPegHit> PegHits,
    bool TimedOut);

/// <summary>
/// Pure, deterministic port of PenguinTwitchBot's Plinko: `PlinkoSimulationEngine` (weighted bucket pick, bucket evaluation) +
/// `PlinkoDropService.TriggerPlinkoDropAsync` (release position biased by bucket weight) + the overlay's puck physics from
/// `plinko.js` (gravity, drag, peg/divider/wall/floor collisions, apex kick, settle rule), run server-side on a fixed 1920x1080
/// board at a fixed 240 Hz step. All randomness comes from one <see cref="Xoshiro256"/> seeded with the recorded seed, and the
/// arithmetic is only + - * / and sqrt (IEEE-exact on every platform), so the same seed and config always give the same result.
/// The landing bucket is whatever the physics decides, exactly as upstream's browser overlay reports it; the weights only bias the
/// release position. There is no reflection and no host call here.
/// </summary>
public static class PlinkoSimulation
{
    private const double Dt = 1.0 / 240.0;
    private const int MaxSteps = 25 * 240;
    private const int KeyframeEvery = 8;
    private const int PegHitMinGapSteps = 18;

    /// <summary>Picks a bucket index by weight (each weight floored at 1), upstream's `PickWeightedBucketIndex`, drawing from <paramref name="rng"/>.</summary>
    public static int PickWeightedBucketIndex(IReadOnlyList<PlinkoBucket> buckets, Xoshiro256 rng)
    {
        if (buckets.Count == 0)
        {
            return 0;
        }

        var total = buckets.Sum(b => Math.Max(1, b.Weight));
        var roll = rng.NextInt(0, total - 1);
        var cumulative = 0;
        for (var i = 0; i < buckets.Count; i++)
        {
            cumulative += Math.Max(1, buckets[i].Weight);
            if (roll < cumulative)
            {
                return i;
            }
        }

        return buckets.Count - 1;
    }

    /// <summary>Upstream's `EvaluateBucketOutcome`: score only counts for a winning bucket; the bullseye is a winning bucket at the best winning score.</summary>
    public static BucketOutcome EvaluateBucketOutcome(IReadOnlyList<PlinkoBucket> buckets, int bucketIndex)
    {
        if (buckets.Count == 0)
        {
            return new BucketOutcome(0, false, "MISS", false);
        }

        var b = buckets[Math.Clamp(bucketIndex, 0, buckets.Count - 1)];
        var maxWinScore = buckets.Where(x => x.IsWin).Select(x => x.Score).DefaultIfEmpty(0).Max();
        return new BucketOutcome(b.IsWin ? b.Score : 0, b.IsWin, b.Label, b.IsWin && b.Score >= maxWinScore);
    }

    /// <summary>Runs one drop from <paramref name="seed"/> under <paramref name="config"/> (clamped like upstream's settings).</summary>
    public static PlinkoResult Run(ulong seed, PlinkoConfig config)
    {
        var cfg = config.Clamped();
        var rng = new Xoshiro256(seed);
        var buckets = cfg.Buckets;
        var cols = buckets.Count;

        // Release position (upstream TriggerPlinkoDropAsync): weighted target bucket -> nearest top-row peg +/- 1 -> micro offset.
        const int topPegCount = PlinkoConfig.PegColumns - 1;
        var weighted = PickWeightedBucketIndex(buckets, rng);
        var targetX = (weighted + 0.5) / cols;
        var basePeg = (int)Math.Round((targetX * PlinkoConfig.PegColumns) - 1.0);
        var jitter = rng.NextInt(-1, 1);
        var pegIndex = Math.Clamp(basePeg + jitter, 0, topPegCount - 1);
        var pegCenterX = (pegIndex + 1.0) / PlinkoConfig.PegColumns;
        var startX = Math.Clamp(pegCenterX + rng.NextDouble(-0.003, 0.003), 0.06, 0.94);

        double w = PlinkoConfig.Width;
        double h = PlinkoConfig.Height;
        var tokenRadius = (double)cfg.PuckRadius;
        var pegRadius = (double)cfg.PegRadius;
        var minDistPeg = tokenRadius + pegRadius;
        var minDistPegSq = minDistPeg * minDistPeg;
        var landingY = h - tokenRadius - 4;
        var bucketH = (double)cfg.BucketHeight;
        var bucketY = h - bucketH;
        var rows = cfg.RowCount;
        var pegPitchX = w / PlinkoConfig.PegColumns;
        var rowTopY = 0.222 * h;
        var bottomGap = Math.Max(80.0, Math.Round(0.111 * h));
        var rowBottomY = h - bucketH - bottomGap;
        var rowPitchY = rows > 1 ? (rowBottomY - rowTopY) / (rows - 1) : 0.0;
        var slotW = w / cols;
        var durationSec = Math.Clamp(4.2 / Math.Sqrt(Math.Sqrt(cfg.Gravity)), 2.5, 7.0);
        var gAccel = cfg.Gravity * (3600.0 / Math.Max(0.7, Math.Min(2.0, durationSec)));
        var bounciness = cfg.Bounciness;

        var px = startX * w;
        var py = -0.06 * h;
        var vx = (rng.NextDouble() - 0.5) * 20;
        var vy = 0.0;

        var keyframes = new List<PlinkoKeyframe> { Frame(0, px, py, w, h) };
        var hits = new List<PlinkoPegHit>();
        var lastHitStep = -1000;
        var floorTouchStep = -1;
        var timedOut = true;
        var step = 0;

        while (step < MaxSteps)
        {
            step++;
            vy += gAccel * Dt;
            vx *= Math.Max(0.0, 1.0 - (0.08 * Dt));
            vy *= Math.Max(0.0, 1.0 - (0.01 * Dt));
            vx = Math.Max(-1800.0, Math.Min(1800.0, vx));
            vy = Math.Max(-1800.0, Math.Min(1800.0, vy));
            px += vx * Dt;
            py += vy * Dt;

            if (px < tokenRadius)
            {
                px = tokenRadius;
                if (vx < 0)
                {
                    vx = -vx * 0.95;
                }
            }
            else if (px > w - tokenRadius)
            {
                px = w - tokenRadius;
                if (vx > 0)
                {
                    vx = -vx * 0.95;
                }
            }

            for (var r = 0; r < rows; r++)
            {
                var rowY = rowTopY + (r * rowPitchY);
                if (Math.Abs(py - rowY) > minDistPeg)
                {
                    continue;
                }

                var isOdd = r % 2 == 1;
                var countInRow = isOdd ? PlinkoConfig.PegColumns - 2 : PlinkoConfig.PegColumns - 1;
                for (var c = 0; c < countInRow; c++)
                {
                    var pegX = isOdd ? (c + 1.5) * pegPitchX : (c + 1.0) * pegPitchX;
                    var dx = px - pegX;
                    var dy = py - rowY;
                    var distSq = (dx * dx) + (dy * dy);
                    if (!(distSq < minDistPegSq && distSq > 0.000001))
                    {
                        continue;
                    }

                    var dist = Math.Sqrt(distSq);
                    var nx = dx / dist;
                    var ny = dy / dist;
                    var overlap = minDistPeg - dist;
                    px += nx * overlap;
                    py += ny * overlap;

                    var vn = (vx * nx) + (vy * ny);
                    if (vn >= 0)
                    {
                        continue;
                    }

                    var rest = Math.Max(0.40, Math.Min(1.15, bounciness * 1.12));
                    var impulse = -(1.0 + rest) * vn;
                    vx += nx * impulse;
                    vy += ny * impulse;

                    var tx = -ny;
                    var ty = nx;
                    var vt = (vx * tx) + (vy * ty);
                    vx -= tx * vt * 0.04;
                    vy -= ty * vt * 0.04;

                    // Keeps the puck from balancing on a peg apex: a lively left/right kick (upstream's rule, random draws from the seed stream).
                    if (ny < -0.85 && Math.Abs(nx) < 0.40)
                    {
                        var sign = nx >= 0 ? 1.0 : -1.0;
                        if (Math.Abs(nx) < 0.04)
                        {
                            sign = rng.NextBool() ? 1.0 : -1.0;
                        }

                        vx += sign * (140 + (rng.NextDouble() * 80)) * bounciness;
                        px += sign * 2.5;
                    }

                    if (step - lastHitStep > PegHitMinGapSteps)
                    {
                        lastHitStep = step;
                        hits.Add(new PlinkoPegHit(TimeMs(step), r, c));
                    }
                }
            }

            if (py + tokenRadius >= bucketY)
            {
                const double divHalfW = 4;
                for (var k = 1; k < cols; k++)
                {
                    var divX = k * slotW;
                    var dx = px - divX;
                    if (!(Math.Abs(dx) < tokenRadius + divHalfW))
                    {
                        continue;
                    }

                    if (py < bucketY)
                    {
                        var tipDx = px - divX;
                        var tipDy = py - bucketY;
                        var tipDistSq = (tipDx * tipDx) + (tipDy * tipDy);
                        var tipMin = tokenRadius + divHalfW;
                        if (tipDistSq < tipMin * tipMin && tipDistSq > 0.0001)
                        {
                            var td = Math.Sqrt(tipDistSq);
                            var tnx = tipDx / td;
                            var tny = tipDy / td;
                            px += tnx * (tipMin - td);
                            py += tny * (tipMin - td);
                            var tvn = (vx * tnx) + (vy * tny);
                            if (tvn < 0)
                            {
                                var rest = Math.Max(0.3, Math.Min(0.9, bounciness * 0.8));
                                vx -= (1.0 + rest) * tvn * tnx;
                                vy -= (1.0 + rest) * tvn * tny;
                            }
                        }
                    }
                    else if (dx > 0)
                    {
                        px = divX + divHalfW + tokenRadius;
                        if (vx < 0)
                        {
                            vx = -vx * 0.45;
                        }
                    }
                    else
                    {
                        px = divX - divHalfW - tokenRadius;
                        if (vx > 0)
                        {
                            vx = -vx * 0.45;
                        }
                    }
                }
            }

            if (py >= landingY)
            {
                py = landingY;
                if (vy > 0)
                {
                    vy = -vy * 0.20;
                    if (Math.Abs(vy) < 30)
                    {
                        vy = 0;
                    }
                }

                vx *= Math.Max(0.0, 1.0 - (0.85 * Dt * 60));
                if (Math.Abs(vx) < 8)
                {
                    vx = 0;
                }

                if (floorTouchStep < 0)
                {
                    floorTouchStep = step;
                }
            }

            if (step % KeyframeEvery == 0)
            {
                keyframes.Add(Frame(step, px, py, w, h));
            }

            var atFloor = py >= landingY - 2;
            if (atFloor && floorTouchStep < 0)
            {
                floorTouchStep = step;
            }

            // Upstream's settle rule: on the floor and at rest, or 120 ms on the floor and slow, or 400 ms on the floor.
            var sinceFloor = floorTouchStep < 0 ? -1 : step - floorTouchStep;
            if (atFloor && ((vy == 0 && vx == 0) || (sinceFloor > 28 && Math.Abs(vy) < 25 && Math.Abs(vx) < 25) || sinceFloor >= 96))
            {
                timedOut = false;
                break;
            }
        }

        if (step % KeyframeEvery != 0)
        {
            keyframes.Add(Frame(step, px, py, w, h));
        }

        var bucketIndex = Math.Max(0, Math.Min(cols - 1, (int)Math.Floor(px / slotW)));
        return new PlinkoResult(startX, bucketIndex, EvaluateBucketOutcome(buckets, bucketIndex), keyframes, hits, timedOut);
    }

    private static int TimeMs(int step) => step * 1000 / 240;

    private static PlinkoKeyframe Frame(int step, double px, double py, double w, double h) =>
        new(TimeMs(step), (int)Math.Round(px / w * 10000.0), (int)Math.Round(py / h * 10000.0));
}
