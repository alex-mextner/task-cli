# Docker integration test — task-cli is advertised + usable on a fresh machine

A hermetic, end-to-end test that provisions `task` the way a real dev machine does and proves,
**with a real cheap LLM agent**, that task-cli is:

- **ADVERTISED** — a fresh agent discovers `task`: the real `task install-skill` wrote the
  `SKILL.md` and the SessionStart blurb, and the SessionStart hook surfaces the `task` blurb
  into a new session.
- **USABLE** — `task new` / `task list` round-trip through the **real** `github-issues` backend
  code path (stdlib `urllib` → REST) against a hermetic mock GitHub server.
- **ENFORCED** — the strict `require-ticket-before-commit` agent-hook (rig-provisioned on a real
  machine; agent-tools#95) BLOCKS a commit with no ticket reference (exit 10 + stable marker) and
  ALLOWS one that references a ticket. Deterministic (no LLM), so it runs on every PR.

> Scope note: the original harness (tg#4580) was advertise-only and explicitly did NOT assert any
> commit-blocking gate, because the require-ticket hook was not yet strict + provisioned. Now that
> it IS (strict default, rig-provisioned), the **ENFORCED** leg above adds that end-to-end
> assertion (issue #19). The enforcement is asserted deterministically against the vendored hook.

## What's here

| File | Role |
| --- | --- |
| `Dockerfile` | the "fresh machine": installs `task` via the real `install.sh` (which runs `task install-skill`), wires the same SessionStart blurb hook a real Claude Code machine runs, lays down a special test repo with a trivial feature request, and the hermetic-backend env. |
| `mock_github.py` | a stdlib-only in-memory mock of the GitHub Issues REST endpoints the backend calls. Lets `task new`/`list` exercise the **real backend code** with no token/network. |
| `assertions.sh` | the **deterministic (no-LLM)** advertised + usable leg. Runs in normal CI. |
| `enforce_test.sh` | the **deterministic (no-LLM)** require-ticket **enforcement** leg — proves the strict `require-ticket-before-commit` agent-hook BLOCKS a ticketless commit (exit 10 + the stable marker) and ALLOWS a ticketed one. Runs in normal CI. |
| `require_ticket_before_commit.py` | a **vendored** copy of the agent-tools `require-ticket-before-commit` hook (see its SYNC header) — the hermetic fixture `enforce_test.sh` drives, so the enforcement leg needs no agent-tools checkout at build time. Drift from the source is currently discipline-only; a mechanical sync-guard is tracked in #20. |
| `agent_tools_gating.sh` | the **tool-gating config + its key-free self-check** for the real-agent leg (the TodoWrite/TaskCreate/Task denylist). Sourced by `agent_test.sh`; run standalone (`--check`) in the deterministic leg so a regression that un-gates a built-in fails a normal PR with no key. |
| `agent_test.sh` | the **gated real-cheap-agent** leg — drives `claude -p` (model `claude-haiku-4-5`) and asserts the agent discovers + uses `task` rather than raw `gh issue` or a built-in to-do tool. Gated behind `ANTHROPIC_API_KEY`. **Status: hardened, non-fatal in CI pending a live-key validation run** — see the gap below. |
| `entrypoint.sh` | starts the mock, runs all three deterministic legs always (advertised+usable, enforcement, gating config self-check), runs the agent leg iff the key is present (else skips with a clear message). |

## The hermetic backend seam

The `github-issues` backend honors `GITHUB_API_URL` (the same env `gh`/octokit read — a real
GitHub Enterprise need, not test-only), so the test points it at `http://127.0.0.1:8771` and
harvests a dummy token from `$GITHUB_TOKEN`. No real GitHub, no real credentials, fully offline.

## Run locally

```bash
# from the repo root
docker build -f test/docker/Dockerfile -t task-cli-itest .

# deterministic leg only (no key) — what normal CI runs:
docker run --rm task-cli-itest

# full real-agent leg (cheap model; ~a few cents):
docker run --rm -e ANTHROPIC_API_KEY=sk-ant-... task-cli-itest
```

`RUN_AGENT=never|auto|always` overrides the gating (default `auto` = run iff the key is set).

## How it maps to CI

The `test-cli` GitHub Actions job (`.github/workflows/test-cli.yml`):

- **Deterministic legs** (advertised+usable AND require-ticket enforcement) — run on every PR and
  on push to `main` (so a merge re-verifies them): build the image, run it with no key. This is the
  fast, unconditional gate.
- **Real-agent leg** — runs **on PR merge** (`push` to `main`, i.e. post-merge), gated behind
  the `ANTHROPIC_API_KEY` repository secret. If the secret is absent the leg **skips with a
  clear message** rather than failing — so a fork PR or an unconfigured repo is never red over a
  missing key.

Why "push to main" for the merge trigger: a squash-merge lands as a single push to `main`, so a
`push: branches: [main]` event is exactly "a PR just merged". `merge_group` would also work if
merge queues are enabled, but this repo squash-merges via `gh ship`, so the post-merge `push` is
the faithful "runs when a PR merges" signal and needs no queue configuration.

## The real-agent leg — hardened (issue #16), gated pending live-key validation

The **deterministic legs are the proven gate** (verified locally: `docker build` + the no-LLM
advertised/usable AND enforcement assertions pass). The real-agent leg is now **hardened** against
the known collision but is still **gated + non-fatal** until a recorded live-key run validates it:

- The collision: a cheap model (haiku) made the *right call* on `gh` (it did **not** reach for raw
  `gh issue create`), but tended to resolve the **harness's own built-in to-do/task tool**
  (`TodoWrite` / `TaskCreate` / `Task`) over the advertised `task` CLI — a name collision between
  "task" the concept and the `task` CLI plus a cheap model's bias to a built-in over a shell tool —
  so no ticket landed in the backend.
- The fix (issue #16): **tool gating** — the canonical `--disallowedTools TodoWrite TaskCreate Task`
  (space-separated multi-token form, per `claude --help`) denies the to-do/task built-ins, with
  `--allowedTools Bash Read` allowing only the shell path; as an extra layer, `--tools "Bash,Read"`
  (comma form — a different delimiter the help documents) restricts the built-in set to a positive
  allowlist, **added only when `claude --help` advertises `--tools`** so an absent/renamed flag can't
  silently break the invocation. Plus a **collision-proof prompt** that names the affordance
  concretely (run the `task` SHELL command via Bash) and forbids any built-in todo/task/scratchpad
  tool. The built-in exclusion (the gating CONFIG) lives in `agent_tools_gating.sh` and is asserted
  against the tool-list arrays **in the deterministic leg** (`agent_tools_gating.sh --check`, key-free,
  every PR) — so a regression that reintroduces a built-in fails a normal PR, not just the gated leg.
- What's left on #16 (the LIVE-key validation, not provable without a key):
  - whether real `claude` accepts `--allowedTools`/`--disallowedTools` as a SPACE-separated variadic
    (it must, for `--disallowedTools TodoWrite TaskCreate Task` to deny all three) — the deterministic
    config check proves the argv we BUILD, not how the CLI PARSES it;
  - whether passing `--tools` (comma) ALONGSIDE `--allowedTools`/`--disallowedTools` (space) composes
    cleanly on a given build, rather than one model of restriction overriding the other;
  - a recorded run proving the agent reliably lands a backend ticket across a few runs.
  Until that lands, CI keeps this leg **gated behind the key AND `continue-on-error: true`**, so it
  reports but never wedges the merge gate; flip `continue-on-error` off once validated. The
  deterministic `smoke` job is the blocking gate.

## Security

- The agent is un-caged **inside the throwaway container only** (`--allowedTools Bash`,
  `acceptEdits`), never on the host.
- The API key is provided **only** via a CI secret / a local `-e` flag; it is never baked into
  the image.
- The backend is a local mock; no real GitHub repo is touched.
