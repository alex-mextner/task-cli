"""Unit tests for `tasklib.stale_notice` — the pure logic behind issue #59's global nudge.

`test_cli.py` covers the CLI-level integration (the `task list` side effect + rate limiting
end to end); these exercise the module's functions directly, independent of argparse/backends.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tasklib import stale_notice
from tasklib.model import State, Ticket


def _iso_hours_ago(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── stale_tickets ────────────────────────────────────────────────────────────────────


def test_stale_tickets_flags_active_ticket_past_threshold():
    ticket = Ticket(title="Forgotten", state=State.IN_PROGRESS, updated_at=_iso_hours_ago(50))
    result = stale_notice.stale_tickets([ticket])
    assert len(result) == 1
    flagged, age = result[0]
    assert flagged is ticket
    assert age >= 49 * 3600


def test_stale_tickets_ignores_ticket_within_threshold():
    ticket = Ticket(title="Fresh", state=State.IN_PROGRESS, updated_at=_iso_hours_ago(1))
    assert stale_notice.stale_tickets([ticket]) == []


def test_stale_tickets_ignores_ticket_without_updated_at():
    ticket = Ticket(title="No timestamp", state=State.IN_PROGRESS, updated_at="")
    assert stale_notice.stale_tickets([ticket]) == []


def test_stale_tickets_ignores_non_active_state():
    ticket = Ticket(title="Done long ago", state=State.DONE, updated_at=_iso_hours_ago(1000))
    assert stale_notice.stale_tickets([ticket]) == []


def test_stale_tickets_ignores_malformed_timestamp():
    ticket = Ticket(title="Garbage timestamp", state=State.TODO, updated_at="not-a-date")
    assert stale_notice.stale_tickets([ticket]) == []


def test_stale_tickets_skips_one_malformed_ticket_without_losing_the_rest():
    # a single bad row (e.g. a non-string updated_at that slipped past a backend adapter) must
    # not raise and discard every OTHER valid finding in the same scan.
    genuinely_stale = Ticket(title="Forgotten", state=State.IN_PROGRESS, updated_at=_iso_hours_ago(60))
    malformed = Ticket(title="Bad row", state=State.IN_PROGRESS)
    malformed.updated_at = 12345  # type: ignore[assignment]  # simulates a malformed backend row
    result = stale_notice.stale_tickets([genuinely_stale, malformed])
    assert [t.title for t, _ in result] == ["Forgotten"]


def test_stale_tickets_ignores_timezone_naive_timestamp():
    # no 'Z'/offset at all -> ambiguous local-time interpretation; never guess, skip instead.
    naive = (datetime.now() - timedelta(hours=100)).strftime("%Y-%m-%dT%H:%M:%S")
    ticket = Ticket(title="Naive timestamp", state=State.TODO, updated_at=naive)
    assert stale_notice.stale_tickets([ticket]) == []


def test_stale_tickets_respects_custom_threshold():
    ticket = Ticket(title="3h old", state=State.TODO, updated_at=_iso_hours_ago(3))
    assert stale_notice.stale_tickets([ticket], stale_after=2 * 3600) != []
    assert stale_notice.stale_tickets([ticket], stale_after=4 * 3600) == []


def test_stale_after_seconds_reflects_env_override(monkeypatch):
    monkeypatch.delenv("TASK_GLOBAL_STALE_SECONDS", raising=False)
    assert stale_notice.stale_after_seconds() == stale_notice._STALE_AFTER_SECONDS_DEFAULT
    monkeypatch.setenv("TASK_GLOBAL_STALE_SECONDS", "3600")
    assert stale_notice.stale_after_seconds() == 3600


def test_stale_tickets_sorts_oldest_first():
    older = Ticket(title="Older", state=State.TODO, updated_at=_iso_hours_ago(100))
    newer = Ticket(title="Newer", state=State.TODO, updated_at=_iso_hours_ago(60))
    result = stale_notice.stale_tickets([newer, older])
    assert [t.title for t, _ in result] == ["Older", "Newer"]


# ── should_check / mark_checked (rate limiting) ─────────────────────────────────────


def test_should_check_true_before_any_mark_false_immediately_after(tmp_path):
    env = {"XDG_STATE_HOME": str(tmp_path)}
    assert stale_notice.should_check("owner/repo", env=env) is True
    stale_notice.mark_checked("owner/repo", env=env)
    assert stale_notice.should_check("owner/repo", env=env) is False


def test_should_check_true_again_after_interval_elapses(tmp_path):
    env = {"XDG_STATE_HOME": str(tmp_path)}
    now = 1_000_000.0
    stale_notice.mark_checked("owner/repo", now=now, env=env)
    assert stale_notice.should_check("owner/repo", now=now + 10, env=env) is False
    later = now + stale_notice._CHECK_INTERVAL_SECONDS_DEFAULT + 1
    assert stale_notice.should_check("owner/repo", now=later, env=env) is True


def test_should_check_is_scoped_per_coordinate(tmp_path):
    env = {"XDG_STATE_HOME": str(tmp_path)}
    stale_notice.mark_checked("owner/repo-a", env=env)
    # a different coordinate has its own independent cache file, unaffected by repo-a's mark.
    assert stale_notice.should_check("owner/repo-b", env=env) is True


def test_should_check_true_when_cache_records_a_future_timestamp(tmp_path):
    # a corrupted/clock-skewed cache recording a FUTURE last-checked time must not permanently
    # suppress the check — a negative elapsed must never mean "not due, forever" (review finding).
    env = {"XDG_STATE_HOME": str(tmp_path)}
    stale_notice.mark_checked("owner/repo", now=2_000_000.0, env=env)
    assert stale_notice.should_check("owner/repo", now=1_000_000.0, env=env) is True


def test_mark_checked_not_called_means_never_marked(tmp_path):
    # a mere should_check() peek must never itself consume the window.
    env = {"XDG_STATE_HOME": str(tmp_path)}
    assert stale_notice.should_check("owner/repo", env=env) is True
    assert stale_notice.should_check("owner/repo", env=env) is True


def test_read_last_checked_survives_non_dict_cache_content(tmp_path):
    # a corrupted/hand-edited cache file (valid JSON, not an object) must not crash the gate —
    # it degrades to "never checked" rather than raising AttributeError out of `.get`.
    env = {"XDG_STATE_HOME": str(tmp_path)}
    path = stale_notice.cache_path("owner/repo", env=env)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[]", encoding="utf-8")
    assert stale_notice.should_check("owner/repo", env=env) is True
