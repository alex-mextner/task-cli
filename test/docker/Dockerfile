# Dockerfile — hermetic integration-test image for task-cli.
#
# Builds a throwaway container that provisions `task` via its REAL install path (install.sh →
# `task install-skill`), wires the same SessionStart blurb hook a real Claude Code machine
# runs, and lays down a special test repo with a trivial feature request. The image is the
# "fresh machine" the test asserts against.
#
# Build context is the REPO ROOT (so `COPY` can grab bin/, tasklib/, install.sh):
#   docker build -f test/docker/Dockerfile -t task-cli-itest .
#
# Run the deterministic leg only (normal CI, no key):
#   docker run --rm task-cli-itest
# Run the full real-agent leg (credentialed / merge):
#   docker run --rm -e ANTHROPIC_API_KEY=sk-... task-cli-itest
FROM python:3.12-slim

# ── OS deps: git (repo + remote resolution), gh (present so the credential-harvest path is
#    realistic; the test sets GITHUB_TOKEN so no real auth is needed), node (for the claude CLI),
#    ca-certificates, curl. Kept minimal. ───────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates curl gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Optional gh (for realism; not required by the test). Best-effort: don't fail the build offline.
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg 2>/dev/null \
        | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg 2>/dev/null \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/* \
    || echo "gh install skipped (offline / unavailable) — test does not require it"

# ── claude CLI (the cheap real agent). Gated leg uses claude-haiku via `claude -p`. ──────────
RUN npm install -g @anthropic-ai/claude-code@latest \
    && claude --version || echo "WARN: claude CLI not installed — the real-agent leg will skip"

# ── unprivileged user (the agent is un-caged INSIDE the throwaway container only) ────────────
RUN useradd -m -s /bin/bash tester
ENV HOME=/home/tester
ENV PATH="/home/tester/.local/bin:${PATH}"

# pyyaml so task.yaml parsing is exercised (the tool degrades without it, but we want it on).
RUN pip install --no-cache-dir pyyaml

# ── copy the task-cli source and install it the REAL way (install.sh runs `task install-skill`)
WORKDIR /opt/task-cli
COPY --chown=tester:tester bin ./bin
COPY --chown=tester:tester tasklib ./tasklib
COPY --chown=tester:tester pyproject.toml README.md install.sh ./
COPY --chown=tester:tester test/docker ./test/docker

USER tester

# Provision exactly as a fresh dev machine does: symlink the binary + register the agent skill
# (writes ~/.agents/skills/task/SKILL.md and ~/.agents/skills/.blurbs/task.md).
RUN bash /opt/task-cli/install.sh

# ── wire the SessionStart blurb hook the way a real Claude Code machine does ─────────────────
# This is the SAME one-liner rig installs into ~/.claude/settings.json (SessionStart). The
# deterministic leg re-invokes this exact command to prove a fresh session sees `task`.
RUN mkdir -p "$HOME/.claude" \
    && cat > "$HOME/.claude/settings.json" <<'JSON'
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "sh -c 'd=\"$HOME/.agents/skills/.blurbs\"; ls \"$d\"/*.md >/dev/null 2>&1 && { printf \"Agent CLI tools installed on this machine (prefer them):\\n\"; cat \"$d\"/*.md; }' # agent-tools-awareness"
          }
        ]
      }
    ]
  }
}
JSON

# ── the special test repo: a small real-world-ish git repo with a trivial feature request ────
# Lives under $HOME so the unprivileged `tester` user owns it (a top-level /work needs root).
RUN mkdir -p "$HOME/test-repo"
WORKDIR /home/tester/test-repo
RUN git init -q \
    && git config user.email tester@example.com \
    && git config user.name tester \
    && git remote add origin https://github.com/mock/mock.git \
    && printf '#!/usr/bin/env bash\n# demo CLI — a trivial app under test.\necho "hello from demo"\n' > demo.sh \
    && chmod +x demo.sh \
    && printf '# demo\n\nA trivial demo CLI. Feature requested: a `--version` flag.\n' > README.md \
    && cat > task.yaml <<'YAML'
version: 1
backend: github-issues
github:
  repo: mock/mock
  default_labels: [agent]
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
session:
  detect: [env:TASK_SESSION, tmux-pane, git-branch]
  label_prefix: "session:"
YAML
RUN git add -A && git commit -q -m "init demo repo"

# Hermetic backend wiring (no real token / network): the github-issues backend honors
# GITHUB_API_URL and harvests the token from $GITHUB_TOKEN. These are NON-secret test values
# (a dummy token + a localhost mock URL); the entrypoint also defaults them, but setting them
# here keeps an ad-hoc `docker run ... task --version` working without the entrypoint.
ENV GITHUB_API_URL=http://127.0.0.1:8771
ENV TASK_SESSION=docker-itest
ENV MOCK_PORT=8771
ENV TEST_REPO=/home/tester/test-repo

WORKDIR /opt/task-cli
ENTRYPOINT ["bash", "/opt/task-cli/test/docker/entrypoint.sh"]
