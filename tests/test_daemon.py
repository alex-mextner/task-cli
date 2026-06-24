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
def _spawn_orphan(code: str) -> int:
    import subprocess
    import sys
    import time

    pidfile = subprocess.run(
        ["mktemp"], capture_output=True, text=True, check=True
    ).stdout.strip()
    # the launcher backgrounds the real sleeper, writes ITS pid, and exits → sleeper orphans to init
    launcher = f'{sys.executable} -c {code!r} & echo $! > {pidfile}'
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
