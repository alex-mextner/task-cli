#!/usr/bin/env python3
# SYNC: VENDORED COPY of agent-tools/agent-hooks/require-ticket-before-commit/
#       require_ticket_before_commit.py. The canonical source lives in agent-tools (rig provisions
#       it onto a real machine). This copy is a hermetic FIXTURE for the Docker `test-cli`
#       enforcement leg (test/docker/enforce_test.sh): a clean-rig machine has this exact hook, so
#       the harness ships it to prove the strict ticket gate blocks a ticketless commit end-to-end
#       without depending on agent-tools at image-build time. Keep it byte-identical to the source;
#       the test asserts the STABLE marker `[require-ticket] BLOCKED: no ticket reference` + exit 10,
#       which is the cross-repo contract both sides keep. Re-copy from agent-tools when that hook
#       changes (its own unit tests guard the marker/exit-code contract).
"""agents-hooks/v1 pre-bash hook — require a ticket reference on a commit.

When the agent is about to `git commit`, this checks the commit message (and the
current branch name) for a reference to a tracking ticket — a task-cli id, a
GitHub issue, or a Linear key. If none is found, it BLOCKS by default (strict),
reminding the author that every non-trivial change should start from a ticket with
acceptance criteria + motivation + user-impact.

Enforces the `strict-ticket-discipline` skill. Pairs with task-cli.

Ticket-detection heuristic (intentionally broad — false-negatives nag, they don't
wedge; default fail policy is open/warn so over-detection is the cheap error):
  - `#123`                              GitHub issue / PR number
  - `GH-123`, `org/repo#123`           qualified GitHub references
  - `ABC-123`                          Linear / Jira-style KEY-NUM (>=2 letters)
  - `task:ABC-12`, `task #12`, `T-12`  task-cli ids
  - `Refs: …`, `Closes #…`, `Fixes …`  the conventional trailer keywords
  - a full tracker URL (github.com/.../issues/123, linear.app/.../issue/…)

Exempt from the gate (no ticket expected): trivial-chore commit types
(`chore:`/`docs:`/`style:`/`ci:`/`build:`/`test:`), and `wip`/`fixup!`/`squash!`
/`amend`/merge/revert commits. Configure via env (see README).

Per-commit escapes (mirror the review-gate's REVIEW_SKIP), for the rare legit
ticketless commit:
  - a `[skip-ticket: <reason>]` trailer in the commit message, and
  - an inline `REQUIRE_TICKET_SKIP=1 git commit …` env on the command.
And `REQUIRE_TICKET_STRICT=0` is an explicit opt-out back to warn-only.

Contract (agents-hooks/v1):
  stdin  : JSON event; the shell command is in args.command
  stdout : protocol JSON only
  exit 0 : allow      exit 10 : BLOCK      other : error (host on_error policy)

on_error is "open": a CRASH in the check still fails open — process discipline,
not a security boundary, so a bug must never make committing impossible. But on a
SUCCESSFUL run with no ticket reference and no exemption/escape, the default is now
a hard BLOCK (exit 10). Set REQUIRE_TICKET_STRICT=0 to fall back to warn-only.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys

BLOCK_EXIT_CODE = 10
HOOK_API = "agents-hooks/v1"

# A STABLE marker embedded in every block message — a fixed string the task-cli Docker
# test (and any external assertion) can grep for to confirm THIS gate blocked, regardless
# of how the surrounding advice is reworded. Do not change it casually.
BLOCK_MARKER = "[require-ticket] BLOCKED: no ticket reference"

# Strict is now the DEFAULT: a missing ticket is a hard BLOCK. Set REQUIRE_TICKET_STRICT=0
# (or false/no/off) to opt back out to warn-only. Any other value — including unset — is
# strict. `os.environ.get(...)` here reads the HOOK's process env, which is the right
# scope for a global on/off knob; the per-commit `REQUIRE_TICKET_SKIP=1 git commit` inline
# escape is parsed from the COMMAND string instead (see has_inline_skip).
_STRICT_FALSEY = frozenset({"0", "false", "no", "off"})
STRICT = os.environ.get("REQUIRE_TICKET_STRICT", "1").strip().lower() not in _STRICT_FALSEY

# Per-commit escape: a `[skip-ticket: <reason>]` trailer in the commit message. The reason
# is mandatory (non-empty), so the escape is a deliberate, documented choice — not a blank
# bypass. Mirrors the review-gate's `[skip-review: <reason>]`.
# Require a NON-WHITESPACE reason between the colon and the closing bracket, so a blank
# `[skip-ticket: ]` is not a valid escape (the reason must be a deliberate, real choice).
SKIP_TICKET_TRAILER = re.compile(r"\[skip-ticket:\s*\S[^\]]*\]", re.IGNORECASE)

# A leading `VAR=value` inline-env assignment on the command (`REQUIRE_TICKET_SKIP=1 git
# commit …`). Mirrors the review-gate's inline `REVIEW_SKIP` parse: read from the COMMAND
# the agent is about to run, NOT the hook's own process env.
_INLINE_ENV = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.DOTALL)
_SKIP_FALSEY = frozenset({"", "0", "false", "no", "off"})

GIT_COMMIT = re.compile(r"\bgit\b.*\bcommit\b")
# `git commit --continue/--abort/--skip` (merge/rebase plumbing) and `--amend`
# are not authoring a fresh change → don't gate them.
SKIP_COMMIT = re.compile(r"--(?:continue|abort|skip|amend)\b")

# Commit-type prefixes that are exempt by default (conventional-commit chores).
# Override the set with REQUIRE_TICKET_EXEMPT_TYPES="chore,docs" (comma-separated).
_DEFAULT_EXEMPT_TYPES = "chore,docs,style,ci,build,test,revert"
EXEMPT_TYPES = {
    t.strip()
    for t in os.environ.get("REQUIRE_TICKET_EXEMPT_TYPES", _DEFAULT_EXEMPT_TYPES).split(",")
    if t.strip()
}
# A conventional-commit header: `type(scope)!: subject`. Capture the bare type.
# Anchored at the start of the (first) line — the type only counts on the subject,
# not on some `test:`-looking line buried in the body.
CONVENTIONAL = re.compile(r"^\s*([a-z]+)(?:\([^)]*\))?!?:", re.IGNORECASE)

# WIP / fixup / merge / revert markers that mean "not a normal authored commit".
EXEMPT_MARKERS = re.compile(
    r"^\s*(?:wip\b|fixup!|squash!|amend!|merge\b|revert\b)", re.IGNORECASE
)

# --- Ticket-reference heuristic -------------------------------------------------
# Kept deliberately permissive: a missed reference at worst nags (fail-open/warn),
# while a false hit just lets a commit through that probably had a ticket anyway.
#
# A KEY-NUM id needs >=2 uppercase letters so it can't collide with a version like
# `A1-456` or a hyphenated word; a trailer keyword (Closes/Fixes/Refs) must be
# followed by a REAL ref, not any token, so `fix: null deref` is NOT a reference.
_KEY_NUM = r"[A-Z]{2,}[A-Z0-9]*-\d+"  # ABC-123, ENG-7, GH style keys
_REF = rf"(?:#\d+|{_KEY_NUM}|T-\d+)"  # the concrete things a trailer can point at
TICKET_PATTERNS = (
    re.compile(r"(?:^|\s|\()#\d+\b"),                       # #123 (GitHub issue/PR)
    re.compile(r"\b[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#\d+\b"),  # org/repo#123
    re.compile(r"\bGH-\d+\b", re.IGNORECASE),               # GH-123
    re.compile(rf"\b{_KEY_NUM}\b"),                          # ABC-123 (Linear/Jira/task)
    re.compile(r"\btask\s*[:#]\s*\S+", re.IGNORECASE),      # task:ABC-12 / task #12
    re.compile(r"\bT-\d+\b"),                                # T-12 (short task id)
    re.compile(r"\[ticket:\s*\S[^\]]*\]", re.IGNORECASE),  # [ticket: <id-or-slug>] trailer (non-blank)
    re.compile(
        rf"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?|ref[s]?)\b[:\s]+{_REF}\b",
        re.IGNORECASE,
    ),  # Closes #12 / Fixes ABC-3 / Refs: T-9  (must point at a real ref)
    re.compile(
        r"https?://\S*(?:github\.com/\S+/issues/\d+|linear\.app/\S+/issue/\S+|"
        r"/browse/[A-Z]+-\d+)",
        re.IGNORECASE,
    ),  # full tracker URLs
)


def emit(decision: str, message: str | None = None) -> None:
    out = {"hook_api": HOOK_API, "decision": decision}
    if message:
        out["message"] = message
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


def warn(msg: str) -> None:
    sys.stderr.write(f"require-ticket: {msg}\n")


def has_ticket_reference(text: str) -> bool:
    return any(p.search(text) for p in TICKET_PATTERNS)


def is_exempt(message: str) -> bool:
    """A commit type / marker that doesn't need a ticket (trivial chore, WIP, merge).

    Judged from the first non-empty line of the message (the subject), so a
    `test:`-looking sentence in the body can't accidentally exempt a real change.
    """
    subject = next((ln for ln in message.splitlines() if ln.strip()), "")
    if EXEMPT_MARKERS.search(subject):
        return True
    m = CONVENTIONAL.match(subject)
    return bool(m and m.group(1).lower() in EXEMPT_TYPES)


def has_skip_trailer(message: str) -> bool:
    """True when the commit message carries a `[skip-ticket: <reason>]` escape trailer."""
    return bool(SKIP_TICKET_TRAILER.search(message))


def has_inline_skip(command: str) -> bool:
    """True when the FIRST `git commit` segment is prefixed with `REQUIRE_TICKET_SKIP=<truthy>`.

    Reads the inline env on the COMMAND the agent is about to run (`REQUIRE_TICKET_SKIP=1 git
    commit …`), not the hook's process env — mirroring the review-gate's `REVIEW_SKIP`. Scoped
    to the leading assignments of the segment that contains `git commit`, so an assignment on a
    SIBLING command (`REQUIRE_TICKET_SKIP=1 echo x; git commit …`) does NOT bypass the gate.
    Falsey values (`0`/`false`/`no`/`off`/empty) are matched case-insensitively so they don't
    accidentally skip. Best-effort: a tokenization failure → no skip (the commit stays gated)."""
    for segment in re.split(r"&&|\|\||;|\|", command):
        try:
            tokens = shlex.split(segment)
        except ValueError:
            continue
        env: dict[str, str] = {}
        rest = tokens
        for idx, tok in enumerate(tokens):
            m = _INLINE_ENV.match(tok)
            if not m:
                rest = tokens[idx:]
                break
            env[m.group(1)] = m.group(2)
        else:
            rest = []
        # Only honor the assignment on the segment that actually runs `git commit`.
        if not (rest and _is_git_executable(rest[0]) and "commit" in rest[1:]):
            continue
        if env.get("REQUIRE_TICKET_SKIP", "").strip().lower() not in _SKIP_FALSEY:
            return True
    return False


def _is_git_executable(tok: str) -> bool:
    """True when `tok` is the git binary — bare `git` or a path to it (`/usr/bin/git`)."""
    return os.path.basename(tok) == "git"


def commit_message_from_command(command: str, cwd: str | None = None) -> str:
    """Pull the inline commit message out of the argv: -m/--message and -F/--file.

    Returns ONLY the message text — the concatenation of every -m value and the
    contents of any -F file. Deliberately excludes the raw command so that the
    conventional-type / WIP exemption check sees the real subject line, not the
    `git commit …` argv. A relative `-F` path is resolved against `cwd` (the
    command's working directory from the event), since the hook may run elsewhere.
    Best-effort: on unbalanced quotes, falls back to the raw command string
    (better to over-scan for a ticket id than to crash).
    """
    parts: list[str] = []
    try:
        tokens = shlex.split(command)
    except ValueError:
        return command  # unbalanced quotes etc. — scan the raw string

    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in ("-m", "--message", "-F", "--file") and i + 1 < len(tokens):
            value = tokens[i + 1]
            if tok in ("-F", "--file"):
                parts.append(_read_message_file(value, cwd))
            else:
                parts.append(value)
            i += 2
            continue
        if tok.startswith("--message="):
            parts.append(tok.split("=", 1)[1])
        elif tok.startswith("--file="):
            parts.append(_read_message_file(tok.split("=", 1)[1], cwd))
        elif tok.startswith("-m") and len(tok) > 2:
            parts.append(tok[2:])  # -mMessage
        i += 1
    return "\n".join(parts)


def _read_message_file(path: str, cwd: str | None = None) -> str:
    resolved = os.path.expanduser(path)
    if cwd and not os.path.isabs(resolved):
        resolved = os.path.join(cwd, resolved)
    try:
        with open(resolved, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError as exc:
        warn(f"could not read commit-message file {resolved}: {exc}")
        return ""


def current_branch(cwd: str | None) -> str:
    """Branch name — a ticket id is often encoded there (feature/ABC-12-foo)."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=cwd or None,
            capture_output=True,
            text=True,
            timeout=2,
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError) as exc:
        warn(f"could not read branch: {exc}")
        return ""


def effective_cwd(command: str, cwd: str | None) -> str | None:
    """Honor ``git -C <dir>``: a `git -C <repo> commit …` acts on <repo>, not the shell cwd,
    so branch detection must read THAT repo. Returns the -C target (``-C dir`` or ``-Cdir``,
    last one wins, resolved against cwd) if present, else cwd. Falls back to cwd when the
    command can't be tokenized."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return cwd
    target: str | None = None
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "-C" and i + 1 < len(tokens):
            target = tokens[i + 1]
            i += 2
            continue
        if tok.startswith("-C") and len(tok) > 2:
            target = tok[2:]
        i += 1
    if target is None:
        return cwd
    target = os.path.expanduser(target)
    if cwd and not os.path.isabs(target):
        target = os.path.join(cwd, target)
    return target


def _argv_without_message(command: str) -> str:
    """The command with -m/--message/-F message VALUES stripped, so a flag named in the commit
    MESSAGE (``-m 'support --amend'``) can't trip SKIP_COMMIT and falsely exempt a real commit.
    On a tokenization failure, returns "" → SKIP_COMMIT can't match → the commit is GATED
    (the safe direction: gate rather than wrongly exempt)."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return ""
    out: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in ("-m", "--message", "-F", "--file") and i + 1 < len(tokens):
            i += 2  # drop the flag AND its value
            continue
        if tok.startswith(("--message=", "--file=")) or (tok.startswith("-m") and len(tok) > 2):
            i += 1  # drop -mMSG / --message=MSG / --file=PATH
            continue
        out.append(tok)
        i += 1
    return " ".join(out)


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as exc:
        # on_error=open → allow on inability to inspect; just warn.
        warn(f"could not parse event: {exc} — allowing (fail-open)")
        return _allow()

    args = event.get("args") or {}
    command = args.get("command") or args.get("cmd") or event.get("command") or ""
    if not isinstance(command, str):
        command = str(command)
    # Honor `git -C <repo>`: branch/message checks must target the repo the commit acts on.
    cwd = effective_cwd(command, event.get("cwd") or args.get("cwd"))

    # SKIP_COMMIT is matched against the argv with the MESSAGE removed, so `--amend`/`--continue`
    # appearing in a commit message can't falsely exempt the commit.
    if not GIT_COMMIT.search(command) or SKIP_COMMIT.search(_argv_without_message(command)):
        return _allow()  # not a normal authoring commit → nothing to gate

    message = commit_message_from_command(command, cwd)
    # Exemption is judged from the message text only (the real subject line), not
    # the `git commit …` argv — otherwise the command words could mis-trigger it.
    if is_exempt(message):
        return _allow()  # trivial chore / WIP / merge — no ticket expected

    # Per-commit escapes (the deliberate, documented bypass for a legit ticketless commit):
    # a `[skip-ticket: <reason>]` message trailer, or an inline `REQUIRE_TICKET_SKIP=1`.
    if has_skip_trailer(message):
        return _allow()  # explicit `[skip-ticket: …]` escape
    if has_inline_skip(command):
        return _allow()  # explicit inline `REQUIRE_TICKET_SKIP=1 git commit …` escape

    branch = current_branch(cwd)
    # Ticket detection is permissive: scan the message, the raw command (a ticket
    # id may ride in any flag, e.g. a -F path), and the branch name.
    if (
        has_ticket_reference(message)
        or has_ticket_reference(command)
        or has_ticket_reference(branch)
    ):
        return _allow()  # a ticket reference is present → proceed

    # The shared human-facing guidance — what's wrong and how to satisfy the gate. It carries NO
    # marker: the stable BLOCK_MARKER must appear ONLY on a real block, so an external check that
    # greps stdout for the marker (the task-cli Docker test) never gets a false positive on a
    # warn-mode allow. The block path prepends the marker; the warn path uses the guidance alone.
    guidance = (
        "Non-trivial changes should start from a ticket "
        "(task-cli / GitHub Issue / Linear) with acceptance criteria, motivation, and "
        "user-impact — then reference it in the commit (e.g. `Closes #123`, `Fixes #4`, "
        "`task:ABC-12`, `ENG-456`). If this is a trivial chore, use a `chore:`/`docs:` type "
        "(exempt). Deliberate escape: add a `[skip-ticket: <reason>]` trailer or run "
        "`REQUIRE_TICKET_SKIP=1 git commit …`; opt the whole gate back to warn-only with "
        "REQUIRE_TICKET_STRICT=0."
    )
    if STRICT:
        # Marker LEADS the block message so an external assertion can grep one fixed string.
        emit("block", f"{BLOCK_MARKER}. {guidance}")
        return BLOCK_EXIT_CODE
    # Warn-only opt-out: surface the reminder but let the commit proceed (and DON'T leak the
    # BLOCK_MARKER — this is an allow). The advisory deliberately says "no ticket reference found",
    # not "BLOCKED", so the marker is unambiguous proof of a real block.
    advisory = f"No ticket reference found — committing anyway (warn-only). {guidance}"
    warn(advisory)
    emit("allow", advisory)
    return 0


def _allow() -> int:
    emit("allow")
    return 0


if __name__ == "__main__":
    sys.exit(main())
