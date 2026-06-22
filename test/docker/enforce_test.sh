#!/usr/bin/env bash
# enforce_test.sh — the DETERMINISTIC (no-LLM) require-ticket ENFORCEMENT leg (issue #19).
#
# The advertised+usable leg (assertions.sh) proves `task` is discoverable and works. This leg
# proves the OTHER half of the discipline on a clean-rig machine: the strict
# `require-ticket-before-commit` agent-hook (agent-tools#95, now strict + rig-provisioned)
# actually BLOCKS a commit that carries no ticket reference, and ALLOWS one that does.
#
# Why deterministic (no LLM, runs on every PR): the hook is a pure function of the commit command
# + repo state — feed it the same JSON event the Claude Code hook-bridge feeds it (the git-commit
# command in `args.command`) and check its exit code + stdout. No key, no agent, no flakiness.
#
# The hook itself is the VENDORED clean-rig copy (require_ticket_before_commit.py next to this
# script — see its SYNC header). The cross-repo contract both sides keep is the STABLE marker
# `[require-ticket] BLOCKED: no ticket reference` + exit 10 on a block, exit 0 / decision:allow
# on a pass. This leg asserts exactly that, then proves the ticketed commit actually LANDS in git.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOOK="$HERE/require_ticket_before_commit.py"
# This leg is self-contained: it builds its own hermetic gate-fixture repo (below) and does NOT
# read $TEST_REPO, so its verdict can't be perturbed by how the shared test repo is checked out.

# The block marker + exit code are the cross-repo contract (kept in sync with the agent-tools hook).
BLOCK_MARKER="[require-ticket] BLOCKED: no ticket reference"
BLOCK_EXIT=10

pass() { printf '  \033[32m✔\033[0m %s\n' "$1"; }
fail() { printf '  \033[31m✗ %s\033[0m\n' "$1" >&2; exit 1; }

echo "── deterministic enforcement: require-ticket gate BLOCKS ticketless, ALLOWS ticketed ──"

[ -f "$HOOK" ] || fail "vendored require-ticket hook not found ($HOOK)"
command -v python3 >/dev/null 2>&1 || fail "python3 not on PATH (needed to run the hook)"
command -v git >/dev/null 2>&1 || fail "git not on PATH (needed for the gate-fixture repo)"

# ── hermetic gate-fixture repo (self-contained — owns all the git state this leg needs) ──────────
# The hook consults the CURRENT BRANCH NAME (a ticket id often rides there, e.g. feature/ENG-12),
# so a fixture branch named like a ticket would make a "ticketless" commit wrongly ALLOWED and break
# the block assertion. We therefore run every hook call against a throwaway repo on a deliberately
# NEUTRAL, ticket-free branch (`gate-fixture`) with a known initial commit — so the verdict depends
# ONLY on the commit message under test, never on the ambient repo/branch. This keeps the leg truly
# deterministic regardless of how the surrounding container is checked out.
GATE_REPO="$(mktemp -d)"
trap 'rm -rf "$GATE_REPO"' EXIT
(
  cd "$GATE_REPO"
  # `git init -b <branch>` needs git >= 2.28; set the initial branch portably instead (works on any
  # git): init, then point HEAD at the neutral branch before the first commit.
  git init -q
  git symbolic-ref HEAD refs/heads/gate-fixture
  git config user.email tester@example.com
  git config user.name tester
  git config commit.gpgsign false
  printf '# gate fixture\n' > README.md
  git add -A
  # core.hooksPath=/dev/null so no ambient pre-commit can interfere with the fixture's setup commit
  # (the fresh repo has none anyway; /dev/null is the unambiguous "no hooks dir").
  git -c core.hooksPath=/dev/null commit -q -m "chore: init gate fixture"
)
[ -n "$(git -C "$GATE_REPO" rev-parse --verify HEAD 2>/dev/null)" ] || fail "gate fixture repo has no initial commit"
# Sanity: the fixture branch must NOT itself look like a ticket ref (else the block test is invalid).
case "$(git -C "$GATE_REPO" rev-parse --abbrev-ref HEAD)" in
  *[0-9]*) fail "gate-fixture branch name looks ticket-shaped — would mask the ticketless-block test";;
esac

# run_hook <git-commit-command> → sets HOOK_OUT (stdout), HOOK_ERR (stderr), HOOK_RC (exit code).
# Builds the v1 event the way the CC hook-bridge does: the command in args.command + cwd=$GATE_REPO
# (the neutral fixture, so branch-name detection can't smuggle in a ticket ref). Strict is the
# default; set it explicitly so the test is independent of the ambient env. Stderr is CAPTURED (not
# discarded) so a failing assertion can surface the hook's traceback — a red CI is otherwise opaque.
run_hook() {
  local cmd="$1" payload errfile
  payload="$(python3 - "$cmd" "$GATE_REPO" <<'PY'
import json, sys
print(json.dumps({"args": {"command": sys.argv[1]}, "cwd": sys.argv[2]}))
PY
)"
  errfile="$(mktemp)"
  set +e
  HOOK_OUT="$(printf '%s' "$payload" | REQUIRE_TICKET_STRICT=1 python3 "$HOOK" 2>"$errfile")"
  HOOK_RC=$?
  set -e
  HOOK_ERR="$(cat "$errfile")"; rm -f "$errfile"
}

# fail_with_hook <message> — fail, dumping the hook's stdout AND stderr so a red CI is diagnosable.
fail_with_hook() {
  echo "  hook stdout: $HOOK_OUT" >&2
  [ -n "${HOOK_ERR:-}" ] && echo "  hook stderr: $HOOK_ERR" >&2
  fail "$1"
}

# ── 1. A ticketless commit is BLOCKED (exit 10 + the stable marker) ─────────────────────
run_hook 'git commit -m "feat: add export"'
[ "$HOOK_RC" -eq "$BLOCK_EXIT" ] || fail_with_hook "ticketless commit not blocked (exit=$HOOK_RC, expected $BLOCK_EXIT)"
case "$HOOK_OUT" in
  *"$BLOCK_MARKER"*) : ;;
  *) fail_with_hook "block message missing the stable marker '$BLOCK_MARKER'";;
esac
echo "$HOOK_OUT" | grep -q '"decision": *"block"' || fail_with_hook "hook did not emit decision:block"
pass "ticketless commit BLOCKED (exit $BLOCK_EXIT + marker '$BLOCK_MARKER')"

# ── 2. A commit referencing a ticket (Closes #1) is ALLOWED (exit 0, no marker) ─────────
run_hook 'git commit -m "feat: add export (Closes #1)"'
[ "$HOOK_RC" -eq 0 ] || fail_with_hook "ticketed commit not allowed (exit=$HOOK_RC, expected 0)"
echo "$HOOK_OUT" | grep -q '"decision": *"allow"' || fail_with_hook "hook did not emit decision:allow for a ticketed commit"
case "$HOOK_OUT" in
  *"$BLOCK_MARKER"*) fail_with_hook "allow path leaked the BLOCK marker (it must appear ONLY on a real block)";;
esac
pass "ticketed commit ALLOWED (Closes #1 → exit 0, no marker)"

# ── 3. A task-created ticket id also satisfies the gate (proves the task→commit loop) ───
# A real `task new` id is `#N` — the same shape the gate accepts. Reference it like an agent would.
run_hook 'git commit -m "feat: implement the tracked work (task #1)"'
[ "$HOOK_RC" -eq 0 ] || fail_with_hook "task-id commit not allowed (exit=$HOOK_RC)"
pass "task-created ticket id (task #1) ALLOWED"

# ── 4. An exempt chore commit is ALLOWED even WITHOUT a ticket (gate doesn't over-block) ─
# The contract is "BLOCKS ticketless AND ALLOWS ticketed" — but a strict gate that blocked EVERY
# ticketless commit (incl. trivial chores) would over-enforce. Prove a `chore:` commit with no
# ticket is allowed, so the gate enforces the discipline without wedging exempt work.
run_hook 'git commit -m "chore: bump lockfile"'
[ "$HOOK_RC" -eq 0 ] || fail_with_hook "exempt chore commit wrongly blocked (exit=$HOOK_RC)"
case "$HOOK_OUT" in *"$BLOCK_MARKER"*) fail_with_hook "exempt chore leaked the BLOCK marker";; esac
pass "exempt chore commit ALLOWED without a ticket (no over-block)"

# ── 5. LANDING MECHANICS: a ticketed commit is a usable commit that lands in git ────────
# Steps 1–4 prove the gate's VERDICT by invoking the hook directly — that IS the enforcement, because
# require-ticket is an AGENT-hook (intercepts the agent's tool call), NOT a git-hook in core.hooksPath.
# So this step does NOT re-run the gate (it can't, from a raw `git commit`); it only confirms the
# allowed-path commit is well-formed and actually lands — stage a change, commit with a ticket ref in
# the hermetic fixture (guaranteed to have an initial commit). core.hooksPath=/dev/null disables any
# ambient git hooks for THIS commit so it tests landing mechanics, not whatever pre-commit the
# host/container has wired. The enforcement verdict itself is already proven in steps 1–4.
cd "$GATE_REPO"
BEFORE_HEAD="$(git rev-parse HEAD)"
printf '\n# enforcement-leg marker: a tracked change\n' >> README.md
git add -A
git -c core.hooksPath=/dev/null commit -q -m "feat: add enforcement marker (Closes #1)" \
  || fail "ticketed git commit failed to land"
AFTER_HEAD="$(git rev-parse HEAD)"
[ "$BEFORE_HEAD" != "$AFTER_HEAD" ] || fail "ticketed commit did not advance HEAD"
git log -1 --pretty=%s | grep -q "Closes #1" || fail "landed commit does not carry the ticket reference"
pass "ticketed commit is well-formed and LANDS in the fixture repo (HEAD advanced, ref present)"

echo "── deterministic enforcement: ALL PASSED ──"
