#!/bin/bash

set -uo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TESTS_DIR="${PROJECT_ROOT}/tests"
VENV_PYTHON="${PROJECT_ROOT}/.venv/bin/python"

WITH_VISION=0
UNIT_ONLY=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --with-vision)
      WITH_VISION=1
      shift
      ;;
    --unit-only)
      UNIT_ONLY=1
      shift
      ;;
    *)
      echo "Unknown flag: $1" >&2
      echo "Usage: $0 [--with-vision] [--unit-only]" >&2
      exit 1
      ;;
  esac
done

export PYTHONPATH="${PROJECT_ROOT}"
export OLLAMA_URL="http://127.0.0.1:11434"
export VISION_MODEL="${VISION_MODEL:-qwen3-vl:2b-instruct-q8_0}"
export HOMEHUNT_DB="${PROJECT_ROOT}/data/homehunt.db"
export HOMEHUNT_CONFIG="${PROJECT_ROOT}/london-search.yaml"

DOTENV_PATH="${DOTENV_PATH:-${PROJECT_ROOT}/.env}"
if [[ -f "${DOTENV_PATH}" ]]; then
  set +u
  source "${DOTENV_PATH}"
  set -u
fi

passed=0
failed=0
skipped=0

should_skip() {
  local test_file="$1"

  if [[ ${UNIT_ONLY} -eq 1 ]]; then
    case "${test_file}" in
      test_config_loader.py | test_region_resolver.py | test_feature_extractor.py)
        return 1
        ;;
      *)
        return 0
        ;;
    esac
  fi

  if [[ "${test_file}" == "test_stage6_carpet.py" || "${test_file}" == "test_stage6b_carpet_room_aware.py" ]]; then
    if [[ ${WITH_VISION} -eq 0 ]]; then
      return 0
    fi
  fi

  return 1
}

get_skip_reason() {
  local test_file="$1"

  if [[ ${UNIT_ONLY} -eq 1 ]]; then
    case "${test_file}" in
      test_config_loader.py | test_region_resolver.py | test_feature_extractor.py)
        ;;
      *)
        echo "unit-only mode"
        return
        ;;
    esac
  fi

  if [[ "${test_file}" == "test_stage6_carpet.py" || "${test_file}" == "test_stage6b_carpet_room_aware.py" ]]; then
    if [[ ${WITH_VISION} -eq 0 ]]; then
      echo "use --with-vision"
      return
    fi
  fi
}

cd "${TESTS_DIR}"
all_tests=$(find . -name "test_*.py" -type f | sed 's|^\./||' | sort)
cd - > /dev/null

total=$(echo "${all_tests}" | wc -l)

run_count=0
for test_file in ${all_tests}; do
  if ! should_skip "${test_file}"; then
    ((run_count++))
  fi
done

current=0
for test_file in ${all_tests}; do
  if should_skip "${test_file}"; then
    ((skipped++))
    reason=$(get_skip_reason "${test_file}")
    printf "[skip] tests/%s ... SKIP (%s)\n" "${test_file}" "${reason}"
    continue
  fi

  ((current++))

  test_path="${TESTS_DIR}/${test_file}"

  start_time=$(date +%s%N)
  output=$(mktemp)
  exit_code=0

  if "${VENV_PYTHON}" "${test_path}" >"${output}" 2>&1; then
    exit_code=0
  else
    exit_code=$?
  fi

  end_time=$(date +%s%N)
  elapsed_ms=$(( (end_time - start_time) / 1000000 ))
  elapsed_sec=$(echo "scale=1; ${elapsed_ms} / 1000" | bc)

  if [[ ${exit_code} -eq 0 ]]; then
    ((passed++))
    printf "[%d/%d] tests/%s ... PASS (%.1fs)\n" "${current}" "${run_count}" "${test_file}" "${elapsed_sec}"
  else
    ((failed++))
    printf "[%d/%d] tests/%s ... FAIL (%.1fs)\n" "${current}" "${run_count}" "${test_file}" "${elapsed_sec}"
    echo "Last 10 lines of output:"
    tail -10 "${output}" | sed 's/^/  /'
  fi

  rm -f "${output}"
done

echo ""
echo "=========="
if [[ ${skipped} -gt 0 ]]; then
  printf "%d tests, %d passed, %d failed, %d skipped\n" "${total}" "${passed}" "${failed}" "${skipped}"
else
  printf "%d tests, %d passed, %d failed\n" "${total}" "${passed}" "${failed}"
fi
echo "=========="

if [[ ${failed} -gt 0 ]]; then
  exit 1
else
  exit 0
fi
