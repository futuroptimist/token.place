use sha2::{Digest, Sha256};

pub fn redact_text_for_log(text: &str) -> String {
    let mut hasher = Sha256::new();
    hasher.update(text.as_bytes());
    let digest = format!("{:x}", hasher.finalize());
    format!("len={} sha256={}", text.len(), &digest[..16])
}

#[cfg(test)]
mod tests {
    use super::redact_text_for_log;

    #[test]
    fn redaction_never_includes_plaintext() {
        let plaintext = "this is sensitive prompt text";
        let redacted = redact_text_for_log(plaintext);
        assert!(!redacted.contains("sensitive"));
        assert!(redacted.contains("len="));
        assert!(redacted.contains("sha256="));
    }
}
