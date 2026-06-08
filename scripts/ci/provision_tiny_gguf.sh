#!/usr/bin/env bash
set -euo pipefail

MODEL_PATH="${TOKENPLACE_CI_TINY_GGUF_PATH:-.ci-models/stories15M-q4_0.gguf}"
MODEL_REV="99dd1a73db5a37100bd4ae633f4cfce6560e1567"
MODEL_SHA256="6151b1929d7f5aa3385d9ddef3393e55587c0a55de661562322bc51dfda93a04"
MODEL_URL="https://huggingface.co/ggml-org/tiny-llamas/resolve/${MODEL_REV}/stories15M-q4_0.gguf"

mkdir -p "$(dirname "$MODEL_PATH")"

if [ ! -s "$MODEL_PATH" ]; then
    echo "Downloading tiny real GGUF model for relay landing-page desktop-bridge API v1 guardrail..."
    curl -fL "$MODEL_URL" -o "$MODEL_PATH.tmp"
    if command -v sha256sum >/dev/null 2>&1; then
        printf '%s  %s\n' "$MODEL_SHA256" "$MODEL_PATH.tmp" | sha256sum --check
    else
        printf '%s  %s\n' "$MODEL_SHA256" "$MODEL_PATH.tmp" | shasum -a 256 --check
    fi
    mv "$MODEL_PATH.tmp" "$MODEL_PATH"
else
    echo "Using cached tiny real GGUF model at $MODEL_PATH"
    if command -v sha256sum >/dev/null 2>&1; then
        printf '%s  %s\n' "$MODEL_SHA256" "$MODEL_PATH" | sha256sum --check
    else
        printf '%s  %s\n' "$MODEL_SHA256" "$MODEL_PATH" | shasum -a 256 --check
    fi
fi

test -s "$MODEL_PATH"
echo "Tiny real GGUF model is ready at $MODEL_PATH"
