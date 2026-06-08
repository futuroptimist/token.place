#!/usr/bin/env bash
# Provision the tiny GGUF model used by CI's real relay landing-page desktop-bridge guardrail.

set -euo pipefail

MODEL_PATH="${1:-${TOKENPLACE_REAL_E2E_MODEL_PATH:-}}"
MODEL_REV="99dd1a73db5a37100bd4ae633f4cfce6560e1567"
MODEL_SHA256="6151b1929d7f5aa3385d9ddef3393e55587c0a55de661562322bc51dfda93a04"
MODEL_URL="https://huggingface.co/ggml-org/tiny-llamas/resolve/${MODEL_REV}/stories15M-q4_0.gguf"

if [ -z "$MODEL_PATH" ]; then
    echo "Error: model path argument or TOKENPLACE_REAL_E2E_MODEL_PATH is required" >&2
    exit 1
fi

case "$MODEL_PATH" in
    /*) ;;
    *)
        echo "Error: TOKENPLACE_REAL_E2E_MODEL_PATH must be absolute, got: $MODEL_PATH" >&2
        exit 1
        ;;
esac

mkdir -p "$(dirname "$MODEL_PATH")"

verify_sha256() {
    local file_path=$1
    if command -v sha256sum >/dev/null 2>&1; then
        printf '%s  %s\n' "$MODEL_SHA256" "$file_path" | sha256sum --check
    else
        local actual_sha
        actual_sha="$(shasum -a 256 "$file_path" | awk '{print $1}')"
        if [ "$actual_sha" != "$MODEL_SHA256" ]; then
            echo "Error: SHA256 mismatch for $file_path" >&2
            echo "Expected: $MODEL_SHA256" >&2
            echo "Actual:   $actual_sha" >&2
            exit 1
        fi
    fi
}

if [ ! -s "$MODEL_PATH" ]; then
    # Keep this guardrail real (not mocked) while staying lightweight: a tiny
    # GGUF still exercises end-to-end llama.cpp inference behavior.
    tmp_path="${MODEL_PATH}.tmp"
    rm -f "$tmp_path"
    curl -fL "$MODEL_URL" -o "$tmp_path"
    verify_sha256 "$tmp_path"
    mv "$tmp_path" "$MODEL_PATH"
else
    verify_sha256 "$MODEL_PATH"
fi

test -s "$MODEL_PATH"
echo "Provisioned tiny GGUF model at $MODEL_PATH"
