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
#   6) enqueue OpenList offline for today's new daily-chart/hot seeds
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
# Export fingerprint baseline. It only advances after the NAS import succeeded: the export
# writes the new fingerprints to PENDING_STATE and we promote it at the very end. (Before,
# a failed rsync/ssh still advanced the baseline, so works seeded while the NAS was down were
# silently dropped from every later incremental bundle.)
STATE_FILE="${EXPORT_STATE_FILE:-$SOURCE_DATA_DIR/export-manifest.json}"
PENDING_STATE="$EXPORTS_ROOT/$(basename "$BUNDLE_DIR").state.json"
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

mkdir -p "$EXPORTS_ROOT"
# export_catalog_bundle creates BUNDLE_DIR and errors if it already exists
if [[ -f "$STATE_FILE" ]]; then
  cp -p "$STATE_FILE" "$PENDING_STATE"
else
  rm -f "$PENDING_STATE"
fi

echo "==> incremental export → $BUNDLE_DIR"
(
  cd "$ROOT"
  PYTHONPATH=src "$PYTHON_BIN" scripts/export_catalog_bundle.py \
    --source-data-dir "$SOURCE_DATA_DIR" \
    --source-database "$SOURCE_DB" \
    --output "$BUNDLE_DIR" \
    --target-data-dir "$TARGET_DATA_DIR" \
    --state-file "$PENDING_STATE" \
    --incremental
)

if [[ "$DRY_RUN" -eq 1 ]]; then
  rm -f "$PENDING_STATE"
  echo "dry-run: skipping rsync / NAS import / service restart (export baseline unchanged)"
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
# The service may still be starting after the restart; retry for up to ~60s.
ssh "$NAS_HOST" "for i in \$(seq 1 20); do curl -fsS 'http://127.0.0.1:8700/api/health' && exit 0; sleep 3; done; echo 'health check failed after retries' >&2; true"

echo "==> auto-offline newly seeded daily chart/hot titles on NAS"
ssh "$NAS_HOST" bash -s <<REMOTE
set -Eeuo pipefail
cd '$NAS_PROJECT/current'
SHADOW_MDC_DATA_DIR='$NAS_DATA_DIR' \
SHADOW_MDC_DATABASE_URL='sqlite:///$NAS_DATA_DIR/shadow-mdc.db' \
PYTHONPATH=src .venv/bin/python scripts/enqueue_seed_offline.py --today || true
REMOTE

echo "==> advance export baseline"
mv -f "$PENDING_STATE" "$STATE_FILE"

echo "done: synced $BUNDLE_DIR → NAS"
echo
echo "NOTE: preferred daily path is seeding on NAS directly:"
echo "  ssh nas 'cd /data/project/shadow-mdc/current && \\"
echo "    SHADOW_MDC_DATA_DIR=/data/project/shadow-mdc/shared/data \\"
echo "    SHADOW_MDC_DATABASE_URL=sqlite:////data/project/shadow-mdc/shared/data/shadow-mdc.db \\"
echo "    PYTHONPATH=src .venv/bin/python scripts/seed_daily_chart.py --limit 10'"
