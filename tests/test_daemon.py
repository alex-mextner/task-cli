"""Daemon — due-date selection, de-dupe, fail-soft loop, lifecycle, and the --due round-trip.

Pure/unit coverage of the watcher core (no real backend, no real notifier, no real spawn for
the selection/dedupe paths). The lifecycle tests exercise the pid-file + liveness helpers with
this process's own pid (no detached spawn needed to prove idempotent-start / stop semantics).
"""

from __future__ import annotations

import json
import os
from datetime import date

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


def test_pid_status_stopped_running_stale(tmp_path):
    pid_path = tmp_path / "x.pid"
    assert daemon.pid_status(pid_path) == ("stopped", None)

    daemon._write_pid(pid_path, os.getpid())
    assert daemon.pid_status(pid_path) == ("running", os.getpid())

    daemon._write_pid(pid_path, 2**30)  # a pid that cannot be alive
    status, pid = daemon.pid_status(pid_path)
    assert status == "stale" and pid == 2**30


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


def test_start_is_idempotent_when_already_running(tmp_path, monkeypatch):
    # a live pid-file → start is a no-op (never double-spawns). We make the pid-file point at this
    # live test process and assert _spawn_detached is NOT called.
    env = {"XDG_STATE_HOME": str(tmp_path), "HOME": str(tmp_path)}
    paths = daemon.paths_for("owner/repo", env=env)
    daemon._write_pid(paths.pid, os.getpid())

    spawned: list = []
    monkeypatch.setattr(daemon, "_spawn_detached", lambda *a, **k: spawned.append(1) or 999)
    outcome, pid = daemon.start("owner/repo", env=env)
    assert outcome == "already-running"
    assert pid == os.getpid()
    assert spawned == []  # the guard prevented a second spawn


def test_start_clears_stale_then_spawns(tmp_path, monkeypatch):
    env = {"XDG_STATE_HOME": str(tmp_path), "HOME": str(tmp_path)}
    paths = daemon.paths_for("owner/repo", env=env)
    daemon._write_pid(paths.pid, 2**30)  # stale

    monkeypatch.setattr(daemon, "_spawn_detached", lambda *a, **k: 4242)
    outcome, pid = daemon.start("owner/repo", env=env)
    assert outcome == "started"
    assert pid == 4242


def test_start_forwards_child_flags_to_the_spawn(tmp_path, monkeypatch):
    # the backend-selecting flags must reach the child so it resolves the SAME coordinate
    env = {"XDG_STATE_HOME": str(tmp_path), "HOME": str(tmp_path)}
    captured: dict = {}
    monkeypatch.setattr(
        daemon, "_spawn_detached", lambda **k: captured.update(k) or 7
    )
    daemon.start("owner/repo", env=env, child_flags=["--repo", "owner/repo"])
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
    assert argv[1:5] == ["-m", "tasklib", "daemon", "run"]
    assert argv[-2:] == ["--backend", "linear"]


def test_spawn_detached_actually_launches_a_detached_process(tmp_path):
    # the real detach path (NOT mocked): spawn `python -m tasklib daemon run` against a config
    # that DISABLES the daemon, so the child exits cleanly on its own. We prove the spawn produced
    # a real child that ran tasklib and exited 0. The spawned process IS this test's child (new
    # SESSION, not reparented), so we reap it with waitpid — os.kill(0) would see a zombie.
    import os
    import signal as _signal
    import time

    (tmp_path / "task.yaml").write_text(
        "version: 1\nbackend: github-issues\ngithub: {repo: owner/name}\ndaemon: {enabled: false}\n",
        encoding="utf-8",
    )
    pid = daemon._spawn_detached(cwd=str(tmp_path), log_path=tmp_path / "spawn.log", child_flags=[])
    assert pid > 0
    deadline = time.time() + 8
    rc = None
    while time.time() < deadline:
        wpid, status = os.waitpid(pid, os.WNOHANG)
        if wpid == pid:
            rc = os.waitstatus_to_exitcode(status)
            break
        time.sleep(0.1)
    if rc is None:  # somehow still alive — kill + reap so no orphan/zombie leaks
        os.kill(pid, _signal.SIGKILL)
        os.waitpid(pid, 0)
    assert rc == 0, "a disabled daemon should spawn, run tasklib, and exit 0"


def test_stop_not_running_clears_stale(tmp_path):
    env = {"XDG_STATE_HOME": str(tmp_path), "HOME": str(tmp_path)}
    paths = daemon.paths_for("owner/repo", env=env)
    daemon._write_pid(paths.pid, 2**30)  # a dead pid
    outcome, _pid = daemon.stop("owner/repo", env=env)
    assert outcome == "not-running"
    assert daemon.read_pid(paths.pid) is None


# Spawn a sleeper ORPHANED to init (reaped by it), so when stop() kills it there is no zombie
# that keeps answering kill(0) as "alive" and masks the result. This matches production, where
# `task daemon stop` is a SEPARATE process from the daemon, never its parent — a same-process
# child would zombie and make stop() see a spurious timeout. We do it with a short-lived launcher
# (`sh -c "python … &"`) that backgrounds the sleeper and exits, reparenting it to init.
def _spawn_orphan(code: str, *, identity: bool = True) -> int:
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
    markers = " -m tasklib daemon run" if identity else ""
    launcher = f'{sys.executable} -c {code!r}{markers} & echo $! > {pidfile}'
    subprocess.run(["sh", "-c", launcher], check=True)
    # read the orphaned sleeper's pid (the launcher has exited)
    deadline = time.time() + 3
    while time.time() < deadline:
        try:
            txt = open(pidfile).read().strip()
        except OSError:
            txt = ""
        if txt.isdigit():
            return int(txt)
        time.sleep(0.05)
    raise AssertionError("could not read orphaned sleeper pid")


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
    # END-TO-END coverage of the CORE mechanism on a REAL process: write the pid-file with the
    # recorded identity derived from the live pid's OWN cmdline (exactly as run_loop does), then prove
    # stop() recognizes it through the recorded-identity branch and signals it. This is the test that
    # would catch a write-side/read-side tokenization mismatch (which would otherwise orphan the
    # daemon). We deliberately spawn WITHOUT the shape markers so ONLY the recorded identity can match.
    env = {"XDG_STATE_HOME": str(tmp_path), "HOME": str(tmp_path)}
    paths = daemon.paths_for("owner/repo", env=env)
    pid = _spawn_orphan("import time; time.sleep(60)", identity=False)  # no `daemon run` shape
    try:
        live_sig = daemon.argv_signature(daemon.process_cmdline(pid))
        assert live_sig, "the live process must have a readable cmdline for this test"
        # sanity: WITHOUT the recorded identity the shape matcher would call it foreign...
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
