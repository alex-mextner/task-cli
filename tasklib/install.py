"""install-skill — register the ``task`` agent skill so harnesses auto-discover it.

Writes a SKILL.md (Agent Skills standard) into ``~/.agents/skills/task/`` so Claude Code,
Codex, opencode, Gemini, and Cursor surface ``task`` as a usable capability. Idempotent:
skips when the SKILL.md is already current. Stdlib-only.
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
  Issues (default) and Linear (per-repo). Commands: `task create`, `task list` (this
  session's tickets), `task read <id>`, `task find <q>`, `task change <id>`,
  `task status <id> [<state>]`, `task classify "<text>"`, `task session`.
metadata:
  author: alex-mextner
  repo: https://github.com/alex-mextner/task-cli
---

# task — the enforced ticket interface

Every request → a ticket the moment it arrives. The tool refuses to create or close a
ticket that lacks the required fields, so work is always traceable back to an ask.

## Commands
```
task create --title "..." --acceptance "..." --why "..." --impact "..." --if-not-done "..."
task list                        # THIS session's tickets (falls back to all when empty)
task list --all                  # every known project's tickets, grouped by project
task read <id>                   # full ticket (every section) — works outside a repo
task find "<query>"              # search title+body (cross-project when outside a repo)
task change <id> --acceptance "..." --screenshot proof.png
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
  project. Only `create` is repo-bound (it writes into one project) — outside a repo it fails
  with a clear 3-part error.
- **Paged like git**: `list`/`find` page through `less` only on an interactive terminal;
  piped/scripted output is plain text. Cap is 100 interactive / 30 piped (override with `-n`).
  Disable with `--no-pager`, `NO_PAGER`, or empty `$PAGER`. `--json` is never paged.
"""


def install_skill() -> int:
    skills_dir = Path(os.path.expanduser("~/.agents/skills")) / SKILL_NAME
    skills_dir.mkdir(parents=True, exist_ok=True)
    target = skills_dir / "SKILL.md"
    if target.is_file() and target.read_text(encoding="utf-8") == SKILL_MD:
        print(f"task: skill already current at {target}")
        return 0
    target.write_text(SKILL_MD, encoding="utf-8")
    print(f"task: wrote skill → {target}")
    return 0
