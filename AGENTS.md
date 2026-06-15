# AGENTS.md — task-cli

Rules for agents working in this repo. English only (no Cyrillic anywhere in repo docs).

## What this is

`task` is the **enforced interface to the ticket system**: a standalone Python CLI (peer to
`tg-cli` / `review-cli` / `rig-cli`) that turns every request into a durable, well-formed
ticket the moment it arrives, and enforces ticket *quality* (acceptance criteria, motivation,
user-impact, cost-of-inaction, screenshots, formatting) in the tool itself, not by convention.
Backends: **GitHub Issues (default)** and **Linear** (per-repo). It consumes `review-cli` for
cheap message classification and is wired into a `tg-cli` inbound hook.

## Hard rules

- **Stdlib-only at import time.** Every `tasklib/*` module imports only the standard library
  when loaded. Heavy/optional deps — `yaml` (config), the backends (`urllib`) — are imported
  lazily inside the function that needs them. `task --help` and every `<subcommand> --help`
  must run with zero third-party imports. Do not add a top-level `import yaml`.
- **Backends call the provider API DIRECTLY** (stdlib `urllib`, no per-call subprocess):
  GitHub REST, Linear GraphQL. The only subprocesses are the one-shot `gh auth token`
  (credential harvest) and the classify shell-out to `review`. Never spawn a CLI per API call.
- **Credentials are harvested, never re-prompted, never logged.** GitHub from
  `$GITHUB_TOKEN` → `gh auth token` → `hosts.yml`; Linear from `$LINEAR_API_KEY` →
  `~/.config/linear/credentials.toml`. All logging goes through `tasklib.logging` which
  **redacts** token-shaped values and masks secret-named keys. A token must never reach a log
  line or stdout.
- **Pure core stays pure.** `model.py` / `render.py` / `policy.py` / `classify.py` (parse +
  chain resolution) / `session.py` (detection) / `config.py` do **no network and no provider
  I/O**. The only I/O in the "pure" layer is the session sidecar (isolated, fail-soft) and
  config file reads. Effects (backend calls, the classify shell-out, the filesystem writes)
  live in `bin/task` → `tasklib/cli.py`.
- **Enforcement is the point.** The gates (`policy.py`) run on `create` and again on
  `change`→`done`. Each gate is (a) disable-able per repo via `enforce:` config and (b)
  skippable with a **recorded, auditable** justification (`--skip-<gate> "<reason>"` → written
  into the ticket's `Skipped gates` section). The on-done screenshot gate demands the
  *implementation* proof specifically — a creation shot does not let you close a UI ticket.
  Never weaken a gate silently; if a gate is wrong, change `policy.py` + add a test.
- **`render.py` is the single source of truth for the ticket body.** The §5 section template
  (fixed order) is the contract; fields are authoritative, the body is derived. Do not
  hand-format a body anywhere else. `validate_format()` is what the formatting gate calls.
- **`task.yaml` is committed by default** (per-repo source of truth) and **overrides** the
  global `~/.config/task-cli/config.yaml`. Scope is by location, never a flag. The tool works
  with **zero config** on any GitHub repo (built-in defaults = github-issues, all gates on).
- **`task list` defaults to THIS session.** Session id = `env:TASK_SESSION` → tmux pane → git
  branch. Tickets are labelled `session:<id>` (durable) AND recorded in a local sidecar
  (`~/.local/state/task-cli/sessions/<id>.jsonl`, a cache). The label is the source of truth.

## Backend seam

`tasklib/backends/__init__.py` defines the `TicketBackend` protocol; `get_backend()` is the
only place that picks an adapter. `github_issues.py` and `linear.py` are the only modules that
know a provider's API shape. Tests use a structural `FakeBackend` (`tests/conftest.py`) — they
never hit live GitHub/Linear. If you add a backend, implement the whole protocol and add a
fake-backed test; do not special-case a backend anywhere in `cli.py`.

## Classification

`task classify` resolves the **first available** model in the per-provider fallback chain
(`classify.resolve_chain`, default head haiku) and shells out to
`review just-ask "<prompt>" -m <model> --pool 1`. Availability = a provider key in env, or
(ollama) the daemon on PATH. Bias to `change` on ambiguity. The shell-out is the only effect;
prompt-building and verdict-parsing are pure and tested without spawning `review`.

## Tests

- `PYTHON=.venv/bin/python python3 -m pytest -q` — the unit suite. Fast, hermetic; uses the
  `FakeBackend` and `tmp_path`/isolated `$HOME`/`$XDG_*`. Tests never touch the real HOME,
  real credentials, or a live provider.
- `bash tests/smoke.sh` — end-to-end: `--help`, every subcommand `--help`, the lazy-import
  invariant, zero-config load, and pytest. No token used.
- Add a test with every behavior change. TDD red-first is the house style.

## Out of scope for THIS repo (separate follow-ups)

The ecosystem integrations are deliberately NOT built here: the `tg-cli` inbound classify
hook, the agent-tools `strict-ticket-discipline` skill + `require-ticket-before-commit` guard
+ `ci/ticket-required` backstop, and the `rig` `tickets:` provisioning. This repo is the TOOL.
Do not edit `tg-cli` / `agent-tools` / `rig` from here.

## Style

- Conventional commits.
- English-only code, comments, and docs.
- No dead code, no underscore-prefixed unused params, no `as-unknown-as` escape hatches.
- Keep `cli.py` thin (argparse + dispatch + effects); pure logic lives in the sibling modules.
