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
from .render import validate_format

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

# The minimum number of acceptance criteria a ticket must declare (rule: a real ticket has
# more than one provable outcome). Overridable via `enforce.acceptance_min`.
DEFAULT_ACCEPTANCE_MIN = 2

# Gates that NO recorded justification can waive — a hard refuse. A ticket must not be closed
# with an unchecked criterion under any escape hatch; only disabling the gate in config removes it.
NON_SKIPPABLE_GATES: frozenset[str] = frozenset({GATE_ACCEPTANCE_CHECKED})

ALL_GATES: tuple[str, ...] = (
    GATE_ACCEPTANCE,
    GATE_MOTIVATION,
    GATE_USER_IMPACT,
    GATE_USER_IMPACT_QUALITY,
    GATE_COST_OF_INACTION,
    GATE_SCREENSHOTS,
    GATE_FORMATTING,
    GATE_LINKS,
    GATE_ACCEPTANCE_CHECKED,
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
    """The outcome of running the gates. ``ok`` iff no un-skipped violations remain."""

    violations: list[Violation] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # gates a justification bypassed

    @property
    def ok(self) -> bool:
        return not self.violations


def check(ticket: Ticket, cfg: EnforceConfig, phase: Phase) -> PolicyResult:
    """Run all active gates for ``phase``, honoring per-gate escape hatches on the ticket."""
    raw: list[Violation] = []
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
    raw.extend(_screenshot_gate(ticket, cfg, phase))
    if phase is Phase.DONE:
        v = unchecked_criteria_violation(ticket, cfg)
        if v is not None:
            raw.append(v)
    raw.extend(_formatting_gate(ticket, cfg))
    return apply_skips(raw, ticket)


def apply_skips(raw: list[Violation], ticket: Ticket) -> PolicyResult:
    """Partition raw violations into still-failing vs. (auditably) skipped ones.

    A gate whose canonical name is recorded in ``ticket.skips`` is bypassed and reported as
    skipped; everything else is a live violation. Shared by :func:`check` and the edit-time
    enforcement so the escape-hatch semantics are defined in exactly one place.
    """
    result = PolicyResult()
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
    """The ticket text the links gate scans: title + every prose field + criterion texts."""
    parts = [ticket.title, ticket.what, ticket.why, ticket.user_impact, ticket.cost_of_inaction]
    parts += [c.text for c in ticket.acceptance]
    return "\n".join(p for p in parts if p)


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


def impact_quality_violation(ticket: Ticket, cfg: EnforceConfig) -> Violation | None:
    """Rule 5: the user-impact must be plain-language and user-framed (only when non-empty)."""
    if not cfg.user_impact_quality or not ticket.user_impact.strip():
        return None
    from .quality import assess_user_impact

    problems = assess_user_impact(ticket.user_impact)
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


def check_create(ticket: Ticket, cfg: EnforceConfig) -> PolicyResult:
    return check(ticket, cfg, Phase.CREATE)


def check_done(ticket: Ticket, cfg: EnforceConfig) -> PolicyResult:
    return check(ticket, cfg, Phase.DONE)


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
    }
    if norm in aliases:
        return aliases[norm]
    raise ValueError(f"unknown gate {name!r} (valid: {', '.join(ALL_GATES)})")
