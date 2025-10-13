#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="$ROOT/docs/prompts/codex"

mkdir -p "$DEST"

mapfile -t PROMPT_FILES < <(find "$ROOT" -maxdepth 2 -type f -name '*.md' \
  \( -iname '*codex*.md' -o -iname '*prompt*.md' \) ! -path "$DEST/*" | sort || true)

for SRC in "${PROMPT_FILES[@]}"; do
  NAME="$(basename "$SRC")"
  NEW_NAME="$(python - <<'PY' "$NAME"
import os
import re
import sys

name = sys.argv[1]
stem, ext = os.path.splitext(name)
stem = re.sub(r'(?i)codex', '', stem)
stem = re.sub(r'(?i)prompts?', '', stem)
stem = re.sub(r'[-_]+', '-', stem).strip('-_')
if not stem:
    stem = 'prompt'
print(stem.lower() + ext)
PY
)"
  DEST_PATH="$DEST/$NEW_NAME"
  if [ "$SRC" = "$DEST_PATH" ]; then
    continue
  fi
  if [ -e "$DEST_PATH" ]; then
    echo "Skipping move for $SRC -> $DEST_PATH (destination exists)"
    continue
  fi
  echo "Moving $SRC -> $DEST_PATH"
  mkdir -p "$(dirname "$DEST_PATH")"
  mv "$SRC" "$DEST_PATH"
done

if [ -f "$DEST/chore.md" ] && [ ! -f "$DEST/automation.md" ]; then
  echo "Renaming chore.md -> automation.md"
  mv "$DEST/chore.md" "$DEST/automation.md"
fi
