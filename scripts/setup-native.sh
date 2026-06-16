#!/usr/bin/env bash
# setup-native.sh
# Idempotent setup for rental-engine native macOS deployment.
# Safe to re-run. Does not modify any Docker containers or volumes.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
VENV_DIR="${PROJECT_ROOT}/.venv"
DATA_DIR="${PROJECT_ROOT}/data"
LOGS_DIR="${PROJECT_ROOT}/logs"
PYTHON="/opt/homebrew/bin/python3.13"

echo "rental-engine native setup"
echo "Project root: ${PROJECT_ROOT}"
echo ""

# Step 1: verify Python 3.13
if [[ ! -x "${PYTHON}" ]]; then
  echo "ERROR: ${PYTHON} not found. Install via: brew install python@3.13"
  exit 1
fi
echo "[1/6] Python: $("${PYTHON}" --version)"

# Step 2: create venv if missing
if [[ ! -d "${VENV_DIR}" ]]; then
  echo "[2/6] Creating venv at ${VENV_DIR}"
  "${PYTHON}" -m venv "${VENV_DIR}"
else
  echo "[2/6] Venv already exists at ${VENV_DIR}, skipping creation"
fi

# Step 3: install requirements
echo "[3/6] Installing requirements from ${PROJECT_ROOT}/requirements.txt"
"${VENV_DIR}/bin/pip" install --quiet --upgrade pip
"${VENV_DIR}/bin/pip" install --quiet -r "${PROJECT_ROOT}/requirements.txt"
echo "      Done."

# Step 4: create data/ and logs/ directories
echo "[4/6] Creating data/ and logs/ directories"
mkdir -p "${DATA_DIR}"
mkdir -p "${LOGS_DIR}"

# Step 5: bootstrap the SQLite database schema
# Runs create_tables once. If the DB already exists this is a no-op because
# SQLModel uses CREATE TABLE IF NOT EXISTS under the hood.
echo "[5/6] Bootstrapping SQLite schema at ${DATA_DIR}/homehunt.db"
HOMEHUNT_DB="${DATA_DIR}/homehunt.db" \
OLLAMA_URL="http://127.0.0.1:11434" \
PYTHONPATH="${PROJECT_ROOT}" \
"${VENV_DIR}/bin/python" - <<'EOF'
import os
from homehunt.core.db import Database
# Pass no arg: Database() reads HOMEHUNT_DB from env and builds sqlite:/// URL
db = Database()
db.create_tables()
print("      Schema bootstrapped.")
EOF

# Step 6: make the smoke-test script executable
chmod +x "${SCRIPT_DIR}/smoke-test-native.sh"
echo "[6/6] smoke-test-native.sh marked executable"

echo ""
echo "Setup complete. Next steps:"
echo ""
echo "  1. Copy the plist to LaunchAgents:"
echo "     cp ${PROJECT_ROOT}/launchd/com.anchitsom.rental-engine.plist \\"
echo "        ~/Library/LaunchAgents/com.anchitsom.rental-engine.plist"
echo ""
echo "  2. Validate the plist:"
echo "     PATH=\"/opt/homebrew/bin:\$PATH\" plutil -lint \\"
echo "        ~/Library/LaunchAgents/com.anchitsom.rental-engine.plist"
echo ""
echo "  3. (Optional) Copy existing DB from Docker volume before stopping the container:"
echo "     docker cp rental-engine:/data/homehunt.db ${DATA_DIR}/homehunt.db"
echo ""
echo "  4. Stop the Docker container to free port 8000:"
echo "     docker stop rental-engine"
echo ""
echo "  5. Load and start the launchd service:"
echo "     launchctl load -w ~/Library/LaunchAgents/com.anchitsom.rental-engine.plist"
echo ""
echo "  6. Smoke test:"
echo "     ${SCRIPT_DIR}/smoke-test-native.sh"
echo ""
echo "  Rollback (if smoke test fails):"
echo "     launchctl unload ~/Library/LaunchAgents/com.anchitsom.rental-engine.plist"
echo "     docker start rental-engine"
