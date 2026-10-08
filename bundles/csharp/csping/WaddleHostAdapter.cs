// Per-project host adapter: implements `WaddleSdk.Stage.IWaddleHost` by
// delegating to THIS project's own wit-bindgen-generated import bindings
// (`StageWorld.wit.Imports.waddle.bundle.v1_0_0.*`, generated fresh into
// `generated/wit/` by `dotnet build` from this project's own `<Wit Include=.../>`
// item -- see `csping.csproj`). `waddle-sdk-cs` cannot provide this
// itself: wit-bindgen's C# codegen runs per-project, so the generated types
// here are a DIFFERENT CLR type from any copy the SDK library might generate
// -- see `sdk/waddle-sdk-cs/waddle-sdk-cs.csproj`'s header comment. Every C#
// bundle needs one of these; copy this file and adjust only the namespace-
// qualified `StageWorld...` references if wit-bindgen's generated shape ever
// changes -- see `sdk/waddle-sdk-cs/README.md` "Writing a bundle". This copy
// is byte-for-byte identical in logic to
// `bundles/csharp/superpenguin-roll/WaddleHostAdapter.cs` (only the
// namespace differs) -- csping itself only exercises `Relay` via
// `CspingDispatch`, but the adapter implements the full `IWaddleHost`
// surface per the README's "copy unmodified" guidance rather than a
// bundle-specific subset.
//
// kv/db availability (2026-09-28): the host `kv` and `db` capabilities are
// currently hardcoded to deny every call in svc-process/svc-action while the
// real backends are implemented. `kv`'s WIT `error` variant has no native
// `denied` case (only `too-large`/`backend`, `wit/waddle-bundle/stage.wit`),
// so the current denial surfaces as a `backend` error; this adapter maps a
// `backend` error whose message signals unavailability onto
// `KvErrorKind.Denied` so callers (e.g. `CooldownGuard`) can degrade
// gracefully instead of treating it as a generic backend fault. This
// heuristic is deliberately narrow and removable: once the real `kv` backend
// lands, a message that happens to contain "denied" would be a genuine
// (if confusingly-worded) backend error -- revisit this mapping at that point.
using WaddleSdk.Clock;
using WaddleSdk.Context;
using WaddleSdk.Db;
using WaddleSdk.Flags;
using WaddleSdk.Http;
using WaddleSdk.Kv;
using WaddleSdk.Log;
using WaddleSdk.Relay;
using WaddleSdk.Stage;

using WitContext = global::StageWorld.wit.Imports.waddle.bundle.v1_0_0.IContextImports;
using WitKv = global::StageWorld.wit.Imports.waddle.bundle.v1_0_0.IKvImports;
using WitDb = global::StageWorld.wit.Imports.waddle.bundle.v1_0_0.IDbImports;
using WitRelay = global::StageWorld.wit.Imports.waddle.bundle.v1_0_0.IRelayImports;
using WitHttp = global::StageWorld.wit.Imports.waddle.bundle.v1_0_0.IHttpImports;
using WitFlags = global::StageWorld.wit.Imports.waddle.bundle.v1_0_0.IFlagsImports;
using WitLog = global::StageWorld.wit.Imports.waddle.bundle.v1_0_0.ILogImports;
using WitClock = global::StageWorld.wit.Imports.waddle.bundle.v1_0_0.IClockImports;

namespace WaddleBundleCsping;

/// <summary>This bundle's <see cref="IWaddleHost"/> implementation over the real WIT host imports.</summary>
public sealed class WaddleHostAdapter : IWaddleHost
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

    private sealed class KvAdapter : IKvClient
    {
        public byte[]? Get(string key)
        {
            try
            {
                return WitKv.Get(key);
            }
            catch (global::StageWorld.WitException<WitKv.Error> ex)
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
            catch (global::StageWorld.WitException<WitKv.Error> ex)
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
            catch (global::StageWorld.WitException<WitKv.Error> ex)
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
            catch (global::StageWorld.WitException<WitKv.Error> ex)
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
            _ when error.AsBackend.Contains("denied", StringComparison.OrdinalIgnoreCase) =>
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
            catch (global::StageWorld.WitException<WitDb.Error> ex)
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
            catch (global::StageWorld.WitException<WitDb.Error> ex)
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
            catch (global::StageWorld.WitException<WitDb.Error> ex)
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
            catch (global::StageWorld.WitException<WitDb.Error> ex)
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
            catch (global::StageWorld.WitException<WitDb.Error> ex)
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
            catch (global::StageWorld.WitException<WitRelay.Error> ex)
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
            catch (global::StageWorld.WitException<WitHttp.Error> ex)
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
}
