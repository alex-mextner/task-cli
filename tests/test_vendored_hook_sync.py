"""Drift guard for the vendored require-ticket hook fixture (issue #20).

WHAT this proves: ``test/docker/require_ticket_before_commit.py`` is a VENDORED copy of the canonical
agent-tools hook (``agent-hooks/require-ticket-before-commit/require_ticket_before_commit.py``). It
must stay byte-identical to the canonical body so the Docker ``test-cli`` enforcement leg tests the
SAME gate a real rig machine runs. Nothing mechanical enforced that before — the SYNC header merely
ASKED for it, so the copy could silently drift while ``enforce_test.sh`` stayed green against a stale
fixture (exactly the false confidence #20 warns about).

HOW the guard works WITHOUT a network / the agent-tools repo at test time: the vendored file carries
a delimited ``# SYNC-HEADER-BEGIN … # SYNC-HEADER-END`` block — the ONLY delta from the canonical
source — plus a pinned ``CANONICAL_SHA256``. This test reconstructs the canonical content (the file
with the SYNC-HEADER block removed) and asserts its SHA256 equals the pinned hash. So:

- a LOCAL edit to the vendored body changes its SHA → mismatch → CI fails;
- a STALE copy after the canonical changes upstream → the pinned SHA is bumped during the documented
  re-sync (``scripts/resync_vendored_hook.py``), and forgetting to re-copy the body fails this test.

Plus a contract check: the vendored fixture still emits the stable ``BLOCK_MARKER`` + exit 10 on a
ticketless commit (the cross-repo contract ``enforce_test.sh`` asserts), so a re-sync that broke that
contract would be caught here too, not only in Docker.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VENDORED = REPO_ROOT / "test" / "docker" / "require_ticket_before_commit.py"

SYNC_BEGIN = "# SYNC-HEADER-BEGIN"
SYNC_END = "# SYNC-HEADER-END"
BLOCK_MARKER = "[require-ticket] BLOCKED: no ticket reference"


def _vendored_text() -> str:
    return VENDORED.read_text(encoding="utf-8")


def _strip_sync_header(text: str) -> str:
    """Return ``text`` with exactly the ``# SYNC-HEADER-BEGIN … # SYNC-HEADER-END`` block removed.

    The block (plus its own trailing newline) is the ONLY delta between the vendored copy and the
    canonical source, so removing it reconstructs the canonical content byte-for-byte.
    """
    lines = text.split("\n")
    out: list[str] = []
    skipping = False
    for line in lines:
        if line.startswith(SYNC_BEGIN):
            skipping = True
            continue
        if line.startswith(SYNC_END):
            skipping = False
            continue
        if not skipping:
            out.append(line)
    return "\n".join(out)


def _pinned_canonical_sha() -> str:
    m = re.search(r"# CANONICAL_SHA256:\s*([0-9a-f]{64})", _vendored_text())
    assert m, "the vendored copy must record a CANONICAL_SHA256 in its SYNC-HEADER block"
    return m.group(1)


def test_vendored_file_exists_with_sync_header():
    assert VENDORED.is_file(), f"vendored hook missing: {VENDORED}"
    text = _vendored_text()
    assert SYNC_BEGIN in text and SYNC_END in text, "the SYNC-HEADER delimiters must be present"
    # exactly one block, well-formed (begin before end)
    assert text.count(SYNC_BEGIN) == 1
    assert text.count(SYNC_END) == 1
    assert text.index(SYNC_BEGIN) < text.index(SYNC_END)


def test_vendored_body_matches_pinned_canonical_sha():
    # THE drift guard: the canonical content reconstructed from the vendored copy must hash to the
    # pinned CANONICAL_SHA256. A drifted/edited vendored body fails here.
    reconstructed = _strip_sync_header(_vendored_text())
    got = hashlib.sha256(reconstructed.encode("utf-8")).hexdigest()
    expected = _pinned_canonical_sha()
    assert got == expected, (
        "the vendored require-ticket hook has DRIFTED from the recorded canonical source.\n"
        f"  reconstructed body sha256 = {got}\n"
        f"  pinned CANONICAL_SHA256   = {expected}\n"
        "Re-sync it: `python scripts/resync_vendored_hook.py <path-to-agent-tools>` "
        "(then commit), or — if you intentionally edited the vendored body — update the pinned "
        "hash via the same script so the documented delta is recorded."
    )


def test_vendored_hook_keeps_the_block_marker_contract():
    # the cross-repo contract enforce_test.sh asserts: a ticketless commit BLOCKS with the stable
    # marker + exit 10. Run the vendored hook directly so a re-sync that broke the contract fails here.
    event = '{"args":{"command":"git commit -m \\"add feature\\""},"cwd":"/tmp"}'
    proc = subprocess.run(
        [sys.executable, str(VENDORED)],
        input=event,
        capture_output=True,
        text=True,
        env={"REQUIRE_TICKET_STRICT": "1", "PATH": "/usr/bin:/bin"},
        timeout=10,
        check=False,
    )
    assert proc.returncode == 10, f"ticketless commit must exit 10; got {proc.returncode}: {proc.stderr}"
    assert BLOCK_MARKER in proc.stdout, f"block message must carry the stable marker; got {proc.stdout}"


def test_vendored_hook_allows_a_ticketed_commit():
    event = '{"args":{"command":"git commit -m \\"add feature (Closes #5)\\""},"cwd":"/tmp"}'
    proc = subprocess.run(
        [sys.executable, str(VENDORED)],
        input=event,
        capture_output=True,
        text=True,
        env={"REQUIRE_TICKET_STRICT": "1", "PATH": "/usr/bin:/bin"},
        timeout=10,
        check=False,
    )
    assert proc.returncode == 0, f"a ticketed commit must be allowed (exit 0); got {proc.returncode}"
    assert BLOCK_MARKER not in proc.stdout, "the allow path must NOT leak the block marker"


def _run_hook(command: str, *, strict: str = "1") -> subprocess.CompletedProcess:
    event = json.dumps({"args": {"command": command}, "cwd": "/tmp"})
    return subprocess.run(
        [sys.executable, str(VENDORED)],
        input=event,
        capture_output=True,
        text=True,
        env={"REQUIRE_TICKET_STRICT": strict, "PATH": "/usr/bin:/bin"},
        timeout=10,
        check=False,
    )


def test_vendored_hook_blocks_F_dash_stdin_message_in_strict():
    # the key new canonical behavior (agent-tools#104): `-F -` (message on git's stdin) is
    # unreadable, so strict mode FAILS CLOSED with the block marker — pin the contract here too.
    proc = _run_hook('git commit -F -')
    assert proc.returncode == 10, proc.stderr
    assert BLOCK_MARKER in proc.stdout


def test_vendored_hook_F_dash_is_warn_only_allow_without_marker():
    # warn-only mode (REQUIRE_TICKET_STRICT=0) must ALLOW the `-F -` commit (exit 0) and NOT leak the
    # block marker — the marker is proof of a real block only
    proc = _run_hook('git commit -F -', strict="0")
    assert proc.returncode == 0, proc.stderr
    assert BLOCK_MARKER not in proc.stdout


def test_vendored_hook_gates_a_ticketless_commit_in_a_chain():
    # a ticketless `git commit` later in an && chain must still BLOCK (the parser walks segments)
    proc = _run_hook('echo hi && git commit -m "no ticket here"')
    assert proc.returncode == 10, proc.stderr
    assert BLOCK_MARKER in proc.stdout


def test_vendored_hook_honors_inline_skip_escape():
    # the documented inline escape REQUIRE_TICKET_SKIP=1 must allow a ticketless commit
    proc = _run_hook('REQUIRE_TICKET_SKIP=1 git commit -m "no ticket"')
    assert proc.returncode == 0, proc.stderr
    assert BLOCK_MARKER not in proc.stdout


def test_resync_check_is_body_only_and_ignores_the_commit_line():
    # the --check drift detector must compare only the canonical BODY, not the SYNC-HEADER's
    # CANONICAL_AGENT_TOOLS_COMMIT line — a shallow CI checkout can't resolve that commit, so a
    # whole-file comparison would flag spurious drift on a byte-identical body. Prove --check still
    # passes when only the recorded commit line differs.
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "resync_vendored_hook", REPO_ROOT / "scripts" / "resync_vendored_hook.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    text = _vendored_text()
    # the reconstruction (header stripped) must round-trip to the canonical body the test hashes
    reconstructed_by_script = mod.reconstruct_canonical_from_vendored(text)
    reconstructed_by_test = _strip_sync_header(text)
    assert reconstructed_by_script == reconstructed_by_test, "script + test must strip the header identically"

    # mutating ONLY the recorded commit line must not change the reconstructed body's hash
    mutated = re.sub(r"# CANONICAL_AGENT_TOOLS_COMMIT:.*", "# CANONICAL_AGENT_TOOLS_COMMIT: deadbeef", text)
    h_orig = hashlib.sha256(mod.reconstruct_canonical_from_vendored(text).encode("utf-8")).hexdigest()
    h_mut = hashlib.sha256(mod.reconstruct_canonical_from_vendored(mutated).encode("utf-8")).hexdigest()
    assert h_orig == h_mut, "a changed commit line must NOT register as body drift"
    assert h_orig == _pinned_canonical_sha()


def _load_resync_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "resync_vendored_hook", REPO_ROOT / "scripts" / "resync_vendored_hook.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fake_agent_tools(tmp_path, body: str) -> str:
    canon = tmp_path / "agent-hooks" / "require-ticket-before-commit" / "require_ticket_before_commit.py"
    canon.parent.mkdir(parents=True)
    canon.write_text(body, encoding="utf-8")
    return str(tmp_path)


def test_resync_check_exit_codes_contract(tmp_path):
    # the workflow keys "drift vs infra error" on the script's exit codes — pin all three.
    mod = _load_resync_module()

    # rc 2 — usage error (wrong arg count) and a missing canonical path
    assert mod.main(["resync", "--check"]) == 2  # no path → usage
    assert mod.main(["resync", "--check", str(tmp_path / "nope")]) == 2  # canonical not found

    # rc 1 — a fake agent-tools whose canonical body differs from the vendored copy → real drift
    drifted = _fake_agent_tools(tmp_path / "a", "#!/usr/bin/env python3\n# different body\n")
    assert mod.main(["resync", "--check", drifted]) == 1

    # rc 0 — the canonical body equal to what the vendored copy reconstructs → in sync
    in_sync_body = _strip_sync_header(_vendored_text())
    synced = _fake_agent_tools(tmp_path / "b", in_sync_body)
    assert mod.main(["resync", "--check", synced]) == 0


def test_resync_cli_maps_a_crash_to_infra_exit_not_drift(tmp_path, monkeypatch):
    # a CRASH (unexpected exception) must exit >=2 (infra error), NOT 1 (which the workflow reads as
    # real drift → "re-sync the body", the wrong advice). cli() wraps main() to guarantee this.
    mod = _load_resync_module()
    synced = _fake_agent_tools(tmp_path, _strip_sync_header(_vendored_text()))

    def boom(*a, **k):
        raise RuntimeError("simulated infra crash")

    monkeypatch.setattr(mod, "reconstruct_canonical_from_vendored", boom)
    rc = mod.cli(["resync", "--check", synced])
    assert rc >= 2, "an unexpected crash must be an infra error (>=2), never drift (1)"
