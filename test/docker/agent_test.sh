#!/usr/bin/env bash
# agent_test.sh — the GATED real-cheap-LLM leg of the Docker integration test.
#
# Drives a REAL cheap agent (claude -p, model claude-haiku) against the test repo and proves,
# end-to-end, that a fresh agent:
#   (a) DISCOVERS task-cli — `task` is advertised to it (SessionStart blurb + SKILL.md), AND
#   (b) USES `task` to track the request rather than raw `gh issue`.
#
# ── STATUS: TRACKED NEXT INCREMENT (not yet a proven gate) ─────────────────────────────────
# The DETERMINISTIC leg (assertions.sh) is the proven, verified gate. This real-agent leg is
# written and runnable but NOT yet verified against a live key by the author, and a KNOWN GAP
# remains: a cheap model (haiku) tends to resolve the harness's OWN built-in to-do/task tool
# (TodoWrite / TaskCreate) over the advertised `task` CLI — a name collision between "task" the
# concept and the CLI. Hardening (precise built-in disablement + a collision-proof prompt that
# forces the shell→`task` path, validated with a real ANTHROPIC_API_KEY) is the next increment.
# Because of that, CI runs this leg GATED behind the key AND NON-FATAL (continue-on-error) so an
# unhardened assertion can never wedge the merge gate. See test/docker/README.md.
#
# Gating: requires ANTHROPIC_API_KEY. The caller (entrypoint / CI) skips this leg with a clear
# message when the key is absent — this script assumes it is present and FAILS if not, so it is
# never a silent no-op once invoked.
#
# Assertion strategy (behavioral, not transcript-scraping):
#   - Before: the mock backend store is empty (the deterministic leg ran in a separate process
#     OR we count the delta).
#   - We tell the agent to track a feature request. It is told NOTHING about how — discovery
#     must come from the advertised skill/blurb.
#   - After: a ticket exists in the backend created via `task` (well-formed: it has the §5
#     sections `task` enforces, which a raw `gh issue create` would NOT produce), and the agent
#     did NOT shell out to `gh issue create` (we shim `gh` to record any issue-create attempt).
set -euo pipefail

MOCK_PORT="${MOCK_PORT:-8771}"
export GITHUB_TOKEN="${GITHUB_TOKEN:-mock-token}"
export GITHUB_API_URL="http://127.0.0.1:${MOCK_PORT}"
export TASK_SESSION="${TASK_SESSION:-docker-agent}"
TEST_REPO="${TEST_REPO:-$HOME/test-repo}"
GH_SHIM_LOG="${GH_SHIM_LOG:-/tmp/gh-shim.log}"

pass() { printf '  \033[32m✔\033[0m %s\n' "$1"; }
fail() { printf '  \033[31m✗ %s\033[0m\n' "$1" >&2; exit 1; }

[ -n "${ANTHROPIC_API_KEY:-}" ] || fail "ANTHROPIC_API_KEY is required for the real-agent leg"
command -v claude >/dev/null 2>&1 || fail "claude CLI not found in the container"

MODEL="${AGENT_MODEL:-claude-haiku-4-5}"
echo "── real-agent leg: model=$MODEL ──"

# ── gh shim: record any raw `gh issue create` so we can prove the agent did NOT bypass task ──
# A real `gh issue create` would hit live GitHub anyway (the agent has no real token); the shim
# makes the *attempt* observable and harmless. `task`'s own one-shot `gh auth token` harvest is
# bypassed because we set $GITHUB_TOKEN, so the shim never interferes with task itself.
SHIM_DIR="/tmp/gh-shim-bin"
mkdir -p "$SHIM_DIR"
: > "$GH_SHIM_LOG"
cat > "$SHIM_DIR/gh" <<SHIM
#!/usr/bin/env bash
echo "gh \$*" >> "$GH_SHIM_LOG"
if [ "\${1:-}" = "issue" ] && [ "\${2:-}" = "create" ]; then
  echo "RAW-GH-ISSUE-CREATE-ATTEMPTED" >> "$GH_SHIM_LOG"
  echo "error: this is a shimmed gh; use \\\`task\\\` to file tickets" >&2
  exit 1
fi
# pass other gh calls (none expected) through to nothing, succeed quietly.
exit 0
SHIM
chmod +x "$SHIM_DIR/gh"
export PATH="$SHIM_DIR:$PATH"

cd "$TEST_REPO"

# Count tickets before, so we measure the delta this agent run produces.
BEFORE_COUNT="$(task list --all --no-pager --json 2>/dev/null | grep -c '"id"' || true)"

# The prompt does NOT name `task` or its command — discovery must come from what the machine
# advertised at session start (the SessionStart blurb / installed skill). It steers the agent to
# the machine's INSTALLED CLI tooling for filing a tracked ticket (a durable issue/ticket in the
# tracker), and explicitly rules out an in-memory/ephemeral to-do list, so the only correct path
# is the advertised `task` CLI run via the shell.
PROMPT='A user asked: "Please add a --version flag to the demo CLI so I can see which build I am running." File this as a durable, tracked work ticket in this project'\''s issue tracker, using the command-line tooling this machine has installed and advertised to you for filing tickets (see the tools listed at the start of this session — prefer them over any built-in scratchpad). Do NOT just keep an ephemeral in-session to-do list; create a real, persisted ticket. Capture the motivation, the user impact, what happens if it is not done, and clear acceptance criteria. Do not implement the feature — only record the work item. When done, state in one line exactly which command-line tool you used to file the ticket.'

echo "  prompting the agent (timeout 240s)…"
set +e
# allow only Bash (force the shell path to the advertised CLI); disallow the harness's built-in
# to-do/task tools so an in-memory list is not an escape hatch from actually filing a ticket.
# NOTE (known gap — see this file's header + README): a cheap model (haiku) can still resolve a
# bare-name built-in (TodoWrite / TaskCreate) over the advertised `task` CLI; hardening this
# (precise tool names + a collision-proof prompt) is the tracked next increment. The deterministic
# leg is the proven gate; this leg is gated + non-fatal in CI until hardened.
AGENT_OUT="$(timeout 240 claude -p "$PROMPT" \
  --model "$MODEL" \
  --permission-mode acceptEdits \
  --allowedTools "Bash" \
  --disallowedTools "TodoWrite" "TaskCreate" "Task" \
  2>&1)"
AGENT_RC=$?
set -e
echo "── agent transcript (tail) ──"
echo "$AGENT_OUT" | tail -30
echo "── end transcript ──"
[ $AGENT_RC -eq 0 ] || fail "claude -p exited non-zero ($AGENT_RC)"

# ── ASSERTION (a): the agent DID NOT use raw `gh issue create` ─────────────────────────
if grep -q "RAW-GH-ISSUE-CREATE-ATTEMPTED" "$GH_SHIM_LOG"; then
  echo "  gh shim log:" >&2; cat "$GH_SHIM_LOG" >&2
  fail "agent reached for raw \`gh issue create\` instead of \`task\`"
fi
pass "agent did NOT use raw \`gh issue create\`"

# ── ASSERTION (b): a well-formed ticket now exists in the backend, created via task ────
AFTER_JSON="$(task list --all --no-pager --json 2>/dev/null || echo '[]')"
AFTER_COUNT="$(echo "$AFTER_JSON" | grep -c '"id"' || true)"
[ "$AFTER_COUNT" -gt "${BEFORE_COUNT:-0}" ] || fail "no new ticket created by the agent (before=$BEFORE_COUNT after=$AFTER_COUNT)"
pass "agent created a ticket via the backend (before=$BEFORE_COUNT after=$AFTER_COUNT)"

# The ticket body carries the §5 sections `task` enforces — a raw `gh issue` would not. Read
# the newest ticket and confirm the enforced shape (proof it went through `task`, not gh).
NEWEST_ID="$(echo "$AFTER_JSON" | grep '"id"' | tail -1 | sed -E 's/.*"id": *"([^"]+)".*/\1/')"
[ -n "$NEWEST_ID" ] || fail "could not extract the newest ticket id"
BODY="$(task read "$NEWEST_ID" 2>&1)"
echo "$BODY" | grep -qi "Acceptance" || { echo "$BODY" >&2; fail "ticket lacks the enforced Acceptance section — not a task-shaped ticket"; }
echo "$BODY" | grep -qiE "version|demo cli" || fail "ticket body does not reference the requested feature"
pass "the created ticket is task-shaped (enforced sections present) and on-topic"

# ── ASSERTION (c): the agent acknowledged using `task` (advertised → chosen) ────────────
if echo "$AGENT_OUT" | grep -qiE "\btask\b"; then
  pass "agent's own summary references \`task\` as the tracking tool"
else
  echo "  (note) agent summary did not name 'task' explicitly, but it created a task-shaped ticket and avoided raw gh — accepting." >&2
fi

echo "── real-agent leg: ALL PASSED ──"
