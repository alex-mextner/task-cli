#!/usr/bin/env bash
# agent_tools_gating.sh — the tool-gating CONFIG + its key-free self-check (issue #16).
#
# This is the single source of truth for HOW the real-agent leg gates the cheap model onto the
# `task` shell CLI and away from the harness's built-in to-do/task tools (the TodoWrite/TaskCreate/
# Task collision). It is intentionally split out of agent_test.sh so the CONFIG can be validated
# WITHOUT a live LLM call — and, crucially, so that validation runs in the DETERMINISTIC leg (which
# the entrypoint runs on every PR with no key), not hidden behind the credentialed + continue-on-error
# agent leg. A regression that drops a built-in from the denylist (or leaks one into the allowlist)
# therefore fails a normal PR, fast and key-free — which is the part of #16 provable without a key.
#
# Two usages:
#   - `source agent_tools_gating.sh`            → defines the arrays + assert_agent_tools_gating()
#   - `bash   agent_tools_gating.sh --check`    → runs the self-check standalone, exits non-zero on fail
#
# Flag delimiters (per `claude --help`, two DIFFERENT conventions — do not mix them):
#   - --allowedTools / --disallowedTools : SPACE-separated, one tool per argv token (example in the
#     help is "Bash(git *) Edit"). So the denylist is THREE argv tokens, not one CSV string.
#   - --tools                            : COMMA-separated ("Bash,Edit,Read"). Non-canonical; added by
#     agent_test.sh only when `claude --help` actually advertises it.

# The canonical, always-present gate.
AGENT_ALLOWED=(Bash Read)                       # --allowedTools (space-separated): only the shell path
AGENT_DISALLOWED=(TodoWrite TaskCreate Task)    # --disallowedTools (space-separated): deny the built-ins
# The extra positive-allowlist layer (comma form). agent_test.sh adds it iff claude advertises --tools.
AGENT_TOOLS_CSV="Bash,Read"
# The built-in to-do/task tools the gate must always exclude (the collision set).
AGENT_BUILTIN_BLOCKLIST=(TodoWrite TaskCreate Task)

_gating_in_list() { local needle="$1"; shift; local x; for x in "$@"; do [ "$x" = "$needle" ] && return 0; done; return 1; }

# assert_agent_tools_gating <pass-fn> <fail-fn> — validate the config against the tool-list ARRAYS
# (the source of truth — NOT a flattened argv string, which also holds $PROMPT and would false-pass).
# <pass-fn>/<fail-fn> are the caller's reporters so output matches the surrounding leg's style.
assert_agent_tools_gating() {
  local pass_fn="$1" fail_fn="$2" builtin
  [ "${#AGENT_DISALLOWED[@]}" -ge "${#AGENT_BUILTIN_BLOCKLIST[@]}" ] \
    || { "$fail_fn" "denylist shrank below the ${#AGENT_BUILTIN_BLOCKLIST[@]} built-in to-do/task tools"; return 1; }
  for builtin in "${AGENT_BUILTIN_BLOCKLIST[@]}"; do
    _gating_in_list "$builtin" "${AGENT_DISALLOWED[@]}" \
      || { "$fail_fn" "built-in '$builtin' missing from --disallowedTools"; return 1; }
    _gating_in_list "$builtin" "${AGENT_ALLOWED[@]}" \
      && { "$fail_fn" "built-in '$builtin' leaked into --allowedTools"; return 1; }
    case ",$AGENT_TOOLS_CSV," in *",$builtin,"*) "$fail_fn" "built-in '$builtin' leaked into --tools allowlist"; return 1;; esac
  done
  "$pass_fn" "built-in to-do/task tools excluded from agent gating (allowed='${AGENT_ALLOWED[*]}', disallowed='${AGENT_DISALLOWED[*]}')"
  return 0
}

# Standalone self-check mode: `bash agent_tools_gating.sh --check`. Guarded by a direct-execution
# check (BASH_SOURCE[0] == $0) so that SOURCING this file — even from a script whose own $1 happens to
# be "--check" — never triggers the self-check or an exit; only running it directly does.
if [ "${BASH_SOURCE[0]}" = "${0}" ] && [ "${1:-}" = "--check" ]; then
  _gp() { printf '  \033[32m✔\033[0m %s\n' "$1"; }
  _gf() { printf '  \033[31m✗ %s\033[0m\n' "$1" >&2; }
  assert_agent_tools_gating _gp _gf || exit 1
fi
