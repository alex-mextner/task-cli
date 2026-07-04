"""msgrefs.py — tg#<id> Telegram-message-reference detection + local-history quoting (task 6109)."""

from __future__ import annotations

import json

from tasklib.msgrefs import (
    HistoryRecord,
    detect_msgrefs,
    load_history,
    render_msgref_quotes,
    title_msgrefs,
)


def test_detect_bare_msgref():
    assert detect_msgrefs("see tg#5900 for context") == [5900]


def test_detect_is_case_insensitive_on_prefix():
    assert detect_msgrefs("TG#42 done") == [42]
    assert detect_msgrefs("Tg#7 and tG#8") == [7, 8]


def test_detect_dedupes_first_appearance_order():
    assert detect_msgrefs("tg#100 then tg#50 then tg#100 again") == [100, 50]


def test_detect_rejects_tg_hash_zero():
    assert detect_msgrefs("tg#0 is not a thing") == []


def test_detect_rejects_leading_zero_id():
    # The id's leading digit must be 1-9 (mirrors detect.ts's `tg#[1-9][0-9]*`) — "tg#007"
    # has no valid id right after `tg#` (it starts with "0"), so it is not a reference at all.
    assert detect_msgrefs("tg#007") == []


def test_detect_boundary_rejects_leading_and_trailing_alnum():
    assert detect_msgrefs("xtg#12 is not a ref") == []
    assert detect_msgrefs("tg#12a is not a ref") == []
    # punctuation boundaries ARE refs
    assert detect_msgrefs("(tg#10)") == [10]
    assert detect_msgrefs("tg#10,") == [10]
    assert detect_msgrefs("tg#10.") == [10]


def test_detect_bare_hash_is_not_a_msgref():
    # a bare GitHub-style #123 (no `tg` prefix) is a DIFFERENT namespace (autolink-prs territory)
    assert detect_msgrefs("closes #123") == []


def test_detect_skips_url_tokens():
    assert detect_msgrefs("see https://example.com/tg#5900/path") == []


def test_detect_empty_text():
    assert detect_msgrefs("") == []


def test_title_msgrefs():
    assert title_msgrefs("see tg#5900") == [5900]
    assert title_msgrefs("closes #123") == []
    assert title_msgrefs("a perfectly ordinary title") == []


def test_title_msgrefs_does_not_apply_the_url_token_skip():
    # detect_msgrefs skips a whitespace token containing "://" (a pasted URL with tg# in its
    # path shouldn't count as a mention in BODY prose) -- but a title has no legitimate reason to
    # contain a pasted URL like that, and the gate's whole point is that the literal string must
    # not appear in a title at all. title_msgrefs must still catch it (task-cli#45 review
    # finding: detect_msgrefs's URL-skip let `see [tg#42](https://example.test/msg)` slip past
    # the non-skippable title gate).
    assert title_msgrefs("see [tg#42](https://example.test/msg)") == [42]
    assert title_msgrefs("see https://example.com/tg#5900/path") == [5900]


def _write_history(path, records: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def test_load_history_reads_matching_files(tmp_path):
    config_dir = tmp_path / ".config" / "tg-cli"
    config_dir.mkdir(parents=True)
    _write_history(
        config_dir / "tg-ctl.123.history.jsonl",
        [
            {"ts": 1000, "message_id": 5900, "direction": "user", "from": "Alex", "text": "hello", "pane": "%0"},
            {"ts": 1001, "message_id": 5901, "direction": "agent", "from": "agent", "text": "world", "pane": None},
        ],
    )
    hist = load_history(env={"HOME": str(tmp_path)})
    assert hist[5900].text == "hello"
    assert hist[5900].from_ == "Alex"
    assert hist[5901].pane is None


def test_load_history_skips_garbage_lines(tmp_path):
    config_dir = tmp_path / ".config" / "tg-cli"
    config_dir.mkdir(parents=True)
    (config_dir / "tg-ctl.123.history.jsonl").write_text(
        "not json at all\n"
        '{"ts": 1000, "message_id": 42, "direction": "user", "from": "Alex", "text": "ok", "pane": null}\n'
        '{"ts": "bad", "message_id": 43, "direction": "user", "from": "Alex", "text": "bad ts", "pane": null}\n'
        "\n",
        encoding="utf-8",
    )
    hist = load_history(env={"HOME": str(tmp_path)})
    assert set(hist.keys()) == {42}


def test_load_history_missing_dir_returns_empty(tmp_path):
    assert load_history(env={"HOME": str(tmp_path)}) == {}


def test_load_history_duplicate_message_id_keeps_the_last_record(tmp_path):
    # the JSONL is append-only chronological: a message resent/edited appends the SAME
    # message_id again later. The most recent (last) record must win, not the first.
    config_dir = tmp_path / ".config" / "tg-cli"
    config_dir.mkdir(parents=True)
    _write_history(
        config_dir / "tg-ctl.123.history.jsonl",
        [
            {"ts": 1000, "message_id": 42, "direction": "user", "from": "Alex", "text": "original", "pane": None},
            {"ts": 2000, "message_id": 42, "direction": "user", "from": "Alex", "text": "edited later", "pane": None},
        ],
    )
    hist = load_history(env={"HOME": str(tmp_path)})
    assert hist[42].text == "edited later"


def test_load_history_rejects_bool_ts_and_message_id(tmp_path):
    # bool is a subclass of int in Python — a malformed `"ts": true` / `"message_id": true`
    # line must not silently pass validation as 1/0.
    config_dir = tmp_path / ".config" / "tg-cli"
    config_dir.mkdir(parents=True)
    _write_history(
        config_dir / "tg-ctl.123.history.jsonl",
        [
            {"ts": True, "message_id": 1, "direction": "user", "from": "Alex", "text": "bad ts", "pane": None},
            {"ts": 1000, "message_id": True, "direction": "user", "from": "Alex", "text": "bad id", "pane": None},
            {"ts": 1001, "message_id": 2, "direction": "user", "from": "Alex", "text": "ok", "pane": None},
        ],
    )
    hist = load_history(env={"HOME": str(tmp_path)})
    assert set(hist.keys()) == {2}


def test_load_history_rejects_non_finite_ts_without_raising(tmp_path):
    # task-cli#45 review finding: Python's json module accepts the non-standard NaN/Infinity
    # literals as float (most JSON parsers reject them). int(float("nan")) raises ValueError,
    # which would violate this function's "garbage never raises" contract on one malformed line.
    config_dir = tmp_path / ".config" / "tg-cli"
    config_dir.mkdir(parents=True)
    (config_dir / "tg-ctl.123.history.jsonl").write_text(
        '{"ts": NaN, "message_id": 1, "direction": "user", "from": "Alex", "text": "bad", "pane": null}\n'
        '{"ts": Infinity, "message_id": 2, "direction": "user", "from": "Alex", "text": "bad", "pane": null}\n'
        '{"ts": 1000, "message_id": 3, "direction": "user", "from": "Alex", "text": "ok", "pane": null}\n',
        encoding="utf-8",
    )
    hist = load_history(env={"HOME": str(tmp_path)})  # must not raise
    assert set(hist.keys()) == {3}


def test_render_msgref_quotes_is_noop_without_a_reference():
    assert render_msgref_quotes("a plain sentence") == "a plain sentence"


def test_render_msgref_quotes_appends_quote_from_history():
    history = {
        5900: HistoryRecord(ts=1751328000, message_id=5900, direction="user", from_="Alex", text="fix the props bug", pane="%0")
    }
    out = render_msgref_quotes("per tg#5900 do X", history=history)
    assert "per tg#5900 do X" in out
    assert "tg#5900" in out
    assert "Alex" in out
    assert "fix the props bug" in out
    # ts is Unix SECONDS (verified against tg-cli's actual writer, not assumed — see
    # HistoryRecord's docstring); assert the EXACT rendered date so a units regression (e.g. if
    # this were ever fed milliseconds) is caught here rather than silently degrading to a
    # dateless quote line (task-cli#45 review finding).
    assert "2025-07-01 00:00 UTC" in out


def test_render_msgref_quotes_truncates_a_long_message_to_the_tg_cli_excerpt_cap():
    # task-cli#45 review finding: a ticket body can land somewhere far more visible than the
    # original Telegram DM (a GitHub Issues repo, a Linear workspace) and backends publish the
    # body verbatim — an unbounded quote could leak/bloat an issue with an entire long or private
    # message. Cap matches tg-cli's OWN excerpt truncation exactly (MSGREF_EXCERPT_MAX = 120,
    # features/autolink-msgrefs/render.ts) — kept in sync deliberately.
    from tasklib.msgrefs import MSGREF_EXCERPT_MAX

    long_text = "x" * 500
    history = {1: HistoryRecord(ts=1700000000, message_id=1, direction="user", from_="Alex", text=long_text, pane=None)}
    out = render_msgref_quotes("per tg#1 do X", history=history)
    assert "x" * MSGREF_EXCERPT_MAX in out
    assert "x" * (MSGREF_EXCERPT_MAX + 1) not in out
    assert "…" in out


def test_render_msgref_quotes_collapses_a_multiline_message_before_truncating():
    # a multi-line message is collapsed to one line (mirrors tg-cli's excerpt()) — this ALSO
    # sidesteps the acceptance-criterion single-line-serialization hazard entirely for the quote
    # text itself (only the trailing marker line remains, which callers already know to exclude
    # acceptance criteria from — see the msgrefs.py module docstring CALLER CAVEAT).
    history = {
        1: HistoryRecord(ts=1700000000, message_id=1, direction="user", from_="Alex", text="line one\nline two\nline three", pane=None)
    }
    out = render_msgref_quotes("per tg#1 do X", history=history)
    assert "line one line two line three" in out


def test_render_msgref_quotes_notes_missing_message():
    out = render_msgref_quotes("per tg#999999 do X", history={})
    assert "not found" in out
    assert "tg#999999" in out


def test_render_msgref_quotes_dedupes_and_orders_multiple_refs():
    history = {
        1: HistoryRecord(ts=1, message_id=1, direction="user", from_="Alex", text="first", pane=None),
        2: HistoryRecord(ts=2, message_id=2, direction="user", from_="Alex", text="second", pane=None),
    }
    out = render_msgref_quotes("tg#2 and tg#1 and tg#2 again", history=history)
    assert out.index("second") < out.index("first")


def test_render_msgref_quotes_is_idempotent_on_a_resubmitted_expanded_value():
    # task-cli#45 review finding: a read-modify-write caller passes back the FULL already-
    # expanded value (mention + its quote block, exactly what an editor round-trip would do).
    # The quote header itself ("> **tg#42**") contains a boundary-valid tg#42 -- expanding again
    # must not pile up a second quote block underneath the first.
    history = {42: HistoryRecord(ts=1700000000, message_id=42, direction="user", from_="Alex", text="fix it", pane=None)}
    once = render_msgref_quotes("per tg#42 do X", history=history)
    twice = render_msgref_quotes(once, history=history)
    assert twice == once
    assert once.count("tg#42") == 2  # the inline mention + the quote header, never more


def test_render_msgref_quotes_still_expands_a_genuinely_new_ref_alongside_an_existing_quote():
    history = {
        1: HistoryRecord(ts=1, message_id=1, direction="user", from_="Alex", text="first", pane=None),
        2: HistoryRecord(ts=2, message_id=2, direction="user", from_="Alex", text="second", pane=None),
    }
    already_has_one_quote = render_msgref_quotes("per tg#1 do X", history=history)
    out = render_msgref_quotes(already_has_one_quote + " also see tg#2", history=history)
    assert "second" in out
    assert out.count("tg#1") == 2  # unchanged: mention + its original quote header, not duplicated
    assert out.count("tg#2") == 2  # the new mention + its freshly-appended quote header


def test_render_msgref_quotes_still_expands_a_bold_markdown_mention_never_generated_by_this_module():
    # task-cli#45 review finding: the OLD idempotency check treated any inline `**tg#<id>**` as
    # already-quoted, so an author's own bold-markdown mention (never generated by
    # render_msgref_quotes) never got its quote appended at all. Checking the provenance marker
    # instead fixes this: a bold mention with no marker is still a genuinely fresh reference.
    history = {42: HistoryRecord(ts=1700000000, message_id=42, direction="user", from_="Alex", text="fix it", pane=None)}
    out = render_msgref_quotes("per **tg#42** do X", history=history)
    assert "fix it" in out
    assert "<!-- tasklib:msgref-quote:42 -->" in out


def test_untrusted_quoted_text_cannot_forge_a_marker_for_an_unrelated_id():
    # task-cli#45 review finding: the quoted (third-party, untrusted) message text itself could
    # contain the literal marker string for some OTHER id -- a marker-only scan would wrongly
    # treat that id as "already quoted" even with no genuine block for it anywhere, suppressing
    # its real expansion elsewhere in the same text. Deriving "already quoted" from the full
    # block regex (header id == marker id, immediately adjacent) instead of a bare marker search
    # closes this: the forged marker has no matching header right before it, so it doesn't count.
    history = {
        1: HistoryRecord(
            ts=1700000000,
            message_id=1,
            direction="user",
            from_="Alex",
            text="see <!-- tasklib:msgref-quote:999 --> for context",
            pane=None,
        ),
    }
    out = render_msgref_quotes("per tg#1 do X, also tg#999", history=history)
    # tg#999 must still be treated as unquoted and get its OWN "not found" note appended
    assert out.count("tg#999") >= 2  # the mention + its own appended quote/not-found line


def test_strip_quoted_blocks_removes_only_a_genuinely_marked_quote():
    from tasklib.msgrefs import strip_quoted_blocks

    # a GENUINE quote (the marker present, as render_msgref_quotes always appends) is stripped
    genuine = "prose\n\n> **tg#42** — Alex, 2026-01-01 00:00 UTC:\n> blocked by HYP-999\n<!-- tasklib:msgref-quote:42 -->"
    assert "HYP-999" not in strip_quoted_blocks(genuine)
    # the IDENTICAL visual shape WITHOUT the marker (a hand-typed lookalike) is left untouched --
    # this is the fix for the review finding that any `>` text shaped like a quote header could
    # smuggle a bare reference past the links gate
    lookalike = "prose\n\n> **tg#42** — Alex, 2026-01-01 00:00 UTC:\n> blocked by HYP-999"
    assert strip_quoted_blocks(lookalike) == lookalike
    # a hand-authored blockquote in a DIFFERENT shape (no `**tg#<id>**` header) is untouched too
    hand_written = "prose\n\n> a normal blockquote mentioning HYP-1"
    assert strip_quoted_blocks(hand_written) == hand_written


def test_strip_inbound_wrap_removes_the_tg_cli_inject_prefix():
    from tasklib.msgrefs import strip_inbound_wrap

    assert strip_inbound_wrap("[TG from Alex tg#1234] fix the header") == "fix the header"
    assert strip_inbound_wrap("[TG from Alex] fix the header") == "fix the header"  # no id case
    assert strip_inbound_wrap("no wrap here") == "no wrap here"


def test_strip_inbound_wrap_survives_a_bracket_inside_the_display_name():
    # task-cli#45 review finding: Telegram allows `]` in a display name. A naive "stop at the
    # first `]`" match would truncate here and leak the wrap's OWN tg#1234 (plus a chunk of the
    # name) right back into the derived title -- exactly the failure this stripping exists to
    # prevent. The id-bearing shape is anchored on the far-more-specific `tg#<digits>]` suffix,
    # so it survives a `]` earlier in the name.
    from tasklib.msgrefs import strip_inbound_wrap

    assert strip_inbound_wrap("[TG from Al]ex tg#1234] fix the header") == "fix the header"


def test_strip_inbound_wrap_does_not_swallow_message_text_that_looks_like_a_wrap_id():
    # task-cli#45 review finding: an EARLIER greedy version of the id-bearing pattern matched
    # through to the LAST " tg#<digits>]" in the whole string -- so a message that itself later
    # contains that same shape had everything up to the SECOND occurrence swallowed into the
    # "wrap", losing real title content. Non-greedy stops at the FIRST occurrence (the wrap's
    # own id), leaving the rest of the message intact.
    from tasklib.msgrefs import strip_inbound_wrap

    assert (
        strip_inbound_wrap("[TG from Alex tg#123] please see tg#456] before changing auth")
        == "please see tg#456] before changing auth"
    )


def test_strip_inbound_wrap_no_id_case_has_a_documented_bracket_limitation():
    # KNOWN LIMITATION (task-cli#45 review finding): unlike the id-bearing shape, the id-LESS
    # wrap has no distinctive anchor to disambiguate a `]` inside the name from the wrap's own
    # closing bracket. This locks in the CURRENT, documented (accepted) behavior so it doesn't
    # silently change — see the comment on _INBOUND_WRAP_NO_ID_RE for why this is accepted
    # rather than fixed: the id-less shape only occurs for rare, non-chat-message input.
    from tasklib.msgrefs import strip_inbound_wrap

    assert strip_inbound_wrap("[TG from Al]ex] fix the header") == "ex] fix the header"
    # the common case (no bracket in the name) is unaffected
    assert strip_inbound_wrap("[TG from Alex] fix the header") == "fix the header"


def test_quote_line_sanitizes_a_display_name_with_embedded_newlines():
    # `from` is Telegram-user-controlled: an embedded newline in a display name could inject a
    # fake `## Heading` into the rendered ticket body (render.split_sections treats any `## ...`
    # line — anchored at line-start — as a real section boundary) if not collapsed to a single
    # line first. The literal substring may still appear (mid-line is harmless); what matters is
    # that it no longer STARTS a line, which is what render.split_sections actually keys on.
    import re

    history = {
        42: HistoryRecord(
            ts=1700000000, message_id=42, direction="user", from_="Alex\n## Injected Heading\nmalicious", text="hi", pane=None
        )
    }
    out = render_msgref_quotes("per tg#42 do X", history=history)
    assert "Alex ## Injected Heading malicious" in out  # collapsed onto one line
    assert not re.search(r"^##\s+", out, re.MULTILINE)  # never starts a line -> not a heading
