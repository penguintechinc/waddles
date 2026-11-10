// Per-project host adapter for the `stage-next-tools` world: implements
// `WaddleSdk.Stage.IWaddleHostNext` by delegating to THIS project's own
// wit-bindgen-generated import bindings
// (`StageNextToolsWorld.wit.Imports.waddle.bundle.v1_0_0.*`, generated fresh into
// `generated/wit/` by `dotnet build` from this project's own `<Wit .../>` item --
// see the bundle's `.csproj`). `waddle-sdk-cs` cannot provide this itself:
// wit-bindgen's C# codegen runs per-project, so the generated types here are a
// DIFFERENT CLR type from any copy the SDK library might generate (see
// `sdk/waddle-sdk-cs/waddle-sdk-cs.csproj`). Every C# bundle on this world copies
// this file and changes only the namespace -- see `docs/sdk-cs.md`.
//
// kv (live on release since the dedicated-Valkey backend landed): `kv`'s WIT `error`
// variant has no native `denied` case (only `too-large`/`backend`), so a host refusal
// (`storage.kv` not declared/approved) arrives as a `backend` error whose message
// says so; this adapter maps those onto `KvErrorKind.Denied` so callers can tell
// "not granted" from a genuine backend fault. The match is deliberately narrow.
using WaddleSdk.Ai;
using WaddleSdk.Clock;
using WaddleSdk.Context;
using WaddleSdk.Db;
using WaddleSdk.Flags;
using WaddleSdk.Economy;
using WaddleSdk.Http;
using WaddleSdk.Identity;
using WaddleSdk.Kv;
using WaddleSdk.Log;
using WaddleSdk.Media;
using WaddleSdk.Overlay;
using WaddleSdk.Relay;
using WaddleSdk.Stage;
using WaddleSdk.Tool;
using WaddleSdk.Types;

using WitContext = global::StageNextToolsWorld.wit.Imports.waddle.bundle.v1_0_0.IContextImports;
using WitKv = global::StageNextToolsWorld.wit.Imports.waddle.bundle.v1_0_0.IKvImports;
using WitDb = global::StageNextToolsWorld.wit.Imports.waddle.bundle.v1_0_0.IDbImports;
using WitRelay = global::StageNextToolsWorld.wit.Imports.waddle.bundle.v1_0_0.IRelayImports;
using WitHttp = global::StageNextToolsWorld.wit.Imports.waddle.bundle.v1_0_0.IHttpImports;
using WitFlags = global::StageNextToolsWorld.wit.Imports.waddle.bundle.v1_0_0.IFlagsImports;
using WitLog = global::StageNextToolsWorld.wit.Imports.waddle.bundle.v1_0_0.ILogImports;
using WitClock = global::StageNextToolsWorld.wit.Imports.waddle.bundle.v1_0_0.IClockImports;
using WitEconomy = global::StageNextToolsWorld.wit.Imports.waddle.bundle.v1_0_0.IEconomyImports;
using WitIdentity = global::StageNextToolsWorld.wit.Imports.waddle.bundle.v1_0_0.IIdentityImports;
using WitTool = global::StageNextToolsWorld.wit.Imports.waddle.bundle.v1_0_0.IToolImports;
using WitOverlay = global::StageNextToolsWorld.wit.Imports.waddle.bundle.v1_0_0.IOverlayImports;
using WitAi = global::StageNextToolsWorld.wit.Imports.waddle.bundle.v1_0_0.IAiImports;
using WitMedia = global::StageNextToolsWorld.wit.Imports.waddle.bundle.v1_0_0.IMediaImports;
using WitTypes = global::StageNextToolsWorld.wit.Imports.waddle.bundle.v1_0_0.ITypesImports;

namespace WaddleBundleSuperpenguinWheel;

/// <summary>This bundle's <see cref="IWaddleHostNext"/> implementation over the real WIT host imports.</summary>
public sealed class WaddleHostAdapter : IWaddleHostNext
{
    public BundleContextInfo Context
    {
        get
        {
            var ctx = WitContext.GetContext();
            // Generated record fields are lowerCamelCase (mirrors the kebab-case
            // WIT field names: `tenant`, `community`, `app-id` -> `appId`, ...),
            // confirmed against the compiler -- never PascalCase, unlike the
            // `Tag`/`As*`/`Tags.*` members a variant's generated wrapper exposes
            // (see e.g. `RelayAdapter` below).
            return new BundleContextInfo(ctx.tenant, ctx.community, ctx.appId, ctx.feature, ctx.version, ctx.messageId, ctx.configJson);
        }
    }

    public IKvClient Kv { get; } = new KvAdapter();
    public IDbClient Db { get; } = new DbAdapter();
    public IRelayClient Relay { get; } = new RelayAdapter();
    public IHttpClient Http { get; } = new HttpAdapter();
    public IFlagsClient Flags { get; } = new FlagsAdapter();
    public ILogClient Log { get; } = new LogAdapter();
    public IClockClient Clock { get; } = new ClockAdapter();
    public IEconomyClient Economy { get; } = new EconomyAdapter();
    public IIdentityClient Identity { get; } = new IdentityAdapter();
    public IToolClient Tool { get; } = new ToolAdapter();
    public IOverlayClient Overlay { get; } = new OverlayAdapter();
    public IAiClient Ai { get; } = new AiAdapter();
    public IMediaClient Media { get; } = new MediaAdapter();

    private sealed class KvAdapter : IKvClient
    {
        public byte[]? Get(string key)
        {
            try
            {
                return WitKv.Get(key);
            }
            catch (global::StageNextToolsWorld.WitException<WitKv.Error> ex)
            {
                throw Convert(ex.TypedValue);
            }
        }

        public void Set(string key, byte[] value, uint ttlSeconds)
        {
            try
            {
                WitKv.Set(key, value, ttlSeconds);
            }
            catch (global::StageNextToolsWorld.WitException<WitKv.Error> ex)
            {
                throw Convert(ex.TypedValue);
            }
        }

        public void Delete(string key)
        {
            try
            {
                WitKv.Delete(key);
            }
            catch (global::StageNextToolsWorld.WitException<WitKv.Error> ex)
            {
                throw Convert(ex.TypedValue);
            }
        }

        public long Increment(string key, long delta, uint ttlSeconds)
        {
            try
            {
                return WitKv.Increment(key, delta, ttlSeconds);
            }
            catch (global::StageNextToolsWorld.WitException<WitKv.Error> ex)
            {
                throw Convert(ex.TypedValue);
            }
        }

        private static WaddleKvException Convert(WitKv.Error error) => error.Tag switch
        {
            WitKv.Error.Tags.TooLarge => new WaddleKvException(KvErrorKind.TooLarge, "kv value too large", error.AsTooLarge),
            // See this file's header comment: `kv` has no native `denied` WIT
            // case yet, so the current host-side hardcoded denial arrives as a
            // `backend` error. Detect it here rather than in the shared SDK.
            _ when error.AsBackend.Contains("denied", StringComparison.OrdinalIgnoreCase)
                   || error.AsBackend.Contains("not granted", StringComparison.OrdinalIgnoreCase) =>
                new WaddleKvException(KvErrorKind.Denied, error.AsBackend),
            _ => new WaddleKvException(KvErrorKind.Backend, error.AsBackend),
        };
    }

    private sealed class DbAdapter : IDbClient
    {
        public DbRow Insert(IReadOnlyList<DbColumnValue> columnValues)
        {
            try
            {
                return FromWit(WitDb.Insert(ToWitColumnValues(columnValues)));
            }
            catch (global::StageNextToolsWorld.WitException<WitDb.Error> ex)
            {
                throw Convert(ex.TypedValue);
            }
        }

        public DbRow Get(string rowId)
        {
            try
            {
                return FromWit(WitDb.Get(rowId));
            }
            catch (global::StageNextToolsWorld.WitException<WitDb.Error> ex)
            {
                throw Convert(ex.TypedValue);
            }
        }

        public IReadOnlyList<DbRow> Query(uint limit, uint offset, DbOrderBy? orderBy = null)
        {
            try
            {
                var rows = WitDb.Query(limit, offset, ToWitOrderBy(orderBy));
                var mapped = new List<DbRow>(rows.Count);
                foreach (var row in rows)
                {
                    mapped.Add(FromWit(row));
                }

                return mapped;
            }
            catch (global::StageNextToolsWorld.WitException<WitDb.Error> ex)
            {
                throw Convert(ex.TypedValue);
            }
        }

        public DbRow Update(string rowId, ulong expectedVersion, IReadOnlyList<DbColumnValue> columnValues)
        {
            try
            {
                return FromWit(WitDb.Update(rowId, expectedVersion, ToWitColumnValues(columnValues)));
            }
            catch (global::StageNextToolsWorld.WitException<WitDb.Error> ex)
            {
                throw Convert(ex.TypedValue);
            }
        }

        public void Delete(string rowId, ulong expectedVersion)
        {
            try
            {
                WitDb.Delete(rowId, expectedVersion);
            }
            catch (global::StageNextToolsWorld.WitException<WitDb.Error> ex)
            {
                throw Convert(ex.TypedValue);
            }
        }

        private static List<WitDb.ColumnValue> ToWitColumnValues(IReadOnlyList<DbColumnValue> columnValues)
        {
            // WIT `list<T>` (T != u8) lowers to `List<T>`, not an array --
            // confirmed against the compiler (see `HttpAdapter.Send`'s own
            // header-list conversion for the same pattern).
            var witColumnValues = new List<WitDb.ColumnValue>(columnValues.Count);
            foreach (var columnValue in columnValues)
            {
                witColumnValues.Add(new WitDb.ColumnValue(columnValue.Column, ToWit(columnValue.Value)));
            }

            return witColumnValues;
        }

        private static WitDb.Value ToWit(DbValue value) => value.Kind switch
        {
            DbValueKind.Null => WitDb.Value.NullValue(),
            DbValueKind.Bool => WitDb.Value.BoolValue(value.BoolValue),
            DbValueKind.Int => WitDb.Value.IntValue(value.IntValue),
            DbValueKind.Float => WitDb.Value.FloatValue(value.FloatValue),
            DbValueKind.Text => WitDb.Value.TextValue(value.TextValue!),
            DbValueKind.Bytes => WitDb.Value.BytesValue(value.BytesValue!),
            _ => throw new ArgumentOutOfRangeException(nameof(value)),
        };

        private static DbValue FromWit(WitDb.Value value) => value.Tag switch
        {
            WitDb.Value.Tags.NullValue => DbValue.Null(),
            WitDb.Value.Tags.BoolValue => DbValue.Of(value.AsBoolValue),
            WitDb.Value.Tags.IntValue => DbValue.Of(value.AsIntValue),
            WitDb.Value.Tags.FloatValue => DbValue.Of(value.AsFloatValue),
            WitDb.Value.Tags.TextValue => DbValue.Of(value.AsTextValue),
            _ => DbValue.Of(value.AsBytesValue),
        };

        private static DbRow FromWit(WitDb.Row row)
        {
            var columns = new List<DbColumnValue>(row.columns.Count);
            foreach (var columnValue in row.columns)
            {
                columns.Add(new DbColumnValue(columnValue.column, FromWit(columnValue.value)));
            }

            return new DbRow(row.rowId, row.version, columns);
        }

        private static WitDb.OrderBy? ToWitOrderBy(DbOrderBy? orderBy)
        {
            if (orderBy is not { } value)
            {
                return null;
            }

            return value.Kind switch
            {
                DbOrderByKind.Random => WitDb.OrderBy.Random(),
                _ => WitDb.OrderBy.Column(new WitDb.OrderColumn(value.Column!.Name, value.Column.Descending)),
            };
        }

        private static WaddleDbException Convert(WitDb.Error error) => error.Tag switch
        {
            WitDb.Error.Tags.Denied => new WaddleDbException(DbErrorKind.Denied, error.AsDenied),
            WitDb.Error.Tags.InvalidColumn => new WaddleDbException(DbErrorKind.InvalidColumn, error.AsInvalidColumn),
            WitDb.Error.Tags.InvalidValue => new WaddleDbException(DbErrorKind.InvalidValue, error.AsInvalidValue),
            WitDb.Error.Tags.NotFound => new WaddleDbException(DbErrorKind.NotFound, "row not found"),
            WitDb.Error.Tags.Conflict => new WaddleDbException(DbErrorKind.Conflict, error.AsConflict),
            WitDb.Error.Tags.QuotaExceeded => new WaddleDbException(DbErrorKind.QuotaExceeded, error.AsQuotaExceeded),
            WitDb.Error.Tags.Timeout => new WaddleDbException(DbErrorKind.Timeout, "db call timed out"),
            _ => new WaddleDbException(DbErrorKind.Backend, error.AsBackend),
        };
    }

    private sealed class RelayAdapter : IRelayClient
    {
        public void Push(string provider, string messageJson)
        {
            try
            {
                WitRelay.Push(provider, messageJson);
            }
            catch (global::StageNextToolsWorld.WitException<WitRelay.Error> ex)
            {
                var detail = ex.TypedValue.Tag switch
                {
                    WitRelay.Error.Tags.Denied => ex.TypedValue.AsDenied,
                    _ => ex.TypedValue.AsBackend,
                };
                var kind = ex.TypedValue.Tag == WitRelay.Error.Tags.Denied ? RelayErrorKind.Denied : RelayErrorKind.Backend;
                throw new WaddleRelayException(kind, detail);
            }
        }
    }

    private sealed class HttpAdapter : IHttpClient
    {
        public HttpResponseInfo Send(HttpRequestInfo request)
        {
            // See `DbAdapter.Execute`'s comment: non-byte `list<T>` lowers to
            // `List<T>`, not an array.
            var witHeaders = new List<WitHttp.Header>(request.Headers.Count);
            foreach (var header in request.Headers)
            {
                witHeaders.Add(new WitHttp.Header(header.Name, header.Value));
            }

            var witSecretRefs = new List<(string, string)>(request.SecretRefs.Count);
            foreach (var secretRef in request.SecretRefs)
            {
                witSecretRefs.Add((secretRef.HeaderName, secretRef.SecretRefName));
            }

            var witRequest = new WitHttp.Request(request.Method, request.Url, witHeaders, request.Body, witSecretRefs);

            try
            {
                var response = WitHttp.Send(witRequest);
                var headers = new List<HttpHeader>(response.headers.Count);
                foreach (var header in response.headers)
                {
                    headers.Add(new HttpHeader(header.name, header.value));
                }

                return new HttpResponseInfo(response.status, headers, response.body, response.truncated);
            }
            catch (global::StageNextToolsWorld.WitException<WitHttp.Error> ex)
            {
                throw ex.TypedValue.Tag switch
                {
                    WitHttp.Error.Tags.Denied => new WaddleHttpException(HttpErrorKind.Denied, ex.TypedValue.AsDenied),
                    WitHttp.Error.Tags.Timeout => new WaddleHttpException(HttpErrorKind.Timeout, "http call timed out"),
                    WitHttp.Error.Tags.TooLarge => new WaddleHttpException(HttpErrorKind.TooLarge, "http response too large", tooLargeBytes: ex.TypedValue.AsTooLarge),
                    WitHttp.Error.Tags.RateLimited => new WaddleHttpException(HttpErrorKind.RateLimited, "http call rate-limited", rateLimitedSeconds: ex.TypedValue.AsRateLimited),
                    _ => new WaddleHttpException(HttpErrorKind.Transport, ex.TypedValue.AsTransport),
                };
            }
        }
    }

    private sealed class FlagsAdapter : IFlagsClient
    {
        public bool Enabled(string key, bool defaultValue) => WitFlags.Enabled(key, defaultValue);
        public string Tier() => WitFlags.Tier();
    }

    private sealed class LogAdapter : ILogClient
    {
        public void Write(WaddleLogLevel level, string message, string fieldsJson)
        {
            // The generated plain `enum` (no payload, unlike a `variant`'s
            // `Tags.*`) uses the WIT identifiers upper-cased -- confirmed
            // against the compiler/generated source
            // (`generated/wit/...ILogImports.cs`: `enum Level { ERROR, WARN, INFO, DEBUG }`).
            var witLevel = level switch
            {
                WaddleLogLevel.Error => WitLog.Level.ERROR,
                WaddleLogLevel.Warn => WitLog.Level.WARN,
                WaddleLogLevel.Info => WitLog.Level.INFO,
                _ => WitLog.Level.DEBUG,
            };
            WitLog.Write(witLevel, message, fieldsJson);
        }
    }

    private sealed class ClockAdapter : IClockClient
    {
        public ulong NowMillis() => WitClock.NowMillis();
        public string NowRfc3339() => WitClock.NowRfc3339();
        public ulong MonotonicNanos() => WitClock.MonotonicNanos();
    }

    private sealed class EconomyAdapter : IEconomyClient
    {
        public long Balance(string user) => Call(() => WitEconomy.Balance(user));
        public long Wager(string user, ulong stake, ulong payout) => Call(() => WitEconomy.Wager(user, stake, payout));
        public void Transfer(string from, string to, ulong amount) => Call(() => { WitEconomy.Transfer(from, to, amount); return 0; });
        public long MaxBet(string user) => Call(() => WitEconomy.MaxBet(user));

        public IReadOnlyList<EconomyEntry> Leaderboard(uint limit) => Call(() =>
        {
            var rows = WitEconomy.Leaderboard(limit);
            var mapped = new List<EconomyEntry>(rows.Count);
            foreach (var row in rows)
            {
                mapped.Add(new EconomyEntry(row.user, row.balance));
            }

            return (IReadOnlyList<EconomyEntry>)mapped;
        });

        public long Credit(string user, ulong amount) => Call(() => WitEconomy.Credit(user, amount));
        public void BatchCredit(IReadOnlyList<EconomyCredit> entries) => Call(() => { WitEconomy.BatchCredit(ToWit(entries)); return 0; });
        public long Debit(string user, ulong amount) => Call(() => WitEconomy.Debit(user, amount));
        public long ConditionalDebit(string user, ulong amount, ulong minBalance) => Call(() => WitEconomy.ConditionalDebit(user, amount, minBalance));
        public void PayPot(IReadOnlyList<EconomyCredit> winners) => Call(() => { WitEconomy.PayPot(ToWit(winners)); return 0; });

        private static List<WitEconomy.CreditEntry> ToWit(IReadOnlyList<EconomyCredit> entries)
        {
            var wit = new List<WitEconomy.CreditEntry>(entries.Count);
            foreach (var entry in entries)
            {
                wit.Add(new WitEconomy.CreditEntry(entry.User, entry.Amount));
            }

            return wit;
        }

        private static T Call<T>(Func<T> call)
        {
            try
            {
                return call();
            }
            catch (global::StageNextToolsWorld.WitException<WitEconomy.Error> ex)
            {
                var e = ex.TypedValue;
                throw e.Tag switch
                {
                    WitEconomy.Error.Tags.Denied => new WaddleEconomyException(EconomyErrorKind.Denied, e.AsDenied),
                    WitEconomy.Error.Tags.InsufficientFunds => new WaddleEconomyException(EconomyErrorKind.InsufficientFunds, "insufficient funds", e.AsInsufficientFunds),
                    WitEconomy.Error.Tags.OverCap => new WaddleEconomyException(EconomyErrorKind.OverCap, "over cap", e.AsOverCap),
                    WitEconomy.Error.Tags.NotAMember => new WaddleEconomyException(EconomyErrorKind.NotAMember, "not a member of this community"),
                    WitEconomy.Error.Tags.Invalid => new WaddleEconomyException(EconomyErrorKind.Invalid, e.AsInvalid),
                    WitEconomy.Error.Tags.Unavailable => new WaddleEconomyException(EconomyErrorKind.Unavailable, e.AsUnavailable),
                    _ => new WaddleEconomyException(EconomyErrorKind.Backend, e.AsBackend),
                };
            }
        }
    }

    private sealed class IdentityAdapter : IIdentityClient
    {
        public string ResolveActor() => Call(() => WitIdentity.ResolveActor());
        public string ResolveMention(string token) => Call(() => WitIdentity.ResolveMention(token));

        private static string Call(Func<string> call)
        {
            try
            {
                return call();
            }
            catch (global::StageNextToolsWorld.WitException<WitIdentity.Error> ex)
            {
                var e = ex.TypedValue;
                throw e.Tag switch
                {
                    WitIdentity.Error.Tags.Denied => new WaddleIdentityException(IdentityErrorKind.Denied, e.AsDenied),
                    WitIdentity.Error.Tags.NotLinked => new WaddleIdentityException(IdentityErrorKind.NotLinked, "identity is not linked to a community user"),
                    WitIdentity.Error.Tags.NotAMember => new WaddleIdentityException(IdentityErrorKind.NotAMember, "not a member of this community"),
                    WitIdentity.Error.Tags.NotFound => new WaddleIdentityException(IdentityErrorKind.NotFound, "mention not found"),
                    WitIdentity.Error.Tags.Ambiguous => new WaddleIdentityException(IdentityErrorKind.Ambiguous, "mention is ambiguous"),
                    WitIdentity.Error.Tags.Invalid => new WaddleIdentityException(IdentityErrorKind.Invalid, e.AsInvalid),
                    WitIdentity.Error.Tags.Unavailable => new WaddleIdentityException(IdentityErrorKind.Unavailable, e.AsUnavailable),
                    _ => new WaddleIdentityException(IdentityErrorKind.Backend, e.AsBackend),
                };
            }
        }
    }

    private sealed class ToolAdapter : IToolClient
    {
        public byte[] Invoke(string target, string tool, byte[] argsJson, string? idempotencyKey)
        {
            try
            {
                return WitTool.Invoke(target, tool, argsJson, idempotencyKey);
            }
            catch (global::StageNextToolsWorld.WitException<WitTool.ToolError> ex)
            {
                var e = ex.TypedValue;
                throw e.Tag switch
                {
                    WitTool.ToolError.Tags.NotGranted => new WaddleToolException(ToolErrorKind.NotGranted, $"{target}/{tool}: no approved tool.call grant"),
                    WitTool.ToolError.Tags.Forbidden => new WaddleToolException(ToolErrorKind.Forbidden, $"{target}/{tool}: forbidden by the kind matrix"),
                    WitTool.ToolError.Tags.Timeout => new WaddleToolException(ToolErrorKind.Timeout, $"{target}/{tool}: timed out"),
                    WitTool.ToolError.Tags.DepthExceeded => new WaddleToolException(ToolErrorKind.DepthExceeded, $"{target}/{tool}: nesting depth exceeded"),
                    WitTool.ToolError.Tags.PoolExhausted => new WaddleToolException(ToolErrorKind.PoolExhausted, $"{target}/{tool}: target pool exhausted"),
                    WitTool.ToolError.Tags.TargetUnavailable => new WaddleToolException(ToolErrorKind.TargetUnavailable, $"{target}/{tool}: target unavailable"),
                    _ => new WaddleToolException(ToolErrorKind.InvalidArgs, e.AsInvalidArgs),
                };
            }
        }
    }

    private sealed class OverlayAdapter : IOverlayClient
    {
        public TransportResultInfo RegisterWidget(OverlayWidget widget) =>
            Call(() => WitOverlay.RegisterWidget(new WitOverlay.WidgetDescriptor(widget.WidgetId, widget.DisplayName, ToWit(widget.Surface), widget.DescriptorJson)));

        public TransportResultInfo Push(string widgetId, OverlaySurface surface, string payloadJson) =>
            Call(() => WitOverlay.Push(widgetId, ToWit(surface), payloadJson));

        private static TransportResultInfo Call(Func<WitTypes.TransportResult> call)
        {
            try
            {
                var r = call();
                return new TransportResultInfo(r.ok, r.status, r.detail, r.providerMessageId);
            }
            catch (global::StageNextToolsWorld.WitException<WitTypes.TransportError> ex)
            {
                var e = ex.TypedValue;
                throw new WaddleTransportException(new TransportErrorInfo(e.retryable, e.code, e.message, e.retryAfterMs));
            }
        }

        private static WitOverlay.Surface ToWit(OverlaySurface surface) => surface switch
        {
            OverlaySurface.FullScreen => WitOverlay.Surface.FULL_SCREEN,
            OverlaySurface.Media => WitOverlay.Surface.MEDIA,
            OverlaySurface.Crawler => WitOverlay.Surface.CRAWLER,
            OverlaySurface.Music => WitOverlay.Surface.MUSIC,
            OverlaySurface.AlertBox => WitOverlay.Surface.ALERT_BOX,
            OverlaySurface.Chat => WitOverlay.Surface.CHAT,
            OverlaySurface.Goals => WitOverlay.Surface.GOALS,
            OverlaySurface.Ticker => WitOverlay.Surface.TICKER,
            OverlaySurface.Image => WitOverlay.Surface.IMAGE,
            OverlaySurface.Caption => WitOverlay.Surface.CAPTION,
            _ => throw new ArgumentOutOfRangeException(nameof(surface)),
        };
    }

    private sealed class AiAdapter : IAiClient
    {
        public string Generate(string prompt, AiOptions options)
        {
            try
            {
                return WitAi.Generate(prompt, new WitAi.Options(options.MaxTokens, options.Temperature, options.Json));
            }
            catch (global::StageNextToolsWorld.WitException<WitAi.Error> ex)
            {
                var e = ex.TypedValue;
                throw e.Tag switch
                {
                    WitAi.Error.Tags.Denied => new WaddleAiException(AiErrorKind.Denied, e.AsDenied),
                    WitAi.Error.Tags.TooManyTokens => new WaddleAiException(AiErrorKind.TooManyTokens, "token bound exceeded", e.AsTooManyTokens),
                    WitAi.Error.Tags.ContentFiltered => new WaddleAiException(AiErrorKind.ContentFiltered, "completion was content-filtered"),
                    WitAi.Error.Tags.Invalid => new WaddleAiException(AiErrorKind.Invalid, e.AsInvalid),
                    WitAi.Error.Tags.Unavailable => new WaddleAiException(AiErrorKind.Unavailable, e.AsUnavailable),
                    _ => new WaddleAiException(AiErrorKind.Backend, e.AsBackend),
                };
            }
        }
    }

    private sealed class MediaAdapter : IMediaClient
    {
        public string GenerateImage(string prompt) => Call(() => WitMedia.GenerateImage(prompt));
        public string SubmitMusic(string prompt) => Call(() => WitMedia.SubmitMusic(prompt));
        public string SubmitVideo(string prompt) => Call(() => WitMedia.SubmitVideo(prompt));
        public string ObjectUrl(string handle) => Call(() => WitMedia.ObjectUrl(handle));

        public MediaJobState JobStatus(string jobId)
        {
            var state = Call(() => WitMedia.JobStatus(jobId));
            return state.Tag switch
            {
                WitMedia.JobState.Tags.Pending => MediaJobState.Pending(),
                WitMedia.JobState.Tags.Done => MediaJobState.Done(state.AsDone),
                _ => FailedState(state.AsFailed),
            };
        }

        private static MediaJobState FailedState(WitMedia.Error error)
        {
            var mapped = Convert(error);
            return MediaJobState.Failed(mapped.Kind, mapped.Message);
        }

        private static T Call<T>(Func<T> call)
        {
            try
            {
                return call();
            }
            catch (global::StageNextToolsWorld.WitException<WitMedia.Error> ex)
            {
                throw Convert(ex.TypedValue);
            }
        }

        private static WaddleMediaException Convert(WitMedia.Error e) => e.Tag switch
        {
            WitMedia.Error.Tags.TierRequired => new WaddleMediaException(MediaErrorKind.TierRequired, e.AsTierRequired),
            WitMedia.Error.Tags.ByokMissing => new WaddleMediaException(MediaErrorKind.ByokMissing, e.AsByokMissing),
            WitMedia.Error.Tags.ProviderRefused => new WaddleMediaException(MediaErrorKind.ProviderRefused, e.AsProviderRefused),
            WitMedia.Error.Tags.Quota => new WaddleMediaException(MediaErrorKind.Quota, e.AsQuota),
            WitMedia.Error.Tags.Timeout => new WaddleMediaException(MediaErrorKind.Timeout, "media call timed out"),
            WitMedia.Error.Tags.Denied => new WaddleMediaException(MediaErrorKind.Denied, e.AsDenied),
            WitMedia.Error.Tags.Invalid => new WaddleMediaException(MediaErrorKind.Invalid, e.AsInvalid),
            WitMedia.Error.Tags.Unavailable => new WaddleMediaException(MediaErrorKind.Unavailable, e.AsUnavailable),
            WitMedia.Error.Tags.NotFound => new WaddleMediaException(MediaErrorKind.NotFound, "media handle or job not found"),
            _ => new WaddleMediaException(MediaErrorKind.Backend, e.AsBackend),
        };
    }
}
