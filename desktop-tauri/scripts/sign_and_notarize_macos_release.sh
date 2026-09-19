#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 4 ]]; then
  echo "usage: $0 APP_PATH DMG_PATH DMG_STAGE_DIR NOTARIZATION_LOG_DIR" >&2
  exit 2
fi

app_path="$1"
dmg_path="$2"
dmg_stage_dir="$3"
notary_log_dir="$4"
mkdir -p "${notary_log_dir}"

required_secrets=(
  APPLE_SIGNING_IDENTITY
  APPLE_CERTIFICATE_P12_BASE64
  APPLE_CERTIFICATE_PASSWORD
  APPLE_NOTARY_KEY_P8_BASE64
  APPLE_NOTARY_KEY_ID
  APPLE_NOTARY_ISSUER_ID
)
for name in "${required_secrets[@]}"; do
  if [[ -z "${!name:-}" ]]; then
    echo "::error::Required macOS release secret ${name} is not configured." >&2
    exit 1
  fi
done
if [[ "${APPLE_SIGNING_IDENTITY}" != Developer\ ID\ Application:* ]]; then
  echo "::error::APPLE_SIGNING_IDENTITY must name a Developer ID Application identity." >&2
  exit 1
fi

private_dir="$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/token-place-apple-private.XXXXXX")"
keychain_path="${private_dir}/release-signing.keychain-db"
certificate_path="${private_dir}/developer-id.p12"
notary_key_path="${private_dir}/AuthKey_${APPLE_NOTARY_KEY_ID}.p8"
keychain_password="$(openssl rand -hex 24)"
original_keychains=()
while IFS= read -r keychain; do
  original_keychains+=("${keychain}")
done < <(security list-keychains -d user | sed 's/^[[:space:]]*"//; s/"$//')

cleanup() {
  security list-keychains -d user -s "${original_keychains[@]}" >/dev/null 2>&1 || true
  security delete-keychain "${keychain_path}" >/dev/null 2>&1 || true
  rm -rf "${private_dir}"
}
trap cleanup EXIT

umask 077
printf '%s' "${APPLE_CERTIFICATE_P12_BASE64}" | base64 --decode > "${certificate_path}"
printf '%s' "${APPLE_NOTARY_KEY_P8_BASE64}" | base64 --decode > "${notary_key_path}"
security create-keychain -p "${keychain_password}" "${keychain_path}"
security set-keychain-settings -lut 21600 "${keychain_path}"
security unlock-keychain -p "${keychain_password}" "${keychain_path}"
security import "${certificate_path}" -k "${keychain_path}" -P "${APPLE_CERTIFICATE_PASSWORD}" -T /usr/bin/codesign
security set-key-partition-list -S apple-tool:,apple: -s -k "${keychain_password}" "${keychain_path}" >/dev/null
security list-keychains -d user -s "${keychain_path}" "${original_keychains[@]}"
security find-identity -v -p codesigning "${keychain_path}" | grep -Fq "${APPLE_SIGNING_IDENTITY}"

sign_one() {
  codesign --force --sign "${APPLE_SIGNING_IDENTITY}" --options runtime --timestamp "$1"
}

# Sign every Mach-O leaf first, then nested code bundles, and finally the app.
# Explicit inside-out signing avoids deep signing and its ambiguous mutation behavior.
while IFS= read -r -d '' candidate; do
  if file -b "${candidate}" | grep -q 'Mach-O'; then
    sign_one "${candidate}"
  fi
done < <(find "${app_path}/Contents" -type f -print0)

while IFS= read -r bundle; do
  sign_one "${bundle}"
done < <(find "${app_path}/Contents" -depth -type d \( -name '*.framework' -o -name '*.xpc' -o -name '*.appex' -o -name '*.app' \) -print)
codesign --force --sign "${APPLE_SIGNING_IDENTITY}" --options runtime --timestamp \
  --entitlements "$(dirname "$0")/../src-tauri/Entitlements.plist" "${app_path}"
codesign --verify --deep --strict --verbose=4 "${app_path}"

submit_and_record() {
  local artifact="$1"
  local label="$2"
  local result_path="${notary_log_dir}/${label}-submit.json"
  local submission_id status
  set +e
  xcrun notarytool submit "${artifact}" --wait --output-format json \
    --key "${notary_key_path}" --key-id "${APPLE_NOTARY_KEY_ID}" --issuer "${APPLE_NOTARY_ISSUER_ID}" \
    > "${result_path}"
  local submit_exit=$?
  set -e
  submission_id="$(jq -r '.id // empty' "${result_path}")"
  status="$(jq -r '.status // empty' "${result_path}")"
  printf '%s\n' "${submission_id}" > "${notary_log_dir}/${label}-submission-id.txt"
  if [[ -n "${submission_id}" ]]; then
    xcrun notarytool log "${submission_id}" --output-format json \
      --key "${notary_key_path}" --key-id "${APPLE_NOTARY_KEY_ID}" --issuer "${APPLE_NOTARY_ISSUER_ID}" \
      > "${notary_log_dir}/${label}-log.json" || true
  fi
  if [[ ${submit_exit} -ne 0 || "${status}" != "Accepted" ]]; then
    echo "::error::Apple notarization failed for ${label}; status=${status:-unavailable}, submission=${submission_id:-unavailable}." >&2
    exit 1
  fi
  echo "Apple notarization accepted for ${label}; submission=${submission_id}."
}

app_zip="${private_dir}/app-notarization.zip"
ditto -c -k --keepParent "${app_path}" "${app_zip}"
submit_and_record "${app_zip}" app
xcrun stapler staple "${app_path}"
xcrun stapler validate "${app_path}"

rm -rf "${dmg_stage_dir}"
mkdir -p "${dmg_stage_dir}"
ditto "${app_path}" "${dmg_stage_dir}/$(basename "${app_path}")"
ln -s /Applications "${dmg_stage_dir}/Applications"
rm -f "${dmg_path}"
hdiutil create -volname "token.place desktop" -srcfolder "${dmg_stage_dir}" -ov -format UDZO "${dmg_path}"
sign_one "${dmg_path}"
submit_and_record "${dmg_path}" dmg
xcrun stapler staple "${dmg_path}"
xcrun stapler validate "${dmg_path}"
codesign --verify --strict --verbose=4 "${dmg_path}"
