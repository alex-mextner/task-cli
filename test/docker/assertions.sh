#!/usr/bin/env bash
# assertions.sh — the DETERMINISTIC (no-LLM) leg of the Docker integration test.
#
# Proves, against a clean rig-style provisioning, that task-cli is:
#   ADVERTISED — the real `task install-skill` wrote the SKILL.md + SessionStart blurb, and
#                the SessionStart hook surfaces the `task` blurb into a fresh session.
#   USABLE     — `task new`/`task list` round-trip through the REAL github-issues backend code
#                path (urllib → REST) against the hermetic mock GitHub server.
#
# No LLM, no real token, no network — runs in normal CI. Exits non-zero on the first failure.
set -euo pipefail

MOCK_PORT="${MOCK_PORT:-8771}"
export GITHUB_TOKEN="${GITHUB_TOKEN:-mock-token}"
export GITHUB_API_URL="http://127.0.0.1:${MOCK_PORT}"
export TASK_SESSION="${TASK_SESSION:-docker-smoke}"
SKILLS_ROOT="$HOME/.agents/skills"
BLURBS_DIR="$SKILLS_ROOT/.blurbs"
TEST_REPO="${TEST_REPO:-$HOME/test-repo}"

pass() { printf '  \033[32m✔\033[0m %s\n' "$1"; }
fail() { printf '  \033[31m✗ %s\033[0m\n' "$1" >&2; exit 1; }

echo "── deterministic assertions: ADVERTISED + USABLE ──"

# ── 1. ADVERTISED: install-skill wrote both artifacts ──────────────────────────────────
[ -f "$SKILLS_ROOT/task/SKILL.md" ] || fail "SKILL.md not written by install-skill ($SKILLS_ROOT/task/SKILL.md)"
grep -q "name: task" "$SKILLS_ROOT/task/SKILL.md" || fail "SKILL.md missing the 'name: task' trigger frontmatter"
pass "SKILL.md present with trigger frontmatter"

[ -f "$BLURBS_DIR/task.md" ] || fail "SessionStart blurb not written ($BLURBS_DIR/task.md)"
grep -q "task" "$BLURBS_DIR/task.md" || fail "blurb does not mention 'task'"
pass "SessionStart blurb present"

# ── 2. ADVERTISED: the SessionStart hook SURFACES the task blurb ────────────────────────
# Run the exact hook command a real Claude Code machine runs (cats every .blurbs/*.md). The
# `task` line must appear in what a fresh session would see.
SESSION_OUTPUT="$(sh -c 'd="$HOME/.agents/skills/.blurbs"; ls "$d"/*.md >/dev/null 2>&1 && { printf "Agent CLI tools installed on this machine (prefer them):\n"; cat "$d"/*.md; }')"
echo "$SESSION_OUTPUT" | grep -qi "Agent CLI tools installed" || fail "SessionStart hook produced no preamble"
echo "$SESSION_OUTPUT" | grep -q "\`task\`" || fail "SessionStart output does not advertise \`task\`"
echo "$SESSION_OUTPUT" | grep -qi "ticket interface" || fail "task blurb body missing from SessionStart output"
pass "SessionStart hook surfaces the \`task\` blurb (advertised at session start)"

# ── 3. USABLE: task new creates a ticket via the real backend → mock GitHub ─────────────
cd "$TEST_REPO"
NEW_JSON="$(task new \
  --title "Add a --version flag to the demo CLI" \
  --why "users need to know which build they are running" \
  --impact "without it, bug reports omit the version and triage stalls" \
  --if-not-done "support keeps guessing versions; repro is unreliable" \
  --acceptance "running 'demo --version' prints the semver and exits 0" \
  --json)"
echo "$NEW_JSON" | grep -q '"id": "#1"' || { echo "$NEW_JSON" >&2; fail "task new did not return issue #1"; }
echo "$NEW_JSON" | grep -q "github.com/mock/mock/issues/1" || fail "task new url not from the mock backend"
pass "task new created a ticket via the real github-issues backend (mock)"

# ── 4. USABLE: task list reads it back (session-scoped) from the backend ────────────────
LIST_OUT="$(task list --no-pager)"
echo "$LIST_OUT" | grep -q "#1" || { echo "$LIST_OUT" >&2; fail "task list did not return the created ticket"; }
echo "$LIST_OUT" | grep -qi "version flag" || fail "task list output missing the ticket title"
pass "task list read the ticket back from the backend"

# ── 5. USABLE: task read shows the full ticket body (read is never paged) ───────────────
READ_OUT="$(task read '#1' 2>&1)"
echo "$READ_OUT" | grep -qi "Acceptance" || fail "task read missing the Acceptance section"
pass "task read returned the full ticket body"

echo "── deterministic assertions: ALL PASSED ──"
