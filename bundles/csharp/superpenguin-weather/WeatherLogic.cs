using System.Globalization;
using System.Text.Json;
using WaddleSdk.Chat;
using WaddleSdk.Http;
using WaddleSdk.Log;
using WaddleSdk.Stage;
using WaddleSdk.Types;

namespace WaddleBundleSuperpenguinWeather;

/// <summary>
/// `!weather [location]`, ported from PenguinTwitchBot's `Misc/Weather.cs` (github.com/Psychoboy/PenguinTwitchBot, MIT, used with
/// permission -- see `bundle.yaml`'s `notice`). It asks WeatherAPI.com's `forecast.json` for the location and answers with current
/// conditions plus today's high/low, in the original's wording.
///
/// <para><b>No weather SERVICE bundle exists</b>, so the app makes the call itself through the host `http` capability with a declared
/// egress FQDN (`api.weatherapi.com`, GET only). <b>The API key never enters the guest:</b> the original puts `key=` in the URL from its
/// configuration; here the request carries a SECRET REFERENCE (`WEATHER_API_KEY`) the stage resolves into the `key` header
/// host-side (the Python weather bundle does the same). The operator provisions that secret; the repo holds none.</para>
///
/// <para><b>Deviations (documented).</b> (1) The default location comes from the bundle config key `default_location` when the host
/// forwards config (it does not yet), otherwise a location argument is required. (2) The location is URL-escaped (the original
/// concatenates it raw into the query string). (3) Failures are specific: bad/missing key (401/403), unknown location (provider code
/// 1006) and an unreachable provider each get their own reply; a missing egress grant (`denied`) is a misconfiguration and throws.</para>
///
/// Gated behind PostHog flag <see cref="FlagKey"/> (default OFF).
/// </summary>
public sealed class WeatherLogic : WaddleProcessStageNext
{
    internal const string FlagKey = "waddles.command-superpenguin-weather";
    internal const string Host = "api.weatherapi.com";
    internal const string KeyHeader = "key";
    internal const string KeySecretRef = "WEATHER_API_KEY";
    internal const int MaxLocationLength = 100;
    internal const int NoMatchCode = 1006;

    protected override PlatformEventInfo? Transform(PlatformEventInfo @event, IWaddleHostNext host)
    {
        var command = ChatCommand.TryParse(@event.PayloadJson, "!");
        if (command is null || !command.Is("weather"))
        {
            return null;
        }

        if (!host.Flags.Enabled(FlagKey, false))
        {
            return null;
        }

        var location = Kit.Clean(Kit.RestOf(command), MaxLocationLength);
        if (location.Length == 0)
        {
            location = DefaultLocation(host.Context.ConfigJson);
        }

        if (location.Length == 0)
        {
            return Kit.Reply(@event, command, "Weather default location is not configured. Provide a location: !weather <location>");
        }

        return Kit.Reply(@event, command, Lookup(host, location));
    }

    private static string DefaultLocation(string configJson)
    {
        try
        {
            var config = JsonSerializer.Deserialize(configJson, WeatherJsonContext.Default.WeatherConfig);
            return Kit.Clean(config?.DefaultLocation ?? string.Empty, MaxLocationLength);
        }
        catch (JsonException)
        {
            return string.Empty;
        }
    }

    private static string Lookup(IWaddleHostNext host, string location)
    {
        var url = $"https://{Host}/v1/forecast.json?q={Uri.EscapeDataString(location)}&days=1&aqi=no&alerts=no";
        HttpResponseInfo response;
        try
        {
            response = host.Http.Send(new HttpRequestInfo("GET", url, [], null, [(KeyHeader, KeySecretRef)]));
        }
        catch (WaddleHttpException ex) when (ex.Kind != HttpErrorKind.Denied)
        {
            host.Log.Error("weather.api_unavailable", new LogFields().With("kind", ex.Kind.ToString()).Build());
            return "The weather service is unavailable right now - please try again later.";
        }

        if (response.Status is 401 or 403)
        {
            host.Log.Error("weather.auth_rejected", new LogFields().With("status", response.Status).Build());
            return "The weather service isn't configured correctly - please tell a moderator.";
        }

        if (response.Status == 400 && IsNoMatch(response.Body))
        {
            host.Log.Info("weather.location_not_found", new LogFields().Build());
            return $"I couldn't find that location: {location}";
        }

        var text = response.Status == 200 && !response.Truncated ? Describe(response.Body) : null;
        if (text is null)
        {
            host.Log.Warn("weather.bad_response", new LogFields().With("status", response.Status).With("truncated", response.Truncated).Build());
            return "Failed to get weather for " + location;
        }

        host.Log.Info("weather.answered", new LogFields().Build());
        return text;
    }

    private static bool IsNoMatch(byte[] body)
    {
        try
        {
            return JsonSerializer.Deserialize(body, WeatherJsonContext.Default.WeatherErrorBody)?.Error?.Code == NoMatchCode;
        }
        catch (JsonException)
        {
            return false;
        }
    }

    /// <summary>The original's two-sentence report, or null when the response lacks any field it needs.</summary>
    internal static string? Describe(byte[] body)
    {
        ForecastResponse? weather;
        try
        {
            weather = JsonSerializer.Deserialize(body, WeatherJsonContext.Default.ForecastResponse);
        }
        catch (JsonException)
        {
            return null;
        }

        var day = weather?.Forecast?.ForecastDay?.FirstOrDefault()?.Day;
        if (weather?.Location is not { Name: { } name, Country: { } country } ||
            weather.Current is not { Condition.Text: { } nowText, TempF: { } tempF, TempC: { } tempC, Humidity: { } humidity } ||
            day is not { Condition.Text: { } dayText, MaxTempF: { } maxF, MaxTempC: { } maxC, MinTempF: { } minF, MinTempC: { } minC })
        {
            return null;
        }

        var c = CultureInfo.InvariantCulture;
        return $"The weather in {Kit.Clean(name, 60)}, {Kit.Clean(country, 60)} is {Kit.Clean(nowText, 60)} {tempF.ToString("0.#", c)}F/{tempC.ToString("0.#", c)}C. Humidity: {humidity.ToString("0.#", c)}%."
            + $" Today will be {Kit.Clean(dayText, 60)} High: {maxF.ToString("0.#", c)}F/{maxC.ToString("0.#", c)}C Low: {minF.ToString("0.#", c)}F/{minC.ToString("0.#", c)}C";
    }
}
