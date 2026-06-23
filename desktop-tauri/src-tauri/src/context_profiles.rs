#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ContextProfile {
    pub id: &'static str,
    pub display_label: &'static str,
    pub total_context_tokens: u32,
    pub default_output_reservation_tokens: u32,
    pub enabled: bool,
}

pub const DEFAULT_CONTEXT_TIER: &str = "8k-fast";

pub const CONTEXT_PROFILES: &[ContextProfile] = &[
    ContextProfile {
        id: "8k-fast",
        display_label: "8K Fast",
        total_context_tokens: 8192,
        default_output_reservation_tokens: 512,
        enabled: true,
    },
    ContextProfile {
        id: "64k-full",
        display_label: "64K Full",
        total_context_tokens: 65536,
        default_output_reservation_tokens: 1024,
        enabled: true,
    },
];

pub fn resolve_context_profile(id: &str) -> Option<&'static ContextProfile> {
    let normalized = id.trim();
    CONTEXT_PROFILES
        .iter()
        .find(|profile| profile.enabled && profile.id == normalized)
}

pub fn normalize_context_tier(id: &str) -> String {
    resolve_context_profile(id)
        .map(|profile| profile.id.to_string())
        .unwrap_or_else(|| DEFAULT_CONTEXT_TIER.to_string())
}
