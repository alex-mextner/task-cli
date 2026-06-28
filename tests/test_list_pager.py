"""``task list`` / ``find`` pager integration: interactive-only paging + higher TTY limit.

Covers the wiring in ``cmd_list``/``cmd_find`` (not the pure pager module — that's
``test_pager.py``):
  - the result LIMIT is chosen by interactivity (100 on a TTY, 30 piped) unless ``-n`` is given;
  - paging happens ONLY interactively, and is suppressed by ``--no-pager`` / ``NO_PAGER`` /
    ``$PAGER=''``, falling back to a plain direct write that capsys can read.

Interactivity is forced by faking ``sys.stdout.isatty`` (capsys' stream is not a TTY otherwise).
The pager is steered with ``$TASK_PAGER`` so a "paged" run writes to a sentinel file we assert on.
"""

from __future__ import annotations

import argparse

import pytest

from tasklib import cli
from tasklib.cli import main


@pytest.fixture(autouse=True)
def _inject_fake(monkeypatch, fake_backend, isolated_state):
    monkeypatch.setattr("tasklib.backends.get_backend", lambda cfg, env=None: fake_backend)
    monkeypatch.setenv("TASK_SESSION", "testsess")
    return fake_backend


def _ns(**over) -> argparse.Namespace:
    base = dict(limit=None, no_pager=False)
    base.update(over)
    return argparse.Namespace(**base)


def _create_argv():
    """A complete ticket (passes every create gate) → creates #1 in the current session."""
    return [
        "create", "--title", "Add a thing", "--what", "the change", "--why", "because",
        "--impact",
        "Users on the dashboard can finally see their report load, so they no longer give up",
        "--if-not-done", "pain", "--acceptance", "it works", "--acceptance", "it also handles empties",
    ]


# ── _effective_limit: the interactive-vs-piped cap ───────────────────────────────────


def test_effective_limit_explicit_wins_over_interactivity(monkeypatch):
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True, raising=False)
    assert cli._effective_limit(_ns(limit=5)) == 5


def test_effective_limit_interactive_is_higher(monkeypatch):
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True, raising=False)
    assert cli._effective_limit(_ns()) == cli._LIMIT_INTERACTIVE == 100


def test_effective_limit_piped_is_small(monkeypatch):
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: False, raising=False)
    assert cli._effective_limit(_ns()) == cli._LIMIT_PIPED == 30


# ── list paging: interactive run pipes through the pager ─────────────────────────────


def test_list_paged_through_pager_when_interactive(capsys, tmp_path, monkeypatch, _inject_fake):
    sink = tmp_path / "sink.txt"
    fake_pager = tmp_path / "pg.sh"
    fake_pager.write_text(f'#!/bin/sh\ncat > "{sink}"\n', encoding="utf-8")
    fake_pager.chmod(0o755)
    monkeypatch.setenv("TASK_PAGER", str(fake_pager))
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True, raising=False)

    main(_create_argv())
    capsys.readouterr()
    rc = main(["list"])
    assert rc == 0
    # the ticket line reached the PAGER (sentinel file), not capsys stdout.
    paged = sink.read_text(encoding="utf-8")
    assert "#1" in paged
    assert capsys.readouterr().out == ""  # nothing ALSO leaked to stdout (no double-emit)


def test_list_piped_not_paged_even_when_pager_configured(capsys, tmp_path, monkeypatch, _inject_fake):
    # The core scriptability guarantee: a pager IS configured but stdout is NOT a tty → plain text
    # to stdout, pager untouched. Proves the isatty gate (not just NO_PAGER) does the work.
    sink = tmp_path / "sink.txt"
    fake_pager = tmp_path / "pg.sh"
    fake_pager.write_text(f'#!/bin/sh\ncat > "{sink}"\n', encoding="utf-8")
    fake_pager.chmod(0o755)
    monkeypatch.setenv("TASK_PAGER", str(fake_pager))
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: False, raising=False)
    main(_create_argv())
    capsys.readouterr()
    rc = main(["list"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "#1" in out  # printed plainly to stdout
    assert not sink.exists()  # pager never invoked


def test_list_no_pager_flag_prints_plain_even_on_tty(capsys, tmp_path, monkeypatch, _inject_fake):
    # a pager IS configured and stdout LOOKS like a tty, but --no-pager forces a direct write.
    boom = tmp_path / "boom.sh"
    boom.write_text('#!/bin/sh\necho PAGED >&2\n', encoding="utf-8")
    boom.chmod(0o755)
    monkeypatch.setenv("TASK_PAGER", str(boom))
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True, raising=False)

    main(_create_argv())
    capsys.readouterr()
    rc = main(["list", "--no-pager"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "#1" in out  # printed directly, not handed to the pager


def test_list_NO_PAGER_env_prints_plain_even_on_tty(capsys, tmp_path, monkeypatch, _inject_fake):
    monkeypatch.setenv("TASK_PAGER", "false")  # would fail if actually invoked
    monkeypatch.setenv("NO_PAGER", "1")
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True, raising=False)

    main(_create_argv())
    capsys.readouterr()
    rc = main(["list"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "#1" in out


def test_emit_list_drops_empty_blocks(monkeypatch):
    # _emit_list must filter falsy blocks so an empty notice doesn't add a leading blank line.
    captured = {}
    monkeypatch.setattr("tasklib.pager.page", lambda text, **kw: captured.setdefault("text", text))
    cli._emit_list(_ns(), ["", "#1 [todo] body", ""])
    assert captured["text"] == "#1 [todo] body"  # no leading/trailing blank from the empty blocks


def test_explicit_limit_flows_into_backend(monkeypatch, _inject_fake):
    # `-n N` must reach the backend call (not just _effective_limit in isolation). Spy on .list.
    seen = {}
    orig = _inject_fake.list

    def spy(*, labels=None, state=None, limit=30):
        seen["limit"] = limit
        return orig(labels=labels, state=state, limit=limit)

    monkeypatch.setattr(_inject_fake, "list", spy)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True, raising=False)  # would pick 100 w/o -n
    # no agent session tickets → falls back to backend.list with the resolved limit
    main(["list", "--no-pager", "-n", "7"])
    assert seen["limit"] == 7


def test_no_n_piped_uses_small_cap_in_backend(monkeypatch, _inject_fake):
    seen = {}
    orig = _inject_fake.list

    def spy(*, labels=None, state=None, limit=30):
        seen["limit"] = limit
        return orig(labels=labels, state=state, limit=limit)

    monkeypatch.setattr(_inject_fake, "list", spy)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: False, raising=False)
    main(["list"])
    assert seen["limit"] == cli._LIMIT_PIPED == 30


def test_find_paged_through_pager_when_interactive(capsys, tmp_path, monkeypatch, _inject_fake):
    # symmetric to test_list_paged_through_pager_when_interactive — the find wiring pipes too.
    sink = tmp_path / "sink.txt"
    fake_pager = tmp_path / "pg.sh"
    fake_pager.write_text(f'#!/bin/sh\ncat > "{sink}"\n', encoding="utf-8")
    fake_pager.chmod(0o755)
    monkeypatch.setenv("TASK_PAGER", str(fake_pager))
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True, raising=False)
    main(_create_argv())  # #1: "Add a thing"
    capsys.readouterr()
    rc = main(["find", "Add"])
    assert rc == 0
    assert "#1" in sink.read_text(encoding="utf-8")  # the hit reached the pager
    assert capsys.readouterr().out == ""  # not ALSO on stdout


def test_find_explicit_limit_flows_into_backend(monkeypatch, _inject_fake):
    # the find path must honor -n / interactivity too (it calls backend.search, not .list).
    seen = {}
    orig = _inject_fake.search

    def spy(query, *, state=None, limit=30):
        seen["limit"] = limit
        return orig(query, state=state, limit=limit)

    monkeypatch.setattr(_inject_fake, "search", spy)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True, raising=False)  # would be 100 w/o -n
    main(["find", "x", "--no-pager", "-n", "9"])
    assert seen["limit"] == 9


def test_find_no_pager_prints_plain_on_tty(capsys, tmp_path, monkeypatch, _inject_fake):
    boom = tmp_path / "boom.sh"
    boom.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    boom.chmod(0o755)
    monkeypatch.setenv("TASK_PAGER", str(boom))
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True, raising=False)
    main(_create_argv())  # #1: title "Add a thing"
    capsys.readouterr()
    rc = main(["find", "Add", "--no-pager"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "#1" in out  # search hit printed directly, not handed to the pager


def test_find_json_is_never_paged(capsys, tmp_path, monkeypatch, _inject_fake):
    sink = tmp_path / "sink.txt"
    fake_pager = tmp_path / "pg.sh"
    fake_pager.write_text(f'#!/bin/sh\ncat > "{sink}"\n', encoding="utf-8")
    fake_pager.chmod(0o755)
    monkeypatch.setenv("TASK_PAGER", str(fake_pager))
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True, raising=False)
    main(_create_argv())
    capsys.readouterr()
    rc = main(["find", "Add", "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    import json

    assert json.loads(out)[0]["id"] == "#1"
    assert not sink.exists()


def test_list_json_is_never_paged(capsys, tmp_path, monkeypatch, _inject_fake):
    # --json on a tty with a pager configured must STILL go straight to stdout (machine-readable).
    sink = tmp_path / "sink.txt"
    fake_pager = tmp_path / "pg.sh"
    fake_pager.write_text(f'#!/bin/sh\ncat > "{sink}"\n', encoding="utf-8")
    fake_pager.chmod(0o755)
    monkeypatch.setenv("TASK_PAGER", str(fake_pager))
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True, raising=False)

    main(_create_argv())
    capsys.readouterr()
    rc = main(["list", "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    import json

    payload = json.loads(out)  # valid JSON on stdout
    assert payload[0]["id"] == "#1"
    assert not sink.exists()  # the pager was never touched
