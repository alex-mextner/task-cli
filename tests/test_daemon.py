"""Daemon — due-date selection, de-dupe, fail-soft loop, lifecycle, and the --due round-trip.

Pure/unit coverage of the watcher core (no real backend, no real notifier, no real spawn for
the selection/dedupe paths). The lifecycle tests exercise the pid-file + liveness helpers with
this process's own pid (no detached spawn needed to prove idempotent-start / stop semantics).
"""

from __future__ import annotations

import json
import os
import signal
import time
from datetime import date
from pathlib import Path

import pytest

from tasklib import daemon
from tasklib.model import State, Ticket


# ── due-date selection (the unit-tested heart) ──────────────────────────────────────


def _t(tid: str, due: str, state: State = State.TODO, title: str = "x") -> Ticket:
    return Ticket(title=title, due=due, state=state, id=tid)


TODAY = date(2026, 7, 1)


def test_select_due_picks_overdue_and_due_soon_only():
    tickets = [
        _t("#1", "2026-06-20", State.IN_PROGRESS),  # overdue
        _t("#2", "2026-07-02"),  # due in 1d (within 3)
        _t("#3", "2026-07-04"),  # due in 3d (boundary, inclusive)
        _t("#4", "2026-07-05"),  # due in 4d (outside)
        _t("#5", ""),  # no due date
        _t("#6", "not-a-date"),  # garbage
        _t("#7", "2026-06-01", State.DONE),  # overdue but DONE
        _t("#8", "2026-06-01", State.CANCELLED),  # overdue but CANCELLED
    ]
    got = [t.id for t in daemon.select_due(tickets, today=TODAY, due_soon_days=3)]
    assert got == ["#1", "#2", "#3"]


def test_select_due_boundary_is_inclusive():
    # exactly today + window qualifies; one day past does not
    in_window = _t("#a", "2026-07-04")
    out_window = _t("#b", "2026-07-05")
    got = [t.id for t in daemon.select_due([in_window, out_window], today=TODAY, due_soon_days=3)]
    assert got == ["#a"]


def test_select_due_zero_window_is_only_due_today_or_overdue():
    got = [
        t.id
        for t in daemon.select_due(
            [_t("#today", "2026-07-01"), _t("#tomorrow", "2026-07-02"), _t("#past", "2026-06-30")],
            today=TODAY,
            due_soon_days=0,
        )
    ]
    assert got == ["#today", "#past"]


def test_is_open():
    assert daemon.is_open(_t("#1", "", State.TODO))
    assert daemon.is_open(_t("#1", "", State.IN_PROGRESS))
    assert daemon.is_open(_t("#1", "", State.IN_REVIEW))
    assert not daemon.is_open(_t("#1", "", State.DONE))
    assert not daemon.is_open(_t("#1", "", State.CANCELLED))


# ── de-dupe ─────────────────────────────────────────────────────────────────────────


def test_needs_notify_dedupes_same_ticket_and_due():
    t = _t("#1", "2026-07-02")
    notified: dict[str, str] = {}
    assert daemon.needs_notify(t, notified)
    notified[t.id] = t.due
    assert not daemon.needs_notify(t, notified)


def test_needs_notify_refires_on_changed_due_date():
    notified = {"#1": "2026-07-02"}
    moved = _t("#1", "2026-06-25")  # the deadline moved → notify again
    assert daemon.needs_notify(moved, notified)


def test_notified_state_roundtrips(tmp_path):
    p = tmp_path / "notified.json"
    daemon.save_notified(p, {"#1": "2026-07-02", "#2": "2026-07-03"})
    assert daemon.load_notified(p) == {"#1": "2026-07-02", "#2": "2026-07-03"}


def test_load_notified_tolerates_missing_and_corrupt(tmp_path):
    assert daemon.load_notified(tmp_path / "absent.json") == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert daemon.load_notified(bad) == {}
    notlist = tmp_path / "list.json"
    notlist.write_text(json.dumps(["a", "b"]), encoding="utf-8")
    assert daemon.load_notified(notlist) == {}


# ── the tick: query → select → notify → dedupe, all fail-soft ───────────────────────


class _FakeBackend:
    def __init__(self, tickets: list[Ticket]) -> None:
        self.tickets = tickets

    def list(self, *, labels=None, state=None, limit=30) -> list[Ticket]:
        return list(self.tickets)


class _BrokenBackend:
    def list(self, **_kw):
        raise RuntimeError("backend exploded")


@pytest.fixture
def paths(tmp_path) -> daemon.DaemonPaths:
    return daemon.DaemonPaths(pid=tmp_path / "p.pid", state=tmp_path / "s.json", log=tmp_path / "l.log")


@pytest.fixture
def capture_notifications(monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr(daemon, "notify", lambda msg, notifier: (sent.append(msg) or True))
    return sent


def _dcfg() -> daemon.DaemonConfig:
    return daemon.DaemonConfig(interval_s=1, due_soon_days=3, notifier=("tg",), enabled=True)


def test_run_tick_notifies_due_and_persists_dedupe(paths, capture_notifications):
    be = _FakeBackend([_t("#1", "2026-06-20", State.IN_PROGRESS), _t("#2", "2026-07-02"), _t("#3", "2026-12-01")])
    n1 = daemon.run_tick(be, _dcfg(), paths, today=TODAY)
    assert n1 == 2
    assert len(capture_notifications) == 2
    # second tick: everything already notified → 0 new sends
    n2 = daemon.run_tick(be, _dcfg(), paths, today=TODAY)
    assert n2 == 0
    assert len(capture_notifications) == 2
    # the dedupe state is durable
    assert daemon.load_notified(paths.state) == {"#1": "2026-06-20", "#2": "2026-07-02"}


def test_run_tick_survives_a_backend_error(paths, capture_notifications):
    # a backend error in a tick must be caught, logged, and yield 0 — never raise out of the loop
    n = daemon.run_tick(_BrokenBackend(), _dcfg(), paths, today=TODAY)
    assert n == 0
    assert capture_notifications == []


def test_run_tick_leaves_unnotified_on_notifier_failure(paths, monkeypatch):
    # a down notifier must NOT mark the ticket notified — it retries next tick
    monkeypatch.setattr(daemon, "notify", lambda msg, notifier: False)
    be = _FakeBackend([_t("#1", "2026-06-20")])
    n = daemon.run_tick(be, _dcfg(), paths, today=TODAY)
    assert n == 0
    assert daemon.load_notified(paths.state) == {}


def test_run_tick_refires_when_due_date_changes(paths, capture_notifications):
    be = _FakeBackend([_t("#1", "2026-07-02")])
    daemon.run_tick(be, _dcfg(), paths, today=TODAY)
    assert len(capture_notifications) == 1
    # move the deadline and tick again → re-notify
    be.tickets = [_t("#1", "2026-06-28")]
    daemon.run_tick(be, _dcfg(), paths, today=TODAY)
    assert len(capture_notifications) == 2


def test_run_tick_prunes_dedupe_for_no_longer_due_tickets(paths, capture_notifications):
    # the state file must not grow forever: once a ticket leaves the due set (closed / resolved /
    # due-date moved far out) its dedupe record is pruned
    be = _FakeBackend([_t("#1", "2026-06-20"), _t("#2", "2026-07-02")])
    daemon.run_tick(be, _dcfg(), paths, today=TODAY)
    assert set(daemon.load_notified(paths.state)) == {"#1", "#2"}
    # #1 is closed (drops out of the due set); #2 stays
    be.tickets = [_t("#1", "2026-06-20", State.DONE), _t("#2", "2026-07-02")]
    daemon.run_tick(be, _dcfg(), paths, today=TODAY)
    assert set(daemon.load_notified(paths.state)) == {"#2"}


def test_run_tick_skips_id_less_tickets(paths, capture_notifications):
    # an id-less ticket can't be de-duped (all empty ids collide) → it is skipped, not mis-keyed
    be = _FakeBackend([_t("", "2026-06-20"), _t("#1", "2026-06-20")])
    n = daemon.run_tick(be, _dcfg(), paths, today=TODAY)
    assert n == 1
    assert set(daemon.load_notified(paths.state)) == {"#1"}


def test_query_limit_is_configurable(paths, capture_notifications):
    # the per-tick fetch cap is honored from config (a big project can raise it)
    seen: list[int] = []

    class _RecordingBackend:
        def list(self, *, labels=None, state=None, limit=30):
            seen.append(limit)
            return []

    dcfg = daemon.DaemonConfig(interval_s=1, due_soon_days=3, notifier=("tg",), query_limit=250)
    daemon.run_tick(_RecordingBackend(), dcfg, paths, today=TODAY)
    assert seen == [250]


def test_run_loop_honors_disabled(tmp_path, monkeypatch, capture_notifications):
    # `task daemon run` (run_loop) must NOT loop when daemon.enabled is false — even invoked
    # directly, bypassing the CLI's start-time check
    from tasklib.config import LoadedConfig

    cfg = LoadedConfig(data={"backend": "github-issues", "daemon": {"enabled": False}}, repo_root=tmp_path)
    be = _FakeBackend([_t("#1", "2026-06-20")])
    monkeypatch.setattr("tasklib.backends.get_backend", lambda c, env=None: be)
    paths = daemon.DaemonPaths(pid=tmp_path / "p.pid", state=tmp_path / "s.json", log=tmp_path / "l.log")
    rc = daemon.run_loop(cfg, paths, max_ticks=5)
    assert rc == 0
    assert capture_notifications == []  # never ticked
    assert daemon.read_pid(paths.pid) is None  # never even wrote a pid-file


def test_run_loop_max_ticks_bounds_the_loop(tmp_path, monkeypatch, capture_notifications):
    # run_loop with max_ticks must terminate; it writes then clears its pid-file. A tiny interval
    # keeps the inter-tick sleep from blocking the test for an hour.
    from tasklib.config import LoadedConfig

    cfg = LoadedConfig(data={"backend": "github-issues", "daemon": {"interval_s": 1}}, repo_root=tmp_path)
    be = _FakeBackend([_t("#1", "2026-06-20")])
    monkeypatch.setattr("tasklib.backends.get_backend", lambda c, env=None: be)
    paths = daemon.DaemonPaths(pid=tmp_path / "p.pid", state=tmp_path / "s.json", log=tmp_path / "l.log")
    rc = daemon.run_loop(cfg, paths, max_ticks=1)
    assert rc == 0
    assert len(capture_notifications) == 1
    # pid-file cleared on exit (finally)
    assert daemon.read_pid(paths.pid) is None


def test_run_loop_survives_backend_construction_failure(tmp_path, monkeypatch):
    from tasklib.config import LoadedConfig

    # interval_s: 1 keeps the single inter-tick sleep (between tick 1 and tick 2) short
    cfg = LoadedConfig(data={"backend": "github-issues", "daemon": {"interval_s": 1}}, repo_root=tmp_path)

    def _boom(c, env=None):
        raise RuntimeError("creds missing")

    monkeypatch.setattr("tasklib.backends.get_backend", _boom)
    paths = daemon.DaemonPaths(pid=tmp_path / "p.pid", state=tmp_path / "s.json", log=tmp_path / "l.log")
    # must not raise — a tick that can't even build the backend is skipped, the loop ends cleanly
    assert daemon.run_loop(cfg, paths, max_ticks=2) == 0


# ── pid-file + liveness lifecycle ───────────────────────────────────────────────────


def test_pid_status_stopped_running_stale(tmp_path, monkeypatch):
    pid_path = tmp_path / "x.pid"
    assert daemon.pid_status(pid_path) == ("stopped", None)

    # A live pid whose argv identifies as our daemon → "running". The pytest process isn't shaped
    # like `task daemon run`, so make its cmdline read as the daemon (pid_status is identity-aware
    # now — #32 — so a bare-liveness "running" requires a daemon-shaped argv).
    monkeypatch.setattr(daemon, "process_cmdline", lambda _pid: ["python", "-m", "tasklib", "daemon", "run"])
    daemon._write_pid(pid_path, os.getpid())
    assert daemon.pid_status(pid_path) == ("running", os.getpid())

    daemon._write_pid(pid_path, 2**30)  # a pid that cannot be alive
    status, pid = daemon.pid_status(pid_path)
    assert status == "stale" and pid == 2**30


def test_pid_status_recycled_foreign_pid_is_not_ours(tmp_path, monkeypatch):
    # #32: a pid-file whose pid is ALIVE but is a recycled, FOREIGN process must read as "not-ours"
    # in status/start too — mirroring stop's identity guard — not a bare-liveness "running".
    pid_path = tmp_path / "x.pid"
    daemon._write_pid(pid_path, os.getpid())  # alive…
    monkeypatch.setattr(daemon, "process_cmdline", lambda _pid: ["some", "unrelated", "process"])  # …but foreign
    assert daemon.pid_status(pid_path) == ("not-ours", os.getpid())


def test_pid_status_unreadable_cmdline_stays_running(tmp_path, monkeypatch):
    # an UNREADABLE cmdline (busybox ps / no /proc → IDENTITY_UNKNOWN) must NOT be called foreign:
    # it stays "running" so status/start never mis-report (or double-spawn over) a real daemon whose
    # argv just can't be read — the same reason stop preserves pre-guard signalling on UNKNOWN.
    pid_path = tmp_path / "x.pid"
    daemon._write_pid(pid_path, os.getpid())
    monkeypatch.setattr(daemon, "process_cmdline", lambda _pid: None)  # can't read → UNKNOWN
    assert daemon.pid_status(pid_path) == ("running", os.getpid())


def test_read_pid_rejects_garbage(tmp_path):
    p = tmp_path / "p"
    p.write_text("notapid", encoding="utf-8")
    assert daemon.read_pid(p) is None
    p.write_text("-5", encoding="utf-8")
    assert daemon.read_pid(p) is None
    p.write_text("  12345\n", encoding="utf-8")
    assert daemon.read_pid(p) == 12345


def test_read_pid_empty_and_whitespace(tmp_path):
    # an empty or whitespace-only first line → None (not a crash)
    p = tmp_path / "p"
    p.write_text("", encoding="utf-8")
    assert daemon.read_pid(p) is None
    p.write_text("   \n", encoding="utf-8")
    assert daemon.read_pid(p) is None
    p.write_text("\n123\n", encoding="utf-8")  # blank first line
    assert daemon.read_pid(p) is None


def test_clear_pid_if_matches_only_clears_the_recorded_pid(tmp_path):
    # the race guard: clearing the pid-file must NOT delete a DIFFERENT (freshly-restarted) daemon's
    # file — only when it still records the pid we were stopping
    p = tmp_path / "x.pid"
    daemon._write_pid(p, 111)
    daemon._clear_pid_if_matches(p, 222)  # file says 111, we hold 222 → must NOT clear
    assert daemon.read_pid(p) == 111
    daemon._clear_pid_if_matches(p, 111)  # matches → cleared
    assert daemon.read_pid(p) is None


def test_is_alive():
    assert daemon.is_alive(os.getpid()) is True
    assert daemon.is_alive(2**30) is False


class _FakePopen:
    """Stands in for the real ``Popen`` :func:`daemon._spawn_detached` now returns — ``start()``
    calls ``.poll()`` on it (see :func:`daemon._daemon_bootstrapped`), so a bare mocked pid no
    longer works as a stand-in."""

    def __init__(self, pid: int, *, returncode: int | None = None):
        self.pid = pid
        self.returncode = returncode

    def poll(self):
        return self.returncode


def test_start_is_idempotent_when_already_running(tmp_path, monkeypatch):
    # a live pid-file whose process IS our daemon → start is a no-op (never double-spawns). We point
    # the pid-file at this live test process and make its cmdline read as the daemon (pid_status is
    # identity-aware now), then assert _spawn_detached is NOT called.
    env = {"XDG_STATE_HOME": str(tmp_path), "HOME": str(tmp_path)}
    paths = daemon.paths_for("owner/repo", env=env)
    daemon._write_pid(paths.pid, os.getpid())
    monkeypatch.setattr(daemon, "process_cmdline", lambda _pid: ["python", "-m", "tasklib", "daemon", "run"])

    spawned: list = []
    monkeypatch.setattr(daemon, "_spawn_detached", lambda *a, **k: spawned.append(1) or _FakePopen(999))
    outcome, pid = daemon.start("owner/repo", env=env, ready_timeout_s=0)
    assert outcome == "already-running"
    assert pid == os.getpid()
    assert spawned == []  # the guard prevented a second spawn


def test_start_clears_recycled_foreign_pid_then_spawns(tmp_path, monkeypatch):
    # #32: a pid-file pointing at a recycled FOREIGN pid must NOT be treated as already-running —
    # start clears the misleading file and spawns a fresh daemon (consistent with the new status).
    env = {"XDG_STATE_HOME": str(tmp_path), "HOME": str(tmp_path)}
    paths = daemon.paths_for("owner/repo", env=env)
    daemon._write_pid(paths.pid, os.getpid())  # alive…
    monkeypatch.setattr(daemon, "process_cmdline", lambda _pid: ["unrelated", "recycled", "process"])  # …foreign

    # SAFETY: start must NEVER deliver a real signal to the foreign pid (that's stop's job, and even
    # stop refuses) — it only stops trusting the pid-file. Spy on os.kill recording only REAL signals
    # (sig != 0); the sig-0 liveness probe in is_alive must still pass through to the real kill.
    signalled: list = []
    real_kill = daemon.os.kill

    def _spy_kill(p, sig):
        if sig != 0:
            signalled.append((p, sig))
            return
        return real_kill(p, sig)  # let the liveness probe work

    monkeypatch.setattr(daemon.os, "kill", _spy_kill)
    monkeypatch.setattr(daemon, "_spawn_detached", lambda *a, **k: _FakePopen(5555))
    outcome, pid = daemon.start("owner/repo", env=env, ready_timeout_s=0)
    assert outcome == "started"
    assert pid == 5555
    assert signalled == [], "start must not deliver a real signal to the recycled foreign pid"
    # the misleading foreign pid-file was cleared (the spawned child writes its own in run_loop, which
    # _spawn_detached is mocked away here — so the file is simply gone, not the old foreign pid).
    assert daemon.read_pid(paths.pid) is None


def test_start_clears_stale_then_spawns(tmp_path, monkeypatch):
    env = {"XDG_STATE_HOME": str(tmp_path), "HOME": str(tmp_path)}
    paths = daemon.paths_for("owner/repo", env=env)
    daemon._write_pid(paths.pid, 2**30)  # stale

    monkeypatch.setattr(daemon, "_spawn_detached", lambda *a, **k: _FakePopen(4242))
    outcome, pid = daemon.start("owner/repo", env=env, ready_timeout_s=0)
    assert outcome == "started"
    assert pid == 4242


def test_start_forwards_child_flags_to_the_spawn(tmp_path, monkeypatch):
    # the backend-selecting flags must reach the child so it resolves the SAME coordinate
    env = {"XDG_STATE_HOME": str(tmp_path), "HOME": str(tmp_path)}
    captured: dict = {}
    monkeypatch.setattr(
        daemon, "_spawn_detached", lambda **k: captured.update(k) or _FakePopen(7)
    )
    daemon.start("owner/repo", env=env, child_flags=["--repo", "owner/repo"], ready_timeout_s=0)
    assert captured["child_flags"] == ["--repo", "owner/repo"]


def test_spawn_detached_argv_includes_child_flags(tmp_path, monkeypatch):
    # the forwarded flags actually land in the child argv after -C
    seen: dict = {}

    class _FakeProc:
        pid = 123

    def fake_popen(argv, **kw):
        seen["argv"] = argv
        return _FakeProc()

    monkeypatch.setattr(daemon.subprocess, "Popen", fake_popen)
    daemon._spawn_detached(cwd=str(tmp_path), log_path=tmp_path / "l.log", child_flags=["--backend", "linear"])
    argv = seen["argv"]
    # -P (PYTHONSAFEPATH) precedes -m: it suppresses Python's auto-prepend of the child's cwd
    # to sys.path, closing the "managed project ships its own tasklib/" shadowing hole.
    assert argv[1:6] == ["-P", "-m", "tasklib", "daemon", "run"]
    assert argv[-2:] == ["--backend", "linear"]


# ── task-cli#63: refuse to spawn at all on a pre-3.11 interpreter (-P needs 3.11+) ──


def test_start_refuses_on_a_pre_311_interpreter_without_spawning(tmp_path, monkeypatch):
    # A stray Python 3.10 symlink install must never reach _spawn_detached at all: passing -P
    # unconditionally there would crash the child ("Unknown option: -P") -- the started-then-
    # stopped flap task-cli#57/#58 fixed -- and OMITTING -P there would silently spawn the
    # daemon without the cwd-shadowing protection -P exists to provide (task-cli#63 review
    # finding: an earlier version chose that unsafe fallback). start() must refuse up front.
    env = {"XDG_STATE_HOME": str(tmp_path), "HOME": str(tmp_path)}
    spawned: list = []
    monkeypatch.setattr(daemon, "_spawn_detached", lambda *a, **k: spawned.append(1))
    monkeypatch.setattr(daemon, "python_supports_safepath", lambda: False)
    outcome, pid = daemon.start("owner/repo", env=env)
    assert outcome == "unsupported-interpreter"
    assert pid is None
    assert spawned == [], "must not attempt to spawn at all on an unsupported interpreter"


def test_start_checks_already_running_before_the_interpreter_version(tmp_path, monkeypatch):
    # Codex review finding: the version gate must not shadow "a daemon is already running" --
    # start()'s idempotency contract ("already-running is always detected first") must hold
    # even when invoked from a pre-3.11 interpreter that could never have spawned one itself.
    env = {"XDG_STATE_HOME": str(tmp_path), "HOME": str(tmp_path)}
    paths = daemon.paths_for("owner/repo", env=env)
    daemon._write_pid(paths.pid, os.getpid())
    monkeypatch.setattr(daemon, "process_cmdline", lambda _pid: ["python", "-m", "tasklib", "daemon", "run"])
    monkeypatch.setattr(daemon, "python_supports_safepath", lambda: False)
    outcome, pid = daemon.start("owner/repo", env=env)
    assert outcome == "already-running"
    assert pid == os.getpid()


def test_spawn_detached_always_passes_safepath_flag(tmp_path, monkeypatch):
    # start() is the ONLY production caller and already refused on a pre-3.11 interpreter (see
    # above), so by the time _spawn_detached runs, -P is always safe to pass unconditionally.
    seen: dict = {}

    class _FakeProc:
        pid = 123

    def fake_popen(argv, **kw):
        seen["argv"] = argv
        return _FakeProc()

    monkeypatch.setattr(daemon.subprocess, "Popen", fake_popen)
    daemon._spawn_detached(cwd=str(tmp_path), log_path=tmp_path / "l.log", child_flags=[])
    assert seen["argv"][1] == "-P"


# ── task-cli#64: no PYTHONPATH bootstrap for a pip/pipx (non-checkout) install layout ──


def test_needs_pythonpath_bootstrap_true_for_a_checkout_with_bin_task(tmp_path):
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "task").write_text("#!/bin/sh\n", encoding="utf-8")
    assert daemon._needs_pythonpath_bootstrap(tmp_path) is True


def test_needs_pythonpath_bootstrap_false_for_a_pip_pipx_install_layout(tmp_path):
    # no sibling bin/task -- this is what _repo_root() resolves to for an installed package
    # (tasklib's parent directory, e.g. site-packages), which never has a bin/task shim.
    assert daemon._needs_pythonpath_bootstrap(tmp_path) is False


def test_spawn_detached_skips_repo_root_injection_for_a_pip_pipx_install(tmp_path, monkeypatch):
    # no repo_root prepend and no bootstrap markers for this layout (task-cli#64) -- but the
    # inherited PYTHONPATH is still present (sanitized, not dropped -- see the sanitization test
    # right below, which proves it's not just passed through raw either).
    seen: dict = {}

    class _FakeProc:
        pid = 123

    def fake_popen(argv, **kw):
        seen.update(kw)
        return _FakeProc()

    monkeypatch.setattr(daemon.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(daemon, "_repo_root", lambda: tmp_path)  # no bin/task under tmp_path
    monkeypatch.setenv("PYTHONPATH", "/kept-as-is")
    daemon._spawn_detached(cwd=str(tmp_path), log_path=tmp_path / "l.log", child_flags=[])
    env = seen["env"]
    assert env is not None
    assert str(tmp_path) not in env["PYTHONPATH"]  # repo_root itself never injected
    assert daemon._BOOTSTRAP_MARKER not in env
    assert env["PYTHONPATH"] == "/kept-as-is"


def test_spawn_detached_still_sanitizes_pythonpath_for_a_pip_pipx_install(tmp_path, monkeypatch):
    # Fable/Codex review finding: an earlier version disabled PYTHONPATH sanitization ENTIRELY
    # for this layout (passed env=None straight through), which let an inherited relative entry
    # (e.g. ".") survive unresolved and reopen the cwd-shadowing hole for the TARGET repo -- the
    # same class of hole -P closes for auto-prepended entries, just for explicit PYTHONPATH
    # content instead. Sanitization (resolve-relative-against-parent-cwd + dedupe) must still run.
    seen: dict = {}

    class _FakeProc:
        pid = 123

    def fake_popen(argv, **kw):
        seen.update(kw)
        return _FakeProc()

    monkeypatch.setattr(daemon.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(daemon, "_repo_root", lambda: tmp_path)  # no bin/task under tmp_path
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYTHONPATH", f".{os.pathsep}.{os.pathsep}/kept")
    daemon._spawn_detached(cwd=str(tmp_path), log_path=tmp_path / "l.log", child_flags=[])
    env = seen["env"]
    assert env["PYTHONPATH"] == f"{tmp_path}{os.pathsep}/kept"  # "." resolved + deduped


def test_sanitized_child_env_drops_a_stale_inherited_bootstrap_marker(monkeypatch):
    # review finding: if the PARENT process's own env happened to carry a bootstrap marker (e.g.
    # a daemon relaunched from within an environment that itself was once bootstrap-injected),
    # _sanitized_child_env must not let it survive into this non-injecting spawn's env -- a later
    # _scrub_bootstrap_pythonpath call in that child would otherwise trust a stale marker and try
    # to restore a snapshot that was never actually taken for THIS child.
    base_env = {
        "PYTHONPATH": "/kept",
        daemon._BOOTSTRAP_MARKER: "/stale/repo/root",
        daemon._BOOTSTRAP_ORIGINAL_PYTHONPATH_MARKER: "/stale/original",
    }
    env = daemon._sanitized_child_env(base_env=base_env)
    assert daemon._BOOTSTRAP_MARKER not in env
    assert daemon._BOOTSTRAP_ORIGINAL_PYTHONPATH_MARKER not in env
    assert env["PYTHONPATH"] == "/kept"


def test_sanitized_child_env_leaves_pythonpath_unset_when_the_caller_had_none(monkeypatch):
    # Fable review finding: a pip/pipx install with NO PYTHONPATH at all must come out with none
    # (the var popped entirely), not an empty string.
    base_env = {"SOME_OTHER_VAR": "x"}
    env = daemon._sanitized_child_env(base_env=base_env)
    assert "PYTHONPATH" not in env
    assert env["SOME_OTHER_VAR"] == "x"


def _reap(pid: int, timeout: float = 8.0) -> int | None:
    """Wait up to ``timeout``s for a detached ``pid`` to exit; return its exit code, or ``None``
    when the exit code could NOT be verified (timeout, OR the rarer GC race below) — NEVER a
    guessed/sentinel "success", so ``assert rc == 0`` fails loudly instead of silently passing
    for a child whose actual outcome is unknown.

    The GC race: the caller never keeps the ``Popen`` object ``_spawn_detached`` creates (only
    the bare pid), so once the child has already exited, CPython MAY finalize and
    garbage-collect that discarded ``Popen`` before we get to poll — confirmed empirically:
    ``Popen.__del__`` performs its OWN reaping ``waitpid`` in that case, so our subsequent
    ``waitpid`` raises ``ChildProcessError: No child processes`` on the now-already-reaped pid.
    We cannot recover the real exit code here, so we report ``None`` rather than assume 0.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            wpid, status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            return None
        if wpid == pid:
            return os.waitstatus_to_exitcode(status)
        time.sleep(0.1)
    try:  # still alive at the deadline — kill + reap so no orphan/zombie leaks
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
    except (ProcessLookupError, ChildProcessError):
        pass
    return None


def _write_decoy_tasklib(root, sentinel) -> None:
    """Plant a decoy top-level ``tasklib/`` package under ``root`` that, if imported and run as
    ``__main__``, writes ``sentinel`` and exits 0 — WITHOUT ever reaching the real tasklib's
    config-driven early exit. So "the child exited 0" alone can't pass a test built on this;
    the sentinel's absence is what actually proves the decoy never executed.
    """
    decoy_pkg = root / "tasklib"
    decoy_pkg.mkdir()
    (decoy_pkg / "__init__.py").write_text("", encoding="utf-8")
    (decoy_pkg / "__main__.py").write_text(f"open({str(sentinel)!r}, 'w').close()\n", encoding="utf-8")


def _write_disabled_daemon_config(root) -> None:
    (root / "task.yaml").write_text(
        "version: 1\nbackend: github-issues\ngithub: {repo: owner/name}\ndaemon: {enabled: false}\n",
        encoding="utf-8",
    )


def test_spawn_detached_actually_launches_a_detached_process(tmp_path):
    # the real detach path (NOT mocked): spawn `python -m tasklib daemon run` against a config
    # that DISABLES the daemon, so the child exits cleanly on its own. We prove the spawn produced
    # a real child that ran tasklib and exited 0. The spawned process IS this test's child (new
    # SESSION, not reparented), so we reap it with waitpid — os.kill(0) would see a zombie.
    _write_disabled_daemon_config(tmp_path)
    proc = daemon._spawn_detached(cwd=str(tmp_path), log_path=tmp_path / "spawn.log", child_flags=[])
    assert proc.pid > 0
    assert _reap(proc.pid) == 0, "a disabled daemon should spawn, run tasklib, and exit 0"


def test_spawn_detached_rejects_a_decoy_tasklib_in_the_target_cwd(tmp_path):
    # The regression this guards: _spawn_detached's child cwd is the TARGET project being
    # managed, not task-cli's own checkout. Python auto-prepends a `-m` invocation's process
    # cwd to sys.path AHEAD of PYTHONPATH, so a managed project shipping its own top-level
    # `tasklib/` package could shadow the real one and run arbitrary code as this daemon. `-P`
    # (PYTHONSAFEPATH) must suppress that auto-prepend.
    sentinel = tmp_path / "decoy-ran.txt"
    _write_decoy_tasklib(tmp_path, sentinel)
    _write_disabled_daemon_config(tmp_path)

    proc = daemon._spawn_detached(cwd=str(tmp_path), log_path=tmp_path / "spawn.log", child_flags=[])
    rc = _reap(proc.pid)

    assert not sentinel.exists(), "the decoy tasklib/__main__.py ran — the cwd shadowed the real package"
    assert rc == 0, "the REAL tasklib (disabled daemon) should still spawn, run, and exit 0"


def test_spawn_detached_rejects_a_decoy_even_with_a_hostile_pythonpath(tmp_path, monkeypatch):
    # Narrower regression than the cwd-shadow test above: an INHERITED PYTHONPATH that lists the
    # target project (".") before task-cli's real repo root must NOT let the decoy win. -P only
    # suppresses Python's OWN auto-prepended entries — PYTHONPATH is an explicit mechanism it
    # does not touch — so _child_env must itself force the real repo root to the FRONT rather
    # than merely "somewhere in" PYTHONPATH, or this exact ordering reopens the shadowing hole.
    sentinel = tmp_path / "decoy-ran.txt"
    _write_decoy_tasklib(tmp_path, sentinel)
    _write_disabled_daemon_config(tmp_path)
    monkeypatch.setenv("PYTHONPATH", f".{os.pathsep}{daemon._repo_root()}")

    proc = daemon._spawn_detached(cwd=str(tmp_path), log_path=tmp_path / "spawn.log", child_flags=[])
    rc = _reap(proc.pid)

    assert not sentinel.exists(), "a hostile PYTHONPATH let the decoy win over the real tasklib"
    assert rc == 0, "the REAL tasklib (disabled daemon) should still spawn, run, and exit 0"


def test_child_env_prepends_repo_root_when_pythonpath_unset():
    repo_root = daemon._repo_root()
    env = daemon._child_env(repo_root, base_env={})
    assert env["PYTHONPATH"] == str(repo_root)
    assert env[daemon._BOOTSTRAP_MARKER] == str(repo_root)


def test_child_env_moves_an_existing_but_non_first_repo_root_to_the_front():
    # the ordering bug this guards: a naive "skip if already present anywhere" dedup leaves an
    # earlier entry (here "/other") ahead of the real repo root, where a decoy could still win.
    repo_root = daemon._repo_root()
    base_env = {"PYTHONPATH": f"/other{os.pathsep}{repo_root}"}
    env = daemon._child_env(repo_root, base_env=base_env)
    assert env["PYTHONPATH"] == f"{repo_root}{os.pathsep}/other"


def test_child_env_preserves_other_pythonpath_entries_behind_repo_root():
    repo_root = daemon._repo_root()
    base_env = {"PYTHONPATH": f"/a{os.pathsep}/b"}
    env = daemon._child_env(repo_root, base_env=base_env)
    assert env["PYTHONPATH"] == f"{repo_root}{os.pathsep}/a{os.pathsep}/b"


def test_child_env_resolves_relative_pythonpath_entries_against_parent_cwd(monkeypatch, tmp_path):
    # An inherited "." resolves against the PROCESS cwd at import time — for the spawned
    # child that's the TARGET project being managed, not wherever the parent (this CLI
    # invocation) was run from. A target repo shipping its own e.g. yaml.py could shadow
    # a real import the moment the child does `import yaml`. Rather than dropping a
    # relative entry (which would break a legitimate tox/CI/direnv-style
    # `PYTHONPATH=src` config), freeze it into an absolute path against the PARENT's cwd
    # — the child cwd can no longer reinterpret it. A bare/doubled separator ("") has no
    # intent to preserve and is dropped.
    # `monkeypatch.chdir` (a real directory) rather than patching `Path.cwd` directly —
    # the latter mutates the stdlib class process-wide for every caller during the test
    # (review finding); chdir exercises the real `Path.cwd()` call path with a narrow
    # blast radius.
    monkeypatch.chdir(tmp_path)
    repo_root = daemon._repo_root()
    base_env = {"PYTHONPATH": f".{os.pathsep}{os.pathsep}relative/dir{os.pathsep}/kept"}
    env = daemon._child_env(repo_root, base_env=base_env)
    assert env["PYTHONPATH"] == (
        f"{repo_root}{os.pathsep}{tmp_path}{os.pathsep}{tmp_path / 'relative/dir'}{os.pathsep}/kept"
    )


def test_child_env_dedupes_relative_entry_that_resolves_to_repo_root(monkeypatch):
    # The common case: running the CLI from the checkout, PYTHONPATH="." set by tox/direnv
    # in that same repo. "." doesn't string-match repo_root, but it RESOLVES to repo_root —
    # the dedupe must compare against the resolved value, not the raw entry, or repo_root
    # ends up twice in the child's PYTHONPATH (review finding).
    repo_root = daemon._repo_root()
    monkeypatch.chdir(repo_root)
    base_env = {"PYTHONPATH": f".{os.pathsep}{repo_root}"}
    env = daemon._child_env(repo_root, base_env=base_env)
    assert env["PYTHONPATH"] == str(repo_root)


def test_scrub_bootstrap_pythonpath_removes_only_the_injected_entry(monkeypatch):
    repo_root = str(daemon._repo_root())
    monkeypatch.setenv("PYTHONPATH", f"{repo_root}{os.pathsep}/kept")
    monkeypatch.setenv(daemon._BOOTSTRAP_MARKER, repo_root)
    daemon._scrub_bootstrap_pythonpath()
    assert os.environ["PYTHONPATH"] == "/kept"
    assert daemon._BOOTSTRAP_MARKER not in os.environ


def test_scrub_bootstrap_pythonpath_unsets_the_var_when_nothing_is_left(monkeypatch):
    repo_root = str(daemon._repo_root())
    monkeypatch.setenv("PYTHONPATH", repo_root)
    monkeypatch.setenv(daemon._BOOTSTRAP_MARKER, repo_root)
    daemon._scrub_bootstrap_pythonpath()
    assert "PYTHONPATH" not in os.environ


def test_scrub_bootstrap_pythonpath_is_a_noop_when_nothing_was_injected(monkeypatch):
    # no _BOOTSTRAP_MARKER at all — must not raise, must not touch PYTHONPATH either way
    monkeypatch.delenv(daemon._BOOTSTRAP_MARKER, raising=False)
    monkeypatch.delenv("PYTHONPATH", raising=False)
    daemon._scrub_bootstrap_pythonpath()
    assert "PYTHONPATH" not in os.environ


def test_scrub_bootstrap_pythonpath_leaves_a_user_owned_pythonpath_untouched(monkeypatch):
    # regression: a developer who legitimately runs with PYTHONPATH=<repo_root> themselves
    # (e.g. an editable-checkout workflow) must NOT have it silently deleted just because the
    # value happens to match repo_root — only an explicit _child_env handshake may remove it.
    repo_root = str(daemon._repo_root())
    monkeypatch.setenv("PYTHONPATH", repo_root)
    monkeypatch.delenv(daemon._BOOTSTRAP_MARKER, raising=False)  # nothing WE injected
    daemon._scrub_bootstrap_pythonpath()
    assert os.environ["PYTHONPATH"] == repo_root


def test_run_loop_scrub_does_not_touch_the_calling_process_env_without_a_marker(
    tmp_path, monkeypatch, capture_notifications
):
    # regression: run_loop() is also called IN-PROCESS by tests (max_ticks=), never having gone
    # through _spawn_detached/_child_env at all. Without the marker gate, run_loop's scrub call
    # would mutate the CALLING process's (here: this test's) os.environ directly — stripping a
    # real developer's own PYTHONPATH for the rest of their pytest session, not just some child.
    from tasklib.config import LoadedConfig

    repo_root = str(daemon._repo_root())
    monkeypatch.setenv("PYTHONPATH", repo_root)  # simulates a dev's own editable-checkout env
    monkeypatch.delenv(daemon._BOOTSTRAP_MARKER, raising=False)
    cfg = LoadedConfig(data={"backend": "github-issues", "daemon": {"enabled": False}}, repo_root=tmp_path)
    paths = daemon.DaemonPaths(pid=tmp_path / "p.pid", state=tmp_path / "s.json", log=tmp_path / "l.log")
    daemon.run_loop(cfg, paths, max_ticks=1)
    assert os.environ["PYTHONPATH"] == repo_root


# ── task-cli#60: byte-exact PYTHONPATH restoration across the full bootstrap/scrub round-trip ──


def test_child_env_then_scrub_restores_caller_pythonpath_containing_repo_root_verbatim(monkeypatch):
    # The regression: a caller-supplied PYTHONPATH that ITSELF already contains repo_root (e.g.
    # an editable-checkout dev also has some other entry alongside it) must come back byte-exact
    # after the daemon's bootstrap scrub — not have its repo_root segment silently dropped just
    # because _child_env's own dedupe made it indistinguishable from the injected copy.
    repo_root = daemon._repo_root()
    original = f"{repo_root}{os.pathsep}/custom"
    child_env = daemon._child_env(repo_root, base_env={"PYTHONPATH": original})
    for key, value in child_env.items():
        monkeypatch.setenv(key, value)
    daemon._scrub_bootstrap_pythonpath()
    assert os.environ["PYTHONPATH"] == original


def test_child_env_then_scrub_restores_unset_pythonpath(monkeypatch):
    # The other side: a caller with NO PYTHONPATH at all must end up with none after the
    # round-trip too (not an empty string, not a leftover entry).
    repo_root = daemon._repo_root()
    monkeypatch.delenv("PYTHONPATH", raising=False)
    child_env = daemon._child_env(repo_root, base_env={})
    for key, value in child_env.items():
        monkeypatch.setenv(key, value)
    daemon._scrub_bootstrap_pythonpath()
    assert "PYTHONPATH" not in os.environ


def test_child_env_then_scrub_restores_a_relative_entry_as_its_resolved_absolute_form(monkeypatch, tmp_path):
    # Codex review finding: restoring the RAW original string (e.g. literal ".") would UNDO the
    # relative-to-absolute security resolution _sanitized_pythonpath_entries performs -- a caller
    # PYTHONPATH="." must come back as the PARENT's cwd (frozen absolute), not literal ".", or a
    # LATER subprocess (the notifier) inheriting this same env could reinterpret "." against ITS
    # own cwd (the managed target repo) and reopen the cwd-shadowing hole.
    monkeypatch.chdir(tmp_path)
    repo_root = daemon._repo_root()
    child_env = daemon._child_env(repo_root, base_env={"PYTHONPATH": "."})
    for key, value in child_env.items():
        monkeypatch.setenv(key, value)
    daemon._scrub_bootstrap_pythonpath()
    assert os.environ["PYTHONPATH"] == str(tmp_path)
    assert os.environ["PYTHONPATH"] != "."


# ── task-cli#65/#66: normalized PYTHONPATH dedupe (trailing separator / symlink spelling) ──


def test_child_env_dedupes_a_trailing_separator_variant_of_repo_root(monkeypatch):
    repo_root = daemon._repo_root()
    base_env = {"PYTHONPATH": f"{repo_root}{os.sep}{os.pathsep}/kept"}
    env = daemon._child_env(repo_root, base_env=base_env)
    assert env["PYTHONPATH"] == f"{repo_root}{os.pathsep}/kept"


def test_dedupe_key_falls_back_to_normpath_on_an_embedded_nul_byte(monkeypatch):
    # Opus review finding: os.path.realpath raises ValueError (not OSError) for a path with an
    # embedded NUL -- a garbage/malicious PYTHONPATH entry must not crash dedupe comparison.
    repo_root = daemon._repo_root()
    base_env = {"PYTHONPATH": f"/tmp/\x00bad{os.pathsep}/kept"}
    env = daemon._child_env(repo_root, base_env=base_env)  # must not raise
    assert "/kept" in env["PYTHONPATH"]


def test_child_env_dedupes_a_symlinked_checkout_alias_of_repo_root(tmp_path, monkeypatch):
    real_repo = tmp_path / "real-checkout"
    real_repo.mkdir()
    alias = tmp_path / "alias-checkout"
    alias.symlink_to(real_repo)
    base_env = {"PYTHONPATH": f"{alias}{os.pathsep}/kept"}
    env = daemon._child_env(real_repo, base_env=base_env)
    assert env["PYTHONPATH"] == f"{real_repo}{os.pathsep}/kept"


def test_child_env_dedupes_exact_duplicate_absolute_entries(monkeypatch):
    repo_root = daemon._repo_root()
    base_env = {"PYTHONPATH": f"/a{os.pathsep}/a"}
    env = daemon._child_env(repo_root, base_env=base_env)
    assert env["PYTHONPATH"] == f"{repo_root}{os.pathsep}/a"


def test_child_env_drops_a_relative_entry_when_the_parent_cwd_is_gone(monkeypatch):
    # A long-lived daemon respawning from a worktree deleted out from under it must not crash
    # resolving a relative PYTHONPATH entry against a nonexistent cwd — drop that one entry.
    repo_root = daemon._repo_root()

    def _raise_cwd():
        raise OSError("cwd gone")

    monkeypatch.setattr(daemon.Path, "cwd", staticmethod(_raise_cwd))
    base_env = {"PYTHONPATH": f"relative/dir{os.pathsep}/kept"}
    env = daemon._child_env(repo_root, base_env=base_env)
    assert env["PYTHONPATH"] == f"{repo_root}{os.pathsep}/kept"


def test_stop_not_running_clears_stale(tmp_path):
    env = {"XDG_STATE_HOME": str(tmp_path), "HOME": str(tmp_path)}
    paths = daemon.paths_for("owner/repo", env=env)
    daemon._write_pid(paths.pid, 2**30)  # a dead pid
    outcome, _pid = daemon.stop("owner/repo", env=env)
    assert outcome == "not-running"
    assert daemon.read_pid(paths.pid) is None


def test_stop_with_no_pid_file_is_not_running(tmp_path):
    # #32 refactor guard: stop must handle a COMPLETELY ABSENT pid-file (status "stopped") without
    # crashing — the identity reads are now reached only on the live-daemon paths, never on no-file.
    env = {"XDG_STATE_HOME": str(tmp_path), "HOME": str(tmp_path)}
    outcome, pid = daemon.stop("owner/repo", env=env)  # no _write_pid at all → no file on disk
    assert outcome == "not-running"
    assert pid is None


# Spawn a sleeper ORPHANED to init (reaped by it), so when stop() kills it there is no zombie
# that keeps answering kill(0) as "alive" and masks the result. This matches production, where
# `task daemon stop` is a SEPARATE process from the daemon, never its parent — a same-process
# child would zombie and make stop() see a spurious timeout. We do it with a short-lived launcher
# (`sh -c "python … &"`) that backgrounds the sleeper and exits, reparenting it to init.
def _spawn_orphan(code: str, *, identity: bool = True, extra: list[str] | None = None) -> int:
    import shlex
    import subprocess
    import sys
    import time

    pidfile = subprocess.run(
        ["mktemp"], capture_output=True, text=True, check=True
    ).stdout.strip()
    # The trailing `-m tasklib daemon run` tokens are passed as extra argv after `-c <code>` (Python
    # ignores them as argv, but `/proc/<pid>/cmdline` & `ps -o args=` show them) so the spawned
    # sleeper's command line carries the daemon-identity token SEQUENCE — exercising the real
    # identify_pid()/is_task_daemon() check in stop(). identity=False → an UNRELATED recycled pid.
    # `extra` injects further argv tokens (shell-quoted so a value with spaces stays ONE token), used
    # to test the cmdline read paths on a spaced argument.
    markers = " -m tasklib daemon run" if identity else ""
    extra_str = (" " + " ".join(shlex.quote(t) for t in extra)) if extra else ""
    launcher = f'{sys.executable} -c {code!r}{markers}{extra_str} & echo $! > {pidfile}'
    subprocess.run(["sh", "-c", launcher], check=True)
    # read the orphaned sleeper's pid (the launcher has exited)
    deadline = time.time() + 3
    pid = None
    while time.time() < deadline:
        try:
            txt = open(pidfile).read().strip()
        except OSError:
            txt = ""
        if txt.isdigit():
            pid = int(txt)
            break
        time.sleep(0.05)
    if pid is None:
        raise AssertionError("could not read orphaned sleeper pid")
    # The pidfile holds `$!` as soon as the shell BACKGROUNDS the child — but that child may not have
    # EXEC'd Python yet (it can still be the `sh -c …` launcher, or have an empty /proc cmdline that
    # falls back to a launcher-shaped ps read). Tests that sample the cmdline immediately would race.
    # Wait until the process's argv shows the Python `-c` invocation (a `-c` token whose executable is
    # NOT the `sh` launcher). The `-c` flag survives both /proc and ps reads identically, so this
    # condition matches quickly on every platform (it does not depend on how the `code` token is split).
    from tasklib import daemon as _d

    while time.time() < deadline:
        tokens = _d.process_cmdline(pid)
        if tokens and "-c" in tokens and os.path.basename(tokens[0]) != "sh":
            return pid
        time.sleep(0.02)
    return pid  # best-effort: return anyway if the cmdline never settled (the test will surface it)


def _kill_quiet(pid: int) -> None:
    import os
    import signal as _signal

    try:
        os.kill(pid, _signal.SIGKILL)
    except ProcessLookupError:
        pass


def test_stop_terminates_a_live_process(tmp_path):
    # the real stop path: a live process that exits on SIGTERM → "stopped", pid-file cleared
    env = {"XDG_STATE_HOME": str(tmp_path), "HOME": str(tmp_path)}
    paths = daemon.paths_for("owner/repo", env=env)
    pid = _spawn_orphan("import time; time.sleep(60)")
    try:
        daemon._write_pid(paths.pid, pid)
        assert daemon.pid_status(paths.pid) == ("running", pid)
        outcome, got = daemon.stop("owner/repo", env=env, timeout_s=5)
        assert outcome == "stopped"
        assert got == pid
        assert daemon.read_pid(paths.pid) is None
    finally:
        _kill_quiet(pid)


def test_stop_escalates_to_sigkill_when_term_ignored(tmp_path):
    # a process that IGNORES SIGTERM must still be stopped — stop escalates to SIGKILL
    env = {"XDG_STATE_HOME": str(tmp_path), "HOME": str(tmp_path)}
    paths = daemon.paths_for("owner/repo", env=env)
    pid = _spawn_orphan(
        "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
    )
    try:
        daemon._write_pid(paths.pid, pid)
        outcome, got = daemon.stop("owner/repo", env=env, timeout_s=1)  # short → forces SIGKILL
        assert outcome == "stopped"
        assert got == pid
        assert daemon.read_pid(paths.pid) is None
    finally:
        _kill_quiet(pid)


# ── PID-identity check on stop (issue #25 part a — the reused-pid hazard) ─────────────


def test_is_task_daemon_true_for_marked_argv(tmp_path):
    # a process whose command line carries the daemon-identity markers is recognized as ours
    pid = _spawn_orphan("import time; time.sleep(30)", identity=True)
    try:
        assert daemon.is_task_daemon(pid) is True
    finally:
        _kill_quiet(pid)


def test_is_task_daemon_false_for_unrelated_process(tmp_path):
    # a live process WITHOUT the markers (a recycled pid) is NOT recognized — stop must not signal it
    pid = _spawn_orphan("import time; time.sleep(30)", identity=False)
    try:
        assert daemon.is_task_daemon(pid) is False
    finally:
        _kill_quiet(pid)


def test_is_task_daemon_false_for_dead_pid():
    # a pid with no process at all → not provably ours
    assert daemon.is_task_daemon(2**30) is False


def test_identify_pid_tristate(tmp_path):
    # the tri-state: a marked process is "daemon", an unmarked live one is "foreign", a dead one
    # is "unknown" (can't read its cmdline at all)
    ours = _spawn_orphan("import time; time.sleep(30)", identity=True)
    theirs = _spawn_orphan("import time; time.sleep(30)", identity=False)
    try:
        assert daemon.identify_pid(ours) == daemon.IDENTITY_DAEMON
        assert daemon.identify_pid(theirs) == daemon.IDENTITY_FOREIGN
        assert daemon.identify_pid(2**30) == daemon.IDENTITY_UNKNOWN
    finally:
        _kill_quiet(ours)
        _kill_quiet(theirs)


def test_identify_pid_rejects_loose_substring_match(monkeypatch):
    # a process whose argv merely MENTIONS the words (e.g. `grep "tasklib daemon run"`) but is not
    # `python -m tasklib daemon run` must be FOREIGN — the contiguous-token check, not substring.
    monkeypatch.setattr(daemon, "process_cmdline", lambda pid: ["grep", "tasklib daemon run", "."])
    assert daemon.identify_pid(123) == daemon.IDENTITY_FOREIGN
    # the real launch shape IS recognized
    monkeypatch.setattr(
        daemon, "process_cmdline", lambda pid: ["python", "-m", "tasklib", "daemon", "run", "-C", "/x"]
    )
    assert daemon.identify_pid(123) == daemon.IDENTITY_DAEMON


def test_argv_is_task_daemon_accepts_both_launch_shapes():
    # the `python -m tasklib daemon run` form (how _spawn_detached starts it)
    assert daemon.argv_is_task_daemon(["python", "-m", "tasklib", "daemon", "run", "-C", "/x"])
    # the console-script form `/path/to/task daemon run` (systemd unit / Docker ENTRYPOINT) — must
    # ALSO be recognized, else a console-script-launched daemon is mis-classified as foreign + orphaned
    assert daemon.argv_is_task_daemon(["/usr/local/bin/task", "daemon", "run", "-C", "/srv/repo"])
    assert daemon.argv_is_task_daemon(["task", "daemon", "run"])
    # NOT ours: `daemon run` present but the preceding token is not tasklib/`task`
    assert not daemon.argv_is_task_daemon(["foo", "daemon", "run"])
    # NOT ours: tasklib referenced but `daemon run` not contiguous
    assert not daemon.argv_is_task_daemon(["python", "-m", "tasklib", "list"])
    # NOT ours: an editor opening the file (substring, not the subcommand)
    assert not daemon.argv_is_task_daemon(["vim", "tasklib/daemon.py"])
    # NOT ours: a stray `task` token NOT immediately before `daemon` (the positional anchor rejects it)
    assert not daemon.argv_is_task_daemon(["task", "list", "foo", "daemon", "run"])
    assert not daemon.argv_is_task_daemon(["tasklib", "x", "daemon", "run"])
    # IS ours: `task` as the executable immediately before the subcommand, even behind a wrapper
    assert daemon.argv_is_task_daemon(["timeout", "60", "task", "daemon", "run"])


def test_identify_pid_recognizes_renamed_entrypoint_via_recorded_identity(monkeypatch):
    # the robust fix: a daemon launched via a NON-standard entrypoint (renamed console-script / frozen
    # binary) whose argv does NOT match argv_is_task_daemon is STILL recognized when its recorded
    # identity (the signature it wrote to its pid-file) matches the live argv.
    frozen_argv = ["/opt/app/mytaskd", "daemon", "run", "--config", "/etc/x"]
    monkeypatch.setattr(daemon, "process_cmdline", lambda pid: frozen_argv)
    recorded = daemon.argv_signature(frozen_argv)
    # without the recorded identity the shape matcher calls it foreign (no tasklib/`task`)...
    assert daemon.identify_pid(123) == daemon.IDENTITY_FOREIGN
    # ...but WITH the recorded identity it is correctly recognized as ours
    assert daemon.identify_pid(123, recorded_identity=recorded) == daemon.IDENTITY_DAEMON
    # a recorded identity that does NOT match the live argv stays foreign (a genuinely recycled pid)
    assert daemon.identify_pid(123, recorded_identity="something else entirely") == daemon.IDENTITY_FOREIGN


def test_identify_pid_recorded_identity_is_authoritative_over_shape(monkeypatch):
    # THE reused-pid hazard the recorded identity must close: coordinate A's daemon crashes (recorded
    # `…-C /A`), the OS reuses its pid for coordinate B's daemon (`…-C /B`, which HAS the task-daemon
    # SHAPE). stop("A") must NOT mistake B for A's daemon and kill it — a recorded identity that does
    # not match the live argv is FOREIGN, NOT a fall-through to the shape matcher.
    b_argv = ["python", "-m", "tasklib", "daemon", "run", "-C", "/repo/B"]
    monkeypatch.setattr(daemon, "process_cmdline", lambda pid: b_argv)
    a_identity = "python -m tasklib daemon run -C /repo/A"
    # without a recorded identity, the shape matcher (correctly) calls B a daemon
    assert daemon.identify_pid(99) == daemon.IDENTITY_DAEMON
    # WITH A's recorded identity, B's mismatching argv is FOREIGN — A's stop won't signal B
    assert daemon.identify_pid(99, recorded_identity=a_identity) == daemon.IDENTITY_FOREIGN


def test_pid_file_records_and_reads_back_identity(tmp_path):
    # _write_pid(identity=...) writes pid on line 1, identity on line 2; read_pid stays compatible
    p = tmp_path / "x.pid"
    daemon._write_pid(p, 4242, identity="python -m tasklib daemon run -C /repo")
    assert daemon.read_pid(p) == 4242  # first line only
    assert daemon.read_recorded_identity(p) == "python -m tasklib daemon run -C /repo"
    # a legacy single-line pid-file has no recorded identity
    legacy = tmp_path / "legacy.pid"
    daemon._write_pid(legacy, 7)
    assert daemon.read_pid(legacy) == 7
    assert daemon.read_recorded_identity(legacy) is None


def test_argv_signature_is_single_line(tmp_path):
    # a token carrying a newline/tab must NOT split the pid-file record across lines — argv_signature
    # collapses internal whitespace so the identity is always one line and read_recorded_identity
    # reads it whole. Otherwise a real daemon would be mis-classified foreign and orphaned.
    sig = daemon.argv_signature(["python", "-m", "tasklib", "daemon", "run", "-C", "/weird\npath\there"])
    assert "\n" not in sig and "\t" not in sig
    p = tmp_path / "x.pid"
    daemon._write_pid(p, 4242, identity=sig)
    assert daemon.read_pid(p) == 4242
    assert daemon.read_recorded_identity(p) == sig  # round-trips whole, not truncated


def test_argv_signature_invariant_to_proc_vs_ps_tokenization():
    # THE invariant the identity guard relies on: `/proc/<pid>/cmdline` keeps a spaced argument as a
    # SINGLE token (NUL-separated), while a `ps -o args=` fallback whitespace-SPLITS that same argument
    # into several tokens. argv_signature MUST produce the SAME signature for both, or a real daemon
    # whose argv contains a spaced value (e.g. `-C "/path with spaces"`) would record one signature and
    # read back a different one across the two sources → orphaned. This pins that equivalence
    # deterministically (no live process), guarding the production code, not just the flaky test body.
    proc_form = ["python", "-m", "tasklib", "daemon", "run", "-C", "/path with spaces"]  # /proc: 1 token
    ps_form = ["python", "-m", "tasklib", "daemon", "run", "-C", "/path", "with", "spaces"]  # ps: split
    assert daemon.argv_signature(proc_form) == daemon.argv_signature(ps_form)
    # the signature is the discriminating part (argv[0] dropped, whitespace collapsed)
    assert daemon.argv_signature(proc_form) == "-m tasklib daemon run -C /path with spaces"
    # argv[0] differing (a venv `python3` symlink vs a resolved `Python`) must not change it either
    a = ["/venv/bin/python3", "-m", "tasklib", "daemon", "run"]
    b = ["/Cellar/.../Python", "-m", "tasklib", "daemon", "run"]
    assert daemon.argv_signature(a) == daemon.argv_signature(b) == "-m tasklib daemon run"
    # whitespace COLLAPSE (not a plain join): a token with a double space / tab normalizes to single
    # spaces, so a `/proc` token `"a  b"` and a `ps` split `["a", "b"]` still match
    assert daemon.argv_signature(["x", "-C", "a  b\tc"]) == daemon.argv_signature(["x", "-C", "a", "b", "c"])
    assert daemon.argv_signature(["x", "-C", "a  b\tc"]) == "-C a b c"


def test_process_cmdline_real_read_paths_agree_on_a_long_spaced_argument(tmp_path, monkeypatch):
    # THE actual root-cause regression (found on Linux CI): `ps` TRUNCATES the command line to ~screen
    # width by default, so for a LONG argv the `ps` read drops the tail while `/proc` keeps it whole —
    # the two signatures diverge and a daemon recorded from `/proc` is orphaned when stop falls back to
    # `ps`. The fix is `ps -ww` (no truncation). This spawns a process with a LONG, spaced trailing
    # argument and asserts the `/proc` read and the (now `-ww`) `ps` read yield the SAME signature, and
    # that the `ps` read is NOT truncated. A spaced arg also makes the raw token lists differ (one token
    # vs split), confirming a real cross-SOURCE comparison.
    if daemon._read_proc_cmdline(os.getpid()) is None:
        pytest.skip("no /proc on this platform — the cross-source (/proc vs ps) check is meaningless here")
    long_value = "/tmp/a b c " + "x" * 300  # long enough to be truncated by a default (no -ww) ps
    pid = _spawn_orphan("__import__('time').sleep(60)", identity=False, extra=["-C", long_value])
    try:
        proc_tokens = daemon.process_cmdline(pid)
        monkeypatch.setattr(daemon, "_read_proc_cmdline", lambda p: None)  # force the ps fallback
        ps_tokens = daemon.process_cmdline(pid)
        assert proc_tokens != ps_tokens, "the /proc (one token) and ps (split) reads must differ raw"
        # the ps read must be UNtruncated (the -ww fix): the full long tail survives
        assert "x" * 300 in " ".join(ps_tokens), "ps must NOT truncate the command line (needs -ww)"
        # and both real read paths yield the SAME signature — the invariant the identity guard depends on
        assert daemon.argv_signature(proc_tokens) == daemon.argv_signature(ps_tokens)
    finally:
        _kill_quiet(pid)


def test_run_loop_records_a_matching_self_identity(tmp_path, monkeypatch, capture_notifications):
    # the daemon records its OWN argv signature, and that signature matches what identify_pid would
    # read back for this process — so a stop() for this pid recognizes it via the recorded identity
    from tasklib.config import LoadedConfig

    cfg = LoadedConfig(data={"backend": "github-issues", "daemon": {"interval_s": 1}}, repo_root=tmp_path)
    be = _FakeBackend([])
    monkeypatch.setattr("tasklib.backends.get_backend", lambda c, env=None: be)
    paths = daemon.DaemonPaths(
        pid=tmp_path / "p.pid", state=tmp_path / "s.json", log=tmp_path / "l.log", lock=tmp_path / "p.lock"
    )
    # capture the identity written during the loop (before the finally clears the pid-file)
    captured = {}
    real_write = daemon._write_pid

    def spy_write(path, pid, *, identity=None):
        captured["identity"] = identity
        return real_write(path, pid, identity=identity)

    monkeypatch.setattr(daemon, "_write_pid", spy_write)
    assert daemon.run_loop(cfg, paths, max_ticks=1) == 0
    assert captured["identity"], "the daemon must record a non-empty self identity"
    # the recorded identity is THIS process's argv signature (the same source stop() reads)
    assert captured["identity"] == daemon.argv_signature(daemon._self_argv())


def test_spawn_detached_argv_is_recognized_by_the_identity_matcher(tmp_path, monkeypatch):
    # CONSISTENCY guard: the EXACT argv _spawn_detached launches must satisfy argv_is_task_daemon, so
    # stop() never mis-classifies the daemon it itself started. We capture the spawned argv and feed it
    # straight to the matcher — if the launch shape and the matcher ever drift apart, this fails.
    seen: dict = {}

    class _FakeProc:
        pid = 123

    def fake_popen(argv, **kw):
        seen["argv"] = argv
        return _FakeProc()

    monkeypatch.setattr(daemon.subprocess, "Popen", fake_popen)
    daemon._spawn_detached(cwd=str(tmp_path), log_path=tmp_path / "l.log", child_flags=[])
    assert daemon.argv_is_task_daemon(seen["argv"]), seen["argv"]


# ── task-cli#61: detect a bootstrap failure instead of reporting "started" on bare Popen success ──


def test_daemon_bootstrapped_returns_false_when_the_child_exits_with_a_nonzero_code(tmp_path):
    # the exact repro this ticket targets: a child that dies with a REAL error before it ever
    # writes a pid-file (e.g. the -P-on-3.10 crash, task-cli#63) must be POSITIVELY detected,
    # not misreported ready. Retaining `proc` (not just its pid) is what lets `.poll()` observe
    # this deterministically instead of racing CPython's GC (task-cli#61 review finding).
    proc = daemon.subprocess.Popen(
        [daemon.sys.executable, "-c", "import sys; sys.exit(1)"],
        stdout=daemon.subprocess.DEVNULL,
        stderr=daemon.subprocess.DEVNULL,
    )
    result = daemon._daemon_bootstrapped(proc, tmp_path / "does-not-exist.pid", timeout_s=2.0)
    assert result == "error"


def test_daemon_bootstrapped_returns_error_even_when_a_stale_matching_pid_file_exists(tmp_path):
    # Codex review finding: a daemon that wrote its own pid-file and then immediately crashed
    # must be reported "error", not "ready" -- a dead process is never "ready" no matter what a
    # (now-stale) pid-file says. An earlier version let pid-file evidence override an observed
    # nonzero exit when both were seen in the same poll iteration.
    pid_path = tmp_path / "d.pid"
    proc = daemon.subprocess.Popen(
        [daemon.sys.executable, "-c", "import sys; sys.exit(1)"],
        stdout=daemon.subprocess.DEVNULL,
        stderr=daemon.subprocess.DEVNULL,
    )
    proc.wait()  # ensure it has actually exited before we ever poll -- no race with the pid-file write
    daemon._write_pid(pid_path, proc.pid)  # simulates a stale file naming the now-dead pid
    result = daemon._daemon_bootstrapped(proc, pid_path, timeout_s=2.0)
    assert result == "error"


def test_daemon_bootstrapped_returns_clean_exit_not_ready_when_a_matching_pid_file_exited_zero(tmp_path):
    # Opus review finding: the docstring's own invariant is "exit status is checked, and trusted,
    # BEFORE the pid-file -- a dead process is never reclassified as ready by anything the
    # pid-file says". An earlier version's post-pid-file-match re-check still returned "ready"
    # for a ZERO exit, contradicting that invariant -- it must be "clean-exit" instead, so
    # start() re-resolves against the coordinate's current state rather than reporting "started"
    # for an already-dead pid.
    pid_path = tmp_path / "d.pid"
    proc = daemon.subprocess.Popen(
        [daemon.sys.executable, "-c", "import sys; sys.exit(0)"],
        stdout=daemon.subprocess.DEVNULL,
        stderr=daemon.subprocess.DEVNULL,
    )
    proc.wait()
    daemon._write_pid(pid_path, proc.pid)  # simulates a stale file naming the now-dead pid
    result = daemon._daemon_bootstrapped(proc, pid_path, timeout_s=2.0)
    assert result == "clean-exit"


def test_daemon_bootstrapped_treats_a_zero_exit_as_not_a_failure(tmp_path):
    # a flock-loser (another `start` won the singleton race) or a `daemon.enabled: false` early
    # exit are LEGITIMATE zero-exit early returns from run_loop, not bootstrap failures -- an
    # earlier version conflated ANY early exit with failure, which would have misreported the
    # race loser (Codex review finding).
    proc = daemon.subprocess.Popen(
        [daemon.sys.executable, "-c", "import sys; sys.exit(0)"],
        stdout=daemon.subprocess.DEVNULL,
        stderr=daemon.subprocess.DEVNULL,
    )
    result = daemon._daemon_bootstrapped(proc, tmp_path / "does-not-exist.pid", timeout_s=2.0)
    assert result == "clean-exit"


def test_daemon_bootstrapped_returns_true_once_the_pid_file_appears(tmp_path):
    pid_path = tmp_path / "d.pid"
    proc = daemon.subprocess.Popen(
        [daemon.sys.executable, "-c", "import time; time.sleep(5)"],
        stdout=daemon.subprocess.DEVNULL,
        stderr=daemon.subprocess.DEVNULL,
    )
    try:
        daemon._write_pid(pid_path, proc.pid)
        result = daemon._daemon_bootstrapped(proc, pid_path, timeout_s=2.0)
        assert result == "ready"
    finally:
        proc.kill()
        proc.wait()


def test_daemon_bootstrapped_is_inconclusive_true_when_neither_is_observed_before_timeout(tmp_path):
    # still alive, no pid-file yet, timeout elapses -- must NOT be misreported as a failure; a
    # slow-but-healthy bootstrap must never be flagged broken.
    proc = daemon.subprocess.Popen(
        [daemon.sys.executable, "-c", "import time; time.sleep(5)"],
        stdout=daemon.subprocess.DEVNULL,
        stderr=daemon.subprocess.DEVNULL,
    )
    try:
        result = daemon._daemon_bootstrapped(proc, tmp_path / "never-written.pid", timeout_s=0.2)
        assert result == "timeout"
    finally:
        proc.kill()
        proc.wait()


def test_start_reports_started_via_the_fast_ready_path(tmp_path, monkeypatch):
    # the happy path reaching "started" via genuine pid-file readiness (not merely via the
    # inconclusive "timeout" classification, which the other idempotency tests exercise since
    # they use ready_timeout_s=0 and a mocked _spawn_detached that never writes a pid-file).
    env = {"XDG_STATE_HOME": str(tmp_path), "HOME": str(tmp_path)}
    paths = daemon.paths_for("owner/repo", env=env)
    spawned: list = []  # review finding: keep the Popen so we can reap it, not just kill by pid

    def _fake_spawn_detached(*, cwd, log_path, child_flags):
        proc = daemon.subprocess.Popen(
            [daemon.sys.executable, "-c", "import time; time.sleep(5)"],
            stdout=daemon.subprocess.DEVNULL,
            stderr=daemon.subprocess.DEVNULL,
        )
        spawned.append(proc)
        daemon._write_pid(paths.pid, proc.pid)  # simulates run_loop writing its own pid-file
        return proc

    monkeypatch.setattr(daemon, "_spawn_detached", _fake_spawn_detached)
    try:
        outcome, pid = daemon.start("owner/repo", env=env, ready_timeout_s=2.0)
        assert outcome == "started"
        assert pid is not None
    finally:
        # review finding: an earlier version referenced the (possibly-unbound, on an early
        # raise) `pid` local here and never reaped the child, leaking a zombie.
        for proc in spawned:
            proc.kill()
            proc.wait()


def test_start_reports_failed_when_the_spawned_child_exits_before_bootstrapping(tmp_path, monkeypatch):
    # end-to-end through start(): a bootstrap failure is reported as an error outcome, not silently
    # as "started" (the flap this ticket exists to close).
    env = {"XDG_STATE_HOME": str(tmp_path), "HOME": str(tmp_path)}

    def _fake_spawn_detached(*, cwd, log_path, child_flags):
        return daemon.subprocess.Popen(
            [daemon.sys.executable, "-c", "import sys; sys.exit(1)"],
            stdout=daemon.subprocess.DEVNULL,
            stderr=daemon.subprocess.DEVNULL,
        )

    monkeypatch.setattr(daemon, "_spawn_detached", _fake_spawn_detached)
    outcome, pid = daemon.start("owner/repo", env=env, ready_timeout_s=2.0)
    assert outcome == "failed"
    assert pid is not None


def test_start_reports_already_running_when_the_spawned_child_is_a_flock_loser(tmp_path, monkeypatch):
    # a race-losing `start` spawns a child that immediately sees another daemon already holds
    # the singleton flock and exits 0 (see run_loop's "daemon.already-running" branch) -- that
    # must NOT be reported as "failed" (Codex review finding), NOR as "started" (Fable review
    # finding: the spawned pid is already dead; the WINNER, discovered via a pid-file re-check,
    # is the one actually running).
    #
    # The winner must NOT be live yet at the INITIAL pid_status check (else start() would
    # short-circuit to "already-running" before ever spawning, which would test plain idempotency
    # instead of the post-spawn clean-exit recheck this test targets -- Codex review finding: an
    # earlier version of this test pre-wrote the winner's pid-file up front and never actually
    # exercised the recheck path). So the winner's pid-file is written only from the SECOND
    # pid_status call onward, simulating it publishing mid-race, after our own spawn already
    # started.
    env = {"XDG_STATE_HOME": str(tmp_path), "HOME": str(tmp_path)}
    paths = daemon.paths_for("owner/repo", env=env)
    monkeypatch.setattr(daemon, "process_cmdline", lambda _pid: ["python", "-m", "tasklib", "daemon", "run"])

    real_pid_status = daemon.pid_status
    calls = {"n": 0}

    def _fake_pid_status(pid_path):
        calls["n"] += 1
        if calls["n"] == 1:
            return "stopped", None  # the INITIAL check: nothing running yet, so start() proceeds to spawn
        daemon._write_pid(paths.pid, os.getpid())  # the winner publishes its pid-file just now
        return real_pid_status(pid_path)

    monkeypatch.setattr(daemon, "pid_status", _fake_pid_status)

    def _fake_spawn_detached(*, cwd, log_path, child_flags):
        return daemon.subprocess.Popen(
            [daemon.sys.executable, "-c", "import sys; sys.exit(0)"],
            stdout=daemon.subprocess.DEVNULL,
            stderr=daemon.subprocess.DEVNULL,
        )

    monkeypatch.setattr(daemon, "_spawn_detached", _fake_spawn_detached)
    outcome, pid = daemon.start("owner/repo", env=env, ready_timeout_s=2.0)
    assert outcome == "already-running"
    assert pid == os.getpid()
    assert calls["n"] >= 2, "must have re-checked pid_status after the clean exit, not just once up front"


def test_start_reports_no_op_when_the_spawned_child_exits_clean_and_nothing_is_running(tmp_path, monkeypatch):
    # the rarer legitimate zero-exit case (e.g. a config-disabled race): the child exits 0
    # without ever owning the pid-file, and NO other daemon is running either -- this is neither
    # "started" (nothing is actually running) nor "failed" (no real error).
    env = {"XDG_STATE_HOME": str(tmp_path), "HOME": str(tmp_path)}

    def _fake_spawn_detached(*, cwd, log_path, child_flags):
        return daemon.subprocess.Popen(
            [daemon.sys.executable, "-c", "import sys; sys.exit(0)"],
            stdout=daemon.subprocess.DEVNULL,
            stderr=daemon.subprocess.DEVNULL,
        )

    monkeypatch.setattr(daemon, "_spawn_detached", _fake_spawn_detached)
    outcome, pid = daemon.start("owner/repo", env=env, ready_timeout_s=0.2)
    assert outcome == "no-op"
    assert pid is None


def test_start_reports_already_running_on_a_timeout_when_a_different_daemon_already_owns_the_pid_file(
    tmp_path, monkeypatch
):
    # Opus review finding: the "timeout" branch (child still alive, never confirmed readiness)
    # didn't re-check the pid-file at all -- if our own child lost a flock race and the WINNER
    # already published its pid-file before our timeout fired, start() must report the real
    # winner instead of "started" for a pid that's about to die (the same class of flap the
    # "clean-exit" path already handles correctly).
    env = {"XDG_STATE_HOME": str(tmp_path), "HOME": str(tmp_path)}
    paths = daemon.paths_for("owner/repo", env=env)
    monkeypatch.setattr(daemon, "process_cmdline", lambda _pid: ["python", "-m", "tasklib", "daemon", "run"])
    daemon._write_pid(paths.pid, os.getpid())  # the winner's pid-file, already published

    proc = daemon.subprocess.Popen(
        [daemon.sys.executable, "-c", "import time; time.sleep(5)"],
        stdout=daemon.subprocess.DEVNULL,
        stderr=daemon.subprocess.DEVNULL,
    )
    try:
        monkeypatch.setattr(daemon, "_spawn_detached", lambda *a, **k: proc)
        outcome, pid = daemon.start("owner/repo", env=env, ready_timeout_s=0.2)
        assert outcome == "already-running"
        assert pid == os.getpid()
    finally:
        proc.kill()
        proc.wait()


def test_stop_rechecks_identity_before_sigkill(tmp_path, monkeypatch):
    # finding: between the SIGTERM wait and the SIGKILL escalation the pid could die + be recycled.
    # stop must RE-CHECK identity before SIGKILL and, if the pid is now foreign, NOT send SIGKILL.
    # Deterministic via mocks: a live (but unsignalled) sleeper, a forced SIGTERM-timeout, and an
    # identify_pid that flips daemon→foreign between the pre-SIGTERM guard and the pre-SIGKILL recheck.
    import signal as _sig

    env = {"XDG_STATE_HOME": str(tmp_path), "HOME": str(tmp_path)}
    paths = daemon.paths_for("owner/repo", env=env)
    pid = _spawn_orphan("import time; time.sleep(60)", identity=True)

    calls = {"n": 0}

    def fake_identify(p, *, recorded_identity=None):
        calls["n"] += 1
        return daemon.IDENTITY_DAEMON if calls["n"] == 1 else daemon.IDENTITY_FOREIGN

    monkeypatch.setattr(daemon, "identify_pid", fake_identify)
    monkeypatch.setattr(daemon, "_wait_gone", lambda p, t: False)  # SIGTERM "didn't take" → escalation path

    sent: list = []
    real_kill = os.kill

    def spy_kill(p, sig):
        sent.append(sig)
        if sig == _sig.SIGTERM:
            return  # swallow the SIGTERM so the live sleeper stays alive for the recheck
        return real_kill(p, sig)

    monkeypatch.setattr(daemon.os, "kill", spy_kill)
    try:
        daemon._write_pid(paths.pid, pid)
        outcome, got = daemon.stop("owner/repo", env=env, timeout_s=1)
        assert outcome == "not-ours", "a pid that turned foreign before SIGKILL must not be killed"
        assert got == pid
        assert _sig.SIGKILL not in sent, "SIGKILL must NOT be sent after the identity recheck fails"
        assert daemon.read_pid(paths.pid) is None  # pid-file cleared
    finally:
        _kill_quiet(pid)


def test_daemon_paths_lock_path_appends_not_replaces_suffix(tmp_path):
    # lock_path must APPEND ".lock" to the full pid-file name, not replace the suffix — robust even if
    # the pid-file name contains dots
    paths = daemon.DaemonPaths(
        pid=tmp_path / "github.com_owner_repo.pid", state=tmp_path / "s.json", log=tmp_path / "l.log"
    )
    assert paths.lock_path.name == "github.com_owner_repo.pid.lock"


def test_stop_refuses_to_signal_a_recycled_pid(tmp_path):
    # the reused-pid guard: the pid-file points at a LIVE but UNRELATED process (OS pid-reuse after a
    # daemon crash). stop must NOT signal it — return "not-ours", clear the stale file, leave it alive.
    env = {"XDG_STATE_HOME": str(tmp_path), "HOME": str(tmp_path)}
    paths = daemon.paths_for("owner/repo", env=env)
    innocent = _spawn_orphan("import time; time.sleep(30)", identity=False)
    try:
        daemon._write_pid(paths.pid, innocent)
        outcome, got = daemon.stop("owner/repo", env=env, timeout_s=1)
        assert outcome == "not-ours"
        assert got == innocent
        assert daemon.read_pid(paths.pid) is None  # the misleading pid-file is cleared
        assert daemon.is_alive(innocent), "the innocent recycled-pid process must NOT be killed"
    finally:
        _kill_quiet(innocent)


def test_stop_signals_a_real_daemon_with_matching_identity(tmp_path):
    # the happy path with the identity check ENABLED: a process carrying the markers IS signalled
    env = {"XDG_STATE_HOME": str(tmp_path), "HOME": str(tmp_path)}
    paths = daemon.paths_for("owner/repo", env=env)
    pid = _spawn_orphan("import time; time.sleep(60)", identity=True)
    try:
        daemon._write_pid(paths.pid, pid)
        outcome, got = daemon.stop("owner/repo", env=env, timeout_s=5)
        assert outcome == "stopped"
        assert got == pid
        assert daemon.read_pid(paths.pid) is None
    finally:
        _kill_quiet(pid)


def test_stop_signals_a_real_daemon_via_the_RECORDED_identity_branch(tmp_path):
    # END-TO-END on a REAL process with the REAL cmdline read on BOTH sides (write-side derivation like
    # run_loop, read-side in stop) — this is what catches a write/read signature mismatch that would
    # orphan a daemon. The process is spawned WITHOUT the daemon shape (`identity=False`), so the shape
    # matcher rejects it (FOREIGN) and a clean stop can ONLY succeed via the recorded-identity branch —
    # the isolation the test's name promises. The earlier `import time; time.sleep(60)` body flaked on
    # CI because a long argv tripped `ps`'s default command-line TRUNCATION (the now-fixed root cause —
    # `process_cmdline` uses `ps -ww`); a short space-free body keeps the spawned argv well under any
    # width limit. The `ps -ww` no-truncation fix is covered by
    # test_process_cmdline_real_read_paths_agree_on_a_long_spaced_argument.
    env = {"XDG_STATE_HOME": str(tmp_path), "HOME": str(tmp_path)}
    paths = daemon.paths_for("owner/repo", env=env)
    pid = _spawn_orphan("__import__('time').sleep(60)", identity=False)  # no `daemon run` shape
    try:
        live_sig = daemon.argv_signature(daemon.process_cmdline(pid))
        assert live_sig, "the live process must have a readable cmdline for this test"
        # sanity: WITHOUT a recorded identity the shape matcher calls it foreign...
        assert daemon.identify_pid(pid) == daemon.IDENTITY_FOREIGN
        # ...so a clean stop here can ONLY succeed via the recorded-identity branch
        daemon._write_pid(paths.pid, pid, identity=live_sig)
        assert daemon.read_recorded_identity(paths.pid) == live_sig
        outcome, got = daemon.stop("owner/repo", env=env, timeout_s=5)
        assert outcome == "stopped", "stop must recognize the daemon via its recorded identity"
        assert got == pid
        assert daemon.read_pid(paths.pid) is None
    finally:
        _kill_quiet(pid)


def test_stop_refuses_real_process_with_mismatching_recorded_identity(tmp_path):
    # the SYMMETRIC end-to-end pair to the recorded-identity happy path: a LIVE real process whose
    # recorded identity does NOT match its live argv (a recycled pid) → stop returns "not-ours" and
    # does NOT signal it. This would catch a write-side serialization bug from the other direction.
    env = {"XDG_STATE_HOME": str(tmp_path), "HOME": str(tmp_path)}
    paths = daemon.paths_for("owner/repo", env=env)
    pid = _spawn_orphan("import time; time.sleep(30)", identity=False)
    try:
        daemon._write_pid(paths.pid, pid, identity="python -m tasklib daemon run -C /some/OTHER/coord")
        outcome, got = daemon.stop("owner/repo", env=env, timeout_s=1)
        assert outcome == "not-ours", "a mismatching recorded identity must refuse to signal"
        assert got == pid
        assert daemon.is_alive(pid), "the process must NOT be killed"
        assert daemon.read_pid(paths.pid) is None  # the misleading file is cleared
    finally:
        _kill_quiet(pid)


def test_stop_still_signals_when_cmdline_unreadable(tmp_path, monkeypatch):
    # REGRESSION: when the cmdline can't be read at all (busybox `ps` / no /proc → IDENTITY_UNKNOWN),
    # stop must NOT refuse — refusing would orphan a real daemon in the minimal Docker images this
    # project tests in. The recorded pid is still signalled (the pre-guard behavior).
    env = {"XDG_STATE_HOME": str(tmp_path), "HOME": str(tmp_path)}
    paths = daemon.paths_for("owner/repo", env=env)
    pid = _spawn_orphan("import time; time.sleep(60)", identity=True)
    # force "can't read the cmdline" for this pid → identify_pid returns UNKNOWN
    monkeypatch.setattr(daemon, "process_cmdline", lambda p: None)
    try:
        daemon._write_pid(paths.pid, pid)
        outcome, got = daemon.stop("owner/repo", env=env, timeout_s=5)
        assert outcome == "stopped", "an unreadable cmdline must NOT orphan the daemon"
        assert got == pid
        assert daemon.read_pid(paths.pid) is None
    finally:
        _kill_quiet(pid)


# ── flock singleton (issue #25 part b — close the start() TOCTOU) ────────────────────


def test_acquire_singleton_grants_then_blocks(tmp_path):
    # the first acquire wins the exclusive flock; a second non-blocking acquire on the SAME lock
    # (while the first handle is open) is refused with None — the race-free singleton guarantee
    lock = tmp_path / "x.lock"
    first = daemon.acquire_singleton(lock)
    assert first is not None
    try:
        second = daemon.acquire_singleton(lock)
        assert second is None, "a second daemon must not acquire the lock while the first holds it"
    finally:
        first.close()


def test_acquire_singleton_reusable_after_release(tmp_path):
    # closing the handle releases the lock → a fresh acquire succeeds (a clean restart re-locks)
    lock = tmp_path / "x.lock"
    h1 = daemon.acquire_singleton(lock)
    assert h1 is not None
    h1.close()
    h2 = daemon.acquire_singleton(lock)
    assert h2 is not None
    h2.close()


def test_acquire_singleton_non_posix_fallback(tmp_path, monkeypatch):
    # on a platform without fcntl (non-POSIX) acquire_singleton degrades to "no flock available":
    # it returns the open (UNLOCKED) handle so the daemon still runs, with the pid-file liveness
    # check as the fallback guard — it must NOT crash on the missing import.
    import builtins

    real_import = builtins.__import__

    def no_fcntl(name, *args, **kwargs):
        if name == "fcntl":
            raise ImportError("no fcntl on this platform")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_fcntl)
    h = daemon.acquire_singleton(tmp_path / "x.lock")
    assert h is not None  # degrades to an unlocked handle, never None/crash
    h.close()


def test_run_loop_loser_exits_without_clobbering_winner(tmp_path, monkeypatch, capture_notifications):
    # two daemons race: the WINNER holds the lock; a second run_loop on the same coordinate gets
    # None from acquire_singleton, logs "already-running", and returns WITHOUT writing a pid-file or
    # ticking — so it can't clobber the winner's state. We simulate the winner by pre-holding the lock.
    from tasklib.config import LoadedConfig

    cfg = LoadedConfig(data={"backend": "github-issues", "daemon": {"interval_s": 1}}, repo_root=tmp_path)
    be = _FakeBackend([_t("#1", "2026-06-20")])
    monkeypatch.setattr("tasklib.backends.get_backend", lambda c, env=None: be)
    paths = daemon.DaemonPaths(
        pid=tmp_path / "p.pid", state=tmp_path / "s.json", log=tmp_path / "l.log", lock=tmp_path / "p.lock"
    )
    winner = daemon.acquire_singleton(paths.lock_path)  # the "other" daemon already holds the lock
    assert winner is not None
    daemon._write_pid(paths.pid, 99999, identity="python -m tasklib daemon run -C /winner")  # winner's pid-file
    try:
        rc = daemon.run_loop(cfg, paths, max_ticks=5)
        assert rc == 0
        assert capture_notifications == [], "the loser must not tick"
        # the KEY guarantee: the loser returns BEFORE its try/finally, so it must NOT _clear_pid the
        # winner's pid-file — the winner's record must be intact and unchanged.
        assert daemon.read_pid(paths.pid) == 99999, "the loser must not clobber the winner's pid-file"
        assert daemon.read_recorded_identity(paths.pid) == "python -m tasklib daemon run -C /winner"
    finally:
        winner.close()


def test_run_loop_winner_holds_lock_during_loop(tmp_path, monkeypatch, capture_notifications):
    # while run_loop is running it must HOLD the lock (a concurrent acquire is refused). We prove it
    # from inside a tick: the backend's list() tries to acquire the same lock and records the result.
    from tasklib.config import LoadedConfig

    cfg = LoadedConfig(data={"backend": "github-issues", "daemon": {"interval_s": 1}}, repo_root=tmp_path)
    paths = daemon.DaemonPaths(
        pid=tmp_path / "p.pid", state=tmp_path / "s.json", log=tmp_path / "l.log", lock=tmp_path / "p.lock"
    )
    acquired_during_loop: list = []

    class _ProbingBackend:
        def list(self, *, labels=None, state=None, limit=30):
            # while the loop owns the lock, a second acquire must be refused (None)
            h = daemon.acquire_singleton(paths.lock_path)
            acquired_during_loop.append(h)
            if h is not None:
                h.close()
            return []

    monkeypatch.setattr("tasklib.backends.get_backend", lambda c, env=None: _ProbingBackend())
    rc = daemon.run_loop(cfg, paths, max_ticks=1)
    assert rc == 0
    assert acquired_during_loop == [None], "the loop must hold the lock exclusively while ticking"


def test_run_loop_releases_lock_on_exit(tmp_path, monkeypatch, capture_notifications):
    # after run_loop returns, the lock is released → a fresh run_loop / acquire succeeds
    from tasklib.config import LoadedConfig

    cfg = LoadedConfig(data={"backend": "github-issues", "daemon": {"interval_s": 1}}, repo_root=tmp_path)
    be = _FakeBackend([])
    monkeypatch.setattr("tasklib.backends.get_backend", lambda c, env=None: be)
    paths = daemon.DaemonPaths(
        pid=tmp_path / "p.pid", state=tmp_path / "s.json", log=tmp_path / "l.log", lock=tmp_path / "p.lock"
    )
    assert daemon.run_loop(cfg, paths, max_ticks=1) == 0
    after = daemon.acquire_singleton(paths.lock_path)
    assert after is not None, "the lock must be released when the loop exits"
    after.close()


def test_daemon_paths_lock_defaults_to_pidfile_sibling(tmp_path):
    # DaemonPaths.lock defaults to the pid-file name + ".lock", so existing constructors that pass
    # only pid/state/log still get a sensible, collision-free lock path next to the pid-file
    paths = daemon.paths_for("owner/repo", env={"XDG_STATE_HOME": str(tmp_path), "HOME": str(tmp_path)})
    assert paths.lock is None
    assert paths.lock_path == paths.pid.parent / (paths.pid.name + ".lock")
    assert paths.lock_path.name.endswith(".pid.lock")


# ── config ──────────────────────────────────────────────────────────────────────────


class _Cfg:
    """Minimal LoadedConfig stand-in exposing only .section()."""

    def __init__(self, daemon_block: dict) -> None:
        self._block = daemon_block

    def section(self, name: str) -> dict:
        return self._block if name == "daemon" else {}


def test_daemon_config_defaults():
    dc = daemon.DaemonConfig.from_config(_Cfg({}))
    assert dc.interval_s == 3600
    assert dc.due_soon_days == 3
    assert dc.notifier == ("tg", "--tag", "report")
    assert dc.enabled is True
    assert dc.query_limit == 100


def test_daemon_config_query_limit_override():
    dc = daemon.DaemonConfig.from_config(_Cfg({"query_limit": 500}))
    assert dc.query_limit == 500
    # garbage / non-positive falls back
    assert daemon.DaemonConfig.from_config(_Cfg({"query_limit": 0})).query_limit == 100


def test_daemon_config_overrides():
    dc = daemon.DaemonConfig.from_config(
        _Cfg({"interval_s": 60, "due_soon_days": 7, "notifier": ["mynotify", "--quiet"], "enabled": False})
    )
    assert dc.interval_s == 60
    assert dc.due_soon_days == 7
    assert dc.notifier == ("mynotify", "--quiet")
    assert dc.enabled is False


def test_daemon_config_string_notifier_is_split():
    dc = daemon.DaemonConfig.from_config(_Cfg({"notifier": "tg --tag report"}))
    assert dc.notifier == ("tg", "--tag", "report")


def test_daemon_config_rejects_bad_ints():
    # 0/negative/garbage interval must fall back (a 0s interval would be a busy loop)
    dc = daemon.DaemonConfig.from_config(_Cfg({"interval_s": 0, "due_soon_days": -5}))
    assert dc.interval_s == 3600
    assert dc.due_soon_days == 3


def test_daemon_config_enabled_string_false_disables():
    # a quoted "false" must NOT re-enable the daemon (bool("false") is True — the trap)
    assert daemon.DaemonConfig.from_config(_Cfg({"enabled": "false"})).enabled is False
    assert daemon.DaemonConfig.from_config(_Cfg({"enabled": "no"})).enabled is False
    assert daemon.DaemonConfig.from_config(_Cfg({"enabled": False})).enabled is False
    assert daemon.DaemonConfig.from_config(_Cfg({"enabled": "true"})).enabled is True
    assert daemon.DaemonConfig.from_config(_Cfg({})).enabled is True


def test_notify_handles_missing_binary(monkeypatch):
    # a notifier binary that doesn't exist → False, never raises
    assert daemon.notify("hi", ("definitely-not-a-real-binary-xyz",)) is False


def test_notify_reports_nonzero_exit():
    # `false` exits 1 → notify returns False
    assert daemon.notify("hi", ("false",)) is False


def test_notify_succeeds_on_zero_exit():
    # `true` exits 0 → notify returns True (the message is appended as a harmless extra arg)
    assert daemon.notify("hi", ("true",)) is True


# ── reminder text ───────────────────────────────────────────────────────────────────


def test_reminder_text_overdue_today_future():
    assert "OVERDUE by 11d" in daemon._reminder_text(_t("#1", "2026-06-20", title="od"), today=TODAY)
    assert "due TODAY" in daemon._reminder_text(_t("#2", "2026-07-01", title="t"), today=TODAY)
    assert "due in 2d" in daemon._reminder_text(_t("#3", "2026-07-03", title="f"), today=TODAY)
