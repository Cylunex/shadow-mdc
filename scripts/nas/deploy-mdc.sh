#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

project_root="${SHADOW_MDC_ROOT:-/data/project/shadow-mdc}"
script_root="${SHADOW_MDC_SCRIPT_ROOT:-/data/project/script}"
backup_root="${SHADOW_MDC_BACKUP_ROOT:-/data/project/.ops-backups/shadow-mdc}"
cache_root="${SHADOW_MDC_CACHE_ROOT:-/data/project/.deploy-cache/shadow-mdc.git}"
repo_url="${SHADOW_MDC_REPO_URL:-https://github.com/Cylunex/shadow-mdc.git}"
branch="${SHADOW_MDC_BRANCH:-main}"
service_name="${SHADOW_MDC_SERVICE:-shadow-mdc}"
health_url="${SHADOW_MDC_HEALTH_URL:-http://127.0.0.1:8700/api/health}"
proxy_url="${SHADOW_MDC_PROXY_URL:-http://127.0.0.1/mdc/}"

releases_root="$project_root/releases"
shared_root="$project_root/shared"
data_dir="$shared_root/data"
current_link="$project_root/current"
lock_file="$script_root/.deploy-mdc.lock"

for command_name in curl flock git mktemp pnpm python3 supervisorctl tar uv; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "missing required command: $command_name" >&2
    exit 10
  fi
done

if [[ $EUID -ne 0 ]]; then
  echo "deploy-mdc.sh must run as root" >&2
  exit 11
fi
if [[ ! "$branch" =~ ^[A-Za-z0-9._/-]+$ ]]; then
  echo "invalid branch name: $branch" >&2
  exit 12
fi
if [[ ! -d "$data_dir" ]]; then
  echo "persistent data directory does not exist: $data_dir" >&2
  exit 13
fi
if [[ -e "$current_link" && ! -L "$current_link" ]]; then
  echo "current path must be a symlink: $current_link" >&2
  exit 14
fi

mkdir -p "$script_root" "$backup_root" "$releases_root" "$(dirname "$cache_root")"
exec 9>"$lock_file"
if ! flock -n 9; then
  echo "another Shadow MDC deployment is already running" >&2
  exit 15
fi

if [[ ! -d "$cache_root/objects" ]]; then
  mkdir -p "$cache_root"
  git init --bare --quiet "$cache_root"
  git --git-dir="$cache_root" remote add origin "$repo_url"
else
  configured_url="$(git --git-dir="$cache_root" remote get-url origin)"
  if [[ "$configured_url" != "$repo_url" ]]; then
    echo "cached repository URL differs from SHADOW_MDC_REPO_URL" >&2
    exit 16
  fi
fi

echo "Fetching latest origin/$branch ..."
fetch_succeeded=false
for attempt in 1 2 3 4; do
  if git -c http.version=HTTP/1.1 \
    --git-dir="$cache_root" \
    fetch --prune --depth=1 origin "refs/heads/$branch"; then
    fetch_succeeded=true
    break
  fi
  echo "Fetch attempt $attempt failed; retrying ..." >&2
  sleep $((attempt * 3))
done
source_archive=""
if [[ "$fetch_succeeded" == true ]]; then
  commit="$(git --git-dir="$cache_root" rev-parse FETCH_HEAD)"
elif [[ "$repo_url" == "https://github.com/Cylunex/shadow-mdc.git" && "$branch" == "main" ]]; then
  echo "Git fetch unavailable; falling back to GitHub API and codeload ..." >&2
  commit="$(
    curl --fail --silent --show-error \
      --retry 5 --retry-all-errors --retry-delay 2 \
      --connect-timeout 15 --max-time 120 \
      https://api.github.com/repos/Cylunex/shadow-mdc/commits/main \
      | python3 -c 'import json, sys; print(json.load(sys.stdin)["sha"])'
  )"
  source_archive="$(mktemp /tmp/shadow-mdc-source.XXXXXX.tar.gz)"
  trap 'rm -f "$source_archive"' EXIT
  curl --fail --silent --show-error --location \
    --retry 5 --retry-all-errors --retry-delay 2 \
    --connect-timeout 15 --max-time 180 \
    --output "$source_archive" \
    "https://codeload.github.com/Cylunex/shadow-mdc/tar.gz/$commit"
else
  echo "unable to fetch origin/$branch after 4 attempts" >&2
  exit 20
fi
if [[ ! "$commit" =~ ^[0-9a-f]{40}$ ]]; then
  echo "remote ref did not resolve to a commit" >&2
  exit 17
fi

current_commit=""
if [[ -f "$current_link/.source-commit" ]]; then
  current_commit="$(<"$current_link/.source-commit")"
fi
if [[ "$current_commit" == "$commit" ]] \
  && curl --fail --silent --show-error "$health_url" >/dev/null \
  && curl --fail --silent --show-error "$proxy_url" >/dev/null; then
  echo "Shadow MDC is already running latest commit $commit"
  exit 0
fi

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
release_dir="$releases_root/${stamp}-${commit:0:12}"
if [[ -e "$release_dir" ]]; then
  echo "release directory already exists: $release_dir" >&2
  exit 18
fi

echo "Preparing release $release_dir ..."
mkdir -p "$release_dir"
if [[ -n "$source_archive" ]]; then
  tar -xzf "$source_archive" --strip-components=1 -C "$release_dir"
else
  git --git-dir="$cache_root" archive "$commit" | tar -x -C "$release_dir"
fi
printf '%s\n' "$commit" > "$release_dir/.source-commit"

uv venv --python 3.12 "$release_dir/.venv"
uv pip install \
  --link-mode=copy \
  --python "$release_dir/.venv/bin/python" \
  "$release_dir"
pnpm --dir "$release_dir/web" install --frozen-lockfile
pnpm --dir "$release_dir/web" build

if [[ ! -f "$release_dir/web/dist/index.html" ]]; then
  echo "frontend build did not create web/dist/index.html" >&2
  exit 19
fi
(
  cd "$release_dir"
  PYTHONPATH="$release_dir/src" "$release_dir/.venv/bin/python" -c "import shadow_mdc.api"
)

previous_release=""
if [[ -L "$current_link" ]]; then
  previous_release="$(readlink -f "$current_link")"
fi
backup_dir="$backup_root/pre-${commit:0:12}-$stamp"
mkdir -p "$backup_dir"
printf '%s\n' "$previous_release" > "$backup_dir/previous-release.txt"
printf '%s\n' "$commit" > "$backup_dir/target-commit.txt"
if [[ -f /etc/supervisor/conf.d/shadow-mdc.conf ]]; then
  cp -a /etc/supervisor/conf.d/shadow-mdc.conf "$backup_dir/"
fi
if [[ -f /etc/nginx/snippets/shadow-mdc.conf ]]; then
  cp -a /etc/nginx/snippets/shadow-mdc.conf "$backup_dir/"
fi

service_stopped=false
activated=false
rollback() {
  status=$?
  trap - ERR INT TERM
  set +e
  echo "Deployment failed; restoring the previous Shadow MDC release." >&2
  if [[ "$activated" == true ]]; then
    supervisorctl stop "$service_name" >/dev/null 2>&1 || true
    if [[ -n "$previous_release" && -d "$previous_release" ]]; then
      rollback_link="${current_link}.rollback.$$"
      ln -s "$previous_release" "$rollback_link"
      mv -Tf "$rollback_link" "$current_link"
    fi
    if [[ -d "$backup_dir/data" ]]; then
      # Keep derived dumps that deploy backups intentionally omit (e.g. r18-dumps).
      preserve_dir="$backup_dir/preserved-derived"
      mkdir -p "$preserve_dir"
      for name in r18-dumps; do
        if [[ -e "$data_dir/$name" ]]; then
          mv "$data_dir/$name" "$preserve_dir/$name"
        fi
      done
      failed_data="$backup_dir/failed-data"
      if [[ -d "$data_dir" ]]; then
        mv "$data_dir" "$failed_data"
      fi
      cp -a "$backup_dir/data" "$data_dir"
      for name in r18-dumps; do
        if [[ -e "$preserve_dir/$name" ]]; then
          rm -rf "$data_dir/$name"
          mv "$preserve_dir/$name" "$data_dir/$name"
        fi
      done
    fi
  fi
  if [[ "$service_stopped" == true ]]; then
    supervisorctl start "$service_name" >/dev/null 2>&1 || true
  fi
  exit "$status"
}
trap rollback ERR INT TERM

echo "Stopping $service_name for a consistent data backup ..."
supervisorctl stop "$service_name"
service_stopped=true
# Exclude large derived dumps; SQLite Online Backup for consistent *.db copies.
backup_shadow_mdc_data() {
  local src="$1"
  local dest="$2"
  local py_bin="$3"
  mkdir -p "$dest"
  rsync -a \
    --exclude 'r18-dumps/' \
    --exclude 'r18-dumps' \
    --exclude '*.db-wal' \
    --exclude '*.db-shm' \
    "$src/" "$dest/"
  "$py_bin" - "$src" "$dest" <<'PYEOF'
import sqlite3
import sys
from pathlib import Path

src_root = Path(sys.argv[1])
dest_root = Path(sys.argv[2])
for db_path in sorted(src_root.rglob("*.db")):
    rel = db_path.relative_to(src_root)
    if rel.parts and rel.parts[0] == "r18-dumps":
        continue
    target = dest_root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    source = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        dest = sqlite3.connect(target)
        try:
            source.backup(dest)
            dest.commit()
        finally:
            dest.close()
    finally:
        source.close()
    print(f"sqlite-backup {rel}")
PYEOF
}

release_python="$release_dir/.venv/bin/python"
if [[ ! -x "$release_python" ]]; then
  release_python="$(command -v python3)"
fi
backup_shadow_mdc_data "$data_dir" "$backup_dir/data" "$release_python"

activation_link="${current_link}.next.$$"
ln -s "$release_dir" "$activation_link"
mv -Tf "$activation_link" "$current_link"
activated=true

supervisorctl start "$service_name"
healthy=false
for _ in $(seq 1 30); do
  if curl --fail --silent --show-error "$health_url" >/dev/null 2>&1; then
    healthy=true
    break
  fi
  sleep 1
done
if [[ "$healthy" != true ]]; then
  echo "health check did not pass within 30 seconds" >&2
  false
fi
curl --fail --silent --show-error "$proxy_url" >/dev/null
supervisorctl status "$service_name"

trap - ERR INT TERM
echo "Deployed Shadow MDC commit $commit"
echo "Release: $release_dir"
echo "Backup: $backup_dir"
