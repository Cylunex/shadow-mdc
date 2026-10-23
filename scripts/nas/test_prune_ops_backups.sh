#!/usr/bin/env bash
# Dry-runnable unit check for deploy-mdc.sh ops-backup retention (keep newest 2).
set -Eeuo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
deploy_script="$script_dir/deploy-mdc.sh"
tmp="$(mktemp -d "${TMPDIR:-/tmp}/shadow-mdc-prune-XXXXXX")"
cleanup() { rm -rf -- "$tmp"; }
trap cleanup EXIT

mkdir -p \
  "$tmp/pre-aaaaaaaaaaaa-20260101T010101Z" \
  "$tmp/pre-bbbbbbbbbbbb-20260102T020202Z" \
  "$tmp/pre-cccccccccccc-20260103T030303Z" \
  "$tmp/pre-dddddddddddd-20260104T040404Z" \
  "$tmp/unrelated-keep-me" \
  "$tmp/pre-not-a-real-backup"

# Ensure mtime order: older → newer by touch ascending
touch -d '2026-01-01 01:01:01' "$tmp/pre-aaaaaaaaaaaa-20260101T010101Z"
touch -d '2026-01-02 02:02:02' "$tmp/pre-bbbbbbbbbbbb-20260102T020202Z"
touch -d '2026-01-03 03:03:03' "$tmp/pre-cccccccccccc-20260103T030303Z"
touch -d '2026-01-04 04:04:04' "$tmp/pre-dddddddddddd-20260104T040404Z"

echo "== dry-run =="
SHADOW_MDC_BACKUP_ROOT="$tmp" SHADOW_MDC_BACKUP_KEEP=2 \
  bash "$deploy_script" --prune-backups --dry-run \
  | tee "$tmp/dry-run.out"

# Dry-run must not delete
test -d "$tmp/pre-aaaaaaaaaaaa-20260101T010101Z"
test -d "$tmp/pre-dddddddddddd-20260104T040404Z"

echo "== prune keep=2 =="
SHADOW_MDC_BACKUP_ROOT="$tmp" SHADOW_MDC_BACKUP_KEEP=2 \
  bash "$deploy_script" --prune-backups

# Newest 2 by mtime remain
test -d "$tmp/pre-dddddddddddd-20260104T040404Z"
test -d "$tmp/pre-cccccccccccc-20260103T030303Z"
# Older matching pre-* gone
test ! -e "$tmp/pre-bbbbbbbbbbbb-20260102T020202Z"
test ! -e "$tmp/pre-aaaaaaaaaaaa-20260101T010101Z"
# Unrelated / non-matching names untouched
test -d "$tmp/unrelated-keep-me"
test -d "$tmp/pre-not-a-real-backup"

echo "OK: prune keeps newest 2 pre-<commit>-<stamp> dirs; leaves unrelated alone"
