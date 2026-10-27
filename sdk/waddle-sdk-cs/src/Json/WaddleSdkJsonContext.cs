using System.Text.Json.Serialization;
using WaddleSdk.Chat;

namespace WaddleSdk.Json;

/// <summary>
/// Source-generated `JsonSerializerContext` for this SDK's own payload POCOs --
/// never reflection-based `JsonSerializer` calls, which NativeAOT-LLVM's
/// trimmer either strips (silent data loss) or throws for at runtime
/// (`general.md` reflection-free requirement; `bundles/csharp/csping/README.md`
/// "JSON handling" gap this SDK closes). A bundle's own payload types need
/// their own sibling `JsonSerializerContext` (see the SDK README) -- C#
/// source generators require each covered type to be declared on the
/// context that will serialize it, so a bundle cannot simply add
/// `[JsonSerializable]` attributes onto this SDK's own (sealed, out-of-project)
/// partial class.
/// </summary>
[JsonSourceGenerationOptions(WriteIndented = false)]
[JsonSerializable(typeof(ChatMessagePayload))]
[JsonSerializable(typeof(ChatReplyPayload))]
[JsonSerializable(typeof(RelayMessagePayload))]
public sealed partial class WaddleSdkJsonContext : JsonSerializerContext;
