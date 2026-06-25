"""install-skill — the THREE advertisement layers, matching the sibling CLIs (draw/tg/review).

Layer 1 is SKILL.md + the always-on ``.blurbs/<tool>.md`` blurb + a ``~/.claude/skills`` compat
symlink. Layer 2 is a marked ``<!-- skill:task -->…`` block in each DETECTED harness instruction
file. Layer 3 is the idempotent SessionStart aggregator hook in ``~/.claude/settings.json``.
Without all three, ``task`` is installed but under-advertised vs its siblings. These tests pin
every layer + idempotency + the conservative "don't clobber unparseable settings" guarantee.
"""

from __future__ import annotations

import json
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


# ── Layer 1: the ~/.claude/skills compat symlink ────────────────────────────────────


def test_install_skill_symlinks_into_claude_skills_when_present(fake_home):
    # Claude Code also scans ~/.claude/skills; install-skill symlinks task there WHEN that dir exists
    (fake_home / ".claude" / "skills").mkdir(parents=True)
    assert install_skill() == 0
    link = fake_home / ".claude" / "skills" / "task"
    assert link.is_symlink(), "task must be symlinked into ~/.claude/skills"
    # the link resolves to the canonical ~/.agents skill dir
    assert link.resolve() == (fake_home / ".agents" / "skills" / "task").resolve()


def test_install_skill_skips_claude_symlink_when_dir_absent(fake_home):
    # no ~/.claude/skills → we must NOT conjure a Claude layout on a box that doesn't run it
    assert install_skill() == 0
    assert not (fake_home / ".claude" / "skills").exists()


def test_install_skill_symlink_leaves_existing_file_untouched(fake_home):
    # a pre-existing file at ~/.claude/skills/task is left as-is (no error, not overwritten)
    (fake_home / ".claude" / "skills").mkdir(parents=True)
    link = fake_home / ".claude" / "skills" / "task"
    link.write_text("pre-existing — do not touch", encoding="utf-8")  # a plain file, not a symlink
    assert install_skill() == 0
    assert not link.is_symlink()
    assert link.read_text(encoding="utf-8") == "pre-existing — do not touch"


def test_install_skill_symlink_idempotent(fake_home):
    # re-running over the symlink we created is a no-op (no error, link unchanged)
    (fake_home / ".claude" / "skills").mkdir(parents=True)
    assert install_skill() == 0
    link = fake_home / ".claude" / "skills" / "task"
    assert link.is_symlink()
    target_before = os.readlink(link)
    assert install_skill() == 0
    assert link.is_symlink()
    assert os.readlink(link) == target_before


# ── Layer 2: the marked block in each detected harness instruction file ──────────────


def test_install_skill_injects_block_into_detected_claude_md(fake_home):
    (fake_home / ".claude").mkdir()
    (fake_home / ".claude" / "CLAUDE.md").write_text("# my notes\nkeep me\n", encoding="utf-8")
    assert install_skill() == 0
    text = (fake_home / ".claude" / "CLAUDE.md").read_text(encoding="utf-8")
    assert "<!-- skill:task -->" in text and "<!-- /skill:task -->" in text
    assert "`task`" in text  # the blurb content landed
    assert "keep me" in text, "pre-existing instruction content must be preserved"


def test_install_skill_skips_undetected_harness(fake_home):
    # gemini is NOT installed (no ~/.gemini) → no GEMINI.md is conjured
    (fake_home / ".claude").mkdir()
    assert install_skill() == 0
    assert not (fake_home / ".gemini").exists()


def test_install_skill_block_is_replaced_not_duplicated(fake_home):
    (fake_home / ".codex").mkdir()
    assert install_skill() == 0
    agents_md = fake_home / ".codex" / "AGENTS.md"
    first = agents_md.read_text(encoding="utf-8")
    assert first.count("<!-- skill:task -->") == 1
    # a second install must REFRESH the block in place, never append a duplicate
    assert install_skill() == 0
    second = agents_md.read_text(encoding="utf-8")
    assert second.count("<!-- skill:task -->") == 1
    assert second.count("<!-- /skill:task -->") == 1


def test_install_skill_injects_into_opencode_and_gemini(fake_home):
    (fake_home / ".config" / "opencode").mkdir(parents=True)
    (fake_home / ".gemini").mkdir()
    assert install_skill() == 0
    assert "<!-- skill:task -->" in (
        fake_home / ".config" / "opencode" / "AGENTS.md"
    ).read_text(encoding="utf-8")
    assert "<!-- skill:task -->" in (fake_home / ".gemini" / "GEMINI.md").read_text(encoding="utf-8")


def test_install_skill_preserves_content_after_the_block_in_place(fake_home):
    # a re-run must refresh the block WHERE IT SITS, never reorder content that follows it
    (fake_home / ".claude").mkdir()
    claude_md = fake_home / ".claude" / "CLAUDE.md"
    seeded = (
        "intro line\n\n"
        "<!-- skill:task -->\n- `task` OLD blurb\n<!-- /skill:task -->\n\n"
        "trailing line that must stay last\n"
    )
    claude_md.write_text(seeded, encoding="utf-8")
    assert install_skill() == 0
    text = claude_md.read_text(encoding="utf-8")
    assert text.index("intro line") < text.index("<!-- skill:task -->")
    assert text.index("<!-- /skill:task -->") < text.index("trailing line that must stay last")
    assert "OLD blurb" not in text, "the stale block content must be refreshed"
    assert text.count("<!-- skill:task -->") == 1


def test_install_skill_refreshes_stale_block_content(fake_home):
    # an existing block with stale text is REPLACED with the current BLURB (not just kept)
    from tasklib.install import BLURB

    (fake_home / ".gemini").mkdir()
    gemini_md = fake_home / ".gemini" / "GEMINI.md"
    gemini_md.write_text(
        "<!-- skill:task -->\n- `task` totally stale wording\n<!-- /skill:task -->\n",
        encoding="utf-8",
    )
    assert install_skill() == 0
    text = gemini_md.read_text(encoding="utf-8")
    assert "totally stale wording" not in text
    assert BLURB.rstrip() in text


def test_install_skill_creates_instruction_file_when_dir_exists_but_file_absent(fake_home):
    # the docstring promises: a DETECTED harness (config dir exists) gets its instruction file
    # CREATED if absent — same as the siblings. Assert it directly (not only via .codex indirectly).
    (fake_home / ".claude").mkdir()
    claude_md = fake_home / ".claude" / "CLAUDE.md"
    assert not claude_md.exists()
    assert install_skill() == 0
    assert claude_md.is_file()
    assert "<!-- skill:task -->" in claude_md.read_text(encoding="utf-8")


def test_install_skill_orphan_open_marker_does_not_lose_user_text(fake_home):
    # an ORPHANED open marker (no matching close — from a manual edit / interrupted write) must NOT
    # cause user text to be deleted on a subsequent run. Regression for the find(start)/find(end)
    # mis-anchoring that would delete everything between the orphan and a freshly-appended block.
    (fake_home / ".claude").mkdir()
    claude_md = fake_home / ".claude" / "CLAUDE.md"
    claude_md.write_text("A<!-- skill:task -->B precious user text\n", encoding="utf-8")
    assert install_skill() == 0
    assert install_skill() == 0  # the dangerous path is the SECOND run
    text = claude_md.read_text(encoding="utf-8")
    assert "precious user text" in text, "user text must survive an orphaned open marker"
    assert text.count("<!-- skill:task -->") == 1  # exactly one well-formed block now
    assert text.count("<!-- /skill:task -->") == 1


def test_install_skill_orphan_open_before_real_block_preserves_text(fake_home):
    # the deeper data-loss path: an orphan open marker BEFORE a well-formed block must NOT cause the
    # user text between them (or inside) to be swallowed. Anchoring on first-close + last-open-before
    # is what makes this safe.
    (fake_home / ".claude").mkdir()
    claude_md = fake_home / ".claude" / "CLAUDE.md"
    claude_md.write_text(
        "PRE <!-- skill:task --> orphan-mid\n"
        "<!-- skill:task -->\n- `task` OLD\n<!-- /skill:task -->\n"
        "POST keep me\n",
        encoding="utf-8",
    )
    assert install_skill() == 0
    text = claude_md.read_text(encoding="utf-8")
    assert "PRE" in text and "orphan-mid" in text, "text before the block must survive"
    assert "POST keep me" in text, "text after the block must survive"
    assert text.count("<!-- skill:task -->") == 1, "the orphan open token is stripped"
    assert text.count("<!-- /skill:task -->") == 1
    assert "OLD" not in text  # the real block content was refreshed
    # a second run is also safe (no further loss, still one well-formed block)
    assert install_skill() == 0
    text2 = claude_md.read_text(encoding="utf-8")
    assert "orphan-mid" in text2 and "POST keep me" in text2
    assert text2.count("<!-- skill:task -->") == 1


def test_install_skill_two_consecutive_orphan_opens_preserve_text(fake_home):
    (fake_home / ".gemini").mkdir()
    gm = fake_home / ".gemini" / "GEMINI.md"
    gm.write_text("<!-- skill:task --> A <!-- skill:task --> B precious\n", encoding="utf-8")
    assert install_skill() == 0
    assert install_skill() == 0  # second run is the dangerous one
    text = gm.read_text(encoding="utf-8")
    assert "A" in text and "B precious" in text, "user text between orphan markers must survive"
    assert text.count("<!-- skill:task -->") == 1
    assert text.count("<!-- /skill:task -->") == 1


def test_install_skill_orphan_close_marker_is_idempotent(fake_home):
    # the SYMMETRIC case to orphan-open: a lone CLOSE marker must NOT make the blurb accumulate or
    # the file rewrite forever. After the first run the file is stable across further runs.
    (fake_home / ".claude").mkdir()
    claude_md = fake_home / ".claude" / "CLAUDE.md"
    claude_md.write_text("<!-- /skill:task -->\nnotes kept\n", encoding="utf-8")
    assert install_skill() == 0
    after_first = claude_md.read_text(encoding="utf-8")
    assert install_skill() == 0
    after_second = claude_md.read_text(encoding="utf-8")
    assert after_first == after_second, "an orphan close marker must not break idempotency"
    # blurb appears exactly once, exactly one well-formed block, user text preserved
    assert after_second.count("<!-- skill:task -->") == 1
    assert after_second.count("<!-- /skill:task -->") == 1
    assert "notes kept" in after_second
    assert after_second.count("Backends:") == 1  # the blurb body is not duplicated


def test_install_skill_two_full_blocks_collapse_to_one(fake_home):
    # a file that somehow has TWO well-formed blocks must converge to one without losing user text
    (fake_home / ".gemini").mkdir()
    gm = fake_home / ".gemini" / "GEMINI.md"
    gm.write_text(
        "<!-- skill:task -->\nA\n<!-- /skill:task -->\nMID\n<!-- skill:task -->\nB\n<!-- /skill:task -->\n",
        encoding="utf-8",
    )
    assert install_skill() == 0
    assert install_skill() == 0  # must stabilize
    text = gm.read_text(encoding="utf-8")
    assert "MID" in text, "user text between the two blocks must survive"
    assert text.count("<!-- skill:task -->") == 1
    assert text.count("<!-- /skill:task -->") == 1


def test_install_skill_does_not_duplicate_a_neighbor_aggregator_hook(fake_home):
    # the SessionStart aggregator marker (# agent-tools-awareness) is SHARED with tg/review/draw. If a
    # neighbor installer already added a hook with that marker (a DIFFERENT command form), task must
    # NOT add a second one — the single aggregator cats every blurb.
    (fake_home / ".claude").mkdir()
    settings = fake_home / ".claude" / "settings.json"
    neighbor_cmd = "sh -c 'echo neighbor' # agent-tools-awareness"
    settings.write_text(
        json.dumps({"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": neighbor_cmd}]}]}}),
        encoding="utf-8",
    )
    assert install_skill() == 0
    cmds = _session_start_commands(settings)
    assert sum("# agent-tools-awareness" in c for c in cmds) == 1, "must not add a second aggregator"
    assert neighbor_cmd in cmds, "the neighbor's hook is left untouched"


def test_install_skill_instruction_file_no_rewrite_when_current(fake_home):
    # idempotency of layer 2: a second run over an already-current instruction file must not rewrite it
    (fake_home / ".codex").mkdir()
    assert install_skill() == 0
    agents_md = fake_home / ".codex" / "AGENTS.md"
    content_before = agents_md.read_text(encoding="utf-8")
    mtime_before = os.path.getmtime(agents_md)
    assert install_skill() == 0
    assert agents_md.read_text(encoding="utf-8") == content_before
    assert os.path.getmtime(agents_md) == mtime_before


def test_install_skill_leaves_broken_pre_existing_symlink_untouched(fake_home):
    # a pre-existing BROKEN symlink at ~/.claude/skills/task is left as-is (link.is_symlink() guard)
    (fake_home / ".claude" / "skills").mkdir(parents=True)
    link = fake_home / ".claude" / "skills" / "task"
    link.symlink_to(fake_home / "nonexistent-target")  # dangling on purpose
    assert link.is_symlink() and not link.exists()
    assert install_skill() == 0
    # still the same dangling link — we did not "fix" or replace it
    assert link.is_symlink()
    assert os.readlink(link) == str(fake_home / "nonexistent-target")


# ── Layer 3: the idempotent SessionStart aggregator hook ─────────────────────────────


def _session_start_commands(settings_path) -> list[str]:
    data = json.loads(settings_path.read_text(encoding="utf-8"))
    cmds: list[str] = []
    for group in data.get("hooks", {}).get("SessionStart", []):
        if not isinstance(group, dict):
            continue  # tolerate foreign junk entries the installer preserves
        for hook in group.get("hooks", []):
            if isinstance(hook, dict):
                cmds.append(hook.get("command", ""))
    return cmds


def test_install_skill_adds_session_start_hook_to_fresh_settings(fake_home):
    (fake_home / ".claude").mkdir()
    assert install_skill() == 0
    settings = fake_home / ".claude" / "settings.json"
    assert settings.is_file()
    cmds = _session_start_commands(settings)
    assert any("# agent-tools-awareness" in c for c in cmds), cmds
    assert any(".blurbs" in c for c in cmds)


def test_install_skill_session_hook_is_idempotent(fake_home):
    (fake_home / ".claude").mkdir()
    assert install_skill() == 0
    settings = fake_home / ".claude" / "settings.json"
    first = _session_start_commands(settings)
    assert install_skill() == 0
    second = _session_start_commands(settings)
    # exactly one aggregator hook, no matter how many times we run
    assert first == second
    assert sum("# agent-tools-awareness" in c for c in second) == 1


def test_install_skill_preserves_existing_settings_and_hooks(fake_home):
    (fake_home / ".claude").mkdir()
    settings = fake_home / ".claude" / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "model": "opus",
                "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo hi"}]}]},
            }
        ),
        encoding="utf-8",
    )
    assert install_skill() == 0
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data["model"] == "opus", "unrelated settings must survive"
    cmds = _session_start_commands(settings)
    assert "echo hi" in cmds, "a pre-existing SessionStart hook must be preserved"
    assert any("# agent-tools-awareness" in c for c in cmds)


def test_install_skill_backs_up_settings_before_rewrite(fake_home):
    (fake_home / ".claude").mkdir()
    settings = fake_home / ".claude" / "settings.json"
    original = json.dumps({"model": "opus"})
    settings.write_text(original, encoding="utf-8")
    assert install_skill() == 0
    bak = fake_home / ".claude" / "settings.json.bak"
    assert bak.is_file(), "the pre-rewrite settings must be backed up"
    assert bak.read_text(encoding="utf-8") == original


def test_install_skill_does_not_clobber_existing_backup(fake_home):
    # a pre-existing settings.json.bak (the user's own, or a prior tool's) must NOT be overwritten
    (fake_home / ".claude").mkdir()
    (fake_home / ".claude" / "settings.json").write_text(json.dumps({"model": "opus"}), encoding="utf-8")
    bak = fake_home / ".claude" / "settings.json.bak"
    bak.write_text("PRECIOUS pre-existing backup", encoding="utf-8")
    assert install_skill() == 0
    assert bak.read_text(encoding="utf-8") == "PRECIOUS pre-existing backup"


def test_install_skill_does_not_clobber_unparseable_settings(fake_home):
    (fake_home / ".claude").mkdir()
    settings = fake_home / ".claude" / "settings.json"
    settings.write_text("{ this is not json", encoding="utf-8")
    # must not crash and must leave the unparseable file exactly as-is
    assert install_skill() == 0
    assert settings.read_text(encoding="utf-8") == "{ this is not json"
    assert not (fake_home / ".claude" / "settings.json.bak").exists()


@pytest.mark.parametrize(
    "raw",
    [
        "[]",  # root is a JSON array, not an object
        json.dumps({"hooks": "not-a-dict"}),  # hooks is not an object
        json.dumps({"hooks": {"SessionStart": "not-a-list"}}),  # SessionStart is not a list
    ],
)
def test_install_skill_leaves_structurally_unexpected_settings_untouched(fake_home, raw):
    # the conservative guard branches: a settings.json with an unexpected SHAPE must be left as-is
    # (no crash, no hook injected) rather than rewritten into something that drops the user's data
    (fake_home / ".claude").mkdir()
    settings = fake_home / ".claude" / "settings.json"
    settings.write_text(raw, encoding="utf-8")
    assert install_skill() == 0
    assert settings.read_text(encoding="utf-8") == raw
    assert not (fake_home / ".claude" / "settings.json.bak").exists()


def test_install_skill_skips_session_hook_when_no_claude_dir(fake_home):
    # no ~/.claude → no settings.json conjured (layer 3 is Claude-Code-specific)
    assert install_skill() == 0
    assert not (fake_home / ".claude").exists()


def test_install_skill_survives_claude_being_a_file(fake_home):
    # a ~/.claude that is a FILE (not a dir) must not crash layers 2/3 (is_dir() → False, skipped)
    (fake_home / ".claude").write_text("not a directory", encoding="utf-8")
    assert install_skill() == 0  # no crash
    # the canonical ~/.agents layer still installed
    assert (fake_home / ".agents" / "skills" / "task" / "SKILL.md").is_file()


def test_install_skill_preserves_non_dict_junk_in_session_start(fake_home):
    # defensive guards: junk (non-dict groups/hooks) in SessionStart is preserved, our hook appended
    (fake_home / ".claude").mkdir()
    settings = fake_home / ".claude" / "settings.json"
    settings.write_text(
        json.dumps({"hooks": {"SessionStart": ["junk-string", {"hooks": ["also-junk"]}]}}),
        encoding="utf-8",
    )
    assert install_skill() == 0
    data = json.loads(settings.read_text(encoding="utf-8"))
    groups = data["hooks"]["SessionStart"]
    assert "junk-string" in groups, "foreign junk entries are preserved"
    cmds = _session_start_commands(settings)
    assert any("# agent-tools-awareness" in c for c in cmds)


def test_install_skill_does_not_clobber_backup_dangling_symlink(fake_home):
    # a dangling settings.json.bak SYMLINK must not be written through (exists() follows the link)
    (fake_home / ".claude").mkdir()
    (fake_home / ".claude" / "settings.json").write_text(json.dumps({"model": "opus"}), encoding="utf-8")
    bak = fake_home / ".claude" / "settings.json.bak"
    bak.symlink_to(fake_home / "no-such-target")  # dangling
    assert install_skill() == 0
    # we did NOT write through the dangling link (target still absent), and the link is untouched
    assert bak.is_symlink()
    assert not (fake_home / "no-such-target").exists()


def test_hook_command_runs_under_sh(fake_home):
    # smoke: the SessionStart hook command (a shell snippet with the trailing comment marker) must be
    # executable by `sh -c` and cat the installed blurb — a broken command would pass silently otherwise
    import subprocess

    from tasklib.install import _HOOK_COMMAND

    assert install_skill() == 0  # writes ~/.agents/skills/.blurbs/task.md
    proc = subprocess.run(
        ["sh", "-c", _HOOK_COMMAND],
        capture_output=True,
        text=True,
        env={"HOME": str(fake_home), "PATH": "/usr/bin:/bin"},
        timeout=10,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "`task`" in proc.stdout, "the aggregator must cat the task blurb"
