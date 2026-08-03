pub const DEFAULT_QWEN_64K_BATCH_PROFILE: &str = "balanced";
pub const QWEN_64K_BATCH_PROFILES: [&str; 3] = ["safe", "balanced", "experimental"];

pub fn normalize_qwen_64k_batch_profile(value: &str) -> String {
    if QWEN_64K_BATCH_PROFILES.contains(&value) {
        value.to_string()
    } else {
        DEFAULT_QWEN_64K_BATCH_PROFILE.to_string()
    }
}

pub fn default_qwen_64k_batch_profile() -> String {
    DEFAULT_QWEN_64K_BATCH_PROFILE.to_string()
}
