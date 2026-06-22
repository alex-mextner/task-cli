"""install-skill — writes the SKILL.md AND the SessionStart .blurbs/<tool>.md entry.

The blurb is the always-on advertisement: a SessionStart hook cats every
~/.agents/skills/.blurbs/*.md into each new agent session ("Agent CLI tools installed
on this machine"). Without a blurb, the tool is installed but invisible at session start
while its siblings (draw/tg/review) are not. These tests pin both writes + idempotency.
"""

from __future__ import annotations

import os

import pytest

from tasklib.install import install_skill


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


def test_install_skill_writes_skill_md(fake_home):
    assert install_skill() == 0
    skill = fake_home / ".agents" / "skills" / "task" / "SKILL.md"
    assert skill.is_file()
    assert "name: task" in skill.read_text(encoding="utf-8")


def test_install_skill_writes_blurb(fake_home):
    assert install_skill() == 0
    blurb = fake_home / ".agents" / "skills" / ".blurbs" / "task.md"
    assert blurb.is_file(), "install-skill must write the SessionStart .blurbs/task.md entry"
    text = blurb.read_text(encoding="utf-8")
    # one-line sibling format: "- `task` — ..."
    assert text.lstrip().startswith("- `task`"), text


def test_install_skill_creates_blurbs_dir(fake_home):
    # the .blurbs dir may not exist yet on a fresh machine
    assert not (fake_home / ".agents" / "skills" / ".blurbs").exists()
    assert install_skill() == 0
    assert (fake_home / ".agents" / "skills" / ".blurbs").is_dir()


def test_install_skill_idempotent_blurb(fake_home, capsys):
    assert install_skill() == 0
    capsys.readouterr()
    blurb = fake_home / ".agents" / "skills" / ".blurbs" / "task.md"
    mtime_before = os.path.getmtime(blurb)
    content_before = blurb.read_text(encoding="utf-8")
    # second run must not rewrite an already-current blurb
    assert install_skill() == 0
    assert blurb.read_text(encoding="utf-8") == content_before
    assert os.path.getmtime(blurb) == mtime_before
