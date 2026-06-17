"""Pager plumbing for the list-style commands (``list`` / ``find``) — git's pager model.

Why this exists
    ``task list``/``find`` can emit a long, cross-project view. Like ``git log``, a human at
    a terminal wants that piped through ``less`` (scrollable, quit on ``q``); a script reading
    the same command wants plain text it can parse. This module is the ONE place that decides
    between those two worlds and does the piping, so the command code stays output-agnostic
    (it builds a string; this module routes it).

How it's reached at runtime
    ``tasklib.cli.cmd_list`` / ``cmd_find`` build their human-readable output into a single
    string and hand it to :func:`page`. JSON output never comes here — it goes straight to
    stdout (machine-readable must stay un-paged and un-decorated).

The decision (git's convention, deliberately)
    Page only when ALL of these hold:
      * the destination stream is an interactive TTY (``stream.isatty()``), AND
      * the caller did not pass ``--no-pager``, AND
      * ``NO_PAGER`` is not set in the environment, AND
      * a usable pager command resolves (``$TASK_PAGER`` → ``$PAGER`` → ``less`` → ``more``), AND
      * the output is actually taller than the terminal (short output prints directly — git's
        ``--no-pager``-when-it-fits behavior; avoids a jarring full-screen pager for 3 lines).

    A non-tty stream (pipe, file, CI) ALWAYS prints plain — that is what makes the output
    scriptable. ``$LESS`` is defaulted to ``FRX`` (quit-if-one-screen, raw control chars for
    our ANSI colors, no init clear) only when the user has not set their own ``$LESS``.

Invariants / past gotchas
    * Robust to pager failure: if the pager can't be spawned (missing binary) we fall back to a
      direct write; if the user quits ``less`` early (broken pipe) we swallow it. A broken/odd
      pager must not crash the list view. (The one exception we don't fight: a SECOND Ctrl+C
      while we're already waiting on the pager will propagate — that's the user insisting.)
    * ``stream`` selects the destination ONLY in the non-paged path and drives the ``isatty()``
      decision. In the PAGED path the pager inherits the process's real stdout (that's the point
      of a pager — it draws on the terminal); ``stream`` is not piped into the pager. Tests steer
      the paged path with a fake ``$TASK_PAGER`` that captures stdin, not via ``stream``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Mapping
from typing import IO


def _resolve_pager(env: Mapping[str, str]) -> list[str] | None:
    """The pager command (argv), honoring ``$TASK_PAGER`` → ``$PAGER`` → ``less`` → ``more``.

    ``$PAGER`` is conventionally one command, possibly with flags (e.g. ``less -R``), so we
    split on whitespace into an argv — no shell, no quoting games (overkill for a pager value).
    An explicit empty ``$PAGER``/``$TASK_PAGER`` (the git convention for "cat, don't page")
    disables paging.
    """
    for key in ("TASK_PAGER", "PAGER"):
        if key in env:
            value = env[key].strip()
            if not value:
                return None  # explicit empty → "don't page" (git treats PAGER='' as cat)
            return value.split()
    for fallback in ("less", "more"):
        found = shutil.which(fallback)
        if found:
            return [found]
    return None


def should_page(
    *,
    stream: IO[str],
    no_pager_flag: bool,
    env: Mapping[str, str] | None = None,
) -> bool:
    """Decide whether output to ``stream`` should go through a pager.

    Pure (no side effects) so the decision is unit-testable. The "output fits the screen" part
    is NOT decided here (it needs the text + terminal height) — :func:`page` handles that, since
    ``less -F`` already does the right thing when we let it. This answers the prerequisites:
    interactive TTY, not opted out, a pager exists.
    """
    if env is None:
        env = os.environ
    if no_pager_flag:
        return False
    if env.get("NO_PAGER"):
        return False
    try:
        if not stream.isatty():
            return False
    except (ValueError, AttributeError):
        # a closed/!isatty-capable stream → treat as non-interactive (safe, scriptable default)
        return False
    return _resolve_pager(env) is not None


def page(
    text: str,
    *,
    stream: IO[str] | None = None,
    no_pager_flag: bool = False,
    env: Mapping[str, str] | None = None,
) -> None:
    """Emit ``text`` to ``stream``, through a pager when interactive, else directly.

    ``text`` is the already-rendered, newline-joined output (with or without a trailing
    newline; we normalize). Falls back to a direct write on any pager failure so the CLI never
    crashes on a missing/odd pager or an early ``q`` in ``less``.
    """
    if stream is None:
        stream = sys.stdout
    if env is None:
        env = os.environ
    if not text:
        return  # nothing to show → don't emit a stray blank line (or spawn a pager for it)
    body = text if text.endswith("\n") else text + "\n"

    if not should_page(stream=stream, no_pager_flag=no_pager_flag, env=env):
        stream.write(body)
        return

    pager_cmd = _resolve_pager(env)
    if pager_cmd is None:  # should_page already guarantees this is non-None, but be defensive
        stream.write(body)
        return

    child_env = dict(env)
    # -F: quit if the output fits one screen (git's "don't page short output").
    # -R: pass our ANSI color codes through raw. -X: don't clear the screen on exit.
    # Only seed defaults when the user hasn't configured less themselves.
    if os.path.basename(pager_cmd[0]) == "less" and "LESS" not in child_env:
        child_env["LESS"] = "FRX"
    try:
        # encoding="utf-8" (not text=True): ticket titles can be non-ASCII (the JSON path uses
        # ensure_ascii=False), and text=True would encode via the locale — a UnicodeEncodeError
        # under LC_ALL=C. errors="replace" keeps a stray char from ever crashing the list.
        proc = subprocess.Popen(
            pager_cmd, stdin=subprocess.PIPE, env=child_env, encoding="utf-8", errors="replace"
        )
    except OSError:
        stream.write(body)  # pager binary vanished between resolve and spawn
        return
    assert proc.stdin is not None
    try:
        proc.stdin.write(body)
    except (BrokenPipeError, OSError):
        # user quit the pager before all output was consumed — entirely normal, not an error.
        pass
    finally:
        # Always close stdin so the pager sees EOF (else it can hang waiting for input); a close
        # on an already-broken pipe is itself benign-but-raises, so guard it too.
        try:
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        try:
            proc.wait()
        except KeyboardInterrupt:
            proc.wait()
