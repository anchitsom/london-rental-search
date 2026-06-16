#!/usr/bin/env bash
set -euo pipefail

# OpenRent ad-hoc run. Triggered manually via:
#   launchctl kickstart -k gui/$UID/com.anchitsom.openrent-adhoc
# or directly:
#   bash scripts/openrent-adhoc-run.sh

cd "$(dirname "$0")/.."
PROJECT_ROOT="$(pwd)"

# run.py loads .env at import time (DOTENV_PATH override, else .env next to
# run.py), so we do not need to source it here. We do set the pilot defaults.

export HOMEHUNT_DB="${HOMEHUNT_DB:-$PROJECT_ROOT/data/homehunt.db}"
export OPENRENT_MAX_LISTINGS="${OPENRENT_MAX_LISTINGS:-10}"

mkdir -p "$PROJECT_ROOT/logs"
LOG="$PROJECT_ROOT/logs/openrent-adhoc-$(date +%Y%m%d-%H%M%S).log"

exec "$PROJECT_ROOT/.venv/bin/python" -u "$PROJECT_ROOT/scripts/openrent-adhoc-run.py" 2>&1 | tee "$LOG"
