"use strict";
const counters = ["total_prompt_tokens", "cached_prompt_tokens", "processed_prompt_tokens", "generated_tokens", "elapsed_ms"];
function relayProgressText(progress) {
    if (!progress || progress.phase === "waiting")
        return "Waiting for compute node…";
    if (progress.phase === "preparing")
        return "Preparing request…";
    if (progress.phase === "prefill" && progress.total_prompt_tokens > 0) {
        const percent = Math.floor((progress.processed_prompt_tokens / progress.total_prompt_tokens) * 100);
        return `Processing prompt: ${progress.processed_prompt_tokens.toLocaleString()} of ${progress.total_prompt_tokens.toLocaleString()} tokens (${percent}%)`;
    }
    if (progress.phase === "prefill")
        return "Processing prompt…";
    return `Generating response… ${progress.generated_tokens.toLocaleString()} tokens generated`;
}
function validProgressEnvelope(envelope, requestId, clientKey, lastSequence) {
    const progress = envelope && envelope.api_v1_progress;
    return Boolean(progress && envelope.protocol === "tokenplace_api_v1_relay_e2ee" && envelope.version === 1
        && envelope.request_id === requestId && envelope.client_public_key === clientKey
        && JSON.stringify(Object.keys(envelope).sort()) === JSON.stringify(["api_v1_progress", "client_public_key", "protocol", "request_id", "version"])
        && JSON.stringify(Object.keys(progress).sort()) === JSON.stringify(["cached_prompt_tokens", "elapsed_ms", "generated_tokens", "phase", "processed_prompt_tokens", "schema_version", "sequence", "total_prompt_tokens"])
        && progress.schema_version === 1 && Number.isSafeInteger(progress.sequence) && progress.sequence > lastSequence
        && ["preparing", "prefill", "generating"].includes(progress.phase)
        && counters.every((key) => Number.isSafeInteger(progress[key]) && progress[key] >= 0)
        && (progress.total_prompt_tokens === 0 || (progress.cached_prompt_tokens <= progress.processed_prompt_tokens && progress.processed_prompt_tokens <= progress.total_prompt_tokens)));
}
window.TokenPlaceProgress = { relayProgressText, validProgressEnvelope };
