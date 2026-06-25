#!/usr/bin/env python3
"""Re-sync the vendored require-ticket hook fixture from the agent-tools canonical source.

WHAT this does: rebuilds ``test/docker/require_ticket_before_commit.py`` so its body is byte-identical
to the current canonical hook in agent-tools, and refreshes the pinned ``CANONICAL_SHA256`` +
``CANONICAL_AGENT_TOOLS_COMMIT`` recorded in the vendored copy's SYNC-HEADER block.

WHY it exists: the vendored copy is a hermetic FIXTURE for the Docker enforcement leg — it must NOT
depend on agent-tools at image-build time, so it carries a snapshot of the canonical hook. That
snapshot drifts the moment agent-tools changes the canonical (issue #20). ``tests/test_vendored_hook_sync.py``
fails CI when the vendored body no longer matches the pinned SHA; this script is the documented,
one-command way to make it match again after an intentional canonical change.

HOW to run::

    python scripts/resync_vendored_hook.py /path/to/agent-tools

It locates the canonical hook under the given agent-tools checkout, recomputes its SHA256 + the
commit that last touched it, and rewrites the vendored fixture (shebang + SYNC header + canonical
body). Re-run the suite afterwards: the sync test goes green and the enforce test still asserts the
``[require-ticket] BLOCKED: no ticket reference`` + exit-10 contract.

INVARIANT: the SYNC-HEADER block is the ONLY delta between the vendored copy and the canonical file.
The header is bounded by ``# SYNC-HEADER-BEGIN`` / ``# SYNC-HEADER-END`` so the guard can strip
exactly it and compare the remainder; do not add content outside that block.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

# The canonical hook's path WITHIN an agent-tools checkout, and the vendored fixture this repo ships.
CANONICAL_REL = Path("agent-hooks/require-ticket-before-commit/require_ticket_before_commit.py")
VENDORED_REL = Path("test/docker/require_ticket_before_commit.py")

SYNC_BEGIN = "# SYNC-HEADER-BEGIN"
SYNC_END = "# SYNC-HEADER-END"


def canonical_commit(canonical: Path) -> str:
    """The agent-tools commit that last touched the canonical hook (for provenance in the header)."""
    try:
        out = subprocess.run(
            ["git", "-C", str(canonical.parent), "log", "-1", "--format=%H", "--", canonical.name],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return out.stdout.strip() or "unknown"


def build_sync_header(canonical_sha: str, canonical_commit_sha: str) -> str:
    """The SYNC-HEADER block (bounded by the BEGIN/END markers the guard strips)."""
    return "\n".join(
        [
            f"{SYNC_BEGIN}  (this block is the ONLY delta from the canonical source; the drift",
            "#   guard strips exactly these lines and asserts the remainder is byte-identical to the hash)",
            "# VENDORED COPY of agent-tools/agent-hooks/require-ticket-before-commit/require_ticket_before_commit.py.",
            "# The canonical source lives in agent-tools (rig provisions it onto a real machine). This copy is a",
            "# hermetic FIXTURE for the Docker `test-cli` enforcement leg (test/docker/enforce_test.sh): a",
            "# clean-rig machine has this exact hook, so the harness ships it to prove the strict ticket gate",
            "# blocks a ticketless commit end-to-end WITHOUT depending on agent-tools at image-build time.",
            "#",
            "# DRIFT GUARD (issue #20): tests/test_vendored_hook_sync.py rebuilds the canonical content from this",
            "# file (shebang + body, with the SYNC-HEADER block removed) and asserts its SHA256 equals",
            "# CANONICAL_SHA256 below. A local edit to this copy, OR a stale copy after the canonical changes,",
            "# fails CI instead of silently diverging — exactly the false-confidence #20 warns about.",
            "#",
            f"# CANONICAL_SHA256: {canonical_sha}",
            f"# CANONICAL_AGENT_TOOLS_COMMIT: {canonical_commit_sha}",
            "#",
            "# TO RE-SYNC after the canonical hook changes: run `python scripts/resync_vendored_hook.py",
            "# <path-to-agent-tools>` (re-copies the canonical body and refreshes CANONICAL_SHA256 + the commit",
            "# above). Keep the marker contract (`[require-ticket] BLOCKED: no ticket reference` + exit 10)",
            "# intact — the enforce test asserts it.",
            SYNC_END,
        ]
    )


def render_vendored(canonical_text: str, canonical_sha: str, canonical_commit_sha: str) -> str:
    """Render the vendored fixture: shebang + SYNC header + the canonical body verbatim."""
    lines = canonical_text.split("\n")
    shebang, body = lines[0], "\n".join(lines[1:])
    header = build_sync_header(canonical_sha, canonical_commit_sha)
    return f"{shebang}\n{header}\n{body}"


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if a != "--check"]
    check_only = "--check" in argv[1:]
    if len(args) != 1:
        print(
            "usage: python scripts/resync_vendored_hook.py [--check] <path-to-agent-tools-checkout>",
            file=sys.stderr,
        )
        return 2
    agent_tools = Path(args[0]).expanduser().resolve()
    canonical = agent_tools / CANONICAL_REL
    if not canonical.is_file():
        print(f"error: canonical hook not found at {canonical}", file=sys.stderr)
        return 2

    repo_root = Path(__file__).resolve().parent.parent
    vendored = repo_root / VENDORED_REL

    canonical_text = canonical.read_text(encoding="utf-8")
    # Hash the SAME reconstructed text the drift test re-derives (read_text → universal newlines),
    # so the recorded CANONICAL_SHA256 is self-consistent with tests/test_vendored_hook_sync.py even
    # if the canonical were ever stored CRLF (read_text would normalize it identically on both sides).
    canonical_sha = hashlib.sha256(canonical_text.encode("utf-8")).hexdigest()

    if check_only:
        # Upstream-drift detector: compare only the drift-RELEVANT part (the canonical body hash),
        # NOT the whole rendered file — the SYNC-HEADER carries CANONICAL_AGENT_TOOLS_COMMIT, which a
        # shallow CI checkout can't resolve, so comparing it would flag spurious "drift" on a
        # byte-identical body. Reconstruct the vendored body and compare its hash to the canonical's.
        if not vendored.is_file():
            # A MISSING fixture is a repo breakage, not body drift — infra error (>=2), so the
            # workflow advises "investigate the check", not the wrong "re-sync the body".
            print(f"error: vendored copy missing at {vendored} (repo breakage, not drift)", file=sys.stderr)
            return 2
        vendored_sha = hashlib.sha256(
            reconstruct_canonical_from_vendored(vendored.read_text(encoding="utf-8")).encode("utf-8")
        ).hexdigest()
        if vendored_sha == canonical_sha:
            print(f"OK: {vendored} body is in sync with the canonical (sha256 {canonical_sha})")
            return 0
        print(
            f"DRIFT: {vendored} body no longer matches the canonical source.\n"
            f"  vendored body sha256  = {vendored_sha}\n"
            f"  canonical sha256      = {canonical_sha}\n"
            "  re-run without --check to re-sync, then commit.",
            file=sys.stderr,
        )
        return 1

    rendered = render_vendored(canonical_text, canonical_sha, canonical_commit(canonical))
    vendored.write_text(rendered, encoding="utf-8")
    print(f"re-synced {vendored} from {canonical}")
    print(f"  CANONICAL_SHA256: {canonical_sha}")
    return 0


def reconstruct_canonical_from_vendored(vendored_text: str) -> str:
    """Strip the ``# SYNC-HEADER-BEGIN … # SYNC-HEADER-END`` block to recover the canonical content.

    The SYNC-HEADER block is the ONLY delta from the canonical source, so removing it yields the
    canonical text byte-for-byte. Kept identical to the stripping in tests/test_vendored_hook_sync.py.
    """
    out: list[str] = []
    skipping = False
    for line in vendored_text.split("\n"):
        if line.startswith(SYNC_BEGIN):
            skipping = True
            continue
        if line.startswith(SYNC_END):
            skipping = False
            continue
        if not skipping:
            out.append(line)
    return "\n".join(out)


def cli(argv: list[str]) -> int:
    """Run :func:`main`, mapping an UNEXPECTED crash to an INFRA exit code (>=2), never to 1.

    The drift workflow keys on the exit code: rc 1 == real drift ("re-sync the body"), rc >=2 ==
    infra error ("don't touch the body, investigate the check"). A bare Python crash exits 1, which
    would falsely read as drift and give the wrong advice — so any unexpected exception here is
    reported as rc 2 with the traceback on stderr instead.
    """
    try:
        return main(argv)
    except Exception:  # noqa: BLE001 - an unexpected crash must be an INFRA error (>=2), not drift (1)
        import traceback

        traceback.print_exc()
        print("error: drift check crashed (infrastructure error, NOT body drift)", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(cli(sys.argv))
