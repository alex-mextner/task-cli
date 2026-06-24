"""render.py — template render/parse round-trip + the formatting gate."""

from __future__ import annotations

from tasklib.model import Screenshot, Ticket
from tasklib.render import SECTIONS, parse, render, split_sections, validate_format


def _full_ticket() -> Ticket:
    return Ticket(
        title="Add dark mode toggle",
        what="A toggle in the header that switches the theme.",
        why="Users asked for it repeatedly.",
        user_impact="Everyone on the web app.",
        cost_of_inaction="Churn from users who hate the bright theme.",
        acceptance=["toggle persists across reloads", "respects prefers-color-scheme"],
        screenshots=[Screenshot(ref="before.png", kind="creation", caption="current header")],
        labels=["ui"],
        links={"PR": "https://github.com/x/y/pull/1", "Session": "session:abc"},
    )


def test_render_has_all_sections_in_order():
    body = render(_full_ticket())
    positions = [body.index(f"## {s}") for s in SECTIONS]
    assert positions == sorted(positions)


def test_render_acceptance_as_checkboxes():
    body = render(_full_ticket())
    assert "- [ ] toggle persists across reloads" in body
    assert "- [ ] respects prefers-color-scheme" in body


def test_round_trip_preserves_fields():
    original = _full_ticket()
    body = render(original)
    parsed = parse(body)
    assert parsed.what == original.what
    assert parsed.why == original.why
    assert parsed.user_impact == original.user_impact
    assert parsed.cost_of_inaction == original.cost_of_inaction
    assert parsed.acceptance == original.acceptance
    assert parsed.links["PR"] == original.links["PR"]
    assert [s.ref for s in parsed.screenshots] == ["before.png"]


def test_parse_keeps_non_body_fields_from_base():
    base = Ticket(id="#42", title="kept", labels=["ui"])
    parsed = parse(render(_full_ticket()), base)
    assert parsed.id == "#42"
    assert parsed.title == "kept"
    assert parsed.labels == ["ui"]


def test_validate_format_passes_for_rendered_body():
    assert validate_format(render(_full_ticket())) == []


def test_validate_format_flags_missing_section():
    body = render(_full_ticket()).replace("## Cost of inaction", "## Something else")
    problems = validate_format(body)
    assert any("Cost of inaction" in p for p in problems)


def test_validate_format_flags_out_of_order():
    body = (
        "## Why (motivation)\nx\n\n## What\ny\n\n## User impact\nz\n\n## Cost of inaction\nc\n\n"
        "## Acceptance criteria\n- [ ] a\n\n## Screenshots\n(none)\n\n## Links\n- (none)\n"
    )
    problems = validate_format(body)
    assert any("out of order" in p for p in problems)


def test_skipped_gates_section_survives_round_trip():
    t = _full_ticket()
    t.skips = {"screenshots": "no UI proof yet"}
    body = render(t)
    assert "## Skipped gates" in body
    assert parse(body).skips["screenshots"] == "no UI proof yet"


def test_split_sections_handles_extra_whitespace():
    body = "##   What  \n  hello  \n\n## Links\n- (none)\n"
    sec = split_sections(body)
    assert sec["What"] == "hello"


# ── due date (the daemon-watched optional section) ──────────────────────────────────


def test_due_section_renders_and_round_trips():
    t = _full_ticket()
    t.due = "2026-07-01"
    body = render(t)
    assert "## Due\n2026-07-01" in body
    assert parse(body).due == "2026-07-01"


def test_due_section_is_omitted_when_unset():
    body = render(_full_ticket())  # no due
    assert "## Due" not in body
    assert parse(body).due == ""


def test_due_does_not_break_the_formatting_gate():
    # the Due section is OPTIONAL (not in SECTIONS) — a body with it (or without) stays valid
    t = _full_ticket()
    t.due = "2026-07-01"
    assert validate_format(render(t)) == []
    assert validate_format(render(_full_ticket())) == []


def test_parse_falls_back_to_base_due_when_body_omits_it():
    # a backend that supplied the date natively (base.due set) isn't clobbered by a due-less body
    base = Ticket(id="#1", due="2026-08-15")
    parsed = parse(render(_full_ticket()), base)  # rendered body has no Due section
    assert parsed.due == "2026-08-15"
