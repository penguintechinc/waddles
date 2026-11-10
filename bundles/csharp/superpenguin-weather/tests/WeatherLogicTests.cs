using System.Text;
using WaddleSdk.Http;
using Xunit;
using static WaddleBundleSuperpenguinWeather.Tests.World;

namespace WaddleBundleSuperpenguinWeather.Tests;

/// <summary>Scripted provider: answers with the supplied function and records every request.</summary>
public sealed class ScriptedHttp(Func<HttpRequestInfo, HttpResponseInfo> answer) : IHttpClient
{
    public List<HttpRequestInfo> Requests { get; } = [];

    public HttpResponseInfo Send(HttpRequestInfo request)
    {
        Requests.Add(request);
        return answer(request);
    }
}

public class WeatherLogicTests
{
    private const string Good = """
        {"location":{"name":"Seattle","country":"United States of America"},
         "current":{"condition":{"text":"Light rain"},"temp_f":50.5,"temp_c":10.3,"humidity":88},
         "forecast":{"forecastday":[{"day":{"condition":{"text":"Cloudy"},"maxtemp_f":55,"maxtemp_c":12.8,"mintemp_f":44.1,"mintemp_c":6.7}}]}}
        """;

    private const string Expected =
        "The weather in Seattle, United States of America is Light rain 50.5F/10.3C. Humidity: 88%. Today will be Cloudy High: 55F/12.8C Low: 44.1F/6.7C";

    private static World NewWorld(Func<HttpRequestInfo, HttpResponseInfo>? answer = null, string config = "{}")
    {
        var w = new World(WeatherLogic.FlagKey)
        {
            Http = new ScriptedHttp(answer ?? (_ => Ok(Good))),
            ConfigJson = config,
        };
        return w;
    }

    private static HttpResponseInfo Ok(string body, ushort status = 200, bool truncated = false) =>
        new(status, [], Encoding.UTF8.GetBytes(body), truncated);

    [Fact]
    public void it_reports_current_conditions_and_todays_range_in_the_originals_wording()
    {
        var w = NewWorld();

        Assert.Equal(Expected, w.Say(new WeatherLogic(), Alice, "!weather Seattle, WA", "m1"));
    }

    [Fact]
    public void the_request_is_a_get_to_the_declared_fqdn_with_the_key_as_a_secret_reference_never_a_value()
    {
        var w = NewWorld();
        w.Say(new WeatherLogic(), Alice, "!weather Sea ttle&x=1", "m1");

        var request = Assert.Single(((ScriptedHttp)w.Http).Requests);
        Assert.Equal("GET", request.Method);
        Assert.StartsWith("https://api.weatherapi.com/v1/forecast.json?q=", request.Url);
        Assert.Contains("q=Sea%20ttlex%3D1&", request.Url.Replace("%26", string.Empty));
        Assert.DoesNotContain("key=", request.Url);
        Assert.Empty(request.Headers);
        Assert.Equal([("key", "WEATHER_API_KEY")], request.SecretRefs);
    }

    [Fact]
    public void the_default_location_comes_from_config_when_no_argument_is_given()
    {
        var w = NewWorld(config: "{\"default_location\":\"Portland\"}");

        Assert.Equal(Expected, w.Say(new WeatherLogic(), Alice, "!weather", "m1"));
        Assert.Contains("q=Portland&", ((ScriptedHttp)w.Http).Requests[0].Url);
    }

    [Theory]
    [InlineData("{}")]
    [InlineData("not json")]
    [InlineData("{\"default_location\":\"  \"}")]
    public void without_an_argument_or_a_usable_default_it_asks_for_a_location_and_calls_nothing(string config)
    {
        var w = NewWorld(config: config);

        Assert.Contains("Provide a location", w.Say(new WeatherLogic(), Alice, "!weather", "m1"));
        Assert.Empty(((ScriptedHttp)w.Http).Requests);
    }

    [Theory]
    [InlineData(401)]
    [InlineData(403)]
    public void a_rejected_key_tells_users_to_ask_a_moderator(int status)
    {
        var w = NewWorld(_ => Ok("{}", (ushort)status));

        Assert.Contains("isn't configured correctly", w.Say(new WeatherLogic(), Alice, "!weather x", "m1"));
        Assert.Contains(w.Log.Writes, l => l.Message == "weather.auth_rejected");
    }

    [Fact]
    public void provider_code_1006_means_unknown_location_but_other_400s_are_failures()
    {
        var w = NewWorld(_ => Ok("{\"error\":{\"code\":1006}}", 400));
        Assert.Equal("I couldn't find that location: Nowhere", w.Say(new WeatherLogic(), Alice, "!weather Nowhere", "m1"));

        var other = NewWorld(_ => Ok("{\"error\":{\"code\":2008}}", 400));
        Assert.Equal("Failed to get weather for x", other.Say(new WeatherLogic(), Alice, "!weather x", "m2"));

        var garbage = NewWorld(_ => Ok("<html>", 400));
        Assert.Equal("Failed to get weather for x", garbage.Say(new WeatherLogic(), Alice, "!weather x", "m3"));
    }

    [Theory]
    [InlineData(500, "{}", false)]
    [InlineData(200, "not json", false)]
    [InlineData(200, "{}", false)]
    [InlineData(200, "null", false)]
    [InlineData(200, Good, true)]
    public void a_bad_or_truncated_response_is_a_failure_reply_never_a_guess(int status, string body, bool truncated)
    {
        var w = NewWorld(_ => Ok(body, (ushort)status, truncated));

        Assert.Equal("Failed to get weather for x", w.Say(new WeatherLogic(), Alice, "!weather x", "m1"));
    }

    [Theory]
    [InlineData(HttpErrorKind.Timeout)]
    [InlineData(HttpErrorKind.RateLimited)]
    [InlineData(HttpErrorKind.Transport)]
    [InlineData(HttpErrorKind.TooLarge)]
    public void provider_outages_get_an_unavailable_reply(HttpErrorKind kind)
    {
        var w = NewWorld(_ => throw new WaddleHttpException(kind, "down"));

        Assert.Contains("unavailable right now", w.Say(new WeatherLogic(), Alice, "!weather x", "m1"));
    }

    [Fact]
    public void a_missing_egress_grant_is_a_loud_failure()
    {
        var w = NewWorld(_ => throw new WaddleHttpException(HttpErrorKind.Denied, "not granted"));

        Assert.Throws<WaddleHttpException>(() => w.Say(new WeatherLogic(), Alice, "!weather x", "m1"));
    }

    [Fact]
    public void provider_text_cannot_inject_placeholders()
    {
        var body = Good.Replace("Light rain", "{user:evil}");
        var w = NewWorld(_ => Ok(body));

        Assert.DoesNotContain("{user:", w.Say(new WeatherLogic(), Alice, "!weather x", "m1"));
    }

    [Fact]
    public void other_commands_and_flag_off_do_nothing()
    {
        var w = NewWorld();
        var l = new WeatherLogic();
        Assert.Null(w.Say(l, Alice, "!other", "m1"));
        Assert.Null(w.Say(l, Alice, "chat", "m2"));
        w.Flags.Overrides[WeatherLogic.FlagKey] = false;
        Assert.Null(w.Say(l, Alice, "!weather x", "m3"));
        Assert.Empty(((ScriptedHttp)w.Http).Requests);
    }
}
