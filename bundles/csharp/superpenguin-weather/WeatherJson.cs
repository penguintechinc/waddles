using System.Text.Json.Serialization;

namespace WaddleBundleSuperpenguinWeather;

/// <summary>The bundle config this port reads (`default_location`, the original's `Weather:DefaultLocation`).</summary>
public sealed record WeatherConfig([property: JsonPropertyName("default_location")] string? DefaultLocation);

/// <summary>WeatherAPI `condition` object.</summary>
public sealed record WeatherCondition([property: JsonPropertyName("text")] string? Text);

/// <summary>WeatherAPI `location` object.</summary>
public sealed record WeatherLocation(
    [property: JsonPropertyName("name")] string? Name,
    [property: JsonPropertyName("country")] string? Country);

/// <summary>WeatherAPI `current` object.</summary>
public sealed record WeatherCurrent(
    [property: JsonPropertyName("condition")] WeatherCondition? Condition,
    [property: JsonPropertyName("temp_f")] double? TempF,
    [property: JsonPropertyName("temp_c")] double? TempC,
    [property: JsonPropertyName("humidity")] double? Humidity);

/// <summary>WeatherAPI forecast `day` object.</summary>
public sealed record WeatherDay(
    [property: JsonPropertyName("condition")] WeatherCondition? Condition,
    [property: JsonPropertyName("maxtemp_f")] double? MaxTempF,
    [property: JsonPropertyName("maxtemp_c")] double? MaxTempC,
    [property: JsonPropertyName("mintemp_f")] double? MinTempF,
    [property: JsonPropertyName("mintemp_c")] double? MinTempC);

/// <summary>One WeatherAPI `forecastday` entry.</summary>
public sealed record WeatherForecastDay([property: JsonPropertyName("day")] WeatherDay? Day);

/// <summary>WeatherAPI `forecast` object.</summary>
public sealed record WeatherForecast([property: JsonPropertyName("forecastday")] List<WeatherForecastDay>? ForecastDay);

/// <summary>The slice of WeatherAPI's `forecast.json` response this bundle reads.</summary>
public sealed record ForecastResponse(
    [property: JsonPropertyName("location")] WeatherLocation? Location,
    [property: JsonPropertyName("current")] WeatherCurrent? Current,
    [property: JsonPropertyName("forecast")] WeatherForecast? Forecast);

/// <summary>WeatherAPI error detail (`{"error":{"code":1006}}`).</summary>
public sealed record WeatherErrorDetail([property: JsonPropertyName("code")] int? Code);

/// <summary>WeatherAPI error body.</summary>
public sealed record WeatherErrorBody([property: JsonPropertyName("error")] WeatherErrorDetail? Error);

/// <summary>This bundle's own source-generated JSON context.</summary>
[JsonSerializable(typeof(WeatherConfig))]
[JsonSerializable(typeof(ForecastResponse))]
[JsonSerializable(typeof(WeatherErrorBody))]
public sealed partial class WeatherJsonContext : JsonSerializerContext;
