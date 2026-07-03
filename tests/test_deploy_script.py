"""scripts/deploy.sh — the guarded fast-forward deploy rig apply runs (task-cli#43).

The install is a symlink chain (~/.local/bin/task -> <checkout>/bin/task), so deploying a
merged change is exactly one safe `git pull` of the live checkout. These tests pin the
script's whole exit-code contract against real throwaway git repos: 0 on up-to-date AND on
a successful fast-forward, 1 on env errors (dirty tracked tree, detached HEAD, not a repo,
bad usage), 2 on a diverged branch. Everything runs via subprocess — no mocks — because
the consumer (rig-cli's ``_run_tool_deploy``) invokes it the same way: ``bash <script>``.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DEPLOY = REPO_ROOT / "scripts" / "deploy.sh"


def _git_env(tmp_path: Path) -> dict[str, str]:
    """A clean git environment: no identity/config/GIT_* leakage from the host or a hook."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        {
            "GIT_CONFIG_GLOBAL": str(tmp_path / "gitconfig-none"),
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
            # Hermetic HOME/XDG: the script may run `task install-skill`, which
            # writes under ~/.agents, ~/.codex, ~/.claude — keep every possible
            # side effect inside tmp_path, never the developer's real HOME.
            "HOME": str(tmp_path),
            "XDG_CONFIG_HOME": str(tmp_path / "xdg-config"),
            "XDG_STATE_HOME": str(tmp_path / "xdg-state"),
        }
    )
    return env


def _write_stub_shim(repo: Path, body: str | None = None) -> Path:
    """A minimal committed bin/task shim (the deploy's post-pull check requires one)."""
    shim = repo / "bin" / "task"
    shim.parent.mkdir(exist_ok=True)
    shim.write_text(
        body
        or '#!/bin/sh\n[ "$1" = "--version" ] && echo "task 0.0.0-stub"\nexit 0\n',
        encoding="utf-8",
    )
    shim.chmod(0o755)
    return shim


def _git(env: dict[str, str], *args: str, cwd: Path) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


@pytest.fixture
def repos(tmp_path):
    """An `origin` repo with one commit and a `checkout` clone of it (like the install)."""
    env = _git_env(tmp_path)
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(env, "init", "-b", "main", cwd=origin)
    (origin / "file.txt").write_text("v1\n", encoding="utf-8")
    (origin / ".gitignore").write_text("*.ignored\n", encoding="utf-8")
    _write_stub_shim(origin)
    _git(env, "add", "file.txt", ".gitignore", "bin/task", cwd=origin)
    _git(env, "commit", "-m", "c1", cwd=origin)
    checkout = tmp_path / "checkout"
    _git(env, "clone", str(origin), str(checkout), cwd=tmp_path)
    return env, origin, checkout


def _origin_commit(env: dict[str, str], origin: Path, text: str = "v2\n") -> None:
    (origin / "file.txt").write_text(text, encoding="utf-8")
    _git(env, "add", "file.txt", cwd=origin)
    _git(env, "commit", "-m", f"c-{text.strip()}", cwd=origin)


def _deploy(env: dict[str, str], checkout: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(DEPLOY), "--checkout", str(checkout), *extra],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_up_to_date_exits_zero(repos):
    env, _origin, checkout = repos
    proc = _deploy(env, checkout)
    assert proc.returncode == 0, proc.stderr
    assert "already up to date" in proc.stdout


def test_fast_forward_deploys_and_exits_zero(repos):
    env, origin, checkout = repos
    _origin_commit(env, origin)
    proc = _deploy(env, checkout)
    assert proc.returncode == 0, proc.stderr
    assert "deploy: done" in proc.stdout
    local = _git(env, "rev-parse", "HEAD", cwd=checkout)
    remote = _git(env, "rev-parse", "HEAD", cwd=origin)
    assert local == remote
    assert (checkout / "file.txt").read_text(encoding="utf-8") == "v2\n"


def test_dry_run_reports_but_does_not_pull(repos):
    env, origin, checkout = repos
    before = _git(env, "rev-parse", "HEAD", cwd=checkout)
    _origin_commit(env, origin)
    proc = _deploy(env, checkout, "--dry-run")
    assert proc.returncode == 0, proc.stderr
    assert "not pulling" in proc.stdout
    assert _git(env, "rev-parse", "HEAD", cwd=checkout) == before


def test_dirty_tracked_tree_refuses_exit_one(repos):
    env, origin, checkout = repos
    _origin_commit(env, origin)
    (checkout / "file.txt").write_text("local edit\n", encoding="utf-8")
    proc = _deploy(env, checkout)
    assert proc.returncode == 1
    assert "local (tracked) changes" in proc.stderr


def test_untracked_files_do_not_block(repos):
    env, origin, checkout = repos
    _origin_commit(env, origin)
    (checkout / "stray.tmp").write_text("scratch\n", encoding="utf-8")
    proc = _deploy(env, checkout)
    assert proc.returncode == 0, proc.stderr
    assert "deploy: done" in proc.stdout


def test_detached_head_refuses_exit_one(repos):
    env, _origin, checkout = repos
    _git(env, "checkout", "--detach", "HEAD", cwd=checkout)
    proc = _deploy(env, checkout)
    assert proc.returncode == 1
    assert "detached-HEAD" in proc.stderr


def test_diverged_branch_exits_two(repos):
    env, origin, checkout = repos
    _origin_commit(env, origin)
    (checkout / "other.txt").write_text("local commit\n", encoding="utf-8")
    _git(env, "add", "other.txt", cwd=checkout)
    _git(env, "commit", "-m", "local-only", cwd=checkout)
    proc = _deploy(env, checkout)
    assert proc.returncode == 2
    assert "cannot fast-forward" in proc.stderr


def test_local_ahead_of_origin_exits_zero(repos):
    """Ahead-only (unpushed local commits, nothing new to pull) is NOT divergence:
    there is nothing to deploy, and exit 2 here would turn every unattended
    rig apply red until the commits are pushed."""
    env, _origin, checkout = repos
    (checkout / "other.txt").write_text("local commit\n", encoding="utf-8")
    _git(env, "add", "other.txt", cwd=checkout)
    _git(env, "commit", "-m", "local-only", cwd=checkout)
    proc = _deploy(env, checkout)
    assert proc.returncode == 0, proc.stderr
    assert "AHEAD" in proc.stdout
    assert "nothing to deploy" in proc.stdout


def test_branch_without_upstream_exits_one(repos):
    env, _origin, checkout = repos
    _git(env, "checkout", "-b", "feature/x", cwd=checkout)
    proc = _deploy(env, checkout)
    assert proc.returncode == 1
    assert "no upstream" in proc.stderr


def test_not_a_git_checkout_exits_one(tmp_path):
    env = _git_env(tmp_path)
    plain = tmp_path / "plain"
    plain.mkdir()
    proc = _deploy(env, plain)
    assert proc.returncode == 1
    assert "not a git checkout" in proc.stderr


def test_unknown_argument_exits_one(repos):
    env, _origin, checkout = repos
    proc = _deploy(env, checkout, "--bogus")
    assert proc.returncode == 1
    assert "unknown argument" in proc.stderr


def test_post_deploy_runs_version_and_install_skill(repos, tmp_path):
    """A checkout with an executable bin/task gets probed (--version) and refreshed
    (install-skill) after a successful fast-forward — via a stub that records its calls."""
    env, origin, checkout = repos
    calls = tmp_path / "calls.log"
    _write_stub_shim(
        origin,
        body=(
            "#!/bin/sh\n"
            f'echo "$1" >> "{calls}"\n'
            '[ "$1" = "--version" ] && echo "task 9.9.9"\n'
            "exit 0\n"
        ),
    )
    _git(env, "add", "bin/task", cwd=origin)
    _git(env, "commit", "-m", "add stub shim", cwd=origin)
    proc = _deploy(env, checkout)
    assert proc.returncode == 0, proc.stderr
    assert "task --version -> task 9.9.9" in proc.stdout
    assert "refreshed task skill" in proc.stdout
    logged = calls.read_text(encoding="utf-8").split()
    assert logged == ["--version", "install-skill"]


def test_daemon_warning_when_tasklib_changed(repos):
    env, origin, checkout = repos
    lib = origin / "tasklib"
    lib.mkdir()
    (lib / "__init__.py").write_text("", encoding="utf-8")
    _git(env, "add", "tasklib/__init__.py", cwd=origin)
    _git(env, "commit", "-m", "touch runtime code", cwd=origin)
    proc = _deploy(env, checkout)
    assert proc.returncode == 0, proc.stderr
    assert "ACTION NEEDED" in proc.stderr
    assert "task daemon stop && task daemon start" in proc.stderr


def test_no_daemon_warning_for_non_runtime_change(repos):
    env, origin, checkout = repos
    _origin_commit(env, origin)
    proc = _deploy(env, checkout)
    assert proc.returncode == 0, proc.stderr
    assert "ACTION NEEDED" not in proc.stderr


def test_checkout_subdir_normalizes_to_repo_root(repos):
    env, origin, checkout = repos
    sub = checkout / "docs"
    sub.mkdir()
    proc = _deploy(env, sub)
    assert proc.returncode == 0, proc.stderr
    assert f"checkout = {checkout}" in proc.stdout


def test_no_args_deploys_the_scripts_own_repo(repos, tmp_path):
    """The rig invocation path: `bash <repo>/scripts/deploy.sh` with NO args must update
    the repo the script lives in — never a `task` found on PATH (here: a console script
    inside a foreign repo, which must stay untouched)."""
    env, origin, checkout = repos
    scripts = checkout / "scripts"
    scripts.mkdir()
    deploy_copy = scripts / "deploy.sh"
    deploy_copy.write_bytes(DEPLOY.read_bytes())  # untracked — must not block the pull
    deploy_copy.chmod(0o755)
    foreign_repo = tmp_path / "foreign"
    (foreign_repo / ".venv" / "bin").mkdir(parents=True)
    _git(env, "init", "-b", "main", cwd=foreign_repo)
    console = foreign_repo / ".venv" / "bin" / "task"
    console.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    console.chmod(0o755)
    _origin_commit(env, origin)
    # CDPATH set: with it, a bare `cd` in a command substitution prints the
    # resolved dir to stdout — the script must not let that corrupt its paths.
    env = dict(
        env,
        PATH=f"{foreign_repo / '.venv' / 'bin'}:{env['PATH']}",
        CDPATH=f".:{tmp_path}",
    )
    proc = subprocess.run(
        ["bash", str(deploy_copy)],
        cwd=checkout,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "deploy: done" in proc.stdout
    assert _git(env, "rev-parse", "HEAD", cwd=checkout) == _git(
        env, "rev-parse", "HEAD", cwd=origin
    )


def test_daemon_warning_when_bin_task_changed(repos):
    """The `bin/task$` anchor of the runtime-change matcher, without any tasklib/ change."""
    env, origin, checkout = repos
    _write_stub_shim(
        origin,
        body='#!/bin/sh\n# changed shim\n[ "$1" = "--version" ] && echo "task 0.0.1-stub"\nexit 0\n',
    )
    _git(env, "add", "bin/task", cwd=origin)
    _git(env, "commit", "-m", "touch shim", cwd=origin)
    proc = _deploy(env, checkout)
    assert proc.returncode == 0, proc.stderr
    assert "ACTION NEEDED" in proc.stderr


def test_untracked_collision_exits_one_friendly(repos):
    """An untracked local file colliding with a file the upstream adds does not block
    the preflight (untracked files deliberately don't), but git refuses the ff merge —
    that must surface as the documented friendly exit 1, not a raw set -e abort."""
    env, origin, checkout = repos
    (origin / "new.txt").write_text("upstream version\n", encoding="utf-8")
    _git(env, "add", "new.txt", cwd=origin)
    _git(env, "commit", "-m", "add new.txt", cwd=origin)
    (checkout / "new.txt").write_text("local untracked\n", encoding="utf-8")
    proc = _deploy(env, checkout)
    assert proc.returncode == 1
    assert "fast-forward failed" in proc.stderr
    assert (checkout / "new.txt").read_text(encoding="utf-8") == "local untracked\n"


def test_ignored_file_collision_refused_not_clobbered(repos):
    """An IGNORED local file that upstream starts tracking: the dirty check
    deliberately skips it, but the merge must refuse (--no-overwrite-ignore)
    with the friendly exit 1 — never silently overwrite the local content."""
    env, origin, checkout = repos
    (origin / "local.ignored").write_text("upstream version\n", encoding="utf-8")
    _git(env, "add", "-f", "local.ignored", cwd=origin)
    _git(env, "commit", "-m", "track a previously ignored file", cwd=origin)
    (checkout / "local.ignored").write_text("my local data\n", encoding="utf-8")
    proc = _deploy(env, checkout)
    assert proc.returncode == 1
    assert "fast-forward failed" in proc.stderr
    assert (checkout / "local.ignored").read_text(encoding="utf-8") == "my local data\n"


def test_inherited_git_dir_env_is_scrubbed(repos, tmp_path):
    """When invoked from a git hook, GIT_DIR/GIT_WORK_TREE point at a FOREIGN repo and
    override `git -C`. The script must scrub them, deploying the requested checkout and
    leaving the foreign repo untouched (review-cli#72 bug class)."""
    env, origin, checkout = repos
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    _git(env, "init", "-b", "main", cwd=foreign)
    (foreign / "f.txt").write_text("x\n", encoding="utf-8")
    _git(env, "add", "f.txt", cwd=foreign)
    _git(env, "commit", "-m", "foreign c1", cwd=foreign)
    foreign_head = _git(env, "rev-parse", "HEAD", cwd=foreign)
    _origin_commit(env, origin)
    hook_env = dict(
        env,
        GIT_DIR=str(foreign / ".git"),
        GIT_WORK_TREE=str(foreign),
        GIT_INDEX_FILE=str(foreign / ".git" / "index"),
    )
    proc = _deploy(hook_env, checkout)
    assert proc.returncode == 0, proc.stderr
    assert "deploy: done" in proc.stdout
    assert _git(env, "rev-parse", "HEAD", cwd=checkout) == _git(
        env, "rev-parse", "HEAD", cwd=origin
    )
    assert _git(env, "rev-parse", "HEAD", cwd=foreign) == foreign_head


def test_fetch_failure_exits_one_friendly(repos, tmp_path):
    """A dead remote (network down, moved repo) must be the documented friendly
    exit 1, not a raw set -e abort mid-script."""
    env, _origin, checkout = repos
    _git(env, "remote", "set-url", "origin", str(tmp_path / "nonexistent"), cwd=checkout)
    proc = _deploy(env, checkout)
    assert proc.returncode == 1
    assert "git fetch" in proc.stderr


def test_honors_configured_upstream_remote(repos, tmp_path):
    """A checkout tracking a differently-named remote (fork workflow) deploys against
    its CONFIGURED @{upstream}, not a hardcoded origin/<branch>."""
    env, origin, checkout = repos
    _git(env, "remote", "rename", "origin", "upstream", cwd=checkout)
    _git(env, "branch", "--set-upstream-to=upstream/main", "main", cwd=checkout)
    _origin_commit(env, origin)
    proc = _deploy(env, checkout)
    assert proc.returncode == 0, proc.stderr
    assert "deploy: done" in proc.stdout
    assert _git(env, "rev-parse", "HEAD", cwd=checkout) == _git(
        env, "rev-parse", "HEAD", cwd=origin
    )


def test_deploy_removing_shim_refuses_before_pull(repos):
    """An upstream that deletes bin/task would leave the installed symlink dead.
    The preflight refuses BEFORE merging, so the working checkout stays intact."""
    env, origin, checkout = repos
    before = _git(env, "rev-parse", "HEAD", cwd=checkout)
    _git(env, "rm", "-q", "bin/task", cwd=origin)
    _git(env, "commit", "-m", "drop shim", cwd=origin)
    proc = _deploy(env, checkout)
    assert proc.returncode == 1
    assert "no executable bin/task" in proc.stderr
    assert "deploy: done" not in proc.stdout
    assert _git(env, "rev-parse", "HEAD", cwd=checkout) == before
    assert os.access(checkout / "bin" / "task", os.X_OK)


def test_deploy_de_executing_shim_refuses_before_pull(repos):
    """Same failure class via a mode change: an upstream chmod -x on the shim."""
    env, origin, checkout = repos
    before = _git(env, "rev-parse", "HEAD", cwd=checkout)
    _git(env, "update-index", "--chmod=-x", "bin/task", cwd=origin)
    _git(env, "commit", "-m", "de-exec shim", cwd=origin)
    proc = _deploy(env, checkout)
    assert proc.returncode == 1
    assert "no executable bin/task" in proc.stderr
    assert _git(env, "rev-parse", "HEAD", cwd=checkout) == before


def test_broken_deployed_tool_exits_one(repos):
    """A pulled checkout whose `task --version` prints nothing (import/syntax error,
    missing python3) is a BROKEN deploy: exit 1, never a silent 'done' — rig apply
    must show red instead of reporting a dead CLI as fresh."""
    env, origin, checkout = repos
    _write_stub_shim(origin, body="#!/bin/sh\nexit 0\n")
    _git(env, "add", "bin/task", cwd=origin)
    _git(env, "commit", "-m", "shim goes silent", cwd=origin)
    proc = _deploy(env, checkout)
    assert proc.returncode == 1
    assert "failed the --version probe" in proc.stderr
    assert "deploy: done" not in proc.stdout


def test_failed_probe_persists_until_recovery(repos):
    """A broken deploy leaves local == remote, so a naive rerun would report
    'up to date' and exit 0 over a dead CLI. The persisted probe marker keeps
    every rerun red until the install actually recovers — then goes green."""
    env, origin, checkout = repos
    _write_stub_shim(origin, body="#!/bin/sh\nexit 0\n")
    _git(env, "add", "bin/task", cwd=origin)
    _git(env, "commit", "-m", "shim goes silent", cwd=origin)
    proc = _deploy(env, checkout)
    assert proc.returncode == 1
    assert "failed the --version probe" in proc.stderr
    # Rerun: up-to-date, but the marker forces a re-probe — still red.
    proc = _deploy(env, checkout)
    assert proc.returncode == 1
    assert "already up to date" in proc.stdout
    assert "re-probing" in proc.stdout
    assert "failed the --version probe" in proc.stderr
    # Upstream fixes the shim; the next deploy pulls it, probes green, clears the marker.
    _write_stub_shim(origin)
    _git(env, "add", "bin/task", cwd=origin)
    _git(env, "commit", "-m", "fix shim", cwd=origin)
    proc = _deploy(env, checkout)
    assert proc.returncode == 0, proc.stderr
    assert "deploy: done" in proc.stdout
    # Steady state again: up-to-date, no re-probe, exit 0.
    proc = _deploy(env, checkout)
    assert proc.returncode == 0, proc.stderr
    assert "already up to date" in proc.stdout
    assert "re-probing" not in proc.stdout


def test_deployed_tool_nonzero_version_exit_is_fatal(repos):
    """A shim that prints an error banner but exits non-zero is just as broken:
    the probe must require BOTH exit 0 and non-empty output."""
    env, origin, checkout = repos
    _write_stub_shim(origin, body='#!/bin/sh\necho "task broken"\nexit 1\n')
    _git(env, "add", "bin/task", cwd=origin)
    _git(env, "commit", "-m", "shim errors out", cwd=origin)
    proc = _deploy(env, checkout)
    assert proc.returncode == 1
    assert "failed the --version probe" in proc.stderr
    assert "deploy: done" not in proc.stdout


def test_stray_script_refuses_foreign_console_script(repos, tmp_path):
    """PATH fallback (script outside any checkout): a package console-script `task`
    living in some OTHER repo's .venv/bin must be REFUSED, not fetched/merged —
    only a repo that looks like the legacy symlink checkout (bin/task + tasklib/)
    is an acceptable fallback target."""
    env, _origin, _checkout = repos
    stray = tmp_path / "stray"
    stray.mkdir()
    deploy_copy = stray / "deploy.sh"
    deploy_copy.write_bytes(DEPLOY.read_bytes())
    deploy_copy.chmod(0o755)
    foreign_repo = tmp_path / "foreign-venv-repo"
    (foreign_repo / ".venv" / "bin").mkdir(parents=True)
    _git(env, "init", "-b", "main", cwd=foreign_repo)
    console = foreign_repo / ".venv" / "bin" / "task"
    console.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    console.chmod(0o755)
    run_env = dict(env, PATH=f"{foreign_repo / '.venv' / 'bin'}:{env['PATH']}")
    proc = subprocess.run(
        ["bash", str(deploy_copy)],
        cwd=stray,
        env=run_env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 1
    assert "foreign repository" in proc.stderr


def test_stray_script_refuses_console_script_even_in_tasklike_repo(repos, tmp_path):
    """The PATH fallback requires the `task` on PATH to BE the candidate repo's own
    bin/task shim. A console script in a repo that merely CONTAINS bin/task + tasklib/
    (another checkout's .venv) fails the identity check and is refused."""
    env, _origin, _checkout = repos
    stray = tmp_path / "stray2"
    stray.mkdir()
    deploy_copy = stray / "deploy.sh"
    deploy_copy.write_bytes(DEPLOY.read_bytes())
    deploy_copy.chmod(0o755)
    lookalike = tmp_path / "lookalike"
    (lookalike / ".venv" / "bin").mkdir(parents=True)
    (lookalike / "tasklib").mkdir()
    (lookalike / "tasklib" / "__init__.py").write_text("", encoding="utf-8")
    _git(env, "init", "-b", "main", cwd=lookalike)
    _write_stub_shim(lookalike)
    console = lookalike / ".venv" / "bin" / "task"
    console.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    console.chmod(0o755)
    run_env = dict(env, PATH=f"{lookalike / '.venv' / 'bin'}:{env['PATH']}")
    proc = subprocess.run(
        ["bash", str(deploy_copy)],
        cwd=stray,
        env=run_env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 1
    assert "foreign repository" in proc.stderr


def test_stray_script_follows_symlink_install_to_checkout(repos, tmp_path):
    """The legitimate PATH fallback: a ~/.local/bin-style `task` symlink pointing at
    <checkout>/bin/task leads the stray script to deploy exactly that checkout."""
    env, origin, checkout = repos
    stray = tmp_path / "stray3"
    stray.mkdir()
    deploy_copy = stray / "deploy.sh"
    deploy_copy.write_bytes(DEPLOY.read_bytes())
    deploy_copy.chmod(0o755)
    (checkout / "tasklib").mkdir()
    (checkout / "tasklib" / "__init__.py").write_text("", encoding="utf-8")  # untracked ok
    link_bin = tmp_path / "linkbin"
    link_bin.mkdir()
    (link_bin / "task").symlink_to(checkout / "bin" / "task")
    _origin_commit(env, origin)
    run_env = dict(env, PATH=f"{link_bin}:{env['PATH']}")
    proc = subprocess.run(
        ["bash", str(deploy_copy)],
        cwd=stray,
        env=run_env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "deploy: done" in proc.stdout
    assert _git(env, "rev-parse", "HEAD", cwd=checkout) == _git(
        env, "rev-parse", "HEAD", cwd=origin
    )


def test_help_exits_zero(tmp_path):
    env = _git_env(tmp_path)
    proc = subprocess.run(
        ["bash", str(DEPLOY), "--help"], env=env, capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0
    assert "Exit codes" in proc.stdout
