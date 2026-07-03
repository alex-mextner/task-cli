#!/usr/bin/env bash
# deploy.sh — update an installed `task` checkout to the latest committed code.
#
# WHY THIS EXISTS
#   `task` is installed as a symlink chain (~/.local/bin/task -> <checkout>/bin/task,
#   a thin shim that imports the sibling tasklib/ package), so the checked-out FILES
#   *are* the running tool (single-file-live-symlink-cli). "Deploying" a merged change
#   is therefore just a fast-forward `git pull` of the checkout — there is no build
#   step and no pipx/editable reinstall. This script makes that one-step deploy safe
#   and idempotent for the repo it lives in (or an explicit --checkout).
#
#   The one moving part a bare pull forgets: `task daemon run` is a resident Python
#   process that imported tasklib into memory at start; after a pull it keeps the OLD
#   code until restarted. Daemons are per-repo (any repo may run one via `task daemon
#   start -C <repo>`), so this script cannot enumerate them — when the deploy touched
#   runtime code it prints an ACTION-NEEDED warning to restart any running daemons
#   (`task daemon stop && task daemon start` in each repo) instead of blind-restarting.
#
#   rig-cli 0.8.0+ runs `bash scripts/deploy.sh` (cwd = this repo) on every `rig apply`
#   so the installed tool never silently drifts stale behind origin/main
#   (alex-mextner/task-cli#43). The script owns ALL its own safety: it refuses a dirty
#   tree, a detached HEAD, and a diverged branch; rig just invokes it and downgrades a
#   non-zero exit to a warning.
#
# USAGE
#   scripts/deploy.sh [--checkout DIR] [--dry-run]
#
#   --checkout DIR   The git checkout to update. Default: the repo THIS script lives
#                    in — exactly the checkout rig invokes it from (cwd=repo, no
#                    args), so multiple clones or a package console-script `task` on
#                    PATH can never redirect the deploy to the wrong repository.
#                    If the script is outside any checkout (a stray copy), fall
#                    back to resolving the `task` on PATH through its symlink
#                    chain. An explicit --checkout is trusted as given.
#   --dry-run        Show what would happen (fetch + report divergence) without
#                    pulling. Always safe to run.
#
# EXIT CODES
#   0  up to date (including ahead-only: unpushed local commits but nothing new
#      to pull), or deployed successfully
#   1  usage / environment error (no checkout, not a git repo, dirty tree,
#      detached HEAD, no upstream, fetch failure, untracked-file collision,
#      broken shim upstream, failed post-deploy version probe)
#   2  cannot fast-forward (checkout diverged from its upstream) — needs a human
set -euo pipefail

usage() {
  cat <<'EOF'
deploy.sh — update an installed `task` checkout to the latest committed code.

`task` is installed as a symlink to this checkout (no build step), so "deploying"
a merged change is a guarded fast-forward `git pull` in that checkout.

Usage:
  scripts/deploy.sh [--checkout DIR] [--dry-run]

  --checkout DIR   The git checkout to update. Default: the repo this script
                   lives in (the checkout rig invokes it from); outside any
                   checkout, fall back to resolving the `task` on PATH through
                   its symlink chain.
  --dry-run        Show what would land (fetch + report) without pulling.

Exit codes: 0 up-to-date/deployed · 1 usage/env error · 2 non-fast-forward.
EOF
}

CHECKOUT=""
DRY_RUN=0

while [ $# -gt 0 ]; do
  case "$1" in
    --checkout)
      if [ $# -lt 2 ] || [ -z "${2:-}" ]; then
        echo "deploy: --checkout requires a directory argument." >&2; exit 1
      fi
      CHECKOUT="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "deploy: unknown argument '$1' (try --help)" >&2; exit 1 ;;
  esac
done

# Scrub repo-pinning GIT_* vars from every git invocation: when this script runs
# from inside a git hook (rig apply triggered by a hook, a hook-spawned shell),
# the environment carries GIT_DIR/GIT_WORK_TREE/GIT_INDEX_FILE which OVERRIDE
# `git -C` and would silently pin every command to a FOREIGN repo (the exact bug
# class fixed in review-cli#72). `env -u` of an absent var is a no-op, so this
# is safe everywhere.
# Kept as an env-argv ARRAY (not only a function) so `timeout` can wrap the whole
# chain — timeout execs a real command and cannot call a shell function.
GIT_ENV_SCRUB=(-u GIT_DIR -u GIT_WORK_TREE -u GIT_INDEX_FILE -u GIT_OBJECT_DIRECTORY
               -u GIT_COMMON_DIR -u GIT_CEILING_DIRECTORIES -u GIT_DISCOVERY_ACROSS_FILESYSTEM)
git_clean() { env "${GIT_ENV_SCRUB[@]}" git "$@"; }

# Bound a command with `timeout SECONDS` when timeout(1)/gtimeout is available,
# so a wedged invocation (network hang, tool blocking on stdin) can't hang the
# whole deploy — rig apply runs this under its own budget.
timeout_bin="$(command -v timeout || command -v gtimeout || true)"
[ -z "$timeout_bin" ] && echo "deploy: NOTE — no timeout(1)/gtimeout; long-running steps run unbounded." >&2
bounded() {
  secs="$1"; shift
  if [ -n "$timeout_bin" ]; then "$timeout_bin" "$secs" "$@"; else "$@"; fi
}

# Resolve the real file behind a symlink, following the chain hop-by-hop (no
# `readlink -f` — it is absent on stock macOS). Echoes the final target. A depth
# cap breaks a symlink cycle (a -> b -> a) instead of looping forever.
resolve_link() {
  target="$1"
  hops=0
  while [ -L "$target" ]; do
    hops=$((hops + 1))
    if [ "$hops" -gt 40 ]; then
      echo "deploy: symlink chain for '$1' is too deep (cycle?) — aborting." >&2
      exit 1
    fi
    link="$(readlink "$target")"
    case "$link" in
      /*) target="$link" ;;                      # absolute
      *)  target="$(dirname "$target")/$link" ;; # relative to its own dir
    esac
  done
  echo "$target"
}

# ── resolve which checkout to deploy ───────────────────────────────────────────
# Default order (no --checkout):
#   1. The checkout THIS script lives in. rig's contract is
#      `bash <repo>/scripts/deploy.sh` (cwd=repo, no args) — "keep the repo you
#      live in fresh". Resolving via a `task` on PATH instead would break the
#      first apply (nothing installed yet) and, with several clones or a package
#      console-script `task` in some other repo's .venv, could pull the WRONG
#      repository.
#   2. Fall back to resolving `task` on PATH through its symlink chain — for a
#      stray copy of this script run from outside any checkout.
if [ -z "$CHECKOUT" ]; then
  script_target="$(resolve_link "${BASH_SOURCE[0]}")" || exit 1
  # `cd` output to /dev/null: with CDPATH set, bash prints the resolved dir on
  # stdout, which would corrupt the captured path.
  script_dir="$(cd "$(dirname "$script_target")" >/dev/null && pwd -P)"
  CHECKOUT="$(git_clean -C "$script_dir" rev-parse --show-toplevel 2>/dev/null || true)"
fi
if [ -z "$CHECKOUT" ]; then
  task_bin="$(command -v task || true)"
  if [ -n "$task_bin" ]; then
    task_real="$(resolve_link "$task_bin")" || exit 1
    task_dir="$(cd "$(dirname "$task_real")" >/dev/null && pwd -P)"
    candidate="$(git_clean -C "$task_dir" rev-parse --show-toplevel 2>/dev/null || true)"
    # Trust the PATH fallback only when the `task` on PATH resolves to the
    # candidate repo's OWN bin/task shim and the sibling tasklib package exists —
    # i.e. the repo actually IS the legacy symlink checkout. A package
    # console-script `task` (some repo's .venv/bin/task) fails the identity
    # check even if its repo happens to contain bin/task + tasklib/, so we never
    # fetch/merge a repository the installed symlink does not point into.
    # ($task_dir and $candidate are both physical paths: pwd -P / rev-parse.)
    if [ -n "$candidate" ] \
       && [ "$task_dir/$(basename "$task_real")" = "$candidate/bin/task" ] \
       && [ -f "$candidate/tasklib/__init__.py" ]; then
      CHECKOUT="$candidate"
    elif [ -n "$candidate" ]; then
      echo "deploy: 'task' on PATH ($task_bin) resolves into '$candidate' but not" >&2
      echo "        via its bin/task shim (+ tasklib/) — refusing to deploy a" >&2
      echo "        foreign repository. Pass --checkout DIR to name the checkout." >&2
      exit 1
    fi
  fi
  if [ -z "$CHECKOUT" ]; then
    echo "deploy: could not locate a checkout (script outside any git repo," >&2
    echo "        and no 'task' on PATH resolving into one)." >&2
    echo "        Pass --checkout DIR to name the checkout to update." >&2
    exit 1
  fi
fi

# Accept any git work tree, including one whose `.git` is a FILE (worktrees,
# submodules) — a `-d .git` check would wrongly reject those.
if ! git_clean -C "$CHECKOUT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "deploy: '$CHECKOUT' is not a git checkout." >&2
  echo "        task may be installed from a tarball rather than a clone;" >&2
  echo "        re-run install.sh to refresh that kind of install." >&2
  exit 1
fi

# Normalize an explicit `--checkout <repo>/subdir` to the repo root, so the
# post-deploy probe of `$CHECKOUT/bin/task` looks in the right place.
CHECKOUT="$(git_clean -C "$CHECKOUT" rev-parse --show-toplevel)"

git_c() { git_clean -C "$CHECKOUT" "$@"; }

echo "deploy: checkout = $CHECKOUT"

# A probe failure is PERSISTED (a marker file inside .git) so a later up-to-date
# run cannot report success over a still-broken install: after a deploy lands
# broken code, local == remote on every following run, and without the marker
# the health check would simply never run again.
# --path-format=absolute: --git-path answers relative to the CALLER's cwd,
# which is not the checkout — a relative marker path would land elsewhere.
probe_marker="$(git_c rev-parse --path-format=absolute --git-path deploy-probe-failed)"

# Health-probe the checkout's OWN shim (NOT whatever `task` is first on PATH —
# that binary may belong to a different install entirely). FATAL on failure:
# a checkout whose `task --version` fails or prints nothing (import error,
# syntax error, missing python3) is a broken install, and exiting 0 would make
# `rig apply` report success while the live CLI is unusable. Both the exit code
# AND non-empty output are required (a shim that prints an error banner and
# exits non-zero is just as broken). Captured without a `| head` pipe so
# pipefail/SIGPIPE can't fake a failure; the `|| probe_status=$?` keeps `set -e`
# from aborting before we can print the diagnostic.
verify_deployed_tool() {
  task_checkout="$CHECKOUT/bin/task"
  if [ ! -x "$task_checkout" ]; then
    echo "deploy: ERROR — checkout has no executable bin/task; the installed" >&2
    echo "        ~/.local/bin/task symlink points at nothing runnable." >&2
    echo "        Inspect the checkout (git -C $CHECKOUT log --oneline -3) and restore the shim." >&2
    touch "$probe_marker"
    exit 1
  fi
  probe_status=0
  task_version_out="$(bounded 30 "$task_checkout" --version 2>/dev/null)" || probe_status=$?
  task_version="${task_version_out%%$'\n'*}"
  if [ "$probe_status" -ne 0 ] || [ -z "$task_version" ]; then
    echo "deploy: ERROR — task failed the --version probe (exit $probe_status," >&2
    echo "        output: '${task_version:-<none>}'): import/syntax error in the" >&2
    echo "        code, or python3 missing. The installed CLI is broken until fixed." >&2
    touch "$probe_marker"
    exit 1
  fi
  echo "deploy: task --version -> $task_version"
  rm -f "$probe_marker"
}

# On a no-op run (up-to-date / ahead-only), re-probe ONLY when a previous run
# left the failure marker — the every-apply hot path stays a pure git check.
reprobe_if_previously_failed() {
  if [ -f "$probe_marker" ]; then
    echo "deploy: a previous deploy failed its health probe — re-probing."
    verify_deployed_tool
    echo "deploy: probe recovered — failure marker cleared."
  fi
}

# ── refuse to clobber a dirty tree ─────────────────────────────────────────────
# Only TRACKED changes block a fast-forward; untracked files (a stray .venv /
# editor temp files) do not, so exclude them. Capture into a var first so a
# `git status` FAILURE (locked index, corrupt repo) aborts under `set -e`
# instead of being read as "clean" inside `$(...)`.
dirty="$(git_c status --porcelain --untracked-files=no)"
if [ -n "$dirty" ]; then
  echo "deploy: checkout has local (tracked) changes — refusing to pull over them." >&2
  echo "        Commit, stash, or discard them, then re-run." >&2
  echo "$dirty" >&2
  exit 1
fi

branch="$(git_c rev-parse --abbrev-ref HEAD)"
if [ "$branch" = "HEAD" ]; then
  echo "deploy: checkout is in detached-HEAD state — no branch to pull." >&2
  echo "        Check out a branch (e.g. 'git -C $CHECKOUT switch main') first." >&2
  exit 1
fi
echo "deploy: branch  = $branch"

# ── fetch and measure divergence ───────────────────────────────────────────────
# Honor the branch's CONFIGURED upstream (`@{upstream}`) — a checkout whose branch
# tracks a differently-named remote (a fork tracking `upstream/main`) or a
# differently-named branch must deploy against what it actually tracks, not a
# hardcoded `origin/$branch`. Fall back to `origin/$branch` only when no upstream
# is configured (a plain `git clone` always configures one).
if upstream="$(git_c rev-parse --abbrev-ref --symbolic-full-name '@{upstream}' 2>/dev/null)"; then
  remote="${upstream%%/*}"
else
  remote="origin"
  upstream="origin/${branch}"
  echo "deploy: branch '$branch' has no configured upstream — assuming '$upstream'."
fi

# A failed fetch (no such remote, network/auth down) must be the documented
# friendly exit 1, not a raw set -e abort mid-script. It is also the step most
# likely to HANG (network/auth prompt), and rig apply cannot downgrade a hung
# deploy — so bound it when timeout(1) is available (generous: real fetches on
# a slow link are legitimate).
# GIT_TERMINAL_PROMPT=0 / GCM_INTERACTIVE=never: an unattended rig apply must
# never wedge on an HTTPS auth or credential-manager prompt — fail fast instead.
if ! bounded 120 env "${GIT_ENV_SCRUB[@]}" GIT_TERMINAL_PROMPT=0 GCM_INTERACTIVE=never \
     git -C "$CHECKOUT" fetch "$remote" --quiet; then
  echo "deploy: 'git fetch $remote' failed or timed out — check the remote/network/auth and re-run." >&2
  exit 1
fi
if ! git_c rev-parse --verify --quiet "$upstream" >/dev/null; then
  echo "deploy: no upstream ref '$upstream' — is this branch pushed?" >&2
  exit 1
fi

local_sha="$(git_c rev-parse HEAD)"
remote_sha="$(git_c rev-parse "$upstream")"

# DELIBERATE: the up-to-date and ahead-only exits below skip the routine CLI
# probe and install-skill refresh (unless a failure marker demands a re-probe).
# They are the every-`rig apply` hot path across all repos; a checkout broken
# LOCALLY is refused earlier by the dirty check (a deleted/chmod-ed tracked shim
# IS a tracked change), and a breakage that arrived via deploy left the marker.
if [ "$local_sha" = "$remote_sha" ]; then
  echo "deploy: already up to date ($(git_c rev-parse --short HEAD)) — nothing to do."
  reprobe_if_previously_failed
  exit 0
fi

# Ahead-only (the upstream has nothing the checkout lacks — only unpushed local
# commits) is NOT divergence: there is nothing to deploy, and hard-failing here
# would turn every unattended rig apply red until the commits are pushed. Report
# and succeed.
if git_c merge-base --is-ancestor "$upstream" HEAD; then
  echo "deploy: checkout is AHEAD of '$upstream' (unpushed local commits) — nothing to deploy."
  git_c log --oneline "$upstream..HEAD" | sed 's/^/  /'
  reprobe_if_previously_failed
  exit 0
fi

# Fast-forward only: refuse if the checkout and its upstream have truly diverged.
if ! git_c merge-base --is-ancestor HEAD "$upstream"; then
  echo "deploy: cannot fast-forward — '$branch' has diverged from '$upstream'." >&2
  echo "        A human must reconcile (rebase/merge). Aborting." >&2
  exit 2
fi

echo "deploy: $(git_c rev-parse --short HEAD) -> $(git_c rev-parse --short "$upstream"), commits to land:"
git_c log --oneline "HEAD..$upstream" | sed 's/^/  /'

# Does the deploy touch code a resident `task daemon run` process has in memory?
# The daemon imports tasklib (bin/task is just the shim in front of it). This only
# drives a WARNING — daemons are per-repo and not enumerable from here — so erring
# toward over-warning is the safe bias: a missed warning silently ships stale
# daemon behavior.
daemon_changed=0
# Capture first, then grep a here-string: under `set -o pipefail` a direct
# `git diff | grep -q` can report 141 (grep -q exits at the first match, git gets
# SIGPIPE on a large diff) and silently swallow the warning.
changed_files="$(git_c diff --name-only "HEAD..$upstream")"
if grep -qE '^(tasklib/|bin/task$)' <<<"$changed_files"; then
  daemon_changed=1
fi

# Preflight the DEPLOY TARGET's shim before touching the tree: refuse to
# fast-forward to a commit whose bin/task is missing or non-executable — the
# installed ~/.local/bin/task symlink would go dead, and by post-pull time the
# damage is already done. Checking `$upstream:bin/task`'s tree mode keeps the
# current (working) checkout intact on refusal.
upstream_shim_mode="$(git_c ls-tree "$upstream" -- bin/task | awk '{print $1}')"
if [ "$upstream_shim_mode" != "100755" ]; then
  echo "deploy: refusing — '$upstream' has no executable bin/task (mode: ${upstream_shim_mode:-absent})." >&2
  echo "        Deploying it would leave the installed symlink pointing at nothing" >&2
  echo "        runnable. Fix upstream first; this checkout was left untouched." >&2
  exit 1
fi

if [ "$DRY_RUN" = "1" ]; then
  echo "deploy: --dry-run — not pulling."
  [ "$daemon_changed" = "1" ] && echo "deploy: (dry-run) would require restarting any running 'task daemon' (runtime code changed)."
  exit 0
fi

# ── fast-forward to the already-validated upstream ─────────────────────────────
# Use `merge --ff-only "$upstream"` (not `pull`, which would re-fetch): we already
# fetched and validated `$upstream` is a strict descendant of HEAD, so this updates
# against the SAME object state the divergence check saw — no second fetch, no race
# window where a new push slips in unchecked.
# The one way this still fails after the clean/ancestor checks: an UNTRACKED local
# file colliding with a tracked file the upstream adds (untracked files
# deliberately don't block above, but git refuses to overwrite one). Surface that
# as the documented friendly exit 1 instead of a raw set -e abort.
# `--no-overwrite-ignore` makes git refuse to clobber IGNORED local files too
# (a stray .venv, local cache upstream starts tracking) — the default would
# silently overwrite them, and the dirty check deliberately skips untracked.
if ! git_c merge --ff-only --no-overwrite-ignore --quiet "$upstream"; then
  echo "deploy: fast-forward failed — most likely an untracked/ignored local file" >&2
  echo "        collides with a file this deploy adds (git refuses to overwrite it)." >&2
  echo "        Move/remove the file named in the git error above, then re-run." >&2
  exit 1
fi
new_sha="$(git_c rev-parse --short HEAD)"
echo "deploy: pulled — now at $new_sha"

# The symlink already points at the new files — health-probe them. FATAL on a
# broken result (see verify_deployed_tool), and the failure is persisted in the
# probe marker so later up-to-date runs keep failing until the install recovers.
verify_deployed_tool

# Re-register the agent skill (idempotent; keeps the skill file + blurb current).
bounded 30 "$task_checkout" install-skill >/dev/null 2>&1 \
  && echo "deploy: refreshed task skill (install-skill)" \
  || echo "deploy: WARNING — 'task install-skill' failed/timed out; re-run it manually." >&2

# A resident `task daemon run` keeps the pre-pull code until restarted. Daemons are
# per-repo (started with `task daemon start -C <repo>`), so we cannot enumerate or
# safely restart them from here — warn and leave them alone.
if [ "$daemon_changed" = "1" ]; then
  echo "deploy: ====================================================================" >&2
  echo "deploy: ACTION NEEDED — this deploy changed task runtime code (tasklib/)." >&2
  echo "deploy: Any running 'task daemon' still holds the OLD code. In each repo" >&2
  echo "deploy: with a daemon, restart it to apply:" >&2
  echo "deploy:     task daemon status   # is one running here?" >&2
  echo "deploy:     task daemon stop && task daemon start" >&2
  echo "deploy: ====================================================================" >&2
fi

echo "deploy: done — deployed $new_sha."
