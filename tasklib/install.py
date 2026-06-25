"""install-skill — register the ``task`` agent skill so harnesses auto-discover it.

WHAT this does: brings ``task install-skill`` to PARITY with the sibling personal CLIs
(draw/tg/review), whose installers each write THREE layers — not just a SKILL.md. The three
layers, all idempotent and safe to re-run:

1. **SKILL.md + blurb + compat symlink.** ``~/.agents/skills/task/SKILL.md`` (Agent Skills
   standard) so Claude Code / Codex / opencode / Gemini / Cursor surface ``task`` as a
   capability, plus the one-line ``~/.agents/skills/.blurbs/task.md`` (the always-on
   advertisement a SessionStart hook cats into every session), plus a
   ``~/.claude/skills/task`` symlink (Claude Code also scans that dir).
2. **A marked block in each detected harness's instruction file.** A
   ``<!-- skill:task -->…<!-- /skill:task -->`` block is written into the instruction file of
   each harness DETECTED on this machine — detected by its config dir existing
   (``~/.claude`` → ``CLAUDE.md``, ``~/.codex`` → ``AGENTS.md``,
   ``~/.config/opencode`` → ``AGENTS.md``, ``~/.gemini`` → ``GEMINI.md``). The instruction file
   is CREATED if absent (same as the sibling installers — a detected harness gets advertised
   even when it has no global instruction file yet). The block is REPLACED on re-run (never
   duplicated), and nothing else in the file is touched.
3. **An idempotent SessionStart aggregator hook** in ``~/.claude/settings.json`` that cats
   every ``.blurbs/*.md`` into each new Claude Code session. Conservative: it is skipped when
   already present, and an unparseable ``settings.json`` is left untouched (never clobbered).

WHY all three: with only layer 1's SKILL.md, ``task`` is installed but UNDER-advertised
relative to draw/tg/review — agents don't get it surfaced at session start, and a fresh
machine bootstrap (``install.sh`` on a new box) never injects the always-on blurb. The whole
point of task-cli (every request becomes a ticket) depends on agents being aware of it at
session start, the same way the siblings are.

Stdlib-only; every write is idempotent (skips an already-current target, replaces — never
duplicates — a marked block, and won't re-add an existing hook).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

SKILL_NAME = "task"
SKILL_MD = """\
---
name: task
description: >-
  The enforced interface to the ticket system. Every user request becomes a durable,
  well-formed ticket — task-cli enforces acceptance criteria, motivation, user-impact,
  cost-of-inaction, screenshots (for UI), and the section template, so a ticket and its
  PR speak one shape. Use INSTEAD of raw `gh issue` / `linear` by hand. Backends: GitHub
  Issues (default) and Linear (per-repo). Commands: `task new`/`task create`, `task list`
  (this session's tickets), `task read <id>`, `task find <q>`, `task change <id>`,
  `task done <id>`, `task status <id> [<state>]`, `task classify "<text>"`, `task session`.
metadata:
  author: alex-mextner
  repo: https://github.com/alex-mextner/task-cli
---

# task — the enforced ticket interface

Every request → a ticket the moment it arrives. The tool refuses to create or close a
ticket that lacks the required fields, so work is always traceable back to an ask.

## Commands
```
task new --title "..." --acceptance "..." --why "..." --impact "..." --if-not-done "..."
task list                        # THIS session's tickets (falls back to all when empty)
task list --all                  # every known project's tickets, grouped by project
task read <id>                   # full ticket (every section) — works outside a repo
task find "<query>"              # search title+body (cross-project when outside a repo)
task change <id> --acceptance "..." --screenshot proof.png
task done <id>                   # close a ticket (runs the on-done gates)
task status <id>                 # read state (works outside a repo)
task status <id> done            # transition (runs the on-done gates)
task classify "<text>" --create  # change|justAsk; on `change`, create/dedup a ticket
task session                     # show current session + its tickets
```

## Key facts
- **Enforcement gates** (on create + on change→done): acceptance criteria, motivation,
  user impact, cost of inaction, screenshots (UI/visual tickets), section formatting.
- **Escape hatch**: any gate is skippable with a written justification recorded ON the
  ticket — `--skip-<gate> "<reason>"` (e.g. `--skip-screenshots "no UI in this change"`).
- **Backends call the API directly** and harvest credentials from `gh auth token` /
  `~/.config/linear/credentials.toml` — no extra setup if you've already authed those CLIs.
- **Session-scoped**: `task list` defaults to the current session (env:TASK_SESSION →
  tmux pane → git branch); tickets are labelled `session:<id>`.
- **Works outside a repo**: `read`/`status`/`find`/`list` run anywhere; outside a git repo
  `list` aggregates the `projects:` registry (in `~/.config/task-cli/config.yaml`) grouped by
  project. Only `new`/`create` is repo-bound (it writes into one project) — outside a repo it
  fails with a clear 3-part error.
- **Paged like git**: `list`/`find` page through `less` only on an interactive terminal;
  piped/scripted output is plain text. Cap is 100 interactive / 30 piped (override with `-n`).
  Disable with `--no-pager`, `NO_PAGER`, or empty `$PAGER`. `--json` is never paged.
"""

# One-line SessionStart blurb, same shape the siblings (draw/tg/review) use. The hook cats
# every ``.blurbs/*.md`` verbatim, so keep this a single bullet line ending in a newline.
BLURB = (
    "- `task` — the enforced ticket interface (task-cli). Every request → a durable, "
    "well-formed ticket (acceptance criteria, motivation, user-impact, cost-of-inaction, "
    "screenshots for UI). Use INSTEAD of raw `gh issue` / `linear` by hand. "
    "`task new --title \"...\" --acceptance \"...\" --why \"...\" --impact \"...\" "
    "--if-not-done \"...\"`, `task list` (this session), `task read <id>`, "
    "`task find \"<q>\"`, `task done <id>`, `task classify \"<text>\"`. Backends: "
    "GitHub Issues (default) + Linear (per-repo); credentials harvested from `gh`/`linear`.\n"
)


# The SessionStart aggregator that cats every installed tool's blurb into a new session. The
# marker (a trailing shell comment) makes the hook self-identifying so re-installs never add a
# second copy; it matches the shape rig + the sibling installers already use on this machine.
_HOOK_MARKER = "# agent-tools-awareness"
_HOOK_COMMAND = (
    'sh -c \'d="$HOME/.agents/skills/.blurbs"; ls "$d"/*.md >/dev/null 2>&1 && '
    '{ printf "Agent CLI tools installed on this machine (prefer them):\\n"; '
    'cat "$d"/*.md; }\' ' + _HOOK_MARKER
)

# The harness instruction files this installer injects the marked blurb-block into — but ONLY
# when the harness is detected on this machine (its config dir exists). Keep in step with the
# siblings (tg/review/draw) so a machine advertises `task` exactly where it advertises them.
_HARNESSES: tuple[tuple[str, str], ...] = (
    (".claude", "CLAUDE.md"),
    (".codex", "AGENTS.md"),
    (".config/opencode", "AGENTS.md"),
    (".gemini", "GEMINI.md"),
)


def _home() -> Path:
    return Path(os.environ.get("HOME") or os.path.expanduser("~"))


def _write_if_changed(target: Path, content: str) -> bool:
    """Write ``content`` to ``target`` unless it is already current. Returns True on write."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file() and target.read_text(encoding="utf-8") == content:
        print(f"task: already current at {target}")
        return False
    target.write_text(content, encoding="utf-8")
    print(f"task: wrote {target}")
    return True


def _ensure_claude_skills_symlink(home: Path) -> None:
    """Symlink ``~/.claude/skills/task`` → the canonical skill dir (Claude Code scans both).

    Only when ``~/.claude/skills`` already exists (so we don't conjure a Claude layout on a
    box that doesn't run it). A pre-existing link/file is left as-is; a symlink failure
    (unsupported FS / race) is non-fatal — the canonical ``~/.agents`` copy is what matters.
    """
    claude_skills = home / ".claude" / "skills"
    if not claude_skills.is_dir():
        return
    link = claude_skills / SKILL_NAME
    if link.exists() or link.is_symlink():
        return
    try:
        # An ABSOLUTE target is robust even when ~/.claude/skills is itself a symlink pointing
        # outside $HOME (a relative ../../ target would resolve from the wrong base in that case).
        link.symlink_to(home / ".agents" / "skills" / SKILL_NAME)
        print(f"task: linked {link}")
    except OSError:
        pass  # symlink unsupported or a race — the ~/.agents copy still advertises the skill


def _blurb_block(blurb: str) -> str:
    """The marked block written into a harness instruction file (replaced wholesale on re-run).

    ``blurb.rstrip()`` + an explicit newline keeps the closing marker on its own line regardless of
    whether ``blurb`` ends in a newline, so the closing comment never glues onto the blurb's text.
    """
    return f"<!-- skill:{SKILL_NAME} -->\n{blurb.rstrip()}\n<!-- /skill:{SKILL_NAME} -->\n"


def _inject_marked_block(path: Path, blurb: str) -> None:
    """Insert (or refresh IN PLACE) the ``<!-- skill:task -->…`` block in ``path``, preserving order.

    Idempotent and order-preserving: when a block for this skill already exists it is replaced
    EXACTLY where it sits — content before AND after the block is kept verbatim and unmoved. Only
    a first-time install appends the block at the end (after the existing content). The rest of the
    file is never reordered.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    new_text = _replace_or_append_block(existing, _blurb_block(blurb))
    if path.is_file() and existing == new_text:
        print(f"task: already current at {path}")
        return
    path.write_text(new_text, encoding="utf-8")
    print(f"task: advertised in {path}")


def _replace_or_append_block(existing: str, block: str) -> str:
    """Return ``existing`` with our marked block refreshed in place, or appended if absent.

    Pure (no I/O) so the order-preserving behavior is unit-testable. When a well-formed
    ``<!-- skill:task -->…<!-- /skill:task -->`` pair exists, ONLY that exact pair is swapped for
    ``block`` at the same offset; everything around it is preserved. To pick the real block (not an
    earlier orphan open) we anchor on the FIRST close marker and the LAST open marker BEFORE it —
    so a stray open marker sitting earlier in the file is treated as an orphan, not as the block's
    opening, and the user text between it and the real block is never swallowed. Any remaining
    orphan open markers are stripped (token only, surrounding text kept). When no pair exists the
    block is appended after the existing content.
    """
    start, end = f"<!-- skill:{SKILL_NAME} -->", f"<!-- /skill:{SKILL_NAME} -->"
    e_idx = existing.find(end)
    s_idx = existing.rfind(start, 0, e_idx) if e_idx != -1 else -1
    if e_idx != -1 and s_idx != -1:
        # The balanced pair: LAST open before the FIRST close. Swap exactly that span in place.
        block_end = e_idx + len(end)
        if block_end < len(existing) and existing[block_end] == "\n":
            block_end += 1  # consume the block's own trailing newline so we don't double it
        rebuilt = existing[:s_idx] + block + existing[block_end:]
        # Drop any STRAY markers (open OR close) left elsewhere — token only, surrounding text kept —
        # so a later run can't mis-anchor on them. Protect the pair we just re-inserted (in `block`).
        return _strip_stray_markers(rebuilt, (start, end), protect=(s_idx, s_idx + len(block)))
    # No well-formed pair: strip every stray marker token (open and close), then append a clean block.
    existing = _strip_stray_markers(existing, (start, end), protect=None)
    body = existing.rstrip()
    return f"{body}\n\n{block}" if body else block


def _strip_stray_markers(text: str, markers: tuple[str, ...], *, protect: tuple[int, int] | None) -> str:
    """Remove every stray skill-marker token (each of ``markers``) from ``text``, keeping all other
    text. Stripping BOTH the open and close markers symmetrically keeps the refresh idempotent: a
    lone open OR a lone close marker (from a manual edit / interrupted write) can't be mis-anchored
    on by a later run and can't make the blurb accumulate.

    ``protect`` is a ``(lo, hi)`` byte span (the freshly-inserted block) whose markers are left
    intact, so the real block we just wrote keeps its own markers. Only the token is removed (not
    its line), so user text sharing a line with a stray marker survives.
    """
    spans: list[tuple[int, int]] = []
    for marker in markers:
        i = 0
        while True:
            j = text.find(marker, i)
            if j == -1:
                break
            spans.append((j, j + len(marker)))
            i = j + len(marker)
    out: list[str] = []
    cursor = 0
    for lo, hi in sorted(spans):
        if lo < cursor:
            continue  # overlapping match already consumed
        if protect is not None and protect[0] <= lo < protect[1]:
            continue  # keep the protected (real-block) marker
        out.append(text[cursor:lo])  # keep text before the stray token, drop the token itself
        cursor = hi
    out.append(text[cursor:])
    return "".join(out)


def _inject_into_detected_harnesses(home: Path, blurb: str) -> None:
    """Inject the marked blurb-block into each detected harness's instruction file."""
    for config_dir, instruction_file in _HARNESSES:
        if (home / config_dir).is_dir():
            _inject_marked_block(home / config_dir / instruction_file, blurb)


def _hook_already_present(session_start: list) -> bool:
    """True if a SessionStart hook carrying our marker is already registered."""
    for group in session_start:
        hooks = group.get("hooks") if isinstance(group, dict) else None
        if not isinstance(hooks, list):
            continue
        for hook in hooks:
            cmd = hook.get("command") if isinstance(hook, dict) else None
            if isinstance(cmd, str) and _HOOK_MARKER in cmd:
                return True
    return False


def _ensure_session_start_hook(home: Path) -> None:
    """Add the SessionStart blurb-aggregator hook to ``~/.claude/settings.json``, idempotently.

    Conservative by design: only when ``~/.claude`` exists; a missing settings file is created
    with just the hook; an UNPARSEABLE settings file is left untouched (never clobbered); and an
    already-present hook (matched by its marker) is a no-op. We back up the file before rewriting.
    """
    if not (home / ".claude").is_dir():
        return
    settings_path = home / ".claude" / "settings.json"
    original = settings_path.read_text(encoding="utf-8") if settings_path.is_file() else None
    try:
        data = json.loads(original) if original is not None else {}
    except ValueError:
        print(f"task: WARNING — {settings_path} is not valid JSON; skipping the SessionStart hook")
        return
    if not isinstance(data, dict):
        print(f"task: WARNING — {settings_path} is not a JSON object; skipping the SessionStart hook")
        return

    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        print(f"task: WARNING — {settings_path} has a non-object 'hooks'; skipping the SessionStart hook")
        return
    session_start = hooks.setdefault("SessionStart", [])
    if not isinstance(session_start, list):
        print(f"task: WARNING — {settings_path} has a non-list 'SessionStart'; skipping the hook")
        return
    if _hook_already_present(session_start):
        print(f"task: SessionStart hook already present in {settings_path}")
        return

    session_start.append({"hooks": [{"type": "command", "command": _HOOK_COMMAND}]})
    if original is not None:
        # Back up the pre-rewrite settings, but NEVER clobber a backup the user (or a prior tool)
        # already made — only write the .bak when nothing (incl. a dangling symlink) is there.
        backup = settings_path.parent / "settings.json.bak"
        if not (backup.exists() or backup.is_symlink()):
            backup.write_text(original, encoding="utf-8")
    _atomic_write(settings_path, json.dumps(data, indent=2) + "\n")
    print(f"task: added SessionStart blurb-aggregator hook to {settings_path}")


def _atomic_write(target: Path, content: str) -> None:
    """Write ``content`` to ``target`` via a temp file + rename, so a crash can't truncate it.

    ``os.replace`` is atomic on the same filesystem; the temp sits beside the target so the rename
    never crosses devices. Used for ``settings.json`` — an outward-facing user file we never want to
    leave half-written.
    """
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, target)


def install_skill() -> int:
    """Install all three advertisement layers (idempotent). See the module docstring."""
    home = _home()
    skills_root = home / ".agents" / "skills"

    # Layer 1 — SKILL.md + always-on blurb + the Claude-Code compat symlink.
    _write_if_changed(skills_root / SKILL_NAME / "SKILL.md", SKILL_MD)
    _write_if_changed(skills_root / ".blurbs" / f"{SKILL_NAME}.md", BLURB)
    _ensure_claude_skills_symlink(home)

    # Layer 2 — a marked blurb-block in each detected harness instruction file.
    _inject_into_detected_harnesses(home, BLURB)

    # Layer 3 — the SessionStart aggregator hook (Claude Code).
    _ensure_session_start_hook(home)
    return 0
