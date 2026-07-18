"""Detect and resolve ``tg#<id>`` Telegram-message references in ticket text.

Mirrors tg-cli's ``autolink-msgrefs`` feature (``~/.files/repos/tg-cli/features/autolink-msgrefs/
{detect,render}.ts``, spec ``docs/specs/autolink-msgrefs.md`` in that repo): a message reference
is the literal ``tg#<id>`` token the tg-cli inbound-inject wrap renders (``[TG from Alex tg#1234]
…``). task-cli can't ``import`` tg-cli's TypeScript across the Python/Bun boundary, so
:func:`find_msgref_matches`/:func:`detect_msgrefs` are a deliberate, spec-linked PORT of the same
detection regex — SYNC comment: keep the boundary rules identical if that spec changes (see the
shared-util-single-source guidance on justified cross-language duplication).

Unlike tg-cli's own linkify — which only turns ``tg#<id>`` into a Telegram deep link, and that
link is ``null`` for a private-DM bot chat (the CTO's actual setup: no per-message URL exists
there) — a ticket needs to be useful with zero live Telegram access. So the payload here is the
QUOTE, read straight from tg-cli's own local history log
(``~/.config/tg-cli/tg-ctl.<botId>.history.jsonl``, one JSON object per line: message_id -> text/
from/ts) — the exact file tg-cli itself appends to on every send/receive (HYP-897 tg#6109).

CALLER CAVEAT: :func:`render_msgref_quotes` embeds a multi-line ``> …`` blockquote. That is safe
inside a free-text prose field (task-cli's ``render.py`` serializes ``## What``/``## Why``/etc. as
a whole-section blob) but NOT inside anything serialized as a single markdown line — e.g. an
acceptance-criterion checkbox (``- [ ] <text>``). Embedding a quote there breaks on the next
render/parse round-trip (everything after the first line is silently dropped). Callers must only
apply this to whole-section prose fields, never to a single-line-serialized one.
"""

from __future__ import annotations

import glob
import json
import math
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# tg# (case-insensitive on `tg`) immediately followed by a positive id (leading digit 1-9 —
# `tg#0` is not a thing). Mirrors detect.ts's MSGREF_RE exactly.
_MSGREF_RE = re.compile(r"tg#[1-9][0-9]*", re.IGNORECASE)


@dataclass
class MsgRefMatch:
    """One boundary-valid ``tg#<id>`` occurrence: its span in the scanned segment + the id."""

    start: int
    end: int
    id: int


# ASCII-only, matching detect.ts's `isAlnum` (`/[A-Za-z0-9]/.test(ch)`) EXACTLY — Python's
# str.isalnum() is Unicode-aware (True for 'д', 'é', full-width digits, …), which would silently
# diverge from the reference implementation at a non-ASCII boundary (e.g. "tg#12д" is a ref in
# tg-cli, since Cyrillic 'д' fails its ASCII-only alnum test; Unicode isalnum() would wrongly
# reject it here).
_ASCII_ALNUM_RE = re.compile(r"[A-Za-z0-9]")


def _is_boundary_alnum(ch: str | None) -> bool:
    return ch is not None and _ASCII_ALNUM_RE.match(ch) is not None


def find_msgref_matches(segment: str) -> list[MsgRefMatch]:
    """Boundary-valid ``tg#<id>`` occurrences in one text segment (no URL-token filtering — see
    :func:`detect_msgrefs` for that). Mirrors ``detect.ts``'s ``findMsgRefMatches``: the char
    after the number must not be alphanumeric (``tg#12a`` is not a ref) and the char before
    ``tg`` must not be alphanumeric (``xtg#12`` is not a ref)."""
    out: list[MsgRefMatch] = []
    for m in _MSGREF_RE.finditer(segment):
        start, end = m.start(), m.end()
        before = segment[start - 1] if start > 0 else None
        after = segment[end] if end < len(segment) else None
        if _is_boundary_alnum(before) or _is_boundary_alnum(after):
            continue
        out.append(MsgRefMatch(start=start, end=end, id=int(m.group(0)[3:])))
    return out


def detect_msgrefs(text: str) -> list[int]:
    """Unique ``tg#<id>`` ids in ``text``, first-appearance order. A whitespace-delimited token
    containing ``://`` (a pasted URL) is skipped — mirrors ``detect.ts``'s ``detectMsgRefs``."""
    seen: set[int] = set()
    ordered: list[int] = []
    for token in text.split():
        if "://" in token:
            continue
        for match in find_msgref_matches(token):
            if match.id in seen:
                continue
            seen.add(match.id)
            ordered.append(match.id)
    return ordered


# tg-cli's inbound-inject wrap (features/tg-ctl/types.ts: `injectWrap: '[TG from {name} {id}]
# {msg}'`) renders `{id}` as `tg#<id>` when a message id is available. An agent that passes the
# WHOLE wrapped inbound text as raw input (task-cli's `create --from-message` / `classify
# --create` hook path — exactly the workflow this module's QUOTE feature exists to support) would
# otherwise carry that `tg#<id>` into a TITLE derived from the first line, tripping the non-
# skippable `msgref-title` gate on a message that legitimately quotes itself (task-cli#45 review
# finding). Stripping this prefix before deriving a title removes the wrap's OWN reference from
# the title without touching the id anywhere else — the full text (wrap included) still flows
# into `what`, where normal msgref expansion picks the reference up.
#
# Two known wrap shapes (tg-ctl's injectWrap template): WITH an id — `[TG from {name} tg#{id}]
# {msg}` — or WITHOUT one (an /agent route or a media item with no message id) — `[TG from
# {name}] {msg}`. {name} is a Telegram display name and can itself contain `]` — a naive
# `[^\]]*` stops at the FIRST `]`, so a name like "Al]ex" would truncate the match early and leak
# the wrap's OWN `tg#<id>] ` right back into the derived title (task-cli#45 review finding: the
# exact failure this stripping exists to prevent). The id-bearing shape is matched FIRST and
# anchored on the far more specific `tg#<digits>]` suffix (a name containing that exact literal
# substring is vanishingly unlikely) so it survives a `]` inside the name.
#
# The `.*?` here is NON-greedy deliberately — an earlier greedy `.*` matched through to the
# LAST " tg#<digits>]" in the whole string, so a message whose OWN text later contains that same
# shape (`[TG from Alex tg#123] please see tg#456] before changing auth`) had everything up to
# the SECOND occurrence swallowed into the "wrap", losing real title content (task-cli#45 review
# finding). Non-greedy stops at the FIRST occurrence — the wrap's own id, which by construction
# always comes before any message content — regardless of what the message later contains.
_INBOUND_WRAP_WITH_ID_RE = re.compile(r"^\[TG from .*? tg#[1-9][0-9]*\]\s*")
# KNOWN LIMITATION (task-cli#45 review finding): the id-less shape has no equivalent anchor —
# there is no rare, distinctive suffix to find the wrap's own closing `]` when the name ALSO
# contains one (`[TG from Al]ex] fix the header` derives "ex] fix the header", not "fix the
# header"). Accepted: per tg-cli's inject.ts, the id-less shape only occurs for a message with NO
# Telegram message_id (an /agent route or a media item) — genuinely rare input to pipe through
# `--from-message`/`classify --create` in the first place, versus the id-bearing shape (an actual
# forwarded chat message, this feature's whole motivating case), which IS anchored correctly.
_INBOUND_WRAP_NO_ID_RE = re.compile(r"^\[TG from [^\]]*\]\s*")


def strip_inbound_wrap(text: str) -> str:
    """Strip a single leading tg-cli inbound-inject wrap from ``text``, if present."""
    with_id = _INBOUND_WRAP_WITH_ID_RE.sub("", text, count=1)
    if with_id != text:
        return with_id
    return _INBOUND_WRAP_NO_ID_RE.sub("", text, count=1)


def title_msgrefs(title: str) -> list[int]:
    """Unique ``tg#<id>`` ids named in ``title``, first-appearance order.

    Instrumentally forbidden (task 6109): a title is one line with no room for a quote, and
    nothing downstream ever links a title the way a body/comment can. The reference must move
    into a prose field instead — see the ``msgref-title`` policy gate.

    Deliberately uses :func:`find_msgref_matches` directly on the WHOLE title — NOT
    :func:`detect_msgrefs` — so the URL-token skip (correct for body prose, where a pasted link
    containing ``tg#`` in its path shouldn't count as a mention) does not also make the TITLE
    gate blind to something like ``see [tg#42](https://example.test/msg)``: the literal
    ``tg#42`` is right there in the title regardless of whatever decorative markdown surrounds
    it, and the whole point of this gate is that the string must not appear in a title at all
    (task-cli#45 review finding).
    """
    seen: set[int] = set()
    ordered: list[int] = []
    for match in find_msgref_matches(title):
        if match.id in seen:
            continue
        seen.add(match.id)
        ordered.append(match.id)
    return ordered


@dataclass
class HistoryRecord:
    """One tg-cli history line — mirrors ``features/replies/history.ts``'s ``HistoryRecord``.

    ``ts`` is Unix SECONDS, not JS's usual milliseconds — VERIFIED against the actual writer
    (not assumed): the `tg` entrypoint writes ``ts: Math.floor(Date.now() / 1000)`` at both
    outbound-history call sites (task-cli#45 review raised this as a risk; confirmed safe by
    reading tg-cli's source directly rather than guessing). If tg-cli's history format ever
    changes units, :func:`_quote_line`'s ``datetime.fromtimestamp`` call needs updating to match.
    """

    ts: int
    message_id: int | None
    direction: str
    from_: str
    text: str
    pane: str | None


def _tg_cli_config_dir(env: dict[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    base = env.get("XDG_CONFIG_HOME") or os.path.join(env.get("HOME", os.path.expanduser("~")), ".config")
    return Path(base) / "tg-cli"


def _parse_history_line(line: str) -> HistoryRecord | None:
    """One JSONL line -> a record, or ``None`` when blank/garbage/incomplete (never raises) —
    same tolerant-parse contract as tg-cli's own ``parseLine`` (``features/replies/history.ts``)."""
    stripped = line.strip()
    if not stripped:
        return None
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    ts, message_id, direction, text, from_, pane = (
        data.get("ts"),
        data.get("message_id"),
        data.get("direction"),
        data.get("text"),
        data.get("from"),
        data.get("pane"),
    )
    # bool is a subclass of int in Python (isinstance(True, int) is True) — reject it explicitly
    # so a malformed `"ts": true` / `"message_id": true` line doesn't silently pass as 1/0.
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return None
    # Python's json.loads accepts the non-standard literals NaN/Infinity/-Infinity as float
    # (most JSON parsers reject them, but Python's does not) — int(float("nan")) raises
    # ValueError, which would violate this function's "garbage never raises" contract on one
    # malformed history line (task-cli#45 review finding).
    if not math.isfinite(ts):
        return None
    if not (message_id is None or (isinstance(message_id, int) and not isinstance(message_id, bool))):
        return None
    if direction not in ("user", "agent"):
        return None
    if not (isinstance(text, str) and isinstance(from_, str)):
        return None
    if not (pane is None or isinstance(pane, str)):
        return None
    return HistoryRecord(ts=int(ts), message_id=message_id, direction=direction, from_=from_, text=text, pane=pane)


def load_history(env: dict[str, str] | None = None) -> dict[int, HistoryRecord]:
    """Read every ``tg-ctl.*.history.jsonl`` in tg-cli's config dir into ``{message_id: record}``.

    Best-effort: a missing directory, no matching file, or a corrupt/partial line never raises —
    ticket creation must never depend on tg-cli being installed or its history being intact. The
    JSONL file is append-only chronological, so the LAST record for a given message id — within
    one file, and across files in sorted-glob order — wins: a message that was resent/edited (the
    same message_id appended again later) must resolve to its most recent text, not its first.
    """
    out: dict[int, HistoryRecord] = {}
    try:
        paths = sorted(glob.glob(str(_tg_cli_config_dir(env) / "tg-ctl.*.history.jsonl")))
    except OSError:
        return out
    for path in paths:
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    rec = _parse_history_line(line)
                    if rec is not None and rec.message_id is not None:
                        out[rec.message_id] = rec
        except OSError:
            continue
    return out


def _sanitize_display_name(name: str) -> str:
    """Collapse a Telegram display name to a single safe line.

    ``from`` is USER-CONTROLLED (Telegram lets an account set an arbitrary first/last name,
    including embedded newlines) — embedding it unsanitized in the quote HEADER line (unlike
    ``rec.text``, which already gets every ``\\n`` turned into ``\\n> `` to stay inside the
    blockquote) could inject a bogus ``## Heading`` into the rendered ticket body:
    ``render.split_sections`` treats any ``## ...`` line as a real section boundary (task-cli#45
    review finding). Collapsing all whitespace to single spaces is a safe, simple normalization.
    """
    return " ".join(name.split())


# Provenance marker appended after every GENERATED quote block, keyed by id. Distinguishes a
# quote render_msgref_quotes itself produced from a user hand-typing the same VISUAL shape
# (`> **tg#42** — …`) to smuggle a bare reference past the links/impact-quality gates, or an
# innocent bold-markdown mention (`per **tg#42** do X`) that only coincidentally looks like a
# quote header — neither is real provenance, and treating either as "already quoted" was a real
# gap: a spoofed block bypassed the links gate with no audited `--skip-links`, and a bold mention
# never got its OWN quote appended (task-cli#45 review finding). Not cryptographically unforgeable
# (a user could type the literal marker), but raises the bar from "type an obvious markdown
# shape" to "type this exact internal implementation-detail comment" — proportionate for a
# personal-tooling quality gate, not a security boundary between untrusted parties.
def _quote_marker(msg_id: int) -> str:
    return f"<!-- tasklib:msgref-quote:{msg_id} -->"


# Matches tg-cli's OWN excerpt cap exactly (features/autolink-msgrefs/render.ts,
# MSGREF_EXCERPT_MAX = 120) — kept in sync deliberately, not an independent choice. A ticket
# body can land somewhere far more widely visible than the original Telegram DM (a GitHub
# Issues repo a team can browse, a Linear workspace) and backends publish render(ticket)
# verbatim: copying an UNBOUNDED quoted message in would let one `tg#<id>` leak or bloat an
# issue with an entire private/lengthy message (task-cli#45 review finding). Collapsing to one
# line first (like tg-cli's own excerpt()) also sidesteps the multi-line blockquote-continuation
# concern entirely — a truncated one-line excerpt never needs a `\n> ` per line.
MSGREF_EXCERPT_MAX = 120


def _excerpt(text: str, max_chars: int = MSGREF_EXCERPT_MAX) -> str:
    one_line = " ".join(text.split())
    if len(one_line) <= max_chars:
        return one_line
    return one_line[:max_chars].rstrip() + "…"


def _quote_line(msg_id: int, rec: HistoryRecord | None) -> str:
    """One ``> …`` quote line quoting a (possibly truncated) excerpt of the referenced message,
    plus its provenance marker — or a plain "not found" note when the id isn't in local history
    (older than retention, or tg-cli was never installed here).

    Never raises: a record with an out-of-range ``ts`` (``datetime.fromtimestamp`` can raise
    ``ValueError``/``OverflowError`` on a corrupt/absurd value that still passed
    :func:`_parse_history_line`'s type-only validation) degrades to a dateless quote line instead
    of crashing ticket creation — the same best-effort contract :func:`load_history` documents.
    """
    marker = _quote_marker(msg_id)
    if rec is None:
        return (
            f"> **tg#{msg_id}** — message not found in local tg-cli history "
            f"(older than local retention, or tg-cli is not installed here).\n{marker}"
        )
    quoted = _excerpt(rec.text)
    from_ = _sanitize_display_name(rec.from_)
    try:
        when = datetime.fromtimestamp(rec.ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, OverflowError, OSError):
        return f"> **tg#{msg_id}** — {from_}: {quoted}\n{marker}"
    return f"> **tg#{msg_id}** — {from_}, {when}: {quoted}\n{marker}"


# One APPENDED quote block: the single quote LINE _quote_line always produces (`> **tg#<id>**
# — …: <excerpt>`, always one line since _excerpt collapses whitespace before truncating) plus
# its trailing provenance marker — with a BACKREFERENCE (`\1`) requiring the marker's id to match
# the header's own id. Requiring the marker (not just the visual header shape) is what keeps a
# hand-typed lookalike out of scope — see the comment on :func:`_quote_marker`. The backreference
# closes a narrower gap: an UNTRUSTED quoted message could itself contain the literal marker
# text for some OTHER id (e.g. its own text is `... <!-- tasklib:msgref-quote:999 -->`), which
# would corrupt a marker-only scan for id 999 even though no genuine block for 999 exists there
# (task-cli#45 review finding) — anchoring the marker to the SAME id as the header it directly
# follows means only a genuinely-matched, internally-consistent block counts. The `.*` between
# header and marker also tolerates the OLDER multi-line `\n>...` shape (from before the excerpt
# truncation was added) in case any already-stored ticket still carries one. Used both to make
# expansion idempotent (below) and, via :func:`strip_quoted_blocks`, to keep a quoted third-party
# message's content out of the links/user-impact-quality gates (tasklib.policy) regardless of
# which command re-scans it.
_QUOTE_BLOCK_RE = re.compile(r"^> \*\*tg#(\d+)\*\*.*(?:\n>.*)*\n<!-- tasklib:msgref-quote:\1 -->", re.MULTILINE)


def strip_quoted_blocks(text: str) -> str:
    """Remove every GENERATED tg#<id> quote block from ``text`` (see :data:`_QUOTE_BLOCK_RE`) —
    identified by its provenance marker matching its own header's id, not merely the visual
    shape or a marker found anywhere in the text (task-cli#45 review finding)."""
    return _QUOTE_BLOCK_RE.sub("", text)


def unquoted_msgrefs(text: str) -> list[int]:
    """The ``tg#<id>`` ids MENTIONED in ``text`` that do NOT yet have a genuinely-matched
    GENERATED quote block, first-appearance order — i.e. exactly the ids
    :func:`render_msgref_quotes` would append a quote for.

    Pure (no I/O): it says WHICH ids lack a quote, not whether any of them is resolvable in
    tg-cli history — that resolvability question is the caller's (the ``msgref-quote`` policy
    gate loads history and decides deny-vs-warn on top of this set). Shared as the single source
    of the "fresh mention minus already-quoted" computation so the auto-expander and the guard
    can never diverge on what "already has its quote" means: both derive it from the same
    :data:`_QUOTE_BLOCK_RE` (header id == marker id), so an innocent bold-markdown mention still
    counts as unquoted while a spoofed lookalike / marker-shaped literal inside an untrusted
    quoted message does not.
    """
    fresh_mentions = detect_msgrefs(strip_quoted_blocks(text))
    already_quoted = {int(m.group(1)) for m in _QUOTE_BLOCK_RE.finditer(text)}
    return [i for i in fresh_mentions if i not in already_quoted]


def render_msgref_quotes(text: str, history: dict[int, HistoryRecord] | None = None, env: dict[str, str] | None = None) -> str:
    """Append a quote block for every ``tg#<id>`` mentioned in ``text``, first-appearance order.

    A no-op (returns ``text`` unchanged) when no reference is found — so calling this on every
    prose field unconditionally is safe. ``history``/``env`` are for tests to inject a fixed
    history map / fake ``$HOME`` without touching the real tg-cli config dir; omitted in
    production, where it loads from disk via :func:`load_history`.

    Idempotent against a read-modify-write caller that resubmits the FULL already-expanded value
    (e.g. `task change --what "$(current what, quote block included)"`): a mention is only
    expanded when (a) it appears OUTSIDE an existing GENERATED quote block — a quote's own header
    line (`> **tg#42** — …`) itself contains a boundary-valid `tg#42`, which would otherwise look
    like a fresh mention on every re-submission — and (b) that id doesn't already have a
    genuinely-matched quote block (:data:`_QUOTE_BLOCK_RE`, header id == marker id). Deriving
    "already quoted" from the same block regex `strip_quoted_blocks` uses — not a bare marker
    scan — means an innocent bold-markdown mention (`per **tg#42** do X`, never generated by this
    module) still gets its OWN quote appended, a spoofed lookalike is never mistaken for a real
    one, and a marker-shaped literal sitting INSIDE an untrusted quoted message's own text can't
    corrupt this set either (it has no matching header immediately before it).
    """
    ids = unquoted_msgrefs(text)
    if not ids:
        return text
    hist = history if history is not None else load_history(env)
    lines = [_quote_line(msg_id, hist.get(msg_id)) for msg_id in ids]
    return text.rstrip() + "\n\n" + "\n".join(lines) + "\n"
