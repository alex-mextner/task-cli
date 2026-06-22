"""Pager plumbing — the git-style decision (page only when interactive & not opted out) and
the routing in :func:`tasklib.pager.page` (pipe to the pager, fall back to a direct write).

The decision is pure and tested directly with a fake stream. The routing is tested with a
REAL child process: a tiny `cat`-like script set as ``$TASK_PAGER`` writing to a sentinel
file, so we assert the text actually reached the pager (not a mock of our own code).
"""

from __future__ import annotations

import io

from tasklib import pager


class _Stream(io.StringIO):
    """A StringIO that lies about being a TTY, so should_page is testable without a terminal."""

    def __init__(self, *, tty: bool) -> None:
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:  # noqa: D401
        return self._tty


# ── should_page: the pure decision ──────────────────────────────────────────────────


def test_no_page_when_stream_is_not_a_tty():
    # the scriptable default: a pipe/file destination is never paged.
    assert pager.should_page(stream=_Stream(tty=False), no_pager_flag=False, env={"PAGER": "less"}) is False


def test_page_when_tty_and_pager_resolves():
    assert pager.should_page(stream=_Stream(tty=True), no_pager_flag=False, env={"PAGER": "less"}) is True


def test_no_page_when_no_pager_flag():
    assert pager.should_page(stream=_Stream(tty=True), no_pager_flag=True, env={"PAGER": "less"}) is False


def test_no_page_when_NO_PAGER_env_set():
    env = {"PAGER": "less", "NO_PAGER": "1"}
    assert pager.should_page(stream=_Stream(tty=True), no_pager_flag=False, env=env) is False


def test_empty_NO_PAGER_does_not_disable_paging():
    # pin the documented behavior: only a TRUTHY NO_PAGER opts out. NO_PAGER='' must NOT disable
    # (so an exported-but-empty var doesn't silently kill the pager). Matches git's GIT_PAGER feel.
    env = {"PAGER": "less", "NO_PAGER": ""}
    assert pager.should_page(stream=_Stream(tty=True), no_pager_flag=False, env=env) is True


def test_resolve_pager_splits_flags():
    # `$PAGER="less -R"` must become argv ['less', '-R'], not a single ['less -R'] token.
    assert pager._resolve_pager({"PAGER": "less -R"}) == ["less", "-R"]
    assert pager._resolve_pager({"TASK_PAGER": "  more  -e "}) == ["more", "-e"]


def test_no_page_when_PAGER_is_explicitly_empty():
    # git treats PAGER='' as "cat, don't page". An empty $PAGER must disable paging even on a TTY.
    assert pager.should_page(stream=_Stream(tty=True), no_pager_flag=False, env={"PAGER": ""}) is False


def test_TASK_PAGER_takes_precedence_over_PAGER():
    # TASK_PAGER='' wins even if PAGER names a real pager → no paging.
    env = {"TASK_PAGER": "", "PAGER": "less"}
    assert pager.should_page(stream=_Stream(tty=True), no_pager_flag=False, env=env) is False


def test_no_page_when_no_pager_binary_resolves(monkeypatch):
    # neither $PAGER/$TASK_PAGER set and less/more both absent on PATH → can't page.
    monkeypatch.setattr(pager.shutil, "which", lambda _name: None)
    assert pager.should_page(stream=_Stream(tty=True), no_pager_flag=False, env={}) is False


def test_no_page_when_isatty_raises_on_closed_stream():
    # a closed stream whose isatty() raises ValueError must be treated as non-interactive, not crash.
    closed = io.StringIO()
    closed.close()
    assert pager.should_page(stream=closed, no_pager_flag=False, env={"PAGER": "less"}) is False


def test_whitespace_only_pager_disables_paging():
    # a misconfigured dotfile exporting PAGER="   " strips to empty → "don't page" (git convention).
    assert pager._resolve_pager({"PAGER": "   "}) is None
    assert pager.should_page(stream=_Stream(tty=True), no_pager_flag=False, env={"PAGER": "   "}) is False


# ── page(): the routing ──────────────────────────────────────────────────────────────


def test_page_writes_directly_when_not_paging():
    out = _Stream(tty=False)
    pager.page("line1\nline2", stream=out, env={})
    assert out.getvalue() == "line1\nline2\n"  # trailing newline normalized


def test_page_appends_no_double_newline():
    out = _Stream(tty=False)
    pager.page("already\n", stream=out, env={})
    assert out.getvalue() == "already\n"


def test_page_pipes_text_to_the_configured_pager(tmp_path):
    # A real child process acting as the pager: it copies stdin to a sentinel file. Proves the
    # body actually flows THROUGH the pager command, not just our fallback write.
    sink = tmp_path / "sink.txt"
    fake_pager = tmp_path / "fakepager.sh"
    fake_pager.write_text(f'#!/bin/sh\ncat > "{sink}"\n', encoding="utf-8")
    fake_pager.chmod(0o755)
    out = _Stream(tty=True)
    pager.page("alpha\nbeta", stream=out, env={"TASK_PAGER": str(fake_pager)})
    assert sink.read_text(encoding="utf-8") == "alpha\nbeta\n"
    # nothing went to the direct stream when the pager handled it.
    assert out.getvalue() == ""


def test_page_falls_back_to_direct_write_when_pager_binary_missing(tmp_path):
    # $TASK_PAGER points at a nonexistent binary: page() must not crash; it writes directly.
    out = _Stream(tty=True)
    missing = tmp_path / "does-not-exist"
    pager.page("hello", stream=out, env={"TASK_PAGER": str(missing)})
    assert out.getvalue() == "hello\n"


def test_page_survives_pager_that_quits_early(tmp_path):
    # A pager that reads nothing and exits 0 (like `less` + immediate `q`) yields a BrokenPipe on
    # our write — must be swallowed, never surface as an error.
    quitter = tmp_path / "quit.sh"
    quitter.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    quitter.chmod(0o755)
    out = _Stream(tty=True)
    # should simply return without raising
    pager.page("x" * 100000, stream=out, env={"TASK_PAGER": str(quitter)})


def test_page_survives_pager_that_exits_nonzero(tmp_path):
    # A pager that consumes nothing and exits NON-zero must also not raise (broader OSError path).
    boom = tmp_path / "boom.sh"
    boom.write_text("#!/bin/sh\nexit 3\n", encoding="utf-8")
    boom.chmod(0o755)
    out = _Stream(tty=True)
    pager.page("y" * 100000, stream=out, env={"TASK_PAGER": str(boom)})  # no raise


def test_page_seeds_LESS_FRX_for_less_only(tmp_path):
    # When the pager is `less` and the user hasn't set $LESS, we seed FRX (quit-if-one-screen,
    # raw colors, no clear). A fake `less` echoes its inherited $LESS so we can assert it.
    sink = tmp_path / "less_env.txt"
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake_less = bindir / "less"
    fake_less.write_text(f'#!/bin/sh\nprintf "%s" "$LESS" > "{sink}"\ncat >/dev/null\n', encoding="utf-8")
    fake_less.chmod(0o755)
    out = _Stream(tty=True)
    pager.page("body", stream=out, env={"PAGER": str(fake_less)})
    assert sink.read_text(encoding="utf-8") == "FRX"


def test_page_does_not_override_user_LESS(tmp_path):
    sink = tmp_path / "less_env.txt"
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake_less = bindir / "less"
    fake_less.write_text(f'#!/bin/sh\nprintf "%s" "$LESS" > "{sink}"\ncat >/dev/null\n', encoding="utf-8")
    fake_less.chmod(0o755)
    out = _Stream(tty=True)
    pager.page("body", stream=out, env={"PAGER": str(fake_less), "LESS": "MYOWN"})
    assert sink.read_text(encoding="utf-8") == "MYOWN"


def test_page_empty_text_emits_nothing():
    out = _Stream(tty=False)
    pager.page("", stream=out, env={})
    assert out.getvalue() == ""  # no stray newline, no pager spawned


def test_page_handles_non_ascii_through_pager(tmp_path):
    # ticket titles can be non-ASCII (Cyrillic, emoji). Piping must NOT crash on a C-locale-ish
    # child — we force utf-8 on the pipe. Regression for the text=True/locale-encoding bug.
    sink = tmp_path / "sink.txt"
    fake_pager = tmp_path / "pg.sh"
    fake_pager.write_text(f'#!/bin/sh\ncat > "{sink}"\n', encoding="utf-8")
    fake_pager.chmod(0o755)
    out = _Stream(tty=True)
    body = "#1 [todo] Починить заголовок — café ☕"
    pager.page(body, stream=out, env={"TASK_PAGER": str(fake_pager), "LC_ALL": "C"})
    assert sink.read_text(encoding="utf-8") == body + "\n"
