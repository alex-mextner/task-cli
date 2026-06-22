# Docker integration test — task-cli is advertised + usable on a fresh machine

A hermetic, end-to-end test that provisions `task` the way a real dev machine does and proves,
**with a real cheap LLM agent**, that task-cli is:

- **ADVERTISED** — a fresh agent discovers `task`: the real `task install-skill` wrote the
  `SKILL.md` and the SessionStart blurb, and the SessionStart hook surfaces the `task` blurb
  into a new session.
- **USABLE** — `task new` / `task list` round-trip through the **real** `github-issues` backend
  code path (stdlib `urllib` → REST) against a hermetic mock GitHub server.

> Scope (CTO, tg#4580): this test does **NOT** assert any commit-blocking ticket gate. It only
> proves *advertised* + *usable*.

## What's here

| File | Role |
| --- | --- |
| `Dockerfile` | the "fresh machine": installs `task` via the real `install.sh` (which runs `task install-skill`), wires the same SessionStart blurb hook a real Claude Code machine runs, lays down a special test repo with a trivial feature request, and the hermetic-backend env. |
| `mock_github.py` | a stdlib-only in-memory mock of the GitHub Issues REST endpoints the backend calls. Lets `task new`/`list` exercise the **real backend code** with no token/network. |
| `assertions.sh` | the **deterministic (no-LLM)** leg — advertised + usable assertions. Runs in normal CI. |
| `agent_test.sh` | the **gated real-cheap-agent** leg — drives `claude -p` (model `claude-haiku-4-5`) and asserts the agent discovers + uses `task` rather than raw `gh issue`. Gated behind `ANTHROPIC_API_KEY`. **Status: tracked next increment, non-fatal in CI** — see the known gap below. |
| `entrypoint.sh` | starts the mock, runs the deterministic leg always, runs the agent leg iff the key is present (else skips with a clear message). |

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

- **Deterministic leg** — runs on every PR and on push to `main` (so a merge re-verifies it):
  builds the image, runs it with no key. This is the fast, unconditional gate.
- **Real-agent leg** — runs **on PR merge** (`push` to `main`, i.e. post-merge), gated behind
  the `ANTHROPIC_API_KEY` repository secret. If the secret is absent the leg **skips with a
  clear message** rather than failing — so a fork PR or an unconfigured repo is never red over a
  missing key.

Why "push to main" for the merge trigger: a squash-merge lands as a single push to `main`, so a
`push: branches: [main]` event is exactly "a PR just merged". `merge_group` would also work if
merge queues are enabled, but this repo squash-merges via `gh ship`, so the post-merge `push` is
the faithful "runs when a PR merges" signal and needs no queue configuration.

## Known gap — the real-agent leg is the tracked next increment

The **deterministic leg is the proven gate** (verified locally: `docker build` + the no-LLM
assertions pass). The **real-agent leg is written and runnable but not yet a proven gate**:

- Run against a real haiku, the agent makes the *right call* on `gh` (it does **not** reach for
  raw `gh issue create`) and its summary references `task` — but a cheap model tends to resolve
  the **harness's own built-in to-do/task tool** (`TodoWrite` / `TaskCreate`) over the advertised
  `task` CLI, so no ticket lands in the backend and the "a ticket was created" assertion fails.
  This is a name collision between "task" the concept and the `task` CLI, plus haiku's bias to a
  built-in over a shell tool.
- The hardening (precise built-in disablement, a collision-proof prompt that forces the
  shell→`task` path, and a live-key validation run) is tracked and is the next increment.

Until then CI runs this leg **gated behind the key AND `continue-on-error: true`**, so it reports
but never wedges the merge gate. The deterministic `smoke` job is the blocking gate.

## Security

- The agent is un-caged **inside the throwaway container only** (`--allowedTools Bash`,
  `acceptEdits`), never on the host.
- The API key is provided **only** via a CI secret / a local `-e` flag; it is never baked into
  the image.
- The backend is a local mock; no real GitHub repo is touched.
