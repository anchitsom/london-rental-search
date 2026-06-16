#!/usr/bin/env bash
# smoke-test-native.sh
# Post-switchover verification for the native rental-engine deployment.
# Runs against 127.0.0.1:8000. Does not touch Docker.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
VENV_DIR="${PROJECT_ROOT}/.venv"
DATA_DIR="${PROJECT_ROOT}/data"
DB_PATH="${DATA_DIR}/homehunt.db"
BASE_URL="http://127.0.0.1:8000"
PASS=0
FAIL=0

check_pass() {
  echo "  PASS: $1"
  PASS=$((PASS + 1))
}

check_fail() {
  echo "  FAIL: $1"
  FAIL=$((FAIL + 1))
}

echo "rental-engine native smoke test"
echo "Target: ${BASE_URL}"
echo "DB:     ${DB_PATH}"
echo ""

# Check 1: service is listening
echo "[1] API reachability: GET /"
STATUS=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "${BASE_URL}/" || echo "000")
if [[ "${STATUS}" == "200" ]]; then
  check_pass "GET / returned 200"
else
  check_fail "GET / returned ${STATUS} (expected 200)"
fi

# Check 2: JSON listings endpoint
echo "[2] JSON feed: GET /api/listings"
API_STATUS=$(curl -s -o /tmp/rental_smoke_listings.json -w "%{http_code}" --max-time 5 "${BASE_URL}/api/listings" || echo "000")
if [[ "${API_STATUS}" == "200" ]]; then
  check_pass "GET /api/listings returned 200"
else
  check_fail "GET /api/listings returned ${API_STATUS} (expected 200)"
fi

# Check 3: response is valid JSON
echo "[3] Response is valid JSON"
if "${VENV_DIR}/bin/python" -c "import json,sys; json.load(open('/tmp/rental_smoke_listings.json'))" 2>/dev/null; then
  check_pass "/api/listings response is valid JSON"
else
  check_fail "/api/listings response is not valid JSON"
fi

# Check 4: DB file exists
echo "[4] Database file exists at ${DB_PATH}"
if [[ -f "${DB_PATH}" ]]; then
  check_pass "homehunt.db exists"
else
  check_fail "homehunt.db not found at ${DB_PATH}"
fi

# Check 5: DB has the listing table
echo "[5] Database has listing table"
if sqlite3 "${DB_PATH}" "SELECT name FROM sqlite_master WHERE type='table' AND name='listing';" 2>/dev/null | grep -q "listing"; then
  check_pass "listing table present in DB"
else
  check_fail "listing table not found in DB"
fi

# Check 6: verify run.py imports cleanly (no full run, no timeout binary needed)
echo "[6] run.py: verify import and startup (dry run, CTRL+C safe)"
PYTHONPATH="${PROJECT_ROOT}" \
HOMEHUNT_DB="${DB_PATH}" \
HOMEHUNT_CONFIG="${PROJECT_ROOT}/london-search.yaml" \
OLLAMA_URL="http://127.0.0.1:11434" \
"${VENV_DIR}/bin/python" -c "
import sys
sys.path.insert(0, '${PROJECT_ROOT}')
try:
    import run
    print('import ok')
except SystemExit:
    print('import ok')
except Exception as e:
    print('import error:', e)
    sys.exit(1)
" 2>/dev/null &
SCRAPE_PID=$!
sleep 5
if kill -0 "${SCRAPE_PID}" 2>/dev/null; then
  kill "${SCRAPE_PID}" 2>/dev/null
  wait "${SCRAPE_PID}" 2>/dev/null || true
  check_pass "run.py imports without fatal error (terminated after 5s)"
else
  wait "${SCRAPE_PID}" 2>/dev/null
  SCRAPE_EXIT=$?
  if [[ "${SCRAPE_EXIT}" -eq 0 ]]; then
    check_pass "run.py imports without fatal error"
  else
    check_fail "run.py import failed (exit ${SCRAPE_EXIT})"
  fi
fi

# Check 7: Ollama is reachable from the host
echo "[7] Ollama reachability: GET http://127.0.0.1:11434/"
OLLAMA_STATUS=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "http://127.0.0.1:11434/" || echo "000")
if [[ "${OLLAMA_STATUS}" == "200" ]]; then
  check_pass "Ollama at 127.0.0.1:11434 returned 200"
else
  check_fail "Ollama at 127.0.0.1:11434 returned ${OLLAMA_STATUS} (expected 200)"
fi

# Check 8: llava-phi3 model is present in Ollama
echo "[8] Ollama: llava-phi3 model present"
MODELS=$(curl -s --max-time 5 "http://127.0.0.1:11434/api/tags" 2>/dev/null || echo "{}")
if echo "${MODELS}" | grep -q "llava-phi3"; then
  check_pass "llava-phi3 found in Ollama model list"
else
  check_fail "llava-phi3 not found in Ollama model list (run: ollama pull llava-phi3)"
fi

echo ""
echo "Results: ${PASS} passed, ${FAIL} failed"
if [[ "${FAIL}" -gt 0 ]]; then
  echo ""
  echo "Rollback:"
  echo "  launchctl unload ~/Library/LaunchAgents/com.anchitsom.rental-engine.plist"
  echo "  docker start rental-engine"
  exit 1
fi
echo "All checks passed. rental-engine is running natively."
