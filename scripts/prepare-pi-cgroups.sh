#!/usr/bin/env bash
# Configure memory cgroups on Raspberry Pi for k3s
set -e

CPUINFO_PATH="${CPUINFO_PATH:-/proc/cpuinfo}"
CGROUPS_PATH="${CGROUPS_PATH:-/proc/cgroups}"
CMDLINE_FILE="${CMDLINE_FILE:-}"

# Detect Raspberry Pi
if ! grep -qi raspberry "$CPUINFO_PATH"; then
    exit 0
fi

# Memory cgroup already enabled?
if grep -qE '^memory[[:space:]]+.*[[:space:]]1$' "$CGROUPS_PATH"; then
    exit 0
fi

# Determine cmdline file
if [ -z "$CMDLINE_FILE" ]; then
    if [ -f /boot/firmware/cmdline.txt ]; then
        CMDLINE_FILE=/boot/firmware/cmdline.txt
    elif [ -f /boot/cmdline.txt ]; then
        CMDLINE_FILE=/boot/cmdline.txt
    else
        echo "cmdline.txt not found" >&2
        exit 1
    fi
fi

PARAMS="cgroup_enable=cpuset cgroup_memory=1 cgroup_enable=memory"

# Remove conflicting or duplicate parameters and append required parameters exactly once.
# Use Python instead of sed -i so the script works with both GNU sed and BSD/macOS sed.
python3 - "$CMDLINE_FILE" "$PARAMS" <<'PY'
import re
import sys
from pathlib import Path

cmdline_path = Path(sys.argv[1])
params = sys.argv[2].split()
text = cmdline_path.read_text(encoding="utf-8").strip()
for token in (
    "cgroup_disable=memory",
    "cgroup_enable=cpuset",
    "cgroup_memory=1",
    "cgroup_enable=memory",
):
    text = re.sub(rf"(?<!\S){re.escape(token)}(?!\S)", "", text)
parts = text.split() + params
cmdline_path.write_text(" ".join(parts) + "\n", encoding="utf-8")
PY

# Check if the memory controller is enabled
if grep -qE '^memory[[:space:]]+.*[[:space:]]1$' "$CGROUPS_PATH"; then
    exit 0
fi

echo "Reboot required"
exit 2
