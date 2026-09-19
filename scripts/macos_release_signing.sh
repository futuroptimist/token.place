#!/usr/bin/env bash
set -euo pipefail

mode="${1:?usage: macos_release_signing.sh sign-app|notarize-app|notarize-dmg PATH [LOG_DIR]}"
target="${2:?target path is required}"
log_dir="${3:-${RUNNER_TEMP:-/tmp}/token-place-notarization}"

require_env() { [ -n "${!1:-}" ] || { echo "::error::required macOS release credential $1 is not configured" >&2; exit 1; }; }
for name in APPLE_SIGNING_IDENTITY APPLE_NOTARY_KEY_ID APPLE_NOTARY_ISSUER_ID APPLE_NOTARY_KEY_PATH; do require_env "$name"; done
identity="${APPLE_SIGNING_IDENTITY}"
entitlements="${APPLE_RELEASE_ENTITLEMENTS:-desktop-tauri/src-tauri/entitlements.plist}"
mkdir -p "$log_dir"

sign_one() {
  codesign --force --options runtime --timestamp --sign "$identity" "$@"
}

notarize() {
  local artifact="$1" stem="$2" result submission_id
  result="$log_dir/${stem}-submission.json"
  if ! xcrun notarytool submit "$artifact" --key "$APPLE_NOTARY_KEY_PATH" \
      --key-id "$APPLE_NOTARY_KEY_ID" --issuer "$APPLE_NOTARY_ISSUER_ID" \
      --wait --output-format json >"$result"; then
    submission_id="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("id", ""))' "$result" 2>/dev/null || true)"
    if [ -n "$submission_id" ]; then
      xcrun notarytool log "$submission_id" --key "$APPLE_NOTARY_KEY_PATH" --key-id "$APPLE_NOTARY_KEY_ID" \
        --issuer "$APPLE_NOTARY_ISSUER_ID" "$log_dir/${stem}-failure-log.json" || true
    fi
    exit 1
  fi
  submission_id="$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert d.get("status")=="Accepted", d; print(d["id"])' "$result")"
  printf '%s\n' "$submission_id" >"$log_dir/${stem}-submission-id.txt"
}

case "$mode" in
  sign-app)
    [ -d "$target" ] && [ "${target##*.}" = app ] || { echo "invalid app bundle: $target" >&2; exit 1; }
    # Sign executable code inside-out. Never use --deep to sign.
    while IFS= read -r -d '' path; do
      if [ -d "$path" ] || file -b "$path" | grep -q 'Mach-O'; then
        sign_one "$path"
      fi
    done < <(find "$target/Contents" -depth \( -type d \( -name '*.framework' -o -name '*.app' -o -name '*.xpc' -o -name '*.plugin' \) -o -type f \( -perm -111 -o -name '*.dylib' -o -name '*.so' \) \) -print0)
    sign_one --entitlements "$entitlements" "$target"
    codesign --verify --deep --strict --verbose=4 "$target"
    ;;
  notarize-app)
    archive="${RUNNER_TEMP:-/tmp}/token-place-notarization-app.zip"
    ditto -c -k --keepParent "$target" "$archive"
    notarize "$archive" app
    xcrun stapler staple "$target"
    xcrun stapler validate "$target"
    ;;
  notarize-dmg)
    sign_one "$target"
    notarize "$target" dmg
    xcrun stapler staple "$target"
    xcrun stapler validate "$target"
    ;;
  *) echo "unknown mode: $mode" >&2; exit 2 ;;
esac
