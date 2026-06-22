#!/usr/bin/env bash
# agent_test.sh — the GATED real-cheap-LLM leg of the Docker integration test.
#
# Drives a REAL cheap agent (claude -p, model claude-haiku) against the test repo and proves,
# end-to-end, that a fresh agent:
#   (a) DISCOVERS task-cli — `task` is advertised to it (SessionStart blurb + SKILL.md), AND
#   (b) USES `task` to track the request rather than raw `gh issue`.
#
# ── STATUS: HARDENED, gated pending a live-key validation run (issue #16) ───────────────────
# The DETERMINISTIC leg (assertions.sh) is the proven, verified gate. This real-agent leg is now
# HARDENED against the known collision: a cheap model (haiku) used to resolve the harness's OWN
# built-in to-do/task tool (TodoWrite / TaskCreate / Task) over the advertised `task` CLI — a name
# collision between "task" the concept and the CLI. Two changes close that:
#   1. TOOL GATING — the canonical, always-present `--disallowedTools TodoWrite TaskCreate Task`
#      (space-separated multi-token form, per `claude --help`) denies the to-do/task built-ins, with
#      `--allowedTools Bash Read` allowing only the shell path. As an extra layer, `--tools "Bash,Read"`
#      (comma form — a DIFFERENT delimiter per the help) restricts the built-in set to a positive
#      allowlist, but it is added ONLY when `claude --help` advertises `--tools`, so an absent/renamed
#      flag can't silently break the whole invocation. The denylist is the gate that always holds.
#   2. COLLISION-PROOF PROMPT — names the affordance concretely (run the `task` SHELL command via
#      Bash) and explicitly forbids any built-in todo/task/scratchpad tool, without spelling out
#      the subcommand (HOW to drive `task` still comes from the advertised skill/blurb).
# The built-in-exclusion is asserted here BEFORE any live call (key-free), so a regression that
# reintroduces a built-in fails fast. What is NOT yet done: a recorded live-key run proving the
# agent reliably lands a backend ticket across a few runs. Until that validation, CI keeps this leg
# GATED behind the key AND NON-FATAL (continue-on-error) so it can never wedge the merge gate; flip
# continue-on-error off once validated. See test/docker/README.md and issue #16.
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
# Collision-proof prompt (issue #16): a cheap model reads "track this work" and reaches for its
# built-in to-do tool because "task" is a loaded word. So the prompt now names the affordance by
# its concrete shape — a `task` SHELL command run via Bash — and explicitly rules out ANY built-in
# todo/task/scratchpad tool. It still does NOT spell out the subcommand/flags (`task new …`);
# discovery of HOW to drive `task` must come from the advertised skill/blurb. What we remove is the
# ambiguity about WHICH affordance to use: the shell `task` CLI, never a built-in.
PROMPT='A user asked: "Please add a --version flag to the demo CLI so I can see which build I am running." File this as a durable, tracked work ticket in this project'\''s issue tracker. IMPORTANT — HOW to file it: run the `task` shell command (the command-line tool this machine installed and advertised to you at session start — see the tools listed there) via the Bash tool. Do NOT use any built-in to-do / task / scratchpad tool (no TodoWrite, no TaskCreate, no in-memory task list) — those do NOT persist a real ticket; only the `task` shell CLI does. Capture the motivation, the user impact, what happens if it is not done, and clear acceptance criteria. Do not implement the feature — only record the work item. When done, state in one line exactly which shell command you ran to file the ticket.'

# ── tool gating (issue #16): structurally exclude the built-in to-do/task tools ──────────────
# The collision is that haiku prefers a BUILT-IN todo/task tool over the advertised `task` CLI. The
# gating CONFIG and its key-free self-check live in agent_tools_gating.sh (the single source of truth,
# also run in the DETERMINISTIC leg so a regression fails a normal PR without a key). Source it to get
# the validated tool-list arrays, then run the self-check before spending a live call.
HERE_AGENT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=test/docker/agent_tools_gating.sh
. "$HERE_AGENT/agent_tools_gating.sh"
assert_agent_tools_gating pass fail || exit 1

# Build the claude argv from the validated arrays. Per `claude --help`, --allowedTools/--disallowedTools
# are SPACE-separated (one tool per argv token), so the lists expand to individual tokens — NOT a single
# comma-joined string (which the parser would read as one literal tool name).
CLAUDE_ARGS=(
  -p "$PROMPT"
  --model "$MODEL"
  --permission-mode acceptEdits
  --allowedTools "${AGENT_ALLOWED[@]}"
  --disallowedTools "${AGENT_DISALLOWED[@]}"
)
# Add the COMMA-separated --tools allowlist ONLY when this claude build documents it as a real flag —
# anchored match so a substring (or a `--tools-foo`) can't falsely enable it and break the invocation.
# Capture --help into a variable FIRST (not `claude --help | grep`): under `set -o pipefail` a
# non-zero exit from `claude --help` would otherwise fail the whole pipe even when the flag IS present,
# falsely dropping --tools. `|| true` keeps a non-zero help exit from tripping `set -e`.
CLAUDE_HELP="$(claude --help 2>/dev/null || true)"
if printf '%s' "$CLAUDE_HELP" | grep -qE -- '(^|[[:space:]])--tools([[:space:]]|,|=|$)'; then
  CLAUDE_ARGS+=(--tools "$AGENT_TOOLS_CSV")
  pass "claude advertises --tools → adding the built-in allowlist ($AGENT_TOOLS_CSV)"
else
  echo "  (note) this claude build does not advertise --tools; relying on --allowedTools/--disallowedTools only." >&2
fi

echo "  prompting the agent (timeout 240s)…"
set +e
AGENT_OUT="$(timeout 240 claude "${CLAUDE_ARGS[@]}" 2>&1)"
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
