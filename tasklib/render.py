"""Ticket ↔ structured markdown body. The single source of truth for the §5 template.

The body is **derived** from a ``Ticket``'s fields, never hand-written, so a ticket and its
PR speak one shape (the same section set as agent-tools' ``pull_request_template.md``).
``render()`` serializes; ``parse()`` reads a body back into a ``Ticket`` (round-trip);
``validate_format()`` is what the formatting gate calls — it checks an arbitrary body
conforms to the fixed section order and headings.

Pure string work — no I/O, no provider knowledge. Section order is FIXED and is the contract.
"""

from __future__ import annotations

import re
from dataclasses import replace

from .model import Criterion, Screenshot, Ticket

# Fixed section order — the contract. Changing this is a breaking change to the template.
SECTIONS: tuple[str, ...] = (
    "What",
    "Why (motivation)",
    "User impact",
    "Cost of inaction",
    "Acceptance criteria",
    "Screenshots",
    "Links",
)

_SKIP_MARKER = "Skipped gates"  # an optional trailing section recording escape hatches
# An optional trailing section carrying the daemon-watched due date. Like Skipped gates it is
# NOT in SECTIONS — keeping it out of the fixed contract means every pre-existing ticket (no
# due date) still passes validate_format, and a ticket with a due date stays format-valid.
_DUE_MARKER = "Due"


def render(ticket: Ticket) -> str:
    """Serialize a ``Ticket`` to the canonical markdown body."""
    out: list[str] = []
    out.append(f"## What\n{ticket.what.strip()}")
    out.append(f"## Why (motivation)\n{ticket.why.strip()}")
    out.append(f"## User impact\n{ticket.user_impact.strip()}")
    out.append(f"## Cost of inaction\n{ticket.cost_of_inaction.strip()}")

    acc = "\n".join(_render_criterion(c) for c in ticket.acceptance) if ticket.acceptance else "- [ ] (none specified)"
    out.append(f"## Acceptance criteria\n{acc}")

    if ticket.screenshots:
        shots = "\n".join(_render_shot(s) for s in ticket.screenshots)
    else:
        shots = "(none)"
    out.append(f"## Screenshots\n{shots}")

    links = _render_links(ticket)
    out.append(f"## Links\n{links}")

    if ticket.due.strip():
        out.append(f"## {_DUE_MARKER}\n{ticket.due.strip()}")

    if ticket.skips:
        lines = "\n".join(f"- {gate}: {reason}" for gate, reason in sorted(ticket.skips.items()))
        out.append(f"## {_SKIP_MARKER}\n{lines}")

    return "\n\n".join(out) + "\n"


def _render_criterion(c: Criterion) -> str:
    """One acceptance-criterion checkbox line. A checked criterion carries its visual proof
    (``— proof: ![proof](ref)``) or, when forced, the recorded reason (``— forced: <reason>``)."""
    box = "x" if c.checked else " "
    line = f"- [{box}] {c.text}"
    if c.checked:
        if c.proof:
            line += f" — proof: ![proof]({c.proof})"
        elif c.force_reason:
            line += f" — forced: {c.force_reason}"
    return line


def _render_shot(shot: Screenshot) -> str:
    caption = f" — {shot.caption}" if shot.caption else ""
    # markdown image when it looks like a URL/path, plain ref otherwise
    return f"- {shot.kind}: ![{shot.kind}]({shot.ref}){caption}"


def _render_links(ticket: Ticket) -> str:
    if not ticket.links:
        return "- (none)"
    return "\n".join(f"- {key}: {val}" for key, val in ticket.links.items())


# ── parsing (round-trip) ──────────────────────────────────────────────────────────

_HEADING_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


def split_sections(body: str) -> dict[str, str]:
    """Split a markdown body into ``{heading: content}``. Tolerant of extra whitespace.

    Headings are matched on ``## <name>`` lines; content is everything until the next
    heading. Unknown headings are kept (so ``Skipped gates`` and stray sections survive a
    round-trip); the caller decides what is required.
    """
    sections: dict[str, str] = {}
    matches = list(_HEADING_RE.finditer(body))
    for i, m in enumerate(matches):
        name = m.group(1).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        sections[name] = body[start:end].strip()
    return sections


def parse(body: str, ticket: Ticket | None = None) -> Ticket:
    """Parse a canonical body back into a ``Ticket`` (round-trip with :func:`render`).

    If ``ticket`` is given, its non-body fields (id, url, state, title, labels) are kept and
    only the body-derived fields are overwritten. ``raw_body`` always records the input.
    """
    base = ticket or Ticket()
    sec = split_sections(body)

    acceptance = _parse_criteria(sec.get("Acceptance criteria", ""))
    screenshots = _parse_screenshots(sec.get("Screenshots", ""))
    links = _parse_links(sec.get("Links", ""))
    skips = _parse_skips(sec.get(_SKIP_MARKER, ""))
    # The Due section is a single ISO date on its own line; fall back to the base ticket's due
    # so a backend that supplied the date natively (Linear dueDate) isn't clobbered by a body
    # that happens to omit the section.
    due = sec.get(_DUE_MARKER, "").strip() or base.due

    return replace(
        base,
        what=sec.get("What", "").strip(),
        why=sec.get("Why (motivation)", "").strip(),
        user_impact=sec.get("User impact", "").strip(),
        cost_of_inaction=sec.get("Cost of inaction", "").strip(),
        acceptance=acceptance,
        screenshots=screenshots,
        links=links or base.links,
        skips=skips or base.skips,
        due=due,
        raw_body=body,
    )


_CRITERION_RE = re.compile(r"^\s*-\s*\[([ xX])\]\s*(.+?)\s*$")


def _parse_criteria(text: str) -> list[Criterion]:
    """Parse the Acceptance-criteria checkbox lines back into Criterion objects (round-trip).

    Reads the checkbox state (``[x]``/``[ ]``) and pulls the trailing proof/forced marker off
    the criterion text so a checked-with-proof criterion survives a render→parse round-trip.
    """
    items: list[Criterion] = []
    for line in text.splitlines():
        m = _CRITERION_RE.match(line)
        if not m:
            continue
        checked = m.group(1).lower() == "x"
        crit_text, proof, force_reason = _split_criterion_rest(m.group(2).strip())
        if crit_text and crit_text.lower() != "(none specified)":
            items.append(Criterion(text=crit_text, checked=checked, proof=proof, force_reason=force_reason))
    return items


def _split_criterion_rest(rest: str) -> tuple[str, str, str]:
    """Split a criterion's text from its trailing ``— proof: …`` / ``— forced: …`` marker.

    Returns ``(text, proof_ref, force_reason)``. Anchored on the specific ``— proof:`` /
    ``— forced:`` markers (unlikely in a normal criterion), so ordinary em-dashes in the text
    are left alone. The proof capture is GREEDY to the trailing ``)`` so a path/URL that itself
    contains parens (e.g. ``img(1).png``) round-trips intact instead of truncating at the first
    ``)`` and falling through to the bare branch (which would re-capture the whole ``![…](…)``
    wrapper and progressively double-wrap on the next render).
    """
    m = re.search(r"\s+—\s+proof:\s*!\[[^\]]*\]\((.+)\)\s*$", rest)
    if m:
        return rest[: m.start()].strip(), m.group(1).strip(), ""
    m = re.search(r"\s+—\s+proof:\s*(\S.*?)\s*$", rest)
    if m:
        return rest[: m.start()].strip(), m.group(1).strip(), ""
    m = re.search(r"\s+—\s+forced:\s*(\S.*?)\s*$", rest)
    if m:
        return rest[: m.start()].strip(), "", m.group(1).strip()
    return rest, "", ""


def _parse_screenshots(text: str) -> list[Screenshot]:
    shots: list[Screenshot] = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("-"):
            continue
        # "- <kind>: ![alt](ref) — caption"
        m = re.match(r"^-\s*(\w+)\s*:\s*!\[[^\]]*\]\(([^)]+)\)(?:\s*—\s*(.*))?$", line)
        if m:
            shots.append(Screenshot(ref=m.group(2).strip(), kind=m.group(1).strip(), caption=(m.group(3) or "").strip()))
            continue
        # bare "- <kind>: <ref>"
        m = re.match(r"^-\s*(\w+)\s*:\s*(\S+)", line)
        if m and m.group(2).lower() != "(none)":
            shots.append(Screenshot(ref=m.group(2).strip(), kind=m.group(1).strip()))
    return shots


def _parse_links(text: str) -> dict[str, str]:
    links: dict[str, str] = {}
    for line in text.splitlines():
        m = re.match(r"^\s*-\s*([^:]+):\s*(.+?)\s*$", line)
        if m:
            key, val = m.group(1).strip(), m.group(2).strip()
            if val and val != "(none)":
                links[key] = val
    return links


def _parse_skips(text: str) -> dict[str, str]:
    skips: dict[str, str] = {}
    for line in text.splitlines():
        m = re.match(r"^\s*-\s*([^:]+):\s*(.+?)\s*$", line)
        if m:
            skips[m.group(1).strip()] = m.group(2).strip()
    return skips


# ── formatting gate ────────────────────────────────────────────────────────────────


def validate_format(body: str) -> list[str]:
    """Return a list of formatting violations (empty = the body matches the template).

    Required: every section in :data:`SECTIONS` is present, in that order. Extra trailing
    sections (e.g. ``Skipped gates``) are allowed. This is exactly what the ``formatting``
    gate enforces.
    """
    problems: list[str] = []
    headings = [m.group(1).strip() for m in _HEADING_RE.finditer(body)]
    present = set(headings)

    for required in SECTIONS:
        if required not in present:
            problems.append(f"missing required section: ## {required}")

    if problems:
        return problems  # order check is meaningless if sections are missing

    # order: the required sections must appear in the canonical order (ignoring extras)
    canonical_positions = [headings.index(s) for s in SECTIONS]
    if canonical_positions != sorted(canonical_positions):
        problems.append("sections are out of order; expected: " + " → ".join(SECTIONS))

    return problems
