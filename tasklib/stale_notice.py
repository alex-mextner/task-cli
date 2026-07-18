"""Global stale-ticket nudge (issue #59).

WHAT this is: ``cli.py``'s ``_session_attention_notice`` only knows about tickets the CURRENT
tmux-pane session touched (``session.py``'s sidecar). A ticket nobody's session ever touched —
or one from a session that ended days ago — is invisible to it. The only tool that catches
general "this is stuck, no progress" tickets is pm-cli, a separate app with its own event
store, an ingest step, and a cron job. A user who only ever runs task-cli gets zero awareness
that an open ticket has gone quiet.

This module is the lightweight, backend-agnostic alternative: fetch a fresh, unfiltered
``backend.list()`` and flag any ACTIVE ticket (todo/in-progress/in-review) whose backend
``updated_at`` is older than the staleness threshold — regardless of which session (if any)
touched it.

HOW it's reached: ``cli.py``'s ``_list_session_scoped`` (the ``task list`` default path) calls
:func:`should_check` then :func:`stale_tickets`, marking success via :func:`mark_checked`, all
wrapped in its own fail-soft try/except (see ``_global_stale_notice_block``). Deliberately NOT
wired into every command via ``main()``'s dispatch — ``classify``'s plain-text stdout is a live
hook contract (the ``tg`` inbound-message shell-out parses it verbatim), and an unconditional
extra line would corrupt it on whichever invocation the rate-limit window happened to be due.

RATE LIMITING: :func:`should_check`/:func:`mark_checked` share a local timestamp cache, one
file per repo/team coordinate (mirrors ``daemon.py``'s per-coordinate state-file convention
under ``$XDG_STATE_HOME``), so a burst of commands only ever triggers the check once per
window.

KNOWN SCOPE-LIMITS (deliberately not addressed here — see the issue-#59 follow-up filed for
them): the backend fetch is capped at one page (``_LIMIT_INTERACTIVE`` tickets, matching
``daemon.py``'s own ``query_limit`` precedent) with NO server-side active-state filter, so in a
repo with heavy churn (many tickets created/closed quickly) that page can be crowded out by
recently-closed tickets, silently hiding an old active one past the cap; and the rate-limit
cache is keyed by the caller's raw coordinate string, not ``Project.coordinate``'s canonical
backend-qualified form, so two projects that happen to share a bare name across backends would
share one cache file.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from .model import State

if TYPE_CHECKING:
    from .model import Ticket

_STALE_AFTER_SECONDS_DEFAULT = 48 * 60 * 60  # no backend update in 48h => stale
_CHECK_INTERVAL_SECONDS_DEFAULT = 4 * 60 * 60  # rate limit: at most one check per 4h
_ACTIVE_STATES = {State.TODO, State.IN_PROGRESS, State.IN_REVIEW}


def _env_seconds(name: str, default: int) -> int:
    """A positive int from an env var, falling back to ``default`` on absent/garbage input."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(0, value)


def _state_dir(env: dict[str, str] | None = None) -> Path:
    """The base dir for this feature's cache files (``$XDG_STATE_HOME`` -> ``~/.local/state``)."""
    env = os.environ if env is None else env
    base = env.get("XDG_STATE_HOME") or os.path.join(
        env.get("HOME", os.path.expanduser("~")), ".local", "state"
    )
    return Path(base) / "task-cli" / "attention"


def _coordinate_key(coordinate: str) -> str:
    """A filesystem-safe key from a repo/team coordinate (``owner/name`` -> ``owner_name``).

    Lossy by construction (every non-alnum/``-._`` char collapses to a single ``_``), so e.g.
    ``"a/b"`` and a literal ``"a_b"`` coordinate would collide on one cache file — the same
    bare-name collision risk already called out in this module's docstring (tracked in the
    issue-#59 follow-up, not fixed here: a best-effort rate-limit cache key, not a security
    boundary, so the worst case is a suppressed nudge, never wrong data).
    """
    safe = "".join(c if c.isalnum() or c in "-._" else "_" for c in coordinate)
    return safe or "default"


def cache_path(coordinate: str, *, env: dict[str, str] | None = None) -> Path:
    """Where this coordinate's last-checked timestamp is recorded."""
    return _state_dir(env) / f"{_coordinate_key(coordinate)}.json"


def _read_last_checked(path: Path) -> float:
    """The recorded last-checked epoch, or 0 for absent/malformed/non-object cache content.

    ``json.loads`` can succeed on non-dict JSON (a bare ``5`` or ``[]``, e.g. a cache file
    corrupted or hand-edited) — ``.get`` on that would raise ``AttributeError``, which is NOT a
    parse error, so it must be checked explicitly rather than folded into the parse except.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0.0
    if not isinstance(data, dict):
        return 0.0
    try:
        return float(data.get("last_checked_at", 0))
    except (TypeError, ValueError):
        return 0.0


def _write_last_checked(path: Path, ts: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"last_checked_at": ts}), encoding="utf-8")


def should_check(coordinate: str, *, now: float | None = None, env: dict[str, str] | None = None) -> bool:
    """Whether the rate-limit window has elapsed for ``coordinate``. Read-only — does NOT mark.

    Pair with :func:`mark_checked` called only AFTER a check actually completes: marking on
    a merely-attempted check (rather than a completed one) would mean a single transient
    backend failure silently suppresses the nudge for the whole window, even though no real
    check ever ran.

    Not a hard mutual-exclusion lock: two processes racing within the same instant could both
    observe ``True`` before either marks. This is a single-shot CLI invoked sequentially in
    normal use, not a long-lived server under real concurrency — the worst case of the race is
    a duplicate nudge printed twice, never a correctness/safety issue.
    """
    now = time.time() if now is None else now
    interval = _env_seconds("TASK_GLOBAL_STALE_CHECK_INTERVAL_SECONDS", _CHECK_INTERVAL_SECONDS_DEFAULT)
    if not interval:
        return True
    elapsed = now - _read_last_checked(cache_path(coordinate, env=env))
    # a corrupted/clock-skewed cache recording a FUTURE "last checked" would otherwise make
    # `elapsed` negative forever (always < interval) — a permanent fail-CLOSED, the one place
    # this feature's fail-soft design would otherwise never recover. Treat it as due instead.
    if elapsed < 0:
        return True
    return elapsed >= interval


def mark_checked(coordinate: str, *, now: float | None = None, env: dict[str, str] | None = None) -> None:
    """Record that a check for ``coordinate`` just completed successfully, right now."""
    now = time.time() if now is None else now
    _write_last_checked(cache_path(coordinate, env=env), now)


def _parse_updated_at(value: object) -> float | None:
    """Epoch seconds from a backend ISO8601 timestamp (GitHub/Linear both emit UTC, 'Z'-suffixed).

    Defensively tolerant, not just type-hinted: a malformed backend row could hand this a
    non-``str`` (a stray dict/int) or an offset-less timestamp, and one bad ticket must never
    take down the whole scan (this feeds ``stale_tickets``, which must skip a single unparseable
    ticket rather than raise and lose every OTHER valid finding for that invocation).

    ``datetime.fromisoformat`` only accepts a trailing 'Z' from Python 3.11 — swap it for an
    explicit UTC offset so this doesn't depend on exactly which 3.11+ patch is running. A
    timestamp with no 'Z'/offset at all parses to a NAIVE datetime, which ``.timestamp()``
    would silently interpret in the machine's local zone — never guess a timezone; treat that
    as unparseable (skip) instead of risking a skewed age in either direction.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    # removesuffix, NOT a blanket replace("Z", "+00:00") — the latter would corrupt any OTHER
    # literal 'Z' a malformed value happened to contain (mirrors _issue_number's removeprefix
    # choice in github_issues.py for the same "don't over-replace" reason).
    iso_text = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(iso_text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.timestamp()


def stale_after_seconds() -> int:
    """The resolved staleness threshold (``TASK_GLOBAL_STALE_SECONDS`` override, else 48h).

    Exposed so a caller formatting the nudge text (``cli.py``) can display the THRESHOLD THAT
    ACTUALLY APPLIED — a hardcoded "48h" in the message would be wrong under a configured
    override.
    """
    return _env_seconds("TASK_GLOBAL_STALE_SECONDS", _STALE_AFTER_SECONDS_DEFAULT)


def stale_tickets(
    tickets: list["Ticket"], *, now: float | None = None, stale_after: int | None = None
) -> list[tuple["Ticket", int]]:
    """Active tickets whose backend ``updated_at`` is older than the staleness threshold.

    Returns ``(ticket, age_seconds)`` pairs, oldest first. A ticket with no parseable
    ``updated_at`` is skipped — never flag staleness we have no evidence for.
    """
    now = time.time() if now is None else now
    threshold = stale_after_seconds() if stale_after is None else stale_after
    out: list[tuple[Ticket, int]] = []
    for ticket in tickets:
        if ticket.state not in _ACTIVE_STATES:
            continue
        updated = _parse_updated_at(ticket.updated_at)
        if updated is None:
            continue
        age = int(now - updated)
        if age >= threshold:
            out.append((ticket, age))
    out.sort(key=lambda pair: -pair[1])
    return out
