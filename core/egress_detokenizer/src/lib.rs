//! Output detokenizer for the PII boundary's outbound placeholder grammar
//! (spec `2026-09-28-bundle-permissions-and-capability-gate.md` S10.4/S10.6,
//! folding in Gemini review condition 5).
//!
//! A WASM bundle never emits a display name -- only a `{user:<uuid>}`
//! placeholder (S10.1: "a bundle only ever emits `{user:<uuid>}`
//! placeholders... rendering to a display name happens exactly once,
//! host-side, at the sink"). This crate IS that host-side renderer. It is:
//!
//! - **Single-pass, non-recursive** ([`render::detokenize`]): the input is
//!   scanned exactly once, left to right ([`grammar::scan`]); a resolved
//!   display name is spliced into the output and escaped for its sink, but
//!   is never itself fed back through the placeholder grammar. This closes
//!   the self-referential injection the spec calls out: a crafted display
//!   name that itself contains literal `{user:...}`-shaped text is rendered
//!   as inert text, never substituted a second time.
//! - **Strict-grammar only**: the only thing ever recognized as a
//!   placeholder is `{user:<uuid-v4-or-v5-shape>}`, byte for byte
//!   (lowercase, canonical hyphenated form, version nibble `4`/`5`, RFC
//!   4122 variant nibble). Anything merely resembling that shape --
//!   `{user:not-a-uuid}`, a nested `{user:{user:...}}`, a wrong-version
//!   UUID, an unterminated `{user:...` -- is never treated as a
//!   placeholder; the delimiting braces are entity-escaped instead so no
//!   downstream consumer can ever reinterpret them as placeholder syntax.
//! - **Per-sink escaping** ([`sink::Sink`]): chat sinks (Twitch plaintext,
//!   Discord markdown/mention escaping) and the overlay HTML sink each
//!   apply their own escaping rules to both literal text and resolved
//!   names -- never one shared escaper assumed safe everywhere.
//! - **Batched, cached name resolution** ([`resolver::NameResolver`],
//!   [`cache::NameCache`]): one batched lookup per `render` call (not one
//!   per mention), a per-tenant TTL cache (default 5 minutes per spec),
//!   with immediate invalidation on rename or erasure rather than waiting
//!   out the TTL.
//! - **Fail-safe on the unknown**: an erased, unknown, or resolver-error
//!   user always renders as the fixed neutral label
//!   ([`resolver::NEUTRAL_LABEL`]) -- never the raw UUID, never a blank
//!   string, never a propagated error that could tempt a caller to fall
//!   back to unrendered text.

mod cache;
mod grammar;
mod render;
mod resolver;
mod sink;

pub use cache::{CacheConfig, NameCache};
pub use render::Detokenizer;
pub use resolver::{NameResolver, ResolveError, NEUTRAL_LABEL};
pub use sink::Sink;
