//! Output validation: content type by MAGIC BYTES (never the provider header) and
//! per-kind size caps, plus prompt bounds.

use crate::types::{MediaError, MediaKind};

/// Image output cap (10 MiB).
pub const MAX_IMAGE_BYTES: usize = 10 * 1024 * 1024;
/// Music output cap (20 MiB).
pub const MAX_MUSIC_BYTES: usize = 20 * 1024 * 1024;
/// Video output cap (100 MiB).
pub const MAX_VIDEO_BYTES: usize = 100 * 1024 * 1024;
/// Prompt length bound in chars.
pub const MAX_PROMPT_CHARS: usize = 2000;

/// Size cap for `kind`.
pub fn max_bytes(kind: MediaKind) -> usize {
    match kind {
        MediaKind::Image => MAX_IMAGE_BYTES,
        MediaKind::Music => MAX_MUSIC_BYTES,
        MediaKind::Video => MAX_VIDEO_BYTES,
    }
}

/// A validated output: canonical `(mime, ext)` decided from the bytes.
#[derive(Debug, PartialEq, Eq)]
pub struct Sniffed {
    /// Canonical content type.
    pub mime: &'static str,
    /// File extension.
    pub ext: &'static str,
}

/// Identifies `bytes` as an allowed type for `kind` by magic bytes, or `None`.
pub fn sniff(kind: MediaKind, bytes: &[u8]) -> Option<Sniffed> {
    let s = |mime, ext| Some(Sniffed { mime, ext });
    match kind {
        MediaKind::Image => {
            if bytes.starts_with(&[0x89, b'P', b'N', b'G', 0x0D, 0x0A, 0x1A, 0x0A]) {
                s("image/png", "png")
            } else if bytes.starts_with(&[0xFF, 0xD8, 0xFF]) {
                s("image/jpeg", "jpg")
            } else if bytes.len() >= 12 && &bytes[..4] == b"RIFF" && &bytes[8..12] == b"WEBP" {
                s("image/webp", "webp")
            } else {
                None
            }
        }
        MediaKind::Music => {
            if bytes.starts_with(b"ID3")
                || (bytes.len() >= 2 && bytes[0] == 0xFF && bytes[1] & 0xE0 == 0xE0)
            {
                s("audio/mpeg", "mp3")
            } else if bytes.len() >= 12 && &bytes[..4] == b"RIFF" && &bytes[8..12] == b"WAVE" {
                s("audio/wav", "wav")
            } else if bytes.starts_with(b"OggS") {
                s("audio/ogg", "ogg")
            } else {
                None
            }
        }
        MediaKind::Video => {
            if bytes.len() >= 12 && &bytes[4..8] == b"ftyp" {
                s("video/mp4", "mp4")
            } else if bytes.starts_with(&[0x1A, 0x45, 0xDF, 0xA3]) {
                s("video/webm", "webm")
            } else {
                None
            }
        }
    }
}

/// Validates `bytes` for `kind`: non-empty, within the cap, allowed magic.
pub fn validate_output(kind: MediaKind, bytes: &[u8]) -> Result<Sniffed, MediaError> {
    if bytes.is_empty() {
        return Err(MediaError::ProviderRefused("no_output".into()));
    }
    if bytes.len() > max_bytes(kind) {
        return Err(MediaError::Backend("output_too_large".into()));
    }
    sniff(kind, bytes).ok_or_else(|| MediaError::Backend("output_content_type".into()))
}

/// Validates the prompt: non-blank, <= [`MAX_PROMPT_CHARS`], no control chars
/// other than newline/tab. (PII rejection is the stage's guard, run before this.)
pub fn validate_prompt(prompt: &str) -> Result<(), MediaError> {
    if prompt.trim().is_empty() {
        return Err(MediaError::Invalid("prompt must not be empty".into()));
    }
    if prompt.chars().count() > MAX_PROMPT_CHARS {
        return Err(MediaError::Invalid(format!(
            "prompt exceeds {MAX_PROMPT_CHARS} characters"
        )));
    }
    if prompt
        .chars()
        .any(|c| c.is_control() && c != '\n' && c != '\t')
    {
        return Err(MediaError::Invalid(
            "prompt contains control characters".into(),
        ));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used)]
    use super::*;

    #[test]
    fn magic_bytes_decide_the_type_per_kind() {
        let png = [0x89, b'P', b'N', b'G', 0x0D, 0x0A, 0x1A, 0x0A, 0];
        assert_eq!(sniff(MediaKind::Image, &png).unwrap().ext, "png");
        assert_eq!(
            sniff(MediaKind::Image, &[0xFF, 0xD8, 0xFF, 0xE0])
                .unwrap()
                .ext,
            "jpg"
        );
        assert_eq!(
            sniff(MediaKind::Image, b"RIFF\0\0\0\0WEBPVP8 ")
                .unwrap()
                .ext,
            "webp"
        );
        assert!(sniff(MediaKind::Image, b"RIFF\0\0\0\0WAVEfmt ").is_none());
        assert_eq!(sniff(MediaKind::Music, b"ID3\x04").unwrap().ext, "mp3");
        assert_eq!(
            sniff(MediaKind::Music, &[0xFF, 0xFB, 0x90]).unwrap().ext,
            "mp3"
        );
        assert_eq!(
            sniff(MediaKind::Music, b"RIFF\0\0\0\0WAVEfmt ")
                .unwrap()
                .ext,
            "wav"
        );
        assert_eq!(sniff(MediaKind::Music, b"OggS\0").unwrap().ext, "ogg");
        assert_eq!(
            sniff(MediaKind::Video, b"\0\0\0\x18ftypmp42").unwrap().ext,
            "mp4"
        );
        assert_eq!(
            sniff(MediaKind::Video, &[0x1A, 0x45, 0xDF, 0xA3, 1])
                .unwrap()
                .ext,
            "webm"
        );
        // Cross-kind confusion is refused: a PNG is not a video, an mp4 not an image.
        assert!(sniff(
            MediaKind::Video,
            &[0x89, b'P', b'N', b'G', 0x0D, 0x0A, 0x1A, 0x0A]
        )
        .is_none());
        assert!(sniff(MediaKind::Image, b"\0\0\0\x18ftypmp42").is_none());
        assert!(sniff(MediaKind::Image, b"<svg xmlns=").is_none());
        assert!(sniff(MediaKind::Image, b"").is_none());
    }

    #[test]
    fn output_validation_enforces_size_caps_and_emptiness() {
        let png = [0x89, b'P', b'N', b'G', 0x0D, 0x0A, 0x1A, 0x0A];
        assert!(validate_output(MediaKind::Image, &png).is_ok());
        assert_eq!(
            validate_output(MediaKind::Image, &[]).unwrap_err(),
            MediaError::ProviderRefused("no_output".into())
        );
        let mut big = png.to_vec();
        big.resize(MAX_IMAGE_BYTES + 1, 0);
        assert!(
            matches!(validate_output(MediaKind::Image, &big), Err(MediaError::Backend(m)) if m == "output_too_large")
        );
        big.truncate(MAX_IMAGE_BYTES);
        assert!(
            validate_output(MediaKind::Image, &big).is_ok(),
            "exactly at the cap is allowed"
        );
        assert_eq!(max_bytes(MediaKind::Music), 20 * 1024 * 1024);
        assert_eq!(max_bytes(MediaKind::Video), 100 * 1024 * 1024);
    }

    #[test]
    fn prompt_bounds() {
        assert!(validate_prompt("a\nb\tc").is_ok());
        assert!(validate_prompt(&"x".repeat(MAX_PROMPT_CHARS)).is_ok());
        assert!(validate_prompt(&"x".repeat(MAX_PROMPT_CHARS + 1)).is_err());
        assert!(validate_prompt("").is_err());
        assert!(validate_prompt("a\u{7}b").is_err());
    }
}
