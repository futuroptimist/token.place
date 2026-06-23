use serde::{Deserialize, Serialize};

pub const DEFAULT_CONTEXT_TIER_ID: &str = "8k-fast";

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct ContextProfile {
    pub id: &'static str,
    pub display_label: &'static str,
    pub total_context_tokens: u32,
    pub default_output_token_reservation: u32,
    pub enabled: bool,
}

pub const CONTEXT_PROFILES: &[ContextProfile] = &[
    ContextProfile {
        id: "8k-fast",
        display_label: "8K Fast",
        total_context_tokens: 8_192,
        default_output_token_reservation: 512,
        enabled: true,
    },
    ContextProfile {
        id: "64k-full",
        display_label: "64K Full",
        total_context_tokens: 65_536,
        default_output_token_reservation: 1_024,
        enabled: true,
    },
];

pub fn context_profile(id: &str) -> Option<&'static ContextProfile> {
    CONTEXT_PROFILES
        .iter()
        .find(|profile| profile.enabled && profile.id == id)
}

pub fn normalize_context_tier(id: &str) -> String {
    context_profile(id)
        .map(|profile| profile.id.to_string())
        .unwrap_or_else(|| DEFAULT_CONTEXT_TIER_ID.to_string())
}

pub fn default_context_tier() -> String {
    DEFAULT_CONTEXT_TIER_ID.to_string()
}
