"""policy.py — the enforcement gates pass/fail + the escape hatch."""

from __future__ import annotations

from tasklib.model import Criterion, Screenshot, Ticket
from tasklib.msgrefs import render_msgref_quotes
from tasklib.policy import (
    EnforceConfig,
    Phase,
    check,
    check_create,
    check_done,
    normalize_skip_gate,
)


def _good_ticket(**over) -> Ticket:
    base = dict(
        title="t",
        what="the change",
        why="because",
        user_impact="Users can read the page without the menu covering the text, so they stop getting lost",
        cost_of_inaction="bad things",
        acceptance=["it works", "it also handles the empty case"],
    )
    base.update(over)
    return Ticket(**base)


def _checked_criteria() -> list[Criterion]:
    """Two acceptance criteria already checked with a visual proof — close-ready (rule 2)."""
    return [
        Criterion("it works", checked=True, proof="proof.png"),
        Criterion("it also handles the empty case", checked=True, proof="proof.png"),
    ]


def test_complete_ticket_passes_create():
    assert check_create(_good_ticket(), EnforceConfig()).ok


def test_missing_acceptance_fails():
    res = check_create(_good_ticket(acceptance=[]), EnforceConfig())
    assert not res.ok
    assert any(v.gate == "acceptance-criteria" for v in res.violations)


def test_missing_motivation_fails():
    res = check_create(_good_ticket(why=""), EnforceConfig())
    assert any(v.gate == "motivation" for v in res.violations)


def test_missing_user_impact_and_cost_fail():
    res = check_create(_good_ticket(user_impact="", cost_of_inaction=""), EnforceConfig())
    gates = {v.gate for v in res.violations}
    assert "user-impact" in gates and "cost-of-inaction" in gates


def test_ui_ticket_requires_creation_screenshot():
    t = _good_ticket(labels=["ui"])  # no screenshot
    res = check_create(t, EnforceConfig())
    assert any(v.gate == "screenshots" for v in res.violations)


def test_ui_ticket_with_screenshot_passes_create():
    t = _good_ticket(labels=["ui"], screenshots=[Screenshot(ref="a.png", kind="creation")])
    assert check_create(t, EnforceConfig()).ok


def test_non_ui_ticket_skips_screenshot_gate():
    t = _good_ticket(labels=["backend"])  # not ui/visual
    assert check_create(t, EnforceConfig()).ok


def test_done_phase_demands_implementation_screenshot():
    # a creation shot alone does NOT satisfy the on-done gate — that is why it runs again.
    creation_only = _good_ticket(labels=["visual"], screenshots=[Screenshot(ref="a.png", kind="creation")])
    assert any(v.gate == "screenshots" for v in check_done(creation_only, EnforceConfig()).violations)
    # no screenshots at all is also refused
    bare = _good_ticket(labels=["visual"])
    assert any(v.gate == "screenshots" for v in check_done(bare, EnforceConfig()).violations)
    # an implementation shot satisfies the done gate (with all criteria checked — rule 2)
    impl = _good_ticket(
        labels=["visual"],
        screenshots=[Screenshot(ref="b.png", kind="implementation")],
        acceptance=_checked_criteria(),
    )
    assert check_done(impl, EnforceConfig()).ok


def test_escape_hatch_bypasses_a_gate():
    t = _good_ticket(acceptance=[], skips={"acceptance-criteria": "spike, no criteria yet"})
    res = check_create(t, EnforceConfig())
    assert res.ok
    assert "acceptance-criteria" in res.skipped


def test_disabled_gate_in_config_is_not_checked():
    cfg = EnforceConfig(acceptance_criteria=False)
    assert check_create(_good_ticket(acceptance=[]), cfg).ok


def test_formatting_gate_catches_broken_body():
    # an otherwise-complete ticket always renders a valid body, so the format gate passes
    assert not any(v.gate == "formatting" for v in check_create(_good_ticket(), EnforceConfig()).violations)


def test_enforce_config_from_spec_dict():
    cfg = EnforceConfig.from_dict(
        {
            "acceptance_criteria": "required",
            "motivation": "optional",
            "formatting": "strict",
            "screenshots": {
                "on_create": {"required_if_label": ["ui", "visual"]},
                "on_done": {"required_if_label": ["ui"]},
            },
        }
    )
    assert cfg.acceptance_criteria is True
    assert cfg.motivation is False
    assert cfg.formatting is True
    assert "ui" in cfg.screenshot_labels and "visual" in cfg.screenshot_labels


def test_from_dict_disables_new_gates():
    cfg = EnforceConfig.from_dict(
        {"links": False, "user_impact_quality": False, "acceptance_checked": False, "msgref_title": False}
    )
    assert cfg.links is False
    assert cfg.user_impact_quality is False
    assert cfg.acceptance_checked is False
    assert cfg.msgref_title is False
    # with the new gates off, a bare-reference / thin-impact / unchecked / tg#-titled ticket passes
    assert check_create(_good_ticket(what="see HYP-789", user_impact="users", title="tg#5900"), cfg).ok
    assert check_done(_good_ticket(), cfg).ok  # unchecked criteria no longer block close


def test_from_dict_disables_links_url_and_msgref_quote_gates():
    # the documented enforce.links_url / enforce.msgref_quote keys must reach the gates through
    # from_dict, so config and CLI behavior can't drift.
    cfg = EnforceConfig.from_dict({"links_url": False, "msgref_quote": False})
    assert cfg.links_url is False and cfg.msgref_quote is False
    assert check_create(_good_ticket(links={"Session": "session:2"}), cfg).ok
    assert check(_good_ticket(what="per tg#42 do X"), cfg, Phase.CREATE, resolvable_ids={42}).ok


def test_from_dict_acceptance_min_override_and_fallback():
    assert EnforceConfig.from_dict({"acceptance_min": 3}).acceptance_min == 3
    # a non-integer value falls back to the default rather than raising
    assert EnforceConfig.from_dict({"acceptance_min": "lots"}).acceptance_min == 2
    # the override actually gates: 2 criteria fail a min of 3
    cfg = EnforceConfig.from_dict({"acceptance_min": 3})
    assert not check_create(_good_ticket(acceptance=["a", "b"]), cfg).ok


def test_normalize_skip_gate_aliases():
    assert normalize_skip_gate("acceptance") == "acceptance-criteria"
    assert normalize_skip_gate("why") == "motivation"
    assert normalize_skip_gate("impact") == "user-impact"
    assert normalize_skip_gate("if-not-done") == "cost-of-inaction"


def test_normalize_skip_gate_rejects_unknown():
    import pytest

    with pytest.raises(ValueError):
        normalize_skip_gate("nonsense")


def test_check_dispatch_create_vs_done():
    assert check(_good_ticket(), EnforceConfig(), Phase.CREATE).ok
    # the DONE phase additionally demands every criterion checked (rule 2)
    assert check(_good_ticket(acceptance=_checked_criteria()), EnforceConfig(), Phase.DONE).ok


# ── rule 4: at least two acceptance criteria ─────────────────────────────────────────


def test_single_acceptance_criterion_fails_min():
    res = check_create(_good_ticket(acceptance=["only one"]), EnforceConfig())
    assert not res.ok
    v = next(v for v in res.violations if v.gate == "acceptance-criteria")
    assert "at least 2" in v.message


def test_two_acceptance_criteria_pass_min():
    assert check_create(_good_ticket(acceptance=["a", "b"]), EnforceConfig()).ok


# ── rule 1: related entities must be links ───────────────────────────────────────────


def test_unlinked_reference_blocks_create():
    res = check_create(_good_ticket(what="blocked by HYP-789 until it lands"), EnforceConfig())
    assert not res.ok
    v = next(v for v in res.violations if v.gate == "links")
    assert "HYP-789" in v.message


def test_links_gate_skippable_for_false_positive():
    t = _good_ticket(what="mentions HYP-789", skips={"links": "erroneous match — that's a SKU"})
    res = check_create(t, EnforceConfig())
    assert res.ok
    assert "links" in res.skipped


def test_linked_reference_passes():
    assert check_create(_good_ticket(what="blocked by [HYP-789](https://linear/HYP-789)"), EnforceConfig()).ok


def test_clean_linked_ticket_closes_after_gate_expansion():
    # the close gates now include links + impact-quality; a close-ready ticket with a PROPERLY
    # linked reference and a good impact must still close cleanly (no over-blocking of valid closes).
    t = _good_ticket(
        what="follows up [HYP-789](https://linear/HYP-789)",
        acceptance=_checked_criteria(),
    )
    assert check_done(t, EnforceConfig()).ok


def test_unlinked_reference_blocks_close_too():
    # rule 1 also gates the close: a close-ready ticket carrying a bare reference (e.g. one made
    # before this version or edited in the web UI) cannot be closed without linking it / skipping.
    t = _good_ticket(what="blocked by HYP-789", acceptance=_checked_criteria())
    res = check_done(t, EnforceConfig())
    assert any(v.gate == "links" for v in res.violations)
    # the close command's --skip-links waives a genuine legacy exception
    t.skips = {"links": "legacy ticket, reference predates the rule"}
    assert check_done(t, EnforceConfig()).ok


def test_links_gate_disabled_in_config():
    assert check_create(_good_ticket(what="see HYP-789"), EnforceConfig(links=False)).ok


def test_msgref_in_title_blocks_create():
    res = check_create(_good_ticket(title="see tg#5900"), EnforceConfig())
    assert not res.ok
    v = next(v for v in res.violations if v.gate == "msgref-title")
    assert "tg#5900" in v.message


def test_msgref_in_body_does_not_trip_the_title_gate():
    # tg#<id> is a DIFFERENT namespace from the links gate's bare #N — it belongs in prose, not
    # the title, and must not itself be treated as an unlinked "issue/PR ref" either.
    res = check_create(_good_ticket(what="per tg#5900 do X"), EnforceConfig())
    assert res.ok


def test_quoted_blockquote_line_is_excluded_from_the_links_scan():
    # a tasklib.msgrefs.render_msgref_quotes quote block is `> …` lines (+ provenance marker)
    # appended to a prose field. Whatever that quoted (someone-else's) message happens to mention
    # must never trip the links gate — the ticket's own author can't be expected to fix quoted
    # third-party text. This is the fix for the review finding that "expand only after THIS
    # command's own gates" (an earlier version) protects only the expanding command: a LATER
    # command re-scans the already-stored, already-expanded field. Stripping the blockquote at
    # the scan site (here) instead of at expansion time is what makes the protection survive
    # across commands.
    quoted_what = (
        "per tg#42 do X\n\n> **tg#42** — Alex, 2026-01-01 00:00 UTC:\n> blocked by HYP-999"
        "\n<!-- tasklib:msgref-quote:42 -->"
    )
    assert check_create(_good_ticket(what=quoted_what), EnforceConfig()).ok


def test_a_hand_typed_lookalike_without_the_provenance_marker_still_trips_the_links_gate():
    # task-cli#45 review finding: without a provenance marker, ANY text shaped like
    # `> **tg#<id>** ...` was excluded from the scan — an author could hand-type that exact
    # shape to smuggle a bare reference past the links gate with no audited --skip-links. The
    # SAME text WITHOUT the genuine marker must still be scanned normally.
    spoofed_what = "per tg#42 do X\n\n> **tg#42** — Alex, 2026-01-01 00:00 UTC:\n> blocked by HYP-999"
    res = check_create(_good_ticket(what=spoofed_what), EnforceConfig())
    assert not res.ok
    assert any(v.gate == "links" for v in res.violations)


def test_msgref_title_gate_is_not_skippable():
    # deliberately hard (task 6109): a recorded skip does NOT waive it — the fix is always
    # "move it into the body", never a judgment call.
    t = _good_ticket(title="see tg#5900", skips={"msgref-title": "trust me"})
    assert not check_create(t, EnforceConfig()).ok


def test_msgref_title_gate_disabled_in_config():
    assert check_create(_good_ticket(title="see tg#5900"), EnforceConfig(msgref_title=False)).ok


def test_msgref_title_gate_also_blocks_close():
    # mirrors the links gate: a ticket whose title carries a bare tg# ref (created before this
    # rule, or edited in the web UI) cannot be closed with it still there either.
    t = _good_ticket(title="see tg#5900", acceptance=_checked_criteria())
    res = check_done(t, EnforceConfig())
    assert any(v.gate == "msgref-title" for v in res.violations)


def test_links_and_quality_gates_disabled_in_config_on_close():
    # disabling a gate in config must lift it on BOTH edges, not only create — a closing ticket
    # with a bare ref / thin impact passes when the respective gate is off.
    assert check_done(
        _good_ticket(what="see HYP-789", acceptance=_checked_criteria()),
        EnforceConfig(links=False),
    ).ok
    assert check_done(
        _good_ticket(user_impact="users", acceptance=_checked_criteria()),
        EnforceConfig(user_impact_quality=False),
    ).ok


# ── rule 5: plain-language user impact ───────────────────────────────────────────────


def test_thin_user_impact_fails_quality():
    res = check_create(_good_ticket(user_impact="users"), EnforceConfig())
    gates = {v.gate for v in res.violations}
    # non-empty, so the emptiness gate passes; the QUALITY gate is what fails
    assert "user-impact-quality" in gates and "user-impact" not in gates


def test_user_impact_quality_skippable():
    t = _good_ticket(user_impact="n/a", skips={"user-impact-quality": "internal tool, no end user"})
    res = check_create(t, EnforceConfig())
    assert res.ok
    assert "user-impact-quality" in res.skipped


def test_empty_impact_is_the_emptiness_gate_not_quality():
    res = check_create(_good_ticket(user_impact=""), EnforceConfig())
    gates = {v.gate for v in res.violations}
    assert "user-impact" in gates and "user-impact-quality" not in gates


def test_thin_impact_blocks_close_too():
    # rule 5 also gates the close: a close-ready ticket whose impact was never plain-language
    # (thinned, or authored in the web UI) cannot be closed without rewriting it / skipping.
    t = _good_ticket(user_impact="users", acceptance=_checked_criteria())
    res = check_done(t, EnforceConfig())
    assert any(v.gate == "user-impact-quality" for v in res.violations)
    # the close command's --skip-user-impact-quality waives a genuine legacy exception
    t.skips = {"user-impact-quality": "legacy ticket, impact predates the rule"}
    assert check_done(t, EnforceConfig()).ok


def test_empty_impact_with_emptiness_gate_off_is_not_blocked_as_low_quality():
    # disabling the emptiness gate (user_impact=False) with an EMPTY impact must not then be
    # re-blocked by the quality gate — an empty impact is empty, not thin/jargon-only.
    cfg = EnforceConfig(user_impact=False, user_impact_quality=True)
    res = check_create(_good_ticket(user_impact=""), cfg)
    assert not any(v.gate in {"user-impact", "user-impact-quality"} for v in res.violations)


# ── rule 2: a ticket cannot close with an unchecked criterion ─────────────────────────


def test_unchecked_criteria_block_close():
    # _good_ticket's criteria are unchecked → close refused, listing them
    res = check_done(_good_ticket(), EnforceConfig())
    v = next(v for v in res.violations if v.gate == "acceptance-checked")
    assert "unchecked" in v.message


def test_all_checked_criteria_allow_close():
    assert check_done(_good_ticket(acceptance=_checked_criteria()), EnforceConfig()).ok


def test_unchecked_gate_is_not_skippable():
    # the acceptance-checked gate is deliberately hard: a recorded skip does NOT waive it
    t = _good_ticket(skips={"acceptance-checked": "trust me"})
    assert not check_done(t, EnforceConfig()).ok


def test_unchecked_gate_create_phase_does_not_fire():
    # rule 2 is on-DONE only: creating with unchecked criteria is fine
    assert check_create(_good_ticket(), EnforceConfig()).ok


def test_checked_without_proof_blocks_close():
    # a box ticked outside `task check` (e.g. the web UI) has no proof → close still refused (rule 3)
    t = _good_ticket(acceptance=[Criterion("a", checked=True, proof="p.png"), Criterion("b", checked=True)])
    res = check_done(t, EnforceConfig())
    v = next(v for v in res.violations if v.gate == "acceptance-checked")
    assert "without a visual proof" in v.message


def test_checked_with_force_reason_allows_close():
    # a criterion checked via --force (proof impossible) carries a recorded reason → closeable
    t = _good_ticket(
        acceptance=[
            Criterion("a", checked=True, proof="p.png"),
            Criterion("b", checked=True, force_reason="backend invariant, no UI"),
        ]
    )
    assert check_done(t, EnforceConfig()).ok


# ── the Links section must contain only URLs (tg#9179) ────────────────────────────────


def test_links_url_rejects_non_url_value():
    # the session:2 leak: a bare, non-URL value rendered as `- Session: session:2` and slid past
    # the (prose-only) links gate. The dedicated links-url gate catches it on the field itself.
    res = check_create(_good_ticket(links={"Session": "session:2"}), EnforceConfig())
    assert not res.ok
    v = next(v for v in res.violations if v.gate == "links-url")
    assert "session:2" in v.message


def test_links_url_accepts_real_https_url():
    t = _good_ticket(links={"PR": "https://github.com/x/y/pull/1"})
    assert check_create(t, EnforceConfig()).ok


def test_links_url_rejects_scheme_that_is_not_http():
    # a Linear key, a mailto:, a bare token — none are http(s) URLs.
    for bad in ("HYP-1001", "mailto:a@b.c", "ftp://host/x", "just words"):
        res = check_create(_good_ticket(links={"Ref": bad}), EnforceConfig())
        assert not res.ok, bad
        assert any(v.gate == "links-url" for v in res.violations), bad


def test_links_url_gate_skippable():
    t = _good_ticket(links={"Session": "session:2"}, skips={"links-url": "legacy junk, will fix"})
    res = check_create(t, EnforceConfig())
    assert res.ok
    assert "links-url" in res.skipped


def test_links_url_gate_disabled_in_config():
    assert check_create(_good_ticket(links={"Session": "session:2"}), EnforceConfig(links_url=False)).ok


def test_links_url_blocks_close_too():
    # a ticket read back from the backend with junk links can't be closed with it still there.
    t = _good_ticket(links={"Session": "session:2"}, acceptance=_checked_criteria())
    res = check_done(t, EnforceConfig())
    assert any(v.gate == "links-url" for v in res.violations)
    t.skips = {"links-url": "legacy value predates the rule"}
    assert check_done(t, EnforceConfig()).ok


# ── a body tg#<id> reference must carry its quote (tg#9161) ────────────────────────────


def _quoted_what(msg_id: int = 42, text: str = "some quoted message") -> str:
    """A prose field with tg#<id> ALREADY expanded into a genuine quote block (marker included),
    produced by the real expander so the block shape can never drift from production."""
    from tasklib.msgrefs import HistoryRecord

    rec = HistoryRecord(ts=1_700_000_000, message_id=msg_id, direction="user", from_="Alex", text=text, pane=None)
    return render_msgref_quotes(f"per tg#{msg_id} do X", history={msg_id: rec})


def test_msgref_quote_denies_resolvable_unquoted_body_ref():
    # a body reference whose message IS in history but whose quote was never attached (a web-UI
    # authored ticket) is refused — the quote can and must be inlined.
    t = _good_ticket(what="per tg#42 do X")
    res = check(t, EnforceConfig(), Phase.CREATE, resolvable_ids={42})
    assert not res.ok
    v = next(v for v in res.violations if v.gate == "msgref-quote")
    assert "tg#42" in v.message


def test_msgref_quote_only_warns_when_unresolvable():
    # a reference to a message older than local retention (not in the resolvable set) cannot be
    # quoted → it warns but never blocks.
    t = _good_ticket(what="per tg#42 do X")
    res = check(t, EnforceConfig(), Phase.CREATE, resolvable_ids=set())
    assert res.ok
    assert not any(v.gate == "msgref-quote" for v in res.violations)
    assert any(w.gate == "msgref-quote" for w in res.warnings)


def test_msgref_quote_none_resolvable_ids_never_denies():
    # the default (no history loaded) can't prove resolvability → every unquoted ref only warns.
    t = _good_ticket(what="per tg#42 do X")
    res = check(t, EnforceConfig(), Phase.CREATE)
    assert res.ok
    assert any(w.gate == "msgref-quote" for w in res.warnings)


def test_msgref_quote_passes_when_already_quoted():
    # once the quote block is present, neither a violation nor a warning fires (idempotent).
    t = _good_ticket(what=_quoted_what(42))
    res = check(t, EnforceConfig(), Phase.CREATE, resolvable_ids={42})
    assert res.ok
    assert not any(v.gate == "msgref-quote" for v in res.violations)
    assert not any(w.gate == "msgref-quote" for w in res.warnings)


def test_msgref_quote_skippable_when_resolvable():
    t = _good_ticket(what="per tg#42 do X", skips={"msgref-quote": "false positive, that's a version tag"})
    res = check(t, EnforceConfig(), Phase.CREATE, resolvable_ids={42})
    assert res.ok
    assert "msgref-quote" in res.skipped


def test_msgref_quote_gate_disabled_in_config():
    t = _good_ticket(what="per tg#42 do X")
    assert check(t, EnforceConfig(msgref_quote=False), Phase.CREATE, resolvable_ids={42}).ok


def test_msgref_quote_ignores_reference_in_acceptance_criterion():
    # acceptance criteria are single-line-serialized (no room for a multi-line quote) — a tg#<id>
    # there is out of the quote gate's scan scope, so it never denies on it.
    t = _good_ticket(what="the change", acceptance=["do tg#42 first", "and the empty case"])
    res = check(t, EnforceConfig(), Phase.CREATE, resolvable_ids={42})
    assert not any(v.gate == "msgref-quote" for v in res.violations)


def test_msgref_quote_blocks_close_too():
    t = _good_ticket(what="per tg#42 do X", acceptance=_checked_criteria())
    res = check(t, EnforceConfig(), Phase.DONE, resolvable_ids={42})
    assert any(v.gate == "msgref-quote" for v in res.violations)


def test_msgref_quote_scans_each_field_independently():
    # a quote for tg#42 in `why` must NOT mask a still-unquoted tg#42 in `what`: the expander
    # attaches a quote into the SAME field the reference sits in, so the fields are scanned
    # separately (a joined scan would wrongly treat `what`'s bare ref as already-quoted).
    t = _good_ticket(what="per tg#42 do X", why=_quoted_what(42))
    res = check(t, EnforceConfig(), Phase.CREATE, resolvable_ids={42})
    assert any(v.gate == "msgref-quote" for v in res.violations)


def test_msgref_quote_skip_also_suppresses_the_unresolvable_warning():
    # a recorded --skip-msgref-quote silences the advisory warning too — otherwise the "no quote
    # could be attached" note reprints on every command with no way to quiet it.
    t = _good_ticket(what="per tg#42 do X", skips={"msgref-quote": "false positive, version tag"})
    res = check(t, EnforceConfig(), Phase.CREATE, resolvable_ids=set())
    assert res.ok
    assert not any(w.gate == "msgref-quote" for w in res.warnings)


def test_msgref_quote_partitions_a_mixed_resolvable_and_unresolvable_ticket():
    # tg#42 resolves (→ deny), tg#99 doesn't (→ warn) — the two land on different channels.
    t = _good_ticket(what="per tg#42 and also tg#99 do X")
    res = check(t, EnforceConfig(), Phase.CREATE, resolvable_ids={42})
    deny = next(v for v in res.violations if v.gate == "msgref-quote")
    assert "tg#42" in deny.message and "tg#99" not in deny.message
    warn = next(w for w in res.warnings if w.gate == "msgref-quote")
    assert "tg#99" in warn.message and "tg#42" not in warn.message


def test_links_url_rejects_malformed_http_shapes():
    # scheme alone isn't enough: internal whitespace, a hostless netloc, userinfo-only authority,
    # an invalid port, and a URL that makes urlparse itself raise are all rejected.
    bad_values = (
        "https://Session: session:2",  # internal whitespace
        "https://",  # empty netloc
        "https://.",  # no alphanumeric host char
        "http:// space.com",  # whitespace
        "https://user@",  # userinfo only, empty host
        "https://user@:443",  # userinfo + port, empty host
        "https://example.com:bad",  # invalid port → urlparse .port raises ValueError
        "https://[::1",  # malformed IPv6 → urlparse raises ValueError
        "https://example.com/\x1bevil",  # ESC control char
        "https://exa\x00mple.com",  # NUL
        "https://exam‮ple.com",  # bidi override
    )
    for bad in bad_values:
        res = check_create(_good_ticket(links={"Ref": bad}), EnforceConfig())
        assert not res.ok, bad
        assert any(v.gate == "links-url" for v in res.violations), bad


def test_links_url_rejects_embedded_credentials():
    # a URL with a real, otherwise-valid host is still rejected when it carries userinfo — this
    # gate persists the value verbatim into a GitHub/Linear issue body, and an accepted
    # credential-shaped userinfo segment would leak it into a shared, often-public tracker
    # (review finding).
    bad_values = (
        "https://user:ghp_deadbeef@example.com/",  # username + token-shaped password
        "https://ghp_deadbeef@example.com/",  # bare token as username, no password
    )
    for bad in bad_values:
        res = check_create(_good_ticket(links={"Ref": bad}), EnforceConfig())
        assert not res.ok, bad
        assert any(v.gate == "links-url" for v in res.violations), bad


def test_links_url_message_strips_control_chars_from_key_and_value():
    # an untrusted key/value (from a backend body round-trip) must not inject a terminal escape
    # into the diagnostic printed to the console / a TG notification.
    t = _good_ticket(links={"\x1b[31mRef": "session:2\x07"})
    res = check_create(t, EnforceConfig())
    v = next(v for v in res.violations if v.gate == "links-url")
    assert "\x1b" not in v.message and "\x07" not in v.message
    assert "Ref: session:2" in v.message  # printable content preserved


def test_links_url_rejects_a_control_char_key_even_with_a_valid_url():
    # a link KEY is rendered verbatim by task read, so a control/bidi char in the key must be
    # rejected too — not only the value (which here is a perfectly valid URL).
    t = _good_ticket(links={"\x1b[31mPR": "https://github.com/x/y/pull/1"})
    res = check_create(t, EnforceConfig())
    assert not res.ok
    v = next(v for v in res.violations if v.gate == "links-url")
    assert "\x1b" not in v.message


def test_links_url_redacts_a_token_shaped_rejected_value_in_the_message():
    # a rejected value that embeds a token must be scrubbed by the shared redactor, not echoed.
    secret = "ghp_" + "A" * 40  # token-shaped, and not a URL → rejected
    t = _good_ticket(links={"Ref": secret})
    res = check_create(t, EnforceConfig())
    v = next(v for v in res.violations if v.gate == "links-url")
    assert secret not in v.message  # full token never echoed
    assert "<redacted>" in v.message  # scrubbed via tasklib.logging.redact


def test_links_url_redacts_a_token_shaped_key_too():
    secret = "lin_api_" + "B" * 40
    res = check_create(_good_ticket(links={secret: "not-a-url"}), EnforceConfig())
    v = next(v for v in res.violations if v.gate == "links-url")
    assert secret not in v.message


def test_links_url_rejects_a_key_with_surrounding_whitespace():
    # render writes the key verbatim but parse strips it, so a key with surrounding whitespace
    # doesn't round-trip unchanged → reject it.
    res = check_create(_good_ticket(links={" PR ": "https://github.com/x/y/pull/1"}), EnforceConfig())
    assert not res.ok
    assert any(v.gate == "links-url" for v in res.violations)


def test_links_url_rejects_a_key_with_an_embedded_colon():
    # render writes `- key: value` and parse splits on the FIRST colon, so a key containing ':'
    # would reparse as a different (non-URL) value after a backend round-trip — reject it up front.
    t = _good_ticket(links={"Docs: API": "https://example.com/docs"})
    res = check_create(t, EnforceConfig())
    assert not res.ok
    assert any(v.gate == "links-url" for v in res.violations)


def test_links_url_lists_every_offending_pair():
    t = _good_ticket(links={"A": "session:2", "B": "https://ok.example/x", "C": "HYP-9"})
    res = check_create(t, EnforceConfig())
    v = next(v for v in res.violations if v.gate == "links-url")
    assert "A: session:2" in v.message and "C: HYP-9" in v.message
    assert "B:" not in v.message  # the valid URL is not listed


def test_links_url_accepts_urls_with_ports_query_and_paths():
    for good in ("http://localhost:3000", "https://example.com/a/b?x=1&y=2"):
        assert check_create(_good_ticket(links={"Ref": good}), EnforceConfig()).ok, good


def test_check_wrappers_forward_resolvable_ids():
    # check_create / check_done are documented entry points — they must be able to DENY, not only
    # warn, so the optional resolvable_ids has to reach the gate through them.
    t = _good_ticket(what="per tg#42 do X")
    assert any(v.gate == "msgref-quote" for v in check_create(t, EnforceConfig(), resolvable_ids={42}).violations)
    tc = _good_ticket(what="per tg#42 do X", acceptance=_checked_criteria())
    assert any(v.gate == "msgref-quote" for v in check_done(tc, EnforceConfig(), resolvable_ids={42}).violations)


def test_normalize_skip_gate_accepts_new_gates():
    assert normalize_skip_gate("links-url") == "links-url"
    assert normalize_skip_gate("link-url") == "links-url"
    assert normalize_skip_gate("msgref-quote") == "msgref-quote"
    assert normalize_skip_gate("msgref_quote") == "msgref-quote"
