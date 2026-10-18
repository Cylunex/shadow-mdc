#!/usr/bin/env bash
# Weekly r18.dev dump refresh on NAS (Wed 10:01 Asia/Shanghai).
#
# Crontab (on the NAS, as root — same user that owns supervisor)::
#
#   CRON_TZ=Asia/Shanghai
#   1 10 * * 3 /data/project/shadow-mdc/current/scripts/nas/r18_dump_weekly.sh >>/var/log/shadow-mdc-r18-dump.log 2>&1
#
# Enable with SHADOW_MDC_R18_DUMP_REFRESH_ENABLED=true in the environment
# (supervisor env, this script's env, or release .env). Default is off so a
# failed bot run is stabilized before cron is armed.
#
# Pipeline: catalog counts → supervisorctl stop → download+import →
# enrich (update-only) → supervisorctl start → health → counts (jav_total
# must not grow). Dump files stay under shared/data/r18-dumps/ (never git).
set -Eeuo pipefail

umask 077

project_root="${SHADOW_MDC_ROOT:-/data/project/shadow-mdc}"
current_link="${SHADOW_MDC_CURRENT:-$project_root/current}"
data_dir="${SHADOW_MDC_DATA_DIR:-$project_root/shared/data}"
database_url="${SHADOW_MDC_DATABASE_URL:-sqlite:///$data_dir/shadow-mdc.db}"
proxy_url="${SHADOW_MDC_PROXY_URL:-http://192.168.0.110:7893}"
service_name="${SHADOW_MDC_R18_DUMP_REFRESH_SERVICE:-${SHADOW_MDC_SERVICE:-shadow-mdc}}"
health_url="${SHADOW_MDC_R18_DUMP_REFRESH_HEALTH_URL:-http://127.0.0.1:8700/api/health}"
python_bin="${SHADOW_MDC_PYTHON:-$current_link/.venv/bin/python}"

if [[ ! -d "$current_link" ]]; then
  echo "missing release symlink: $current_link" >&2
  exit 10
fi
if [[ ! -x "$python_bin" ]]; then
  echo "missing python: $python_bin" >&2
  exit 11
fi
if [[ ! -d "$data_dir" ]]; then
  echo "missing data dir: $data_dir" >&2
  exit 12
fi

cd "$current_link"

export SHADOW_MDC_DATA_DIR="$data_dir"
export SHADOW_MDC_DATABASE_URL="$database_url"
export SHADOW_MDC_PROXY_URL="$proxy_url"
# Pass through enable flag / cron docs from env if already set by supervisor/.env.
export SHADOW_MDC_R18_DUMP_REFRESH_SERVICE="$service_name"
export SHADOW_MDC_R18_DUMP_REFRESH_HEALTH_URL="$health_url"

extra_args=()
if [[ "${1:-}" == "--force" ]]; then
  extra_args+=(--force)
fi

exec env PYTHONPATH=src "$python_bin" scripts/run_r18_dump_weekly.py \
  --data-dir "$data_dir" \
  --database-url "$database_url" \
  --proxy "$proxy_url" \
  --service "$service_name" \
  --health-url "$health_url" \
  "${extra_args[@]}"
