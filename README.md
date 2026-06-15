# task-cli

**The enforced interface to the ticket system.** Every request becomes a durable,
well-formed ticket the moment it arrives — `task` enforces ticket *quality* (acceptance
criteria, motivation, user-impact, cost-of-inaction, screenshots, formatting) in the tool
itself, not by convention. A ticket and its PR speak one shape.

A standalone Python CLI, peer to [`review-cli`](https://github.com/alex-mextner/rig-cli),
[`rig-cli`](https://github.com/alex-mextner/rig-cli), and `tg-cli`. Backends: **GitHub Issues
(default)** and **Linear** (per-repo). Stdlib-first; the backends call the provider API
directly (no `requests`, no per-call subprocess), and credentials are harvested from the CLIs
you already authed — zero extra setup.

> Why this exists: agents drop requests, lose the thread, and produce work nobody can trace
> back to an ask. `task` makes "promise = durable action" mechanical — every request becomes a
> well-formed ticket, and `task list` always answers "what am I doing for you right now".

## Install

```bash
git clone https://github.com/alex-mextner/task-cli && cd task-cli && ./install.sh
# or, one-liner:
curl -fsSL https://raw.githubusercontent.com/alex-mextner/task-cli/main/install.sh | bash
```

The installer symlinks `bin/task` into `~/.local/bin` and registers the agent skill. The only
runtime dep is `pyyaml` (for `task.yaml`); without it the tool falls back to built-in defaults.

**Credentials** are read from the CLIs you already use — run these once and you're done:

```bash
gh auth login      # GitHub Issues (the default backend)
linear auth        # Linear (per-repo, via task.yaml)
```

`task` reads `gh auth token` / `~/.config/linear/credentials.toml` (or `$GITHUB_TOKEN` /
`$LINEAR_API_KEY`) and calls the API directly. Tokens are never logged or persisted.

## Commands

```
task create   --title "..." --why "..." --impact "..." --if-not-done "..." --acceptance "..."
task list                          # THIS session's tickets (status + first paragraph)
task list --all                    # all tickets
task read <id>     (alias: view)   # the full ticket — every section
task find "<query>"                # search title+body via the backend
task change <id>  [--done]         # update; --done runs the on-done gates (close)
task status <id> [<new-state>]     # read or transition state
task classify "<text>" [--create]  # change|justAsk via review; --create makes/dedups a ticket
task session [show|bind <id>]      # show/bind the current session and its tickets
```

Global flags: `-C/--cwd`, `--backend`, `--repo`, `--config`, `--json`, and the per-gate escape
hatch `--skip-<gate> "<reason>"`.

### Example

```bash
task create \
  --title "Add a logout button to the header" \
  --what "A button in the top-right that ends the session and redirects to /login." \
  --why "Users have no way to sign out on shared machines." \
  --impact "Every authenticated user on the web app." \
  --if-not-done "Security complaint risk; sessions linger on shared devices." \
  --acceptance "button visible when logged in" \
  --acceptance "click clears the session cookie and redirects to /login" \
  --label ui --screenshot mock.png
```

`task` refuses to create the ticket if a required gate is unmet, with a precise message and
the exact flag (or escape hatch) to satisfy it.

## Enforcement — the point of the tool

On `create`, and again on `change`→`done`:

1. **Acceptance criteria** — ≥1, rendered as a checkbox list. Required.
2. **Motivation / User impact / Cost of inaction** — three required, non-empty sections.
3. **Screenshots** — required at *creation* and at *done* for UI/visual tickets (label-gated,
   configurable). The on-done gate demands the **implementation** proof specifically — a
   creation mock does not let you close a UI ticket. That is why the gate runs twice.
4. **Formatting** — the body must match the fixed section template (`render.py` validates).

Every gate has an **escape hatch**: `--skip-<gate> "<reason>"` writes the justification into
the ticket's `Skipped gates` section (auditable, recorded forever). Gates are also disable-able
per repo via `enforce:` in config.

| gate | flag to satisfy | escape hatch |
| --- | --- | --- |
| acceptance-criteria | `--acceptance "..."` (repeatable) | `--skip-acceptance "<reason>"` |
| motivation | `--why "..."` | `--skip-motivation "<reason>"` |
| user-impact | `--impact "..."` | `--skip-user-impact "<reason>"` |
| cost-of-inaction | `--if-not-done "..."` | `--skip-cost-of-inaction "<reason>"` |
| screenshots | `--screenshot <path>` | `--skip-screenshots "<reason>"` |
| formatting | (automatic — body is rendered) | `--skip-formatting "<reason>"` |

## The ticket body template

```
## What
## Why (motivation)
## User impact
## Cost of inaction
## Acceptance criteria
- [ ] …
## Screenshots
## Links
```

This is the same section set as the agent-tools `pull_request_template.md`, so a ticket and
its PR speak one shape. `render.py` is the single source of truth — fields are authoritative,
the body is derived.

## Session-scoped `task list`

A "session" is the unit of work `task` is doing for you. The id is detected by precedence:

1. `$TASK_SESSION` — explicit, harness-set.
2. tmux pane (`$TMUX_PANE`).
3. git branch.

Every ticket created/touched in a session is labelled `session:<id>` (portable) **and**
recorded in a local sidecar (`~/.local/state/task-cli/sessions/<id>.jsonl`, fast/offline).
`task list` defaults to the current session's tickets.

## Classification

`task classify "<text>"` decides `change` (→ a ticket) vs `justAsk` (a pure question) by
shelling out to `review just-ask -m <model> --pool 1`. The model is the **first available**
in a per-provider fallback chain (default head `claude-haiku-4-5`; degrades through OpenAI →
commandcode → z.ai → Google → local ollama), so it works with whatever you have — offline via
ollama, or on any one key. **Bias is to `change` on ambiguity** — most questions to a dev agent
are latent change requests. `task classify "<text>" --create` is the entry point the `tg-cli`
inbound hook calls.

## Config — `task.yaml` (per-repo) + `~/.config/task-cli/config.yaml` (global)

```yaml
version: 1
backend: github-issues            # or: linear
github:   { repo: auto, default_labels: [agent] }   # repo: auto = origin owner/name
linear:   { team: HYP, project: "" }
enforce:
  acceptance_criteria: required
  motivation: required
  user_impact: required
  cost_of_inaction: required
  formatting: strict
  screenshots:
    on_create: { required_if_label: [ui, visual] }
    on_done:   { required_if_label: [ui, visual] }
  escape_hatch: explain
classify:
  fallbacks:
    - { anthropic:   claude-haiku-4-5 }
    - { openai:      gpt-5-mini }
    - { commandcode: deepseek/deepseek-v4-flash }
    - { zai:         glm-4.6-flash }
    - { google:      gemini-2.5-flash }
    - { ollama:      qwen2.5:3b }
  bias: change
session:
  detect: [env:TASK_SESSION, tmux-pane, git-branch]
  label_prefix: "session:"
```

`task.yaml` is committed by default and overrides the global layer. Scope is by location,
never a flag. With no config at all the tool defaults to `github-issues` with every gate on.

## Architecture

- `bin/task` — thin shim → `tasklib.cli:main`.
- `tasklib/cli.py` — argparse + dispatch + the effects (backend calls, the classify shell-out,
  sidecar writes). Kept thin.
- Pure core (no provider I/O): `model.py` (the `Ticket`), `render.py` (template ↔ Ticket),
  `policy.py` (the gates), `classify.py` (chain resolution + verdict parse), `session.py`
  (detection + sidecar), `config.py` (cascade loader).
- `tasklib/backends/` — the `TicketBackend` protocol + `github_issues.py` (REST) and
  `linear.py` (GraphQL), each calling the API directly via the tiny `http.py` urllib helper.
- `tasklib/credentials.py` — harvest tokens from existing CLI configs.
- `tasklib/logging.py` — structured JSONL in the `agenttools_log` shape, with secret redaction.

## Tests

```bash
python3 -m pytest -q     # the unit suite (FakeBackend; never hits live GitHub/Linear)
bash tests/smoke.sh      # --help, every subcommand --help, lazy-import invariant, pytest
```

## License

MIT.
