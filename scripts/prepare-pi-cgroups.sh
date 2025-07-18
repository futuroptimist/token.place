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
if grep -qE '^memory\s+.*\s1$' "$CGROUPS_PATH"; then
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

# Remove conflicting or duplicate parameters
sed -i -e 's/\<cgroup_disable=memory\>//g' \
       -e 's/\<cgroup_enable=cpuset\>//g' \
       -e 's/\<cgroup_memory=1\>//g' \
       -e 's/\<cgroup_enable=memory\>//g' "$CMDLINE_FILE"
# Collapse multiple spaces and trim trailing whitespace
sed -i -e 's/  */ /g' -e 's/[[:space:]]*$//' "$CMDLINE_FILE"

# Append the required parameters exactly once
sed -i "s/\$/ $PARAMS/" "$CMDLINE_FILE"

# Check if the memory controller is enabled
if grep -qE '^memory\s+.*\s1$' "$CGROUPS_PATH"; then
    exit 0
fi

echo "Reboot required"
exit 2
