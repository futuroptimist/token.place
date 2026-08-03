pub const DEFAULT_QWEN_64K_BATCH_PROFILE: &str = "balanced";

pub fn default_qwen_64k_batch_profile() -> String {
    DEFAULT_QWEN_64K_BATCH_PROFILE.to_string()
}

pub fn normalize_qwen_64k_batch_profile(value: &str) -> String {
    match value {
        "safe" | "balanced" | "experimental" => value.to_string(),
        _ => default_qwen_64k_batch_profile(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn missing_or_invalid_values_never_enable_experimental() {
        assert_eq!(normalize_qwen_64k_batch_profile(""), "balanced");
        assert_eq!(normalize_qwen_64k_batch_profile("EXPERIMENTAL"), "balanced");
        assert_eq!(
            normalize_qwen_64k_batch_profile("experimental"),
            "experimental"
        );
    }
}
