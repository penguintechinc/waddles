//! The strict, single-pass placeholder grammar: `{user:<uuid-v4-or-v5>}`,
//! nothing looser (spec S10.4). No regex crate, no backtracking -- a
//! hand-rolled byte scanner that makes the "single-pass, non-recursive"
//! property structurally obvious rather than an invariant callers have to
//! trust.

use uuid::Uuid;

/// One segment of a scanned payload, in original-string order. Walking a
/// `Vec<Segment>` left to right and never looking back at already-emitted
/// output is what makes [`crate::render::Detokenizer::render`]
/// non-recursive: a resolved name is escaped and appended once, it is
/// never re-fed through [`scan`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum Segment<'a> {
    /// Literal bundle/guest-authored text, byte-for-byte from the input,
    /// with no placeholder recognized inside it. Still subject to
    /// per-sink escaping by the caller -- this module only classifies,
    /// it never escapes.
    Literal(&'a str),
    /// A fully-grammar-matched `{user:<uuid>}` placeholder, parsed to its
    /// `Uuid` value for batched resolution.
    Placeholder(Uuid),
}

/// The exact placeholder prefix. Case-sensitive and space-sensitive by
/// design (spec: "nothing looser") -- host-emitted placeholders are always
/// this exact literal; anything else is guest text that merely resembles
/// the grammar and must not be misidentified.
const PREFIX: &str = "{user:";
/// Length of the canonical hyphenated UUID text form,
/// `xxxxxxxx-xxxx-Nxxx-yxxx-xxxxxxxxxxxx`.
const UUID_LEN: usize = 36;

/// Scans `input` once, left to right, splitting it into [`Segment`]s. Every
/// byte of `input` appears in exactly one segment, in order -- this
/// function never revisits a byte range once it has classified it, and
/// never re-scans a `Placeholder`'s resolved value (there isn't one yet;
/// resolution happens later, in [`crate::render`]).
pub(crate) fn scan(input: &str) -> Vec<Segment<'_>> {
    let bytes = input.as_bytes();
    let mut segments = Vec::new();
    let mut literal_start = 0usize;
    let mut i = 0usize;

    while i < bytes.len() {
        // `{` is single-byte ASCII (0x7B); it can never appear as a
        // continuation byte of a multi-byte UTF-8 sequence, so scanning
        // for it at the byte level is safe over `&str` input and every
        // match boundary below lands on a valid `char` boundary.
        if bytes[i] == b'{' {
            if let Some((uuid, match_len)) = try_match_placeholder(bytes, i) {
                if literal_start < i {
                    segments.push(Segment::Literal(&input[literal_start..i]));
                }
                segments.push(Segment::Placeholder(uuid));
                i += match_len;
                literal_start = i;
                continue;
            }
        }
        i += 1;
    }

    if literal_start < bytes.len() {
        segments.push(Segment::Literal(&input[literal_start..]));
    }
    segments
}

/// Attempts to match the strict grammar starting at `bytes[start]` (which
/// the caller has already confirmed is `{`). Returns the parsed `Uuid` and
/// the total matched byte length (`start..start+match_len`) on success.
/// Never backtracks and never consumes more than one candidate placeholder
/// per call -- a failed match consumes nothing, leaving the caller to
/// advance past the single `{` and keep scanning.
fn try_match_placeholder(bytes: &[u8], start: usize) -> Option<(Uuid, usize)> {
    let prefix_end = start + PREFIX.len();
    if bytes.len() < prefix_end {
        return None;
    }
    if &bytes[start..prefix_end] != PREFIX.as_bytes() {
        return None;
    }

    let uuid_end = prefix_end + UUID_LEN;
    if bytes.len() <= uuid_end || bytes[uuid_end] != b'}' {
        return None;
    }

    let candidate = std::str::from_utf8(&bytes[prefix_end..uuid_end]).ok()?;
    let uuid = parse_v4_or_v5_shape(candidate)?;

    // Total match: `{user:` + 36-char uuid + `}`.
    let match_len = PREFIX.len() + UUID_LEN + 1;
    Some((uuid, match_len))
}

/// Validates `candidate` against the canonical lowercase hyphenated UUID
/// shape with an RFC 4122 variant nibble AND a version nibble of `4` or
/// `5` -- "uuid-v4-or-v5-shape" per spec, matching the two ways this
/// platform ever mints a placeholder's identity (S10.3: a canonical
/// `hub_users` UUID is v4; an unlinked-mention ephemeral pseudonym is
/// `UUIDv5(WADDLES_MENTION_NAMESPACE, ...)`). Any other version (a forged
/// or malformed token) fails the grammar entirely rather than being
/// accepted and then rejected at resolution time -- it is treated as
/// ordinary literal text, per [`scan`]'s doc.
fn parse_v4_or_v5_shape(candidate: &str) -> Option<Uuid> {
    let b = candidate.as_bytes();
    if b.len() != UUID_LEN {
        return None;
    }
    // Dash positions for 8-4-4-4-12.
    for &pos in &[8usize, 13, 18, 23] {
        if b[pos] != b'-' {
            return None;
        }
    }
    let is_lower_hex = |c: u8| c.is_ascii_digit() || (b'a'..=b'f').contains(&c);
    for (idx, &c) in b.iter().enumerate() {
        if [8, 13, 18, 23].contains(&idx) {
            continue;
        }
        if !is_lower_hex(c) {
            return None;
        }
    }
    // Version nibble: first char of the third group (index 14).
    let version = b[14];
    if version != b'4' && version != b'5' {
        return None;
    }
    // RFC 4122 variant nibble: first char of the fourth group (index 19)
    // must be one of 8/9/a/b.
    let variant = b[19];
    if !matches!(variant, b'8' | b'9' | b'a' | b'b') {
        return None;
    }
    Uuid::parse_str(candidate).ok()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn v4() -> String {
        "11111111-1111-4111-8111-111111111111".to_string()
    }

    fn v5() -> String {
        "22222222-2222-5222-9222-222222222222".to_string()
    }

    #[test]
    fn scans_a_single_valid_placeholder() {
        let input = format!("hi {{user:{}}}!", v4());
        let segs = scan(&input);
        assert_eq!(
            segs,
            vec![
                Segment::Literal("hi "),
                Segment::Placeholder(Uuid::parse_str(&v4()).unwrap()),
                Segment::Literal("!"),
            ]
        );
    }

    #[test]
    fn scans_v5_shape_too() {
        let input = format!("{{user:{}}}", v5());
        let segs = scan(&input);
        assert_eq!(
            segs,
            vec![Segment::Placeholder(Uuid::parse_str(&v5()).unwrap())]
        );
    }

    #[test]
    fn rejects_not_a_uuid() {
        let input = "{user:not-a-uuid}";
        let segs = scan(input);
        assert_eq!(segs, vec![Segment::Literal(input)]);
    }

    #[test]
    fn rejects_wrong_version_nibble() {
        // Version nibble '1' (not 4/5) -- structurally UUID-shaped but not
        // one this platform ever mints; treated as literal text entirely,
        // including the hex body (S10.4's "no UUID survives" guarantee
        // only covers real v4/v5-shaped placeholders -- see module doc).
        let input = "{user:11111111-1111-1111-8111-111111111111}";
        let segs = scan(input);
        assert_eq!(segs, vec![Segment::Literal(input)]);
    }

    #[test]
    fn rejects_wrong_variant_nibble() {
        let input = "{user:11111111-1111-4111-0111-111111111111}";
        let segs = scan(input);
        assert_eq!(segs, vec![Segment::Literal(input)]);
    }

    #[test]
    fn rejects_uppercase_hex() {
        // Strict grammar: lowercase only, "nothing looser".
        let input = "{user:11111111-1111-4111-8111-11111111111A}";
        let segs = scan(input);
        assert_eq!(segs, vec![Segment::Literal(input)]);
    }

    #[test]
    fn rejects_unterminated_placeholder() {
        let input = "{user:11111111-1111-4111-8111-111111111111";
        let segs = scan(input);
        assert_eq!(segs, vec![Segment::Literal(input)]);
    }

    #[test]
    fn nested_placeholder_resolves_only_the_inner_valid_one() {
        let input = format!("{{user:{{user:{}}}}}", v4());
        let segs = scan(&input);
        // Outer `{user:` is literal (its own grammar attempt fails because
        // the next byte is `{`, not a hex digit); the inner placeholder
        // matches; the leftover trailing `}` is literal.
        assert_eq!(
            segs,
            vec![
                Segment::Literal("{user:"),
                Segment::Placeholder(Uuid::parse_str(&v4()).unwrap()),
                Segment::Literal("}"),
            ]
        );
    }

    #[test]
    fn two_placeholders_in_one_string_each_resolve_independently() {
        let input = format!("from {{user:{}}} to {{user:{}}}", v4(), v5());
        let segs = scan(&input);
        assert_eq!(
            segs,
            vec![
                Segment::Literal("from "),
                Segment::Placeholder(Uuid::parse_str(&v4()).unwrap()),
                Segment::Literal(" to "),
                Segment::Placeholder(Uuid::parse_str(&v5()).unwrap()),
            ]
        );
    }

    #[test]
    fn empty_input_scans_to_no_segments() {
        assert_eq!(scan(""), vec![]);
    }

    #[test]
    fn lone_braces_are_literal() {
        let input = "cost is {5} and {}";
        assert_eq!(scan(input), vec![Segment::Literal(input)]);
    }
}
