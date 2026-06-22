#!/usr/bin/env bash
# entrypoint.sh — orchestrates the in-container integration test.
#
# 1. Starts the hermetic mock GitHub server.
# 2. Runs the DETERMINISTIC (no-LLM) legs — always:
#      2a. advertised + usable (assertions.sh)
#      2b. require-ticket ENFORCEMENT — ticketless commit blocked, ticketed passes (enforce_test.sh)
#      2c. real-agent tool-gating config self-check, key-free (agent_tools_gating.sh --check)
# 3. Runs the GATED real-agent leg IFF $ANTHROPIC_API_KEY is set; otherwise SKIPS with a clear
#    message (never a silent pass, never a hard failure for the un-credentialed normal-CI run).
#
# Mode override: RUN_AGENT=never|auto|always (default auto = run iff the key is present).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MOCK_PORT="${MOCK_PORT:-8771}"
RUN_AGENT="${RUN_AGENT:-auto}"
export MOCK_PORT
export TEST_REPO="${TEST_REPO:-$HOME/test-repo}"
# Hermetic backend creds: a dummy token (non-secret) + the localhost mock URL. Exported here so
# the legs and any ad-hoc `task` call resolve the github-issues backend against the mock.
export GITHUB_TOKEN="${GITHUB_TOKEN:-mock-token}"
export GITHUB_API_URL="${GITHUB_API_URL:-http://127.0.0.1:${MOCK_PORT}}"

echo "════════════════════════════════════════════════════════════"
echo " task-cli Docker integration test"
echo "   mock-github port : $MOCK_PORT"
echo "   test repo        : $TEST_REPO"
echo "   run-agent mode   : $RUN_AGENT"
echo "════════════════════════════════════════════════════════════"

# ── 0. sanity: task is on PATH (installed by the image's real install.sh) ──────────────
command -v task >/dev/null 2>&1 || { echo "FATAL: task not on PATH — install.sh did not run" >&2; exit 1; }
task --version >/dev/null 2>&1 || { echo "FATAL: task --version failed" >&2; exit 1; }

# ── 1. start the mock GitHub server ────────────────────────────────────────────────────
python3 "$HERE/mock_github.py" "$MOCK_PORT" &
MOCK_PID=$!
cleanup() { kill "$MOCK_PID" 2>/dev/null || true; }
trap cleanup EXIT

# wait for it to accept connections (max ~10s) using stdlib only
for _ in $(seq 1 50); do
  if python3 - "$MOCK_PORT" <<'PY' 2>/dev/null; then break; fi
import socket, sys
s = socket.socket()
s.settimeout(0.3)
try:
    s.connect(("127.0.0.1", int(sys.argv[1]))); sys.exit(0)
except OSError:
    sys.exit(1)
finally:
    s.close()
PY
  sleep 0.2
done

# ── 2. DETERMINISTIC leg (always) ──────────────────────────────────────────────────────
# 2a. advertised + usable
bash "$HERE/assertions.sh"
# 2b. require-ticket ENFORCEMENT — ticketless commit blocked (exit 10 + marker), ticketed passes.
#     No LLM / key needed, so it runs on every PR alongside 2a.
echo
bash "$HERE/enforce_test.sh"
# 2c. real-agent TOOL-GATING config self-check (issue #16) — the to-do/task built-ins are denied and
#     not allowlisted. Key-free, so it runs HERE (deterministic, every PR) rather than hidden behind
#     the credentialed + continue-on-error agent leg: a regression that un-gates a built-in fails CI.
echo
echo "── deterministic: real-agent tool-gating config self-check (key-free) ──"
bash "$HERE/agent_tools_gating.sh" --check

# ── 3. GATED real-agent leg ────────────────────────────────────────────────────────────
should_run_agent() {
  case "$RUN_AGENT" in
    never)  return 1 ;;
    always) return 0 ;;
    auto)   [ -n "${ANTHROPIC_API_KEY:-}" ] ;;
    *)      echo "WARN: unknown RUN_AGENT='$RUN_AGENT', treating as auto" >&2; [ -n "${ANTHROPIC_API_KEY:-}" ] ;;
  esac
}

if should_run_agent; then
  echo
  echo "── ANTHROPIC_API_KEY present → running the real-agent leg ──"
  bash "$HERE/agent_test.sh"
else
  echo
  echo "────────────────────────────────────────────────────────────"
  echo " SKIP: real-agent leg (no ANTHROPIC_API_KEY / RUN_AGENT=$RUN_AGENT)."
  echo "       The deterministic clean-rig → advertised → usable assertions PASSED."
  echo "       The full real-LLM-agent verification runs on the credentialed merge job."
  echo "────────────────────────────────────────────────────────────"
fi

echo
echo "✔ task-cli Docker integration test: DONE"
