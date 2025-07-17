#!/usr/bin/env bash
set -e

tmpdir=$(mktemp -d)
CPUINFO=$tmpdir/cpuinfo
CGROUPS=$tmpdir/cgroups
CMDLINE=$tmpdir/cmdline.txt

echo "Intel" > "$CPUINFO"
: > "$CGROUPS"
: > "$CMDLINE"

CPUINFO_PATH="$CPUINFO" CGROUPS_PATH="$CGROUPS" CMDLINE_FILE="$CMDLINE" bash scripts/prepare-pi-cgroups.sh
[ $? -eq 0 ]

# Scenario: Pi without memory cgroup
echo "Raspberry Pi" > "$CPUINFO"
echo -e "memory\t0\t1\t0" > "$CGROUPS"
echo "console=ttyAMA0" > "$CMDLINE"

set +e
CPUINFO_PATH="$CPUINFO" CGROUPS_PATH="$CGROUPS" CMDLINE_FILE="$CMDLINE" bash scripts/prepare-pi-cgroups.sh
rc=$?
set -e
[ $rc -eq 2 ]
grep -q "cgroup_memory=1" "$CMDLINE"
count=$(grep -o "cgroup_memory=1" "$CMDLINE" | wc -l)
[ "$count" -eq 1 ]

# Running again without reboot should not duplicate
set +e
CPUINFO_PATH="$CPUINFO" CGROUPS_PATH="$CGROUPS" CMDLINE_FILE="$CMDLINE" bash scripts/prepare-pi-cgroups.sh
rc=$?
set -e
[ $rc -eq 2 ]
count=$(grep -o "cgroup_memory=1" "$CMDLINE" | wc -l)
[ "$count" -eq 1 ]

# After reboot (memory cgroup enabled)
echo -e "memory\t0\t1\t1" > "$CGROUPS"
CPUINFO_PATH="$CPUINFO" CGROUPS_PATH="$CGROUPS" CMDLINE_FILE="$CMDLINE" bash scripts/prepare-pi-cgroups.sh
[ $? -eq 0 ]

rm -r "$tmpdir"
