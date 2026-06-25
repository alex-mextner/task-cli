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
linear auth        # Linear (per-repo, via the rig.yaml task: block)
```

`task` reads `gh auth token` / `~/.config/linear/credentials.toml` (or `$GITHUB_TOKEN` /
`$LINEAR_API_KEY`) and calls the API directly. Tokens are never logged or persisted.

## Commands

```
task new      --title "..." --why "..." --impact "..." --if-not-done "..." --acceptance "..." [--due YYYY-MM-DD]
task create                        # alias of `new` (same arguments, same gates)
task list                          # THIS session's tickets (falls back to all when empty)
task list --all                    # every known project's tickets, grouped by project
task gantt        [--all] [--json] # read-only due-date timeline (Gantt) — see below
task read <id>     (alias: view)   # the full ticket — every section (works outside a repo)
task find "<query>"                # search title+body (cross-project when outside a repo)
task done <id>    [--screenshot p] # close a ticket — runs the on-done gates
task change <id>  [--due ...] [--done]  # update; --due sets/clears the due date; --done closes (gates)
task status <id> [<new-state>]     # read or transition state (works outside a repo)
#   close/transition verbs validate legality first: a cancelled ticket is a dead-end and a
#   re-close of an already-done ticket is rejected (no silent re-write). `--force` overrides.
task classify "<text>" [--create]  # change|justAsk via review; --create makes/dedups a ticket
task session [show|bind <id>]      # show/bind the current session and its tickets
task daemon start|stop|status|run  # the due-date reminder watcher (see Daemon below)
```

Global flags: `-C/--cwd`, `--backend`, `--repo`, `--config`, `--json`, and the per-gate escape
hatch `--skip-<gate> "<reason>"`.

### Due-date reminders (the daemon)

A ticket can carry a `--due YYYY-MM-DD` date (set on `new`/`create`, changed or cleared on
`change`). It is stored backend-portably — a `## Due` section in the ticket body that round-trips
through both backends (Linear also mirrors it into the native `dueDate`).

The **daemon** is a background watcher that polls the backend on an interval, selects open
tickets that are overdue or due within a window, and pushes a reminder to the CTO's channel (the
`tg` CLI by default):

```
task daemon start    # spawn the detached daemon (idempotent — never double-starts)
task daemon stop     # stop it (SIGTERM, then SIGKILL on timeout); clears the pid-file
task daemon status   # running / not-ours / stale / stopped + pid + config (--json for machine output)
task daemon run      # the foreground loop (what `start` spawns)
```

`status` is pid-identity-aware (consistent with `stop`/`start`): a pid that was recycled by the OS
for an unrelated process after a crash reports as `not-ours` (in `--json` too), not `running`. To
verify that, a live daemon's status reads the process argv (`ps -ww` / `/proc`), slightly more than a
bare liveness probe.

The loop is fail-soft: a backend error, a malformed ticket, or a down notifier in one tick is
caught and logged — the daemon keeps running. De-dupe is per `(ticket, due-date)`, so a ticket
is reminded once; a changed due date re-fires. One daemon per repo coordinate (the state files
are keyed by it). Tunables live in a `daemon:` config block (see Configuration).

### Timeline view (`task gantt`)

`task gantt` is a **read-only** Gantt: it charts the same tickets `task list` would show
(same session / `--all` / outside-a-repo scoping) on a date axis by their `--due` date.

```
task gantt                 # this session's tickets on a due-date timeline
task gantt --all           # every known project's tickets, flattened onto one axis
task gantt --state todo    # filter by state / --label, same flags as `task list`
task gantt --json          # machine-readable timeline (window + per-row bar geometry)
task gantt --width 60      # override the bar-area width (default: auto-fit the terminal)
```

Each dated ticket is a row with a status marker on the axis: `○` todo, `◐` in-progress,
`◑` in-review, `●` done, `!` (red) **overdue** (an open ticket past its due date), `✗`
cancelled. The `│`/`▼` gridline marks today. The date window auto-fits the tickets' range and
always includes today; a degenerate (single-date) range still renders. Tickets with **no due
date** are listed in a clearly-marked `undated` section — never hidden. It is purely a view:
no ticket is mutated. Output is paged like `list` in an interactive terminal (`--no-pager` /
`NO_PAGER` opt out); `--json` is never paged.

### Example

```bash
task new \
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

## Working outside a repo / across projects

A tool's *read* and *global* operations should not demand you stand inside a git repo. So:

- **`task list` outside any repo** → shows **all** tickets across the projects you've
  registered, **grouped by project** (a heading per project, tickets beneath). The output
  says `showing all project tasks` so it's clear why you see everything.
- **`task list` inside a repo** → scopes to that repo's current session. With **no agent
  session**, or a session with **no tickets**, it falls back to *all* of that repo's tickets
  and says so. **`task list --all`** gives the cross-project grouped view from anywhere.
- **`task read` / `task status` / `task find`** work outside a repo too. An id is routed to a
  registered project (a Linear `HYP-3` by its team; a `#123` when exactly one GitHub project
  is registered); an ambiguous id fails with a clear, actionable error.
- **Only `task new`/`create` is repo-bound** — it writes a ticket into one specific project, so
  it needs a repo (or `--repo owner/name`). Outside one it fails with a 3-part WHAT/WHY/HOW error.
- A project whose backend errors (auth, offline, unknown team) is shown as a **degraded
  group** — it never aborts the whole cross-project listing.

`--json` follows the view: the session/single-repo list is a flat `[ticket]`, while the
grouped cross-project view (outside a repo, or `--all`) is `[{project, backend, current, error,
tickets}]` — one object per project group, so a degraded project is visible to scripts too. The
in-repo fallback (session empty → all of *this* repo's tickets) stays the flat `[ticket]` shape,
scoped to the current repo, even though the text output prints the `showing all project tasks`
line. Only the cross-project view is grouped.

### Pagination (`list` / `find`)

Like `git log`, the human (non-`--json`) output is paged through `less` **only when stdout is an
interactive terminal**. Piped or scripted (`task list | …`, CI), it prints plain text so it stays
parseable — no pager, no surprises. Short output that fits one screen prints directly (`less -F`).

- The result cap follows the same split: **100** in a terminal (the pager scrolls), **30** when
  piped. An explicit **`-n N`** always wins.
- Opt out of the pager with **`--no-pager`**, **`NO_PAGER=1`** (any non-empty value), or an empty
  **`$PAGER`/`$TASK_PAGER`** (git's "cat, don't page"). Choose the pager via `$TASK_PAGER` →
  `$PAGER` → `less` → `more`. `$LESS` defaults to `FRX` (quit-if-one-screen, raw colors, no screen
  clear) unless you set it.

The cross-project view reads a **`projects:`** registry from the config cascade — usually the
**global** `~/.config/task-cli/config.yaml`, since it spans repos:

```yaml
projects:
  - { repo: acme/frontend }                       # GitHub shorthand → group "acme/frontend"
  - { name: Backend, github: { repo: acme/api } }  # explicit block + display name
  - { name: HYP, backend: linear, team: HYP }      # a Linear team/project
```

The repo you're currently inside is always one of the groups, even if it isn't (yet) listed.

## Classification

`task classify "<text>"` decides `change` (→ a ticket) vs `justAsk` (a pure question) by
shelling out to `review just-ask -m <model> --pool 1`. The model is the **first available**
in a per-provider fallback chain (default head `claude-haiku-4-5`; degrades through OpenAI →
commandcode → z.ai → Google → local ollama), so it works with whatever you have — offline via
ollama, or on any one key. **Bias is to `change` on ambiguity** — most questions to a dev agent
are latent change requests. `task classify "<text>" --create` is the entry point the `tg-cli`
inbound hook calls.

## Config — `rig.yaml` `task:` block (per-repo) + `task.yaml` + global

The per-repo tracker backend is selected from the repo's committed **`rig.yaml`** — the single
source of truth for the whole agent toolchain (rig provisions it). Drop a `task:` block in:

```yaml
# rig.yaml (repo root) — selects the tracker backend for this repo
task:
  backend: linear      # or github-issues (the default for every other repo)
  team: HYP            # → linear.team (Linear coordinate)
  # project: ""        # → linear.project
  # repo: owner/name   # → github.repo (for the github-issues backend)
```

The block is intentionally flat: `team`/`project`/`repo` are shorthands translated onto
task-cli's own config shape, and the full sections (`github:`/`linear:`/`enforce:`/`classify:`/
`session:`/`projects:`) may be nested in verbatim for fine control. Unknown
sub-keys under `task:` are warned-and-ignored, never fatal — `rig.yaml` is owned by rig-cli and
a newer key must not crash an older task-cli. **DEFAULT = GitHub Issues**: a repo with no
`task:` block (or no `rig.yaml`) falls through cleanly to `github-issues`. A repo that keeps a
native `task.yaml` still has it win (the cascade is defaults → global → `rig.yaml` task: →
`task.yaml` → `--config`).

The full native shape (also accepted as `task.yaml`, or nested under `rig.yaml` `task:`):

```yaml
version: 1
backend: github-issues            # or: linear
github:   { repo: auto, default_labels: [agent] }   # repo: auto = origin owner/name
linear:   { team: HYP, project: "" }
projects:                          # cross-project registry (mostly in the GLOBAL config)
  - { repo: acme/frontend }        # `task list` outside a repo / `--all` aggregates these
  - { name: HYP, backend: linear, team: HYP }
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
  capability: ""                     # optional (rig#8): a role/capability tag (e.g. `fast` /
                                     # `reasoning` / `code`) resolved from the shared model
                                     # manifest (agent-tools `lib/contracts/models.yaml`) and
                                     # PREFERRED ahead of `fallbacks`. Empty → manifest unused.
                                     # The manifest's exact model id is used for every provider
                                     # EXCEPT gemini (there it steers the provider/role only —
                                     # review's gemini backend picks the version). Fail-soft: a
                                     # missing manifest / resolver falls through to `fallbacks`.
                                     # Point at a manifest with $TASK_MODELS_MANIFEST.
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
daemon:                              # the due-date reminder watcher (all keys optional)
  enabled: true                      # false → both `daemon start` AND `daemon run` are no-ops
  interval_s: 3600                   # poll interval (seconds); a 0/negative value falls back
  due_soon_days: 3                   # remind when due within N days (or already overdue)
  query_limit: 100                   # tickets fetched per tick; raise it for a big/old project
  notifier: [tg, --tag, report]      # the reminder command; the message is appended as the last arg
```

Config is committed by default and scoped by location, never a flag. With no config at all the
tool defaults to `github-issues` with every gate on (and the daemon's built-in defaults above).

When `classify.capability` is set, the model manifest is located by probing a few conventional
`agent-tools` checkout paths; set `$TASK_MODELS_MANIFEST` to an explicit `models.yaml` to make the
choice deterministic on a machine with more than one checkout (it always wins over the heuristics).

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

## Roadmap (what v1 does NOT do yet)

v1 is the usable core: `new`/`create`, `list`, `read`, `find`, `change`, `done`, `status`,
`classify`, `session` against GitHub Issues (default) and Linear, with the enforcement gates.
Deferred to follow-up issues:

- **Dependency system + Gantt rendering** — [#1](https://github.com/alex-mextner/task-cli/issues/1).
- **Daemon service + webhooks** (adapter-based trackers, survives restarts) —
  [#2](https://github.com/alex-mextner/task-cli/issues/2).
- **Completion + due-date notifications** (tmux-inject into the agent pane) —
  [#3](https://github.com/alex-mextner/task-cli/issues/3).
- **Integrations** — tg classify-on-inbound hook, the agent-tools `require-ticket-before-commit`
  guard, and rig cross-repo provisioning — [#4](https://github.com/alex-mextner/task-cli/issues/4).

## License

MIT.
