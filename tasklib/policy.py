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

ALL_GATES: tuple[str, ...] = (
    GATE_ACCEPTANCE,
    GATE_MOTIVATION,
    GATE_USER_IMPACT,
    GATE_COST_OF_INACTION,
    GATE_SCREENSHOTS,
    GATE_FORMATTING,
)


class Phase(str, Enum):
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

        return cls(
            acceptance_criteria=_on("acceptance_criteria"),
            motivation=_on("motivation"),
            user_impact=_on("user_impact"),
            cost_of_inaction=_on("cost_of_inaction"),
            formatting=_on("formatting"),
            screenshots_on_create=soc,
            screenshots_on_done=sod,
            screenshot_labels=frozenset(labels) if labels else cls.screenshot_labels,
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

    if cfg.acceptance_criteria and not ticket.acceptance:
        raw.append(
            Violation(
                GATE_ACCEPTANCE,
                "at least one acceptance criterion is required",
                hint='add --acceptance "<criterion>" (repeatable)',
            )
        )
    if cfg.motivation and not ticket.why.strip():
        raw.append(Violation(GATE_MOTIVATION, "the Why (motivation) section is required", hint='add --why "..."'))
    if cfg.user_impact and not ticket.user_impact.strip():
        raw.append(Violation(GATE_USER_IMPACT, "the User impact section is required", hint='add --impact "..."'))
    if cfg.cost_of_inaction and not ticket.cost_of_inaction.strip():
        raw.append(
            Violation(
                GATE_COST_OF_INACTION,
                "the Cost of inaction section is required",
                hint='add --if-not-done "..."',
            )
        )

    screenshots_required = (
        (phase is Phase.CREATE and cfg.screenshots_on_create)
        or (phase is Phase.DONE and cfg.screenshots_on_done)
    ) and _ticket_needs_screenshots(ticket, cfg)
    if screenshots_required:
        if phase is Phase.CREATE:
            # any screenshot satisfies the creation gate (the "what we want to build" proof).
            has = bool(ticket.screenshots)
            want = "creation"
        else:
            # the on-done gate demands the IMPLEMENTATION proof specifically — a creation
            # shot alone does not let you close a UI ticket. This is why the gate runs again.
            has = any(s.kind == "implementation" for s in ticket.screenshots)
            want = "implementation"
        if not has:
            raw.append(
                Violation(
                    GATE_SCREENSHOTS,
                    f"a {want} screenshot is required for UI/visual tickets",
                    hint="add --screenshot <path>",
                )
            )

    if cfg.formatting:
        # validate the body that WOULD be written (render is the source of truth)
        from .render import render

        for problem in validate_format(render(ticket)):
            raw.append(Violation(GATE_FORMATTING, problem, hint="fix the section template"))

    # apply escape hatches: a gate with a recorded justification is bypassed (audited).
    result = PolicyResult()
    for v in raw:
        if v.gate in ticket.skips:
            if v.gate not in result.skipped:
                result.skipped.append(v.gate)
        else:
            result.violations.append(v)
    return result


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
    }
    if norm in aliases:
        return aliases[norm]
    raise ValueError(f"unknown gate {name!r} (valid: {', '.join(ALL_GATES)})")
