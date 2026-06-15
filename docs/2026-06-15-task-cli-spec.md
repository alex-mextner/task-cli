# task-cli — specification (v0.1 draft, 2026-06-15)

A standalone CLI (peer to `tg-cli` / `review-cli` / `rig-cli`) that is **the single, enforced
interface to the ticket system**. Every user request creates or updates a ticket immediately;
ticket *quality* (acceptance criteria, motivation, user-impact, cost-of-inaction, screenshots,
formatting) is enforced by the tool itself, not by convention. Backends: **GitHub Issues
(default)** and **Linear** (per-repo config, e.g. the hyperide repo). It reuses `review-cli`
for cheap message classification and is wired into a `tg-cli` inbound hook so that incoming
Telegram messages are classified and turned into tickets with no babysitting.

> Why this exists: agents drop requests, lose the thread, and produce work nobody can trace
> back to an ask. Make "promise = durable action" mechanical: every request becomes a durable,
> well-formed ticket the moment it arrives, and a session view always answers "what am I doing
> for you right now". This is the *control + observability* layer for the request→work loop.

---

## 1. Architecture

- `bin/task` shim → `tasklib/` package. uv-runnable, **stdlib-only at import time**, heavy deps
  lazy-imported inside `run()` (same rule as review/rig/3d). Logs through the shared
  `agenttools_log` JSONL library (observability).
- **Backend abstraction** `tasklib/backends/` behind a `TicketBackend` protocol —
  `create / get / update / list / search / comment / attach / transition / session_tickets`.
  Adapters call the provider **API directly** (stdlib `urllib`, no subprocess per call):
  `github_issues` → GitHub REST/GraphQL; `linear` → Linear GraphQL. This removes the runtime
  dependency on a CLI being installed/version-matched, is faster (no spawn), and exposes the full
  API (attachments, custom fields) the CLI may not.
  **Credentials are harvested from the existing CLI configs** rather than re-prompted:
  GitHub token from `gh auth token` / `~/.config/gh/hosts.yml` (or `$GITHUB_TOKEN`); Linear key
  from the `linear` config (`.linear.toml` / `~/.config/linear`) or `$LINEAR_API_KEY`. So a user
  who already ran `gh auth login` / `linear auth` needs zero extra setup — the CLI is only needed
  for the one-time auth flow, not for task-cli's calls. Tokens are never logged (the
  `agenttools_log` 0600 + secret-redaction posture applies). The rest of the tool never touches a
  backend directly.
- **Pure core** (testable, no I/O): `model.py` (the `Ticket` dataclass), `render.py`
  (Ticket ↔ structured markdown body, fixed section order), `policy.py` (enforcement rules over
  a Ticket), `classify.py` (parse `review just-ask` output → `change|justAsk`), `session.py`
  (session id detection + sidecar index), `config.py` (load `task.yaml` + global defaults).
- **Effectful entrypoint** `bin/task`: backend calls, filesystem, the classification shell-out.

## 2. Config — `task.yaml` (committed per repo) + `~/.config/task-cli/config.yaml` (global)

```yaml
version: 1
backend: github-issues            # or: linear
github:   { repo: auto, default_labels: [agent] }   # repo: auto = origin owner/name
linear:   { team: HYP, project: "" }
enforce:
  acceptance_criteria: required
  motivation: required            # "Why"
  user_impact: required           # who / what it affects
  cost_of_inaction: required      # what happens if we don't
  formatting: strict              # body must match the section template
  screenshots:
    on_create: required_if_label: [ui, visual]
    on_done:   required_if_label: [ui, visual]
  escape_hatch: explain           # any gate skippable with a written justification, recorded on the ticket
classify:
  # Prioritized fallback chain — ONE cheap/fast model PER PROVIDER. The runner uses the first
  # AVAILABLE one (key present / reachable), so it works with whatever the user has — same
  # availability-failover as the review board, but pool=1. Default head is haiku.
  fallbacks:
    - anthropic:   claude-haiku-4-5            # default head
    - openai:      gpt-5-mini
    - commandcode: deepseek/deepseek-v4-flash
    - zai:         glm-4.6-flash
    - google:      gemini-2.5-flash
    - ollama:      qwen2.5:3b                  # free / offline, when present
  bias: change                                 # ambiguous → change
session:
  detect: [env:TASK_SESSION, tmux-pane, git-branch]
  label_prefix: "session:"
```

The hyperide repo ships `task.yaml` with `backend: linear, team: HYP`. Everywhere else the
default is `github-issues`, so the tool works with zero config on any GitHub repo.

## 3. Command surface

CTO sketch was `task find|read|change|status|list` ("something like that"); extended for a
complete create/enforce loop:

| Command | What it does |
| --- | --- |
| `task create` | Create a ticket. **Enforces** the policy (§4). Inputs: `--title`, `--from-message "<raw user text>"` (the hook path — derive title/body), `--acceptance "…"` (repeatable), `--why`, `--impact`, `--if-not-done`, `--screenshot <path>` (repeatable), `--label`. Interactive TUI when TTY + no flags; semi-interactive when some flags given; non-interactive with `--yes`. Refuses with a precise message if a required gate is unmet. |
| `task list` | **Default: this session's tickets**, one per line `<id> [<state>] <title> — <first paragraph>`. Flags `--all`, `--mine`, `--state`, `--label`, `-n`, `--json`. |
| `task read <id>` (`view`) | Full ticket: every section, state, attachments, links. |
| `task find <query>` | Search tickets (title+body) via the backend; same line format; `--state`, `--all`. |
| `task change <id>` | Update: append/replace sections, add acceptance criteria, `--screenshot` (implementation proof), `--why/--impact/--if-not-done`, `--title`, `--label`. Enforces the on-done gates when used to close. |
| `task status <id> [<new-state>]` | Read or transition state (`todo / in-progress / in-review / done / cancelled`, mapped per backend). Transition → `done` runs on-done enforcement. |
| `task classify "<text>" [--create \| --update <id>]` | Classify `change \| justAsk` via `review just-ask`; with `--create`, a `change` verdict creates (or dedups → updates) a ticket. **This is what the tg hook calls.** |
| `task session [show \| bind <id>]` | Show/bind the current session and list its tickets. |

Global flags: `--backend`, `--repo`, `--config`, `--json`, `--yes`, and the per-gate escape
hatch `--skip-<gate> "<reason>"`.

## 4. Enforcement — the point of the tool

On `create`, and again on `change`→`done`:

1. **Acceptance criteria** — ≥1, as a checkbox list. Required.
2. **Motivation / User impact / Cost of inaction** — three required, non-empty sections. ("Why
   are we doing this, who does it affect, what happens if we don't.")
3. **Screenshots** — required at *creation* and at *done* when the ticket is UI/visual
   (label-gated, configurable). gh backend embeds/attaches the image in the issue body; Linear
   backend uses `linear issue attach <id> <file>`.
4. **Formatting** — the body must conform to the §5 section template (fixed order, headed
   sections). `render.py` validates; `task lint <id>` checks an existing ticket.

Every gate has an **escape hatch**: `--skip-<gate> "<reason>"` writes the justification onto the
ticket (auditable) — this is the CTO's "написать объяснительную и пропустить". Gates are also
disable-able globally or per-repo via `enforce:` in config. (Mirrors the existing
`ci/screenshots` "explain-and-skip" precedent.)

## 5. The ticket body template (what `render.py` enforces)

```
## What
<one paragraph: the change>

## Why (motivation)
## User impact
## Cost of inaction
## Acceptance criteria
- [ ] …
## Screenshots
- creation: <img>      - implementation: <img>
## Links
- PR: …    Session: session:<id>
```

This is deliberately the **same section set** as agent-tools' existing
`ci/pr-checklist/pull_request_template.md` (Motivation/what&why · Acceptance criteria · Screenshots/proof)
so a ticket and its PR speak one shape. Note the history: the internal `ship` script once had
issue-tracker coupling (ticket-id extraction, attaching proof to a tracker) and it was
**deliberately stripped** when generalized into `ci/ship` (`ci/ship/README.md:58-64`). task-cli
re-introduces exactly that coupling — but as a portable, parameterized tool, not baked into ship.

## 6. Session-scoped `task list`

Session id is detected: `env:TASK_SESSION` (explicit, set by the harness) → tmux pane →
git branch. Every ticket created/touched in a session is (a) labelled `session:<id>` (portable
across machines) **and** (b) recorded in a local sidecar `~/.local/state/task-cli/sessions/<id>.jsonl`
(fast, offline lookup). `task list` defaults to the current session's tickets with status +
first paragraph.

## 7. Classification (`change | justAsk`)

`task classify` resolves the **first available model in the per-provider fallback chain**
(§2 `classify.fallbacks`, default head haiku — whichever provider the user actually has) and
shells out to `review just-ask -m <resolved model> --pool 1` with a fixed prompt:
*"Classify this message to a dev agent as `change` (should result in a code/doc/config change →
needs a ticket) or `justAsk` (pure question, no change). **Bias to `change` when ambiguous —
most questions to a dev agent are latent change requests.** Output one word."* `--pool 1` = a
single fast/cheap model, no panel. The chain is provider-agnostic: it degrades to whatever's
reachable (Anthropic → OpenAI → commandcode → z.ai → Google → local ollama), so classification
keeps working offline (ollama) or on any one key.

- `change` → dedup: `task find` an open ticket matching the request (same session + high title
  similarity); found → `task change` (append/comment); else → `task create --from-message`.
- `justAsk` → no ticket, but logged to `agenttools_log` (still observable).

## 8. tg-cli inbound hook

A new `tg-ctl` inbound hook (provisioned by rig) calls `task classify "<msg>" --create` on every
inbound user message. The hook wires **task-cli** (not the classifier inline) — "task-cli именно
оно будет в хуки прописываться". Classification runs off the critical path so it never delays the
agent seeing the message; the ticket is created/updated in parallel. Optionally the inbound wrap
can surface the mapped ticket id (`[TG from Alex #<msgid>] (→ HYP-NNN) …`).

> **Contract note:** agent-tools' `agents-hooks/v1` today defines only three hook points —
> `pre-bash` / `pre-write` / `stop`. There is **no inbound/on-message point**. This is the one
> place the design *extends* the hook contract rather than reusing it: we add an
> `on-inbound` (message-received) point and document it in `agent-hooks/README.md`. Everything
> else (the ticket guard) reuses the existing contract verbatim.

## 9. review-cli addition (from this request)

review-cli gains **ollama as just another provider** — orthogonal to how it's used (it is *not*
a "classification backend"; classification is merely one caller). Like every other provider it
plugs into the same model-resolution path (likely routed through opencode), so `-m ollama/<model>`
works in any subcommand — `review review`, `brainstorm`, `just-ask`. It happens to make task-cli's
classifier (§7) free/offline because a local model is in the fallback chain, but that's a
consequence of "ollama is available," not its purpose.

## 10. rig integration

`rig.yaml` gains a `tickets:` section (backend, team/project, `enforce` policy, `classify`
models). `rig apply` writes `task.yaml`, installs the tg inbound classify hook, and — when
`backend: linear` — installs the `linear` CLI via `rig doctor` **for the one-time auth flow only**
(task-cli then reads the key from its config and calls the API directly, per §1). The hyperide
repo's rig config selects Linear/HYP; everything else defaults to GitHub Issues.

## 11. agent-tools addition

Precise placement, from a full read of agent-tools (which today has **zero** ticket/issue
tracking — clean gap). Cite `docs/carrier-decision-guide.md` as the placement authority and bump
the README inventory counts (currently exact — adding without bumping introduces the first drift):

- **Skill (the rule):** `skills/universal/strict-ticket-discipline/SKILL.md` — every request → a
  ticket; the `change|justAsk` distinction; the required fields; "use `task`, never raw
  `gh issue` / `linear` by hand". Cross-link the existing `deferred-findings-tracking` skill
  (that's the "not now → track it" case; this is the "every request" case). Bump universal 31→32.
- **Guard (mid-session prevent):** an agent-hook `agent-hooks/require-ticket-before-commit/`
  (`pre-bash` point, modeled directly on `require-review-before-commit` — same marker-file
  pattern: a `TICKET_MARKER` touched when task-cli opens/links a ticket; `on_error: open` because
  this is *discipline*, not security). Per the carrier guide's "prevent + backstop" rule,
  optionally also a CI slot `ci/ticket-required/` (workflow + shell + README, modeled on
  `pr-checklist` + `screenshots`, escape hatch `ALLOW_NO_TICKET='<reason>'`) as the merge-time
  backstop.
- **task-cli itself: documented, NOT vendored** — same treatment as the `agent-browser`
  precedent. Reference it from the skill body and add a one-paragraph slot to `mcp/README.md` if
  it exposes an MCP surface.
- **Inbound classifier hook:** `agent-hooks/classify-inbound-request/` at the new `on-inbound`
  point (§8). This is the single contract extension.

The enforcement therefore = task-cli (the tool) + the tg classify hook + the ticket guard + rig
provisioning, sitting next to existing analogues (`ci/screenshots`, `deferred-findings-tracking`,
`visual-proof-cycle`, `promise-durable-action`).

## 12. Open decision-forks (need a CTO call)

- **(a) Session id source** — `env:TASK_SESSION` (explicit, harness-set) vs tmux-pane
  (automatic) vs git-branch. Recommend: env if set, else tmux-pane.
- **(b) Dedup aggressiveness** — how eagerly `classify→change` updates an existing ticket vs
  opens a new one. Recommend conservative: same session + high title similarity → update; else
  new.
- **(c) Default classifier model** — `haiku` (API, cheap, needs a key, available now) vs a local
  `ollama` model (free, needs ollama + the review-ollama work in §9). Recommend haiku now, flip
  the default to ollama once §9 lands.
