#!/usr/bin/env bash
#
# Headless Codex implementer for the rental-engine.
#
# Hands a written implementation plan to the Codex CLI running non-interactively
# (`codex exec`) and lets it implement + test + commit the code-side work
# autonomously, then leaves the result for the orchestrator (Claude) to verify.
#
# This replaces the unreliable Hermes executor in tri-agent-orchestrate for this
# project: one agent, one prompt, do the whole code track, check the result.
#
# Usage:
#   scripts/codex-implement.sh [PLAN_PATH]
#
# Defaults to the newness-filter plan. PLAN_PATH may be absolute or relative to
# the project root.
#
# What it does NOT do: Track B (n8n schedule, launchd) -- that is host-side and
# runs outside the sandbox. The prompt explicitly scopes Codex to Track A.

set -euo pipefail

# Worktree-friendly root resolution: never hardcode the canonical path.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_ROOT}"

PLAN_PATH="${1:-docs/plans/2026-05-29-newness-filter-and-12h-scrape.md}"
if [[ ! -f "${PLAN_PATH}" ]]; then
  echo "Plan not found: ${PLAN_PATH}" >&2
  exit 1
fi

MODEL="${CODEX_MODEL:-gpt-5.5}"
TS="$(date +%Y%m%d-%H%M%S)"
RUN_DIR="${PROJECT_ROOT}/logs/codex-${TS}"
mkdir -p "${RUN_DIR}"
LAST_MSG="${RUN_DIR}/last-message.md"
EVENTS="${RUN_DIR}/events.jsonl"
LOG="${RUN_DIR}/run.log"

# Write the prompt to a file and feed it via stdin. macOS ships bash 3.2, whose
# parser mis-handles parentheses inside a heredoc nested in $(...). Writing to a
# file with a quoted heredoc sidesteps that bug entirely.
PROMPT_FILE="${RUN_DIR}/prompt.txt"
cat > "${PROMPT_FILE}" <<EOF
You are implementing a written plan in this repository (the rental-engine).

PLAN FILE: ${PLAN_PATH}
Read it in full first.

SCOPE: Implement TRACK A ONLY (Tasks 1 through 4). Do NOT attempt TRACK B
(the n8n schedule / launchd changes) -- that is handled outside this sandbox.

IDEMPOTENCY: Some tasks may already be implemented from a prior run. For each
task, FIRST run its test(s). If a task's test already passes (ALL PASS / exit 0),
that task is already done -- skip it and move to the next task. Only run the
failing-test/implement/green cycle for tasks that are NOT yet done. A test that
passes when the plan said it should fail means the task is already complete; this
is NOT a divergence to stop on.

RULES:
- Follow the plan task-by-task, in order, for the code and test work: write the
  failing test, run it, implement, run it green. Honour the IDEMPOTENCY note.
- This project's tests are plain async scripts run with
  .venv/bin/python tests/<file>.py -- NOT pytest. Use that exact runner.
- DO NOT run any git command. This sandbox cannot write to .git, and the
  orchestrator commits and verifies afterward. SKIP every "Commit" step in the
  plan. Implement all four tasks in sequence and leave the tree uncommitted.
- Do not refactor unrelated code. Do not touch run.py, the scrapers, or the
  scorer. The only files you change are the ones the plan names.
- If a step's actual output differs from the plan's "Expected" (other than a
  Commit step, which you skip), stop and write what happened in your final
  message rather than forcing it green.

When done, run Task 4's full regression sweep and report in your final message:
which tasks you completed, the exact pass/fail summary line of every test file
you ran, and the list of files you created or modified.
EOF

echo "Plan:    ${PLAN_PATH}"
echo "Model:   ${MODEL}"
echo "Sandbox: workspace-write (no network; fixture tests only)"
echo "Logs:    ${RUN_DIR}"
echo "Running codex exec headless..."
echo

# -C            : working root = project
# -s            : sandbox allows writes inside the workspace, blocks network
# approval_policy=never : never pause for confirmation (headless)
# -o            : capture the agent's final message
# --json + tee  : full event stream for post-mortem
codex exec \
  -C "${PROJECT_ROOT}" \
  -m "${MODEL}" \
  -s workspace-write \
  -c approval_policy="never" \
  -o "${LAST_MSG}" \
  --json \
  - < "${PROMPT_FILE}" 2>&1 | tee "${EVENTS}" | tee "${LOG}"

echo
echo "=========================================================="
echo "Codex final message: ${LAST_MSG}"
echo "Event log:           ${EVENTS}"
echo "Next: orchestrator verifies (git log, re-run tests, inspect diff)."
