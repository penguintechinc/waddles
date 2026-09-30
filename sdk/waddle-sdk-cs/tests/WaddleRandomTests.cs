using WaddleSdk.Random;
using Xunit;

namespace WaddleSdk.Tests;

public class WaddleRandomTests
{
    [Fact]
    public void next_int_stays_within_the_inclusive_range()
    {
        for (var i = 0; i < 200; i++)
        {
            var value = WaddleRandom.NextInt(1, 6);
            Assert.InRange(value, 1, 6);
        }
    }

    [Fact]
    public void next_int_supports_a_single_value_range()
    {
        Assert.Equal(4, WaddleRandom.NextInt(4, 4));
    }

    [Fact]
    public void next_int_throws_when_max_is_below_min()
    {
        Assert.Throws<ArgumentOutOfRangeException>(() => WaddleRandom.NextInt(6, 1));
    }

    [Fact]
    public void next_double_stays_in_unit_range()
    {
        for (var i = 0; i < 50; i++)
        {
            var value = WaddleRandom.NextDouble();
            Assert.InRange(value, 0.0, 1.0);
        }
    }

    [Fact]
    public void pick_returns_an_element_from_the_list()
    {
        string[] items = ["a", "b", "c"];
        for (var i = 0; i < 50; i++)
        {
            Assert.Contains(WaddleRandom.Pick(items), items);
        }
    }

    [Fact]
    public void pick_throws_on_empty_list()
    {
        Assert.Throws<ArgumentException>(() => WaddleRandom.Pick(Array.Empty<string>()));
    }
}
