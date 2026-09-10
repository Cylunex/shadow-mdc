#!/usr/bin/env bash
# Incremental local → NAS catalog sync (fallback path).
#
# Preferred when NAS cannot reach JavDB: seed on the box (FANZA rankings), then
# run this hop. Also usable after any local catalog edits that must merge to NAS.
# NAS direct seed remains fine when JavDB is reachable there.
#
# Flow:
#   1) export_catalog_bundle.py --incremental → /workspace/exports/…
#   2) rsync light bundle to NAS imports/
#   3) supervisorctl stop shadow-mdc
#   4) import_catalog_bundle.py merge on NAS
#   5) supervisorctl start shadow-mdc
#
# Usage:
#   ./scripts/sync_catalog_to_nas.sh
#   ./scripts/sync_catalog_to_nas.sh --dry-run
#   SOURCE_DATA_DIR=data NAS_HOST=nas ./scripts/sync_catalog_to_nas.sh
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE_DATA_DIR="${SOURCE_DATA_DIR:-$ROOT/data}"
SOURCE_DB="${SOURCE_DATABASE:-$SOURCE_DATA_DIR/shadow-mdc.db}"
EXPORTS_ROOT="${EXPORTS_ROOT:-/workspace/exports}"
NAS_HOST="${NAS_HOST:-nas}"
NAS_PROJECT="${NAS_PROJECT:-/data/project/shadow-mdc}"
NAS_DATA_DIR="${NAS_DATA_DIR:-$NAS_PROJECT/shared/data}"
NAS_IMPORTS="${NAS_IMPORTS:-$NAS_PROJECT/shared/imports}"
NAS_SERVICE="${NAS_SERVICE:-shadow-mdc}"
TARGET_DATA_DIR="${TARGET_DATA_DIR:-$NAS_DATA_DIR}"
PYTHON_BIN="${PYTHON_BIN:-$ROOT/.venv/bin/python}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BUNDLE_DIR="$EXPORTS_ROOT/shadow-mdc-daily-chart-incr-$STAMP"
DRY_RUN=0

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    -h|--help)
      sed -n '2,20p' "$0"
      exit 0
      ;;
    *)
      echo "unknown arg: $arg" >&2
      exit 2
      ;;
  esac
done

if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="$(command -v python3)"
fi
if [[ ! -f "$SOURCE_DB" ]]; then
  echo "source database not found: $SOURCE_DB" >&2
  exit 3
fi

mkdir -p "$EXPORTS_ROOT" "$BUNDLE_DIR"

echo "==> incremental export → $BUNDLE_DIR"
(
  cd "$ROOT"
  PYTHONPATH=src "$PYTHON_BIN" scripts/export_catalog_bundle.py \
    --source-data-dir "$SOURCE_DATA_DIR" \
    --source-database "$SOURCE_DB" \
    --output "$BUNDLE_DIR" \
    --target-data-dir "$TARGET_DATA_DIR" \
    --incremental
)

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "dry-run: skipping rsync / NAS import / service restart"
  echo "bundle ready at $BUNDLE_DIR"
  exit 0
fi

echo "==> ensure NAS imports dir"
ssh "$NAS_HOST" "mkdir -p '$NAS_IMPORTS'"

REMOTE_BUNDLE="$NAS_IMPORTS/$(basename "$BUNDLE_DIR")"
echo "==> rsync bundle → $NAS_HOST:$REMOTE_BUNDLE"
rsync -a --delete "$BUNDLE_DIR/" "$NAS_HOST:$REMOTE_BUNDLE/"

echo "==> stop $NAS_SERVICE on NAS"
ssh "$NAS_HOST" "supervisorctl stop '$NAS_SERVICE'"

echo "==> merge-import on NAS"
ssh "$NAS_HOST" bash -s <<REMOTE
set -Eeuo pipefail
cd '$NAS_PROJECT/current'
SHADOW_MDC_DATA_DIR='$NAS_DATA_DIR' \
SHADOW_MDC_DATABASE_URL='sqlite:///$NAS_DATA_DIR/shadow-mdc.db' \
PYTHONPATH=src .venv/bin/python scripts/import_catalog_bundle.py \
  --bundle '$REMOTE_BUNDLE' \
  --data-dir '$NAS_DATA_DIR' \
  --database-url 'sqlite:///$NAS_DATA_DIR/shadow-mdc.db'
REMOTE

echo "==> start $NAS_SERVICE on NAS"
ssh "$NAS_HOST" "supervisorctl start '$NAS_SERVICE'"

echo "==> health check"
ssh "$NAS_HOST" "curl -fsS 'http://127.0.0.1:8700/api/health' || true"

echo "done: synced $BUNDLE_DIR → NAS"
echo
echo "NOTE: preferred daily path is seeding on NAS directly:"
echo "  ssh nas 'cd /data/project/shadow-mdc/current && \\"
echo "    SHADOW_MDC_DATA_DIR=/data/project/shadow-mdc/shared/data \\"
echo "    SHADOW_MDC_DATABASE_URL=sqlite:////data/project/shadow-mdc/shared/data/shadow-mdc.db \\"
echo "    PYTHONPATH=src .venv/bin/python scripts/seed_daily_chart.py --limit 10'"
