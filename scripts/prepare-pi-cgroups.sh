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
    if [ -f /boot/cmdline.txt ]; then
        CMDLINE_FILE=/boot/cmdline.txt
    elif [ -f /boot/firmware/cmdline.txt ]; then
        CMDLINE_FILE=/boot/firmware/cmdline.txt
    else
        echo "cmdline.txt not found" >&2
        exit 1
    fi
fi

PARAMS="cgroup_enable=cpuset cgroup_memory=1 cgroup_enable=memory"

if ! grep -q "cgroup_memory=1" "$CMDLINE_FILE"; then
    sed -i "s/\$/ $PARAMS/" "$CMDLINE_FILE"
fi

echo "Reboot required"
exit 2
