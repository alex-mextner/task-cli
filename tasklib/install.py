"""install-skill — register the ``task`` agent skill so harnesses auto-discover it.

Writes two files, mirroring how the sibling personal CLIs (draw/tg/review) are installed:

1. A SKILL.md (Agent Skills standard) into ``~/.agents/skills/task/`` so Claude Code,
   Codex, opencode, Gemini, and Cursor surface ``task`` as a usable capability.
2. A one-line blurb into ``~/.agents/skills/.blurbs/task.md``. A SessionStart hook cats
   every ``.blurbs/*.md`` into each new agent session ("Agent CLI tools installed on this
   machine"). Without the blurb the tool is installed but invisible at session start while
   its siblings are advertised — so this write is what makes ``task`` an equal citizen.

Idempotent: skips a write when the target is already current. Stdlib-only.
"""

from __future__ import annotations

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


def _write_if_changed(target: Path, content: str) -> bool:
    """Write ``content`` to ``target`` unless it is already current. Returns True on write."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file() and target.read_text(encoding="utf-8") == content:
        print(f"task: already current at {target}")
        return False
    target.write_text(content, encoding="utf-8")
    print(f"task: wrote {target}")
    return True


def install_skill() -> int:
    skills_root = Path(os.path.expanduser("~/.agents/skills"))
    _write_if_changed(skills_root / SKILL_NAME / "SKILL.md", SKILL_MD)
    _write_if_changed(skills_root / ".blurbs" / f"{SKILL_NAME}.md", BLURB)
    return 0
