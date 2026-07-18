"""Enforcement — the point of the tool. Pure rules over a ``Ticket`` + an ``EnforceConfig``.

No I/O. ``check()`` runs the gates and returns structured violations; the entrypoint turns a
non-empty result into a refusal with a precise message. Each gate is individually skippable
via the escape hatch (``--skip-<gate> "<reason>"`` → recorded in ``Ticket.skips``), and each
gate is disable-able in config (``enforce:``). The escape hatch is auditable: the
justification lives on the ticket forever.

Two entry points, one engine:
- :func:`check_create` — the gates at ticket *creation*.
- :func:`check_done`   — the gates at ``change``→``done`` (close).

They differ only in which screenshot kind is demanded; everything else is shared.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .model import Ticket
from .msgrefs import strip_quoted_blocks as _strip_quoted_blocks
from .msgrefs import unquoted_msgrefs as _unquoted_msgrefs
from .render import validate_format

# _strip_quoted_blocks (tasklib.msgrefs.strip_quoted_blocks) removes an appended tg#<id> quote
# block before the `links`/`user-impact-quality` gates scan any text — that quote is machine-
# appended text from someone ELSE's Telegram message, not authored prose, so its content must
# never itself cause a refusal. Without this, "expand only after THIS command's own gates"
# (cmd_create/cmd_change in cli.py) protects only the command that did the expanding: the quote
# is then STORED on the ticket, and any LATER command (an edit to an unrelated field, or `change
# --done`) re-scans the stored, already-expanded text and can refuse on content the author never
# wrote and cannot fix (task-cli#45 review finding — verified: the first "expand after gates" fix
# does not survive a second command). Stripping at the SCAN site is order-independent and covers
# every phase/command uniformly. The stripper is anchored on the EXACT quote-block shape
# tasklib.msgrefs generates (not any `>`-prefixed line), so a hand-authored blockquote elsewhere
# in ticket prose — a different shape — is not swept up and stays subject to the links gate.

# The canonical gate names. These are also the ``--skip-<gate>`` suffixes (hyphenated).
GATE_ACCEPTANCE = "acceptance-criteria"
GATE_MOTIVATION = "motivation"
GATE_USER_IMPACT = "user-impact"
GATE_COST_OF_INACTION = "cost-of-inaction"
GATE_SCREENSHOTS = "screenshots"
GATE_FORMATTING = "formatting"
# New gates (the enforcement-doctrine rules):
GATE_LINKS = "links"  # related entities must be proper links, not bare tokens
GATE_USER_IMPACT_QUALITY = "user-impact-quality"  # impact must be plain-language + user-framed
# DONE-phase only, and deliberately NOT skippable: a ticket cannot close with an unchecked
# acceptance criterion. It is absent from `normalize_skip_gate` so no `--skip-…` can waive it.
GATE_ACCEPTANCE_CHECKED = "acceptance-checked"
# Deliberately NOT skippable (task 6109): a `tg#<id>` reference in the title has no legitimate
# reason to stay there — the fix is always "move it into a prose field", never a judgment call
# the way a false-positive `links` match can be. Absent from `normalize_skip_gate` for the same
# reason GATE_ACCEPTANCE_CHECKED is: no `--skip-…` can waive it.
GATE_MSGREF_TITLE = "msgref-title"
# A tg#<id> reference sits in a BODY prose field but its QUOTED content was never attached.
# Skippable (unlike msgref-title): unlike a title reference — which is always wrong and always
# fixable by moving it out — an unquoted body reference has a legitimate waiver (a false-positive
# `tg#<n>` in ordinary prose, or a reference the author knowingly can't quote). See
# :func:`msgref_quote_reports` for the deny-vs-warn split (deny only when the id IS resolvable in
# local tg-cli history yet unquoted; a reference to a message too old for local retention only
# warns).
GATE_MSGREF_QUOTE = "msgref-quote"
# The Links section must contain only real URLs. A bare, non-URL value (`Session: session:2`)
# rendered as `- key: value` escaped the `links` gate entirely — that gate scans PROSE for bare
# references, never the Links field itself — so junk masqueraded as a link (CTO report, tg#9179).
# Skippable: a genuinely legacy ticket read back from the backend may carry a non-URL value the
# author can't retroactively fix in one step.
GATE_LINKS_URL = "links-url"

# The minimum number of acceptance criteria a ticket must declare (rule: a real ticket has
# more than one provable outcome). Overridable via `enforce.acceptance_min`.
DEFAULT_ACCEPTANCE_MIN = 2

# Gates that NO recorded justification can waive — a hard refuse. A ticket must not be closed
# with an unchecked criterion under any escape hatch; only disabling the gate in config removes it.
NON_SKIPPABLE_GATES: frozenset[str] = frozenset({GATE_ACCEPTANCE_CHECKED, GATE_MSGREF_TITLE})

ALL_GATES: tuple[str, ...] = (
    GATE_ACCEPTANCE,
    GATE_MOTIVATION,
    GATE_USER_IMPACT,
    GATE_USER_IMPACT_QUALITY,
    GATE_COST_OF_INACTION,
    GATE_SCREENSHOTS,
    GATE_FORMATTING,
    GATE_LINKS,
    GATE_LINKS_URL,
    GATE_ACCEPTANCE_CHECKED,
    GATE_MSGREF_TITLE,
    GATE_MSGREF_QUOTE,
)


class Phase(str, Enum):
    """The two enforcement edges `check()` runs at — and the ONLY values it is ever passed.

    There is deliberately no third "check"/"edit" phase: the `task check` command toggles a
    criterion in place (`cmd_check`) and never calls `check()`, and a non-closing `task change`
    runs its own narrow edit gates (`_enforce_edit_or_die`), also not `check()`. So a gate run
    unconditionally inside `check()` runs at exactly create AND close — the two edges — and
    nowhere else.
    """

    CREATE = "create"
    DONE = "done"


@dataclass
class Violation:
    """One failed gate. ``gate`` is the canonical name; ``hint`` tells the user the fix."""

    gate: str
    message: str
    hint: str = ""


@dataclass
class EnforceConfig:
    """Which gates are active. Mirrors the ``enforce:`` config block (§2).

    A gate set to ``False`` is disabled entirely (no check, no skip needed). Screenshot
    gating is label-driven (``screenshot_labels``): screenshots are required only when the
    ticket carries one of those labels, separately for create/done.
    """

    acceptance_criteria: bool = True
    motivation: bool = True
    user_impact: bool = True
    cost_of_inaction: bool = True
    formatting: bool = True
    screenshots_on_create: bool = True
    screenshots_on_done: bool = True
    screenshot_labels: frozenset[str] = frozenset({"ui", "visual"})
    # enforcement-doctrine gates (default-on)
    links: bool = True
    user_impact_quality: bool = True
    acceptance_checked: bool = True  # block close while any criterion is unchecked
    acceptance_min: int = DEFAULT_ACCEPTANCE_MIN  # minimum acceptance criteria a ticket must declare
    msgref_title: bool = True  # refuse a tg#<id> reference in the title (task 6109)
    msgref_quote: bool = True  # refuse a body tg#<id> whose quote is missing yet resolvable (tg#9161)
    links_url: bool = True  # refuse a non-URL value in the Links section (tg#9179)

    @classmethod
    def from_dict(cls, data: dict | None) -> "EnforceConfig":
        """Build from the parsed ``enforce:`` config block. Absent keys keep defaults.

        Accepts the spec's shape: scalar ``required``/``optional``/``strict`` for the text
        gates, and a nested ``screenshots: {on_create, on_done}`` map whose
        ``required_if_label`` list drives ``screenshot_labels``.
        """
        if not data:
            return cls()

        def _on(key: str, default: bool = True) -> bool:
            v = data.get(key)
            if v is None:
                return default
            if isinstance(v, bool):
                return v
            return str(v).strip().lower() in {"required", "strict", "true", "on", "yes"}

        shots = data.get("screenshots") or {}
        labels: set[str] = set()
        soc = True
        sod = True
        if isinstance(shots, dict):
            soc = _label_gate_enabled(shots.get("on_create"), labels)
            sod = _label_gate_enabled(shots.get("on_done"), labels)

        try:
            acc_min = int(data.get("acceptance_min", DEFAULT_ACCEPTANCE_MIN))
        except (TypeError, ValueError):
            acc_min = DEFAULT_ACCEPTANCE_MIN

        return cls(
            acceptance_criteria=_on("acceptance_criteria"),
            motivation=_on("motivation"),
            user_impact=_on("user_impact"),
            cost_of_inaction=_on("cost_of_inaction"),
            formatting=_on("formatting"),
            screenshots_on_create=soc,
            screenshots_on_done=sod,
            screenshot_labels=frozenset(labels) if labels else cls.screenshot_labels,
            links=_on("links"),
            user_impact_quality=_on("user_impact_quality"),
            acceptance_checked=_on("acceptance_checked"),
            acceptance_min=acc_min,
            msgref_title=_on("msgref_title"),
            msgref_quote=_on("msgref_quote"),
            links_url=_on("links_url"),
        )


def _label_gate_enabled(spec: object, labels_out: set[str]) -> bool:
    """Interpret a screenshots ``on_create``/``on_done`` spec, collecting its labels.

    Forms accepted:
      - ``required`` / ``optional`` / bool — enable/disable, no label restriction change.
      - ``{required_if_label: [ui, visual]}`` — enable, gated on those labels.
    """
    if spec is None:
        return True
    if isinstance(spec, bool):
        return spec
    if isinstance(spec, str):
        return spec.strip().lower() in {"required", "strict", "true", "on", "yes"}
    if isinstance(spec, dict):
        lbls = spec.get("required_if_label")
        if isinstance(lbls, list):
            labels_out.update(str(x).strip().lower() for x in lbls)
        return True
    return True


@dataclass
class PolicyResult:
    """The outcome of running the gates. ``ok`` iff no un-skipped violations remain.

    ``warnings`` is a NON-blocking channel, distinct from ``violations``: it never affects
    ``ok`` and never refuses a command. It carries advisory notes the caller should surface but
    not enforce — currently only an unresolvable ``msgref-quote`` (a body ``tg#<id>`` whose
    message is too old for local retention, so no quote could be attached; see
    :func:`msgref_quote_reports`). A gate that can genuinely block belongs in ``violations``.
    """

    violations: list[Violation] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # gates a justification bypassed
    warnings: list[Violation] = field(default_factory=list)  # advisory, never blocks (see docstring)

    @property
    def ok(self) -> bool:
        return not self.violations


def check(
    ticket: Ticket,
    cfg: EnforceConfig,
    phase: Phase,
    resolvable_ids: set[int] | None = None,
) -> PolicyResult:
    """Run all active gates for ``phase``, honoring per-gate escape hatches on the ticket.

    ``resolvable_ids`` is the set of ``tg#<id>`` ids that resolve in local tg-cli history (the
    caller loads it — this module does no I/O). It powers the ``msgref-quote`` gate's deny-vs-warn
    split: a body reference that is unquoted AND in this set is a blocking violation (the quote
    could and should have been attached); one that is unquoted but NOT in this set only warns (the
    message is older than local retention / tg-cli isn't installed here, so no quote is possible).
    ``None`` (the default, and what every caller that doesn't load history passes) means "cannot
    prove resolvability", so no reference is ever DENIED on that basis — every unquoted reference
    degrades to a warning. This keeps ``check`` pure and every legacy caller unchanged.
    """
    raw: list[Violation] = []
    warnings: list[Violation] = []
    raw.extend(_text_gates(ticket, cfg))
    # links + user-impact-quality are content rules enforced at BOTH create and close: a ticket
    # must not be CREATED carrying a bare reference / thin impact, and must not be CLOSED carrying
    # one either. Running them on the close transition catches a ticket that never passed create —
    # one made before this version, or edited directly in the GitHub/Linear web UI — so it can't be
    # closed with an un-linked HYP-789 or a "users"-thin impact. A genuine legacy exception is
    # waived on the close command with --skip-links / --skip-user-impact-quality (both wired there).
    v = links_violation(ticket, cfg)
    if v is not None:
        raw.append(v)
    v = msgref_title_violation(ticket, cfg)
    if v is not None:
        raw.append(v)
    v = links_url_violation(ticket, cfg)
    if v is not None:
        raw.append(v)
    deny, warn = msgref_quote_reports(ticket, cfg, resolvable_ids)
    if deny is not None:
        raw.append(deny)
    if warn is not None:
        warnings.append(warn)
    raw.extend(_screenshot_gate(ticket, cfg, phase))
    if phase is Phase.DONE:
        v = unchecked_criteria_violation(ticket, cfg)
        if v is not None:
            raw.append(v)
    raw.extend(_formatting_gate(ticket, cfg))
    return apply_skips(raw, ticket, warnings)


def apply_skips(raw: list[Violation], ticket: Ticket, warnings: list[Violation] | None = None) -> PolicyResult:
    """Partition raw violations into still-failing vs. (auditably) skipped ones.

    A gate whose canonical name is recorded in ``ticket.skips`` is bypassed and reported as
    skipped; everything else is a live violation. Shared by :func:`check` and the edit-time
    enforcement so the escape-hatch semantics are defined in exactly one place. ``warnings`` (the
    advisory, non-blocking channel) is carried straight onto the result so callers never have to
    remember to assign it afterwards — the invariant lives here, not at each call site.
    """
    result = PolicyResult(warnings=list(warnings) if warnings else [])
    for v in raw:
        if v.gate in ticket.skips and v.gate not in NON_SKIPPABLE_GATES:
            if v.gate not in result.skipped:
                result.skipped.append(v.gate)
        else:
            result.violations.append(v)
    return result


def _text_gates(ticket: Ticket, cfg: EnforceConfig) -> list[Violation]:
    """The structured-content gates: acceptance count, motivation, user-impact, cost-of-inaction.

    All run in every phase — a ticket can't be created OR closed without them. The user-impact
    *quality* check (rule 5) fires whenever the impact is non-empty, on create and on close alike,
    so a ticket can't be closed carrying a thin/jargon-only impact it never had to justify at
    create (e.g. one authored in the GitHub/Linear web UI). These gates are phase-agnostic, so the
    helper takes no ``phase``.
    """
    out: list[Violation] = []
    if cfg.acceptance_criteria and len(ticket.acceptance) < cfg.acceptance_min:
        out.append(
            Violation(
                GATE_ACCEPTANCE,
                f"at least {cfg.acceptance_min} acceptance criteria are required "
                f"(have {len(ticket.acceptance)})",
                hint='add --acceptance "<criterion>" (repeatable)',
            )
        )
    if cfg.motivation and not ticket.why.strip():
        out.append(Violation(GATE_MOTIVATION, "the Why (motivation) section is required", hint='add --why "..."'))
    if cfg.user_impact and not ticket.user_impact.strip():
        out.append(Violation(GATE_USER_IMPACT, "the User impact section is required", hint='add --impact "..."'))
    elif ticket.user_impact.strip():
        # the impact is present → grade its quality, on every phase (create and close), so a
        # thin/jargon-only impact can't slip through at either edge. Gated on a non-empty impact
        # so disabling the emptiness gate (user_impact=False) with an empty impact is not then
        # re-blocked here as "low quality" — an empty impact is empty, not thin.
        v = impact_quality_violation(ticket, cfg)
        if v is not None:
            out.append(v)
    if cfg.cost_of_inaction and not ticket.cost_of_inaction.strip():
        out.append(
            Violation(
                GATE_COST_OF_INACTION,
                "the Cost of inaction section is required",
                hint='add --if-not-done "..."',
            )
        )
    return out


def _screenshot_gate(ticket: Ticket, cfg: EnforceConfig, phase: Phase) -> list[Violation]:
    """The label-gated screenshot proof: a creation shot on create, an implementation shot on done."""
    required = (
        (phase is Phase.CREATE and cfg.screenshots_on_create)
        or (phase is Phase.DONE and cfg.screenshots_on_done)
    ) and _ticket_needs_screenshots(ticket, cfg)
    if not required:
        return []
    if phase is Phase.CREATE:
        # any screenshot satisfies the creation gate (the "what we want to build" proof).
        has = bool(ticket.screenshots)
        want = "creation"
    else:
        # the on-done gate demands the IMPLEMENTATION proof specifically — a creation
        # shot alone does not let you close a UI ticket. This is why the gate runs again.
        has = any(s.kind == "implementation" for s in ticket.screenshots)
        want = "implementation"
    if has:
        return []
    return [
        Violation(
            GATE_SCREENSHOTS,
            f"a {want} screenshot is required for UI/visual tickets",
            hint="add --screenshot <path>",
        )
    ]


def _formatting_gate(ticket: Ticket, cfg: EnforceConfig) -> list[Violation]:
    """The body must match the fixed section template (render is the source of truth)."""
    if not cfg.formatting:
        return []
    from .render import render

    return [Violation(GATE_FORMATTING, problem, hint="fix the section template") for problem in validate_format(render(ticket))]


def _scanned_text(ticket: Ticket) -> str:
    """The ticket text the links gate scans: title + every prose field + criterion texts, minus
    any tg#<id> quote block (see :func:`_strip_quoted_blocks`)."""
    parts = [ticket.title, ticket.what, ticket.why, ticket.user_impact, ticket.cost_of_inaction]
    parts += [c.text for c in ticket.acceptance]
    return _strip_quoted_blocks("\n".join(p for p in parts if p))


def links_violation(ticket: Ticket, cfg: EnforceConfig) -> Violation | None:
    """Rule 1: every related entity named in the ticket must be a proper LINK, not a bare token.

    Returns a single ``links`` violation listing each un-linked reference, or ``None`` when the
    text is clean or the gate is disabled. Shared by :func:`check` (create/close) and the
    edit-time enforcement, so the scan + message live in one place.
    """
    if not cfg.links:
        return None
    from .references import find_unlinked_references

    refs = find_unlinked_references(_scanned_text(ticket))
    if not refs:
        return None
    listed = "; ".join(f"{r.text} [{r.kind}]" for r in refs)
    return Violation(
        GATE_LINKS,
        f"related entities must be links, not bare references: {listed}",
        hint="make each a markdown link [text](url) or paste a full URL; waive a false positive "
        'with --skip-links "<reason>" (works on new and on close; --force "<reason>" is a '
        "create/new-only shorthand)",
    )


def msgref_title_violation(ticket: Ticket, cfg: EnforceConfig) -> Violation | None:
    """The title must never carry a ``tg#<id>`` Telegram-message reference (task 6109): a title
    is one line with no room to quote the message, and it is never linkified downstream — so the
    reference is functionally useless there and must move into a prose field. Non-skippable (see
    :data:`GATE_MSGREF_TITLE` in :data:`NON_SKIPPABLE_GATES`) — there is no legitimate reason to
    keep it in the title, only a config-level opt-out (``enforce.msgref_title: false``).

    KNOWN LIMITATION (task-cli#45 review finding): the ``tg#[1-9][0-9]*`` pattern is broad by
    design (mirrors tg-cli's own detector) and could in principle false-positive on an ordinary
    title that happens to contain that exact shape (e.g. "migrate tg#2 protocol"). Because this
    gate also runs at CLOSE (not just create — see :func:`check`) and carries no per-ticket
    ``--skip-…`` waiver, such a title would need to be rewritten (or the gate disabled globally
    via ``enforce.msgref_title: false``) before the ticket could close. Accepted: a real
    `tg#<id>` string colliding with unrelated prose is rare, and the fix (rename the title) is
    always available — unlike the quoted-message content the ``links``/``user-impact-quality``
    gates must never block on.
    """
    if not cfg.msgref_title:
        return None
    from .msgrefs import title_msgrefs

    refs = title_msgrefs(ticket.title)
    if not refs:
        return None
    listed = ", ".join(f"tg#{r}" for r in refs)
    return Violation(
        GATE_MSGREF_TITLE,
        f"the title contains a Telegram message reference ({listed}) — move it into the body",
        hint='rewrite --title without the tg#<id>, and add "per tg#<id>…" to --what/--why/etc. instead',
    )


def msgref_quote_scan_fields(ticket: Ticket) -> tuple[str, ...]:
    """The prose fields the ``msgref-quote`` gate scans — the four the auto-expander appends a
    quote INTO (what / why / user_impact / cost_of_inaction). Deliberately NOT the title (a title
    reference is the ``msgref-title`` gate's job; a title has no room for a quote) and NOT the
    acceptance criteria (each serialized as a single ``- [ ] <text>`` line — msgrefs' module
    docstring forbids a multi-line quote there, it does not survive the render/parse round-trip).

    Returned as separate fields, NEVER joined: the expander appends a quote into the SAME field
    the reference sits in, so scanning a concatenation would let a quote for ``tg#42`` in ``why``
    mask a still-unquoted ``tg#42`` in ``what`` (and a stray ``> …`` line in one field followed by
    a marker in the next could even form a spurious cross-field quote block). :func:`unquoted_body_ids`
    scans each field independently for exactly this reason."""
    return (ticket.what, ticket.why, ticket.user_impact, ticket.cost_of_inaction)


def unquoted_body_ids(ticket: Ticket) -> list[int]:
    """Every ``tg#<id>`` referenced in a scanned prose field that lacks its GENERATED quote block,
    scanned PER FIELD (see :func:`msgref_quote_scan_fields`) and de-duplicated across fields in
    first-appearance order. Pure — the resolvability question (deny vs warn) is the caller's."""
    seen: set[int] = set()
    ordered: list[int] = []
    for field_text in msgref_quote_scan_fields(ticket):
        if not field_text:
            continue
        for i in _unquoted_msgrefs(field_text):
            if i not in seen:
                seen.add(i)
                ordered.append(i)
    return ordered


def _msgref_quote_deny(ids: list[int]) -> Violation:
    listed = ", ".join(f"tg#{i}" for i in ids)
    return Violation(
        GATE_MSGREF_QUOTE,
        f"the body references {listed} but the quoted message content is not attached "
        "(the message IS in local tg-cli history, so the quote can and must be inlined)",
        hint="re-run the create/change so the quote is auto-attached (e.g. `task change <id> "
        '--what "$(current what)"`), or waive a false positive with --skip-msgref-quote "<reason>"',
    )


def _msgref_quote_warn(ids: list[int]) -> Violation:
    listed = ", ".join(f"tg#{i}" for i in ids)
    return Violation(
        GATE_MSGREF_QUOTE,
        f"the body references {listed} but no quote could be attached — not in local tg-cli "
        "history (older than local retention, or tg-cli is not installed here)",
        hint="none required; the reference is kept as-is (the message predates local history)",
    )


def msgref_quote_reports(
    ticket: Ticket, cfg: EnforceConfig, resolvable_ids: set[int] | None
) -> tuple[Violation | None, Violation | None]:
    """Split the body's UNQUOTED ``tg#<id>`` references into a blocking violation + an advisory
    warning. Returns ``(deny, warn)`` — each a single ``msgref-quote`` :class:`Violation` or
    ``None``:

    - ``deny`` lists ids that are unquoted YET resolvable in ``resolvable_ids`` — the quote COULD
      have been attached (re-run create/change so the expander appends it) and wasn't, so the
      ticket is refused until it is.
    - ``warn`` lists ids that are unquoted AND unresolvable (older than local retention, or
      ``resolvable_ids`` is ``None`` because the caller loaded no history) — nothing can be quoted,
      so it is surfaced but never blocks.

    A ticket created/changed through task-cli never trips this: the auto-expander
    (``cli._expand_msgrefs``) always appends a block — real, or a "not found" note that still
    carries the provenance marker — BEFORE the gate runs, so :func:`_unquoted_msgrefs` finds
    nothing. The gate is the backstop for a ticket authored elsewhere (the GitHub/Linear web UI)
    or before this feature existed, caught at its next create/close/edit through task-cli.
    """
    if not cfg.msgref_quote:
        return None, None
    unquoted = unquoted_body_ids(ticket)
    if not unquoted:
        return None, None
    # A recorded --skip-msgref-quote waives the gate. Emit the deny UNCONDITIONALLY (resolvability
    # is irrelevant once waived, and the CLI skips loading history in this case) so it flows
    # through apply_skips into ``PolicyResult.skipped`` — otherwise this gate would be the one
    # skippable gate that never shows in the "skipped gates (justified)" audit line (review
    # finding). The advisory warning is suppressed in the same breath; the deny becomes the
    # audit record instead.
    if GATE_MSGREF_QUOTE in ticket.skips:
        return _msgref_quote_deny(unquoted), None
    resolvable = resolvable_ids or set()
    deny_ids = [i for i in unquoted if i in resolvable]
    warn_ids = [i for i in unquoted if i not in resolvable]
    deny = _msgref_quote_deny(deny_ids) if deny_ids else None
    warn = _msgref_quote_warn(warn_ids) if warn_ids else None
    return deny, warn


def _safe_display(s: str) -> str:
    """Drop control/format characters so an untrusted link key/value can't inject a terminal
    escape (ANSI colour, bidi override) into a diagnostic printed to the console or forwarded to
    a Telegram notification. ``str.isprintable()`` is False for exactly those (Cc/Cf/Zl/Zp)."""
    return "".join(ch for ch in s if ch.isprintable())


def _redact_link_display(s: str) -> str:
    """Sanitize a link key/value for a diagnostic echoed to the console / a notification: run the
    SHARED token redactor (:func:`tasklib.logging.redact`, the same one that scrubs log lines) so a
    token-shaped substring (`ghp_…`, `lin_api_…`, a long opaque secret) becomes ``<redacted>``,
    then strip control chars (terminal-injection). Applied to BOTH key and value, honouring the
    repo's no-token-output posture rather than re-inventing redaction here (review finding)."""
    from .logging import redact

    return _safe_display(redact(s))


def _redact_link_value_display(key: str, value: str) -> str:
    """Like :func:`_redact_link_display` for a link VALUE, but also KEY-aware: mirrors
    :func:`tasklib.logging._redact_fields`'s secret-KEY masking (``api_key``, ``token``,
    ``secret``, …), not just the value-shape regex. A value-shape-only check misses a
    non-gh/lin_api/sk-/Bearer-shaped secret (e.g. a Slack ``xoxb-…`` token) sitting under an
    obviously-sensitive key name — the key name alone is enough signal to redact wholesale
    rather than echo it back in a violation message (review finding)."""
    from .logging import _REDACTED, _SECRET_KEY_RE

    if _SECRET_KEY_RE.search(key):
        return _REDACTED
    return _redact_link_display(value)


def _is_valid_link_key(key: str) -> bool:
    """A Links KEY must round-trip through render→parse and be safe to echo. ``render._render_links``
    writes ``- {key}: {value}`` and ``render._parse_links`` splits on the FIRST ``:``, so a key
    containing ``:`` reparses as ``{first: 'rest: value'}`` — the ticket passes the gate before
    persistence and FAILS after a backend round-trip (review finding). A key must therefore be
    non-empty, printable (no control/bidi injection), and free of ``:``."""
    return bool(key.strip()) and _safe_display(key) == key and key == key.strip() and ":" not in key


def _is_http_url(value: str) -> bool:
    """A Links value is valid iff it is an absolute http/https URL with a real host and no
    embedded credentials. Uses :func:`urllib.parse.urlparse` (not a regex) so an odd-but-real
    URL — query strings, ports — is accepted while a bare token is not: ``session:2`` parses to
    scheme ``session`` (rejected — only http/https pass), a Linear key ``HYP-1001`` has an empty
    scheme, and a schemeless ``foo`` has an empty netloc. Additionally rejects malformed shapes
    that still carry an http scheme (review finding): any internal whitespace (a real URL has
    none, so ``https://Session: session:2`` is out) and a netloc with no alphanumeric host
    character (``https://``, ``https://.``, ``https://:`` are out)."""
    stripped = value.strip()
    # A real URL is printable with no whitespace: reject internal whitespace AND any non-printable
    # control/format character (NUL, ESC, bidi overrides), which would otherwise pass the scheme
    # check and then render verbatim into the ticket body / a violation message / a TG notification
    # (review finding). ``str.isprintable()`` is False for any Cc/Cf/Zl/Zp char (space excluded,
    # but we already rejected whitespace above).
    if not stripped or not stripped.isprintable() or any(ch.isspace() for ch in stripped):
        return False
    from urllib.parse import urlparse

    try:
        parsed = urlparse(stripped)
        _ = parsed.port  # property access validates port syntax (raises ValueError if malformed)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    # Reject embedded userinfo (`https://user:ghp_xxx@host/...`) outright: this gate persists the
    # value verbatim into a GitHub/Linear issue body, so an accepted credential-shaped userinfo
    # segment leaks it into a shared, often-public tracker (review finding). There is no
    # legitimate reason for a ticket Link to carry embedded auth.
    if parsed.username is not None or parsed.password is not None:
        return False
    # A credential can also ride in the path/query/fragment, not just userinfo
    # (`https://example.com/download?token=ghp_...`, `.../ghp_.../artifact`) — this gate
    # persists the value verbatim, so that leaks it into a shared, often-public tracker the
    # same as embedded userinfo does (review finding). Reuse the shared token-shaped-value
    # detector (gh/Linear/OpenAI-style opaque tokens, `Bearer ...`) rather than duplicating
    # its pattern here.
    from .logging import _SECRET_VALUE_RE

    if _SECRET_VALUE_RE.search(stripped):
        return False
    # Require a real host: `https://` (empty), `https://user@` / `https://user@:443` (userinfo
    # only, empty host), and `https://.` (no alphanumeric host character) are all rejected (review
    # finding). `parsed.hostname` strips any userinfo/port, leaving the host alone to inspect.
    host = parsed.hostname or ""
    return any(ch.isalnum() for ch in host)


def links_url_violation(ticket: Ticket, cfg: EnforceConfig) -> Violation | None:
    """The Links section must hold only real URLs — never a bare, non-URL value like
    ``Session: session:2`` (CTO report tg#9179). That junk renders as ``- key: value`` and slides
    past the ``links`` gate entirely (which scans PROSE for bare references, never the structured
    Links field), so it surfaces in the ticket as a fake link. Returns one ``links-url`` violation
    listing every offending ``key: value`` pair, or ``None`` when every value is a URL or the gate
    is disabled. Skippable (``--skip-links-url``) for a legacy ticket read back from the backend
    whose Links can't be fixed in one step."""
    if not cfg.links_url:
        return None
    # An entry is bad if its value isn't a URL OR its key is not a valid link key — the key is
    # rendered verbatim by render._render_links (`- key: value`) and split back on the first `:`
    # by render._parse_links, so a key with a control/bidi escape (terminal injection into `task
    # read`) or an embedded `:` (breaks the round-trip) is rejected even when its value is a valid
    # URL (review finding). See :func:`_is_valid_link_key`.
    bad = [(k, v) for k, v in ticket.links.items() if not _is_http_url(v) or not _is_valid_link_key(k)]
    if not bad:
        return None
    # Strip control/format chars from BOTH the key and the value before echoing them: the value
    # is validated but a REJECTED one still reaches this message, and the KEY is never validated
    # at all — either could carry an ANSI/bidi escape that would inject into the console output or
    # a TG notification (review finding).
    listed = "; ".join(f"{_redact_link_display(k)}: {_redact_link_value_display(k, v)}" for k, v in bad)
    # A value that IS a well-formed https:// URL but was rejected for carrying an embedded
    # credential gets a message tailored to that reason — "not a URL" is factually wrong for
    # it and gives no hint the real problem is the embedded credential (review finding).
    from urllib.parse import urlparse

    from .logging import _SECRET_VALUE_RE

    def _is_credential_reject(value: str) -> bool:
        if not value or not value.isprintable() or any(ch.isspace() for ch in value):
            return False
        try:
            parsed = urlparse(value)
        except ValueError:
            return False
        if parsed.scheme not in ("http", "https"):
            return False
        return parsed.username is not None or parsed.password is not None or bool(_SECRET_VALUE_RE.search(value))

    if any(_is_credential_reject(v) for _, v in bad):
        message = f"the Links section must not contain a value with an embedded credential: {listed}"
        hint = "remove the embedded token/credential from the URL (a ticket Link is never the place for " \
            'one); waive a legacy value with --skip-links-url "<reason>"'
    else:
        message = f"the Links section must contain only URLs (http(s)://…), not bare values: {listed}"
        hint = (
            "use a full https:// URL for each link (a tg#<id> reference belongs in a body prose "
            'field, not Links); waive a legacy value with --skip-links-url "<reason>"'
        )
    return Violation(GATE_LINKS_URL, message, hint=hint)


def impact_quality_violation(ticket: Ticket, cfg: EnforceConfig) -> Violation | None:
    """Rule 5: the user-impact must be plain-language and user-framed (only when non-empty)."""
    if not cfg.user_impact_quality or not ticket.user_impact.strip():
        return None
    from .quality import assess_user_impact

    problems = assess_user_impact(_strip_quoted_blocks(ticket.user_impact))
    if not problems:
        return None
    return Violation(
        GATE_USER_IMPACT_QUALITY,
        "user impact is not plain-language/user-framed: " + problems[0],
        hint="rewrite the impact in the user's terms (--impact on new, `task change --impact` on "
        'an existing ticket); waive with --skip-user-impact-quality "<reason>" (works on new and '
        'on close; --force "<reason>" is a create/new-only shorthand) if truly N/A',
    )


def unchecked_criteria_violation(ticket: Ticket, cfg: EnforceConfig) -> Violation | None:
    """Rules 2 + 3 at close: a ticket can close only when EVERY criterion is checked AND each
    checked one carries a visual proof (or a recorded ``force_reason``). Not skippable.

    The proof half matters because a ``- [x]`` ticked in the GitHub/Linear web UI round-trips as
    ``checked`` with an empty ``proof`` — the close gate is the backstop that still demands the
    proof the ``task check`` command would have required.
    """
    if not cfg.acceptance_checked:
        return None
    unchecked = [c for c in ticket.acceptance if not c.checked]
    unproven = [c for c in ticket.acceptance if c.checked and not c.proof and not c.force_reason]
    if not unchecked and not unproven:
        return None
    parts: list[str] = []
    if unchecked:
        parts.append(f"{len(unchecked)} unchecked: " + "; ".join(c.text for c in unchecked))
    if unproven:
        parts.append(f"{len(unproven)} checked without a visual proof: " + "; ".join(c.text for c in unproven))
    return Violation(
        GATE_ACCEPTANCE_CHECKED,
        "a ticket closes only when every criterion is checked WITH a proof — " + " | ".join(parts),
        hint="check each with a visual proof: task check <id> <n> --proof <path>",
    )


def check_create(ticket: Ticket, cfg: EnforceConfig, resolvable_ids: set[int] | None = None) -> PolicyResult:
    return check(ticket, cfg, Phase.CREATE, resolvable_ids=resolvable_ids)


def check_done(ticket: Ticket, cfg: EnforceConfig, resolvable_ids: set[int] | None = None) -> PolicyResult:
    return check(ticket, cfg, Phase.DONE, resolvable_ids=resolvable_ids)


def _ticket_needs_screenshots(ticket: Ticket, cfg: EnforceConfig) -> bool:
    """Screenshots are label-gated: required only when the ticket carries a gating label."""
    ticket_labels = {label.strip().lower() for label in ticket.labels}
    return bool(ticket_labels & cfg.screenshot_labels)


def normalize_skip_gate(name: str) -> str:
    """Normalize a ``--skip-<gate>`` suffix to a canonical gate name. Raises on unknown."""
    norm = name.strip().lower().replace("_", "-")
    aliases = {
        "acceptance": GATE_ACCEPTANCE,
        "acceptance-criteria": GATE_ACCEPTANCE,
        "motivation": GATE_MOTIVATION,
        "why": GATE_MOTIVATION,
        "user-impact": GATE_USER_IMPACT,
        "impact": GATE_USER_IMPACT,
        "cost-of-inaction": GATE_COST_OF_INACTION,
        "if-not-done": GATE_COST_OF_INACTION,
        "screenshots": GATE_SCREENSHOTS,
        "screenshot": GATE_SCREENSHOTS,
        "formatting": GATE_FORMATTING,
        "format": GATE_FORMATTING,
        "links": GATE_LINKS,
        "link": GATE_LINKS,
        "user-impact-quality": GATE_USER_IMPACT_QUALITY,
        "impact-quality": GATE_USER_IMPACT_QUALITY,
        # NB: no bare "msgref" alias — it would silently map to msgref-QUOTE, but a user typing
        # --skip-msgref most likely means the (non-skippable) msgref-TITLE gate; a silent no-op
        # against the wrong gate is worse than an "unknown gate" error (review finding).
        "msgref-quote": GATE_MSGREF_QUOTE,
        "links-url": GATE_LINKS_URL,
        "link-url": GATE_LINKS_URL,
    }
    if norm in aliases:
        return aliases[norm]
    raise ValueError(f"unknown gate {name!r} (valid: {', '.join(ALL_GATES)})")
