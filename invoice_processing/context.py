"""The canonical case context: one builder, one field registry.

Rule conditions are evaluated against a flattened "context" dict built from the
pipeline's stage artifacts. Until now that dict was built twice, by two
independent copies of the same logic:

  * ``ActingAgentAdapter._promote_convenience_fields`` -- used by the live ALF
    engine when it decides whether a rule fires.
  * ``CaseLoaderAgent._build_context`` -- used by the impact assessor when it
    predicts which cases a proposed rule *would* affect.

Its docstring even said it "replicates the context-building logic from ALF ...
to guarantee field-path compatibility". A copy maintained by hand is a copy that
drifts, and this one already had: the loader overrode ``has_waf`` from the phase
1 artifact while the engine derived it from raw extraction evidence. The two
therefore could disagree about the same case -- meaning the impact report an SME
approves a rule against might not match what the rule actually does in
production. That undermines the whole "measured blast radius" guarantee.

This module is the single builder both now use.

Two further problems it closes:

  * The list of fields a rule may reference lived as hand-written prose in
    ``core/prompts.py`` and was fed to the rule-writing LLM. Nothing kept it in
    step with the builder, so it could advertise fields that did not exist.
    ``describe_fields()`` now generates that text from the same registry the
    builder populates.
  * Nothing validated a rule's ``field`` path. ``resolve_field`` returns None for
    an unknown path, the condition quietly evaluates to False, and the rule
    simply never fires -- silently, forever. ``validate_field_path()`` makes that
    a write-time error instead.

Phase 0.5 moves this module into the extracted ``invoice_core`` package; the
public surface (``build_case_context``, ``describe_fields``,
``validate_field_path``) is intended to survive that move unchanged.
"""

from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Field registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldSpec:
    """One field a rule condition may reference."""

    path: str
    type: str  # "str" | "bool" | "int" | "float" | "list" | "dict"
    description: str
    source: str  # where the value comes from -- shown to the rule author
    group: str = "Top-level"
    operators: tuple[str, ...] = field(default_factory=tuple)
    note: str = ""


# Ordered for presentation; grouping mirrors how a rule author thinks.
FIELDS: tuple[FieldSpec, ...] = (
    # --- Phase decisions -------------------------------------------------
    FieldSpec(
        "decision_phase1",
        "str",
        'Phase 1 decision: "CONTINUE" or "REJECT"',
        "phase1.decision",
    ),
    FieldSpec(
        "decision_phase2",
        "str",
        "Phase 2 decision",
        "phase2.decision",
    ),
    FieldSpec(
        "decision_phase3",
        "str",
        'Phase 3 decision: "CONTINUE" or "REJECT"',
        "phase3.decision",
    ),
    FieldSpec(
        "decision_phase4",
        "str",
        'Phase 4 decision: "ACCEPT" or "REJECT"',
        "phase4.decision",
    ),
    # --- Work authorization ----------------------------------------------
    FieldSpec(
        "has_waf",
        "bool",
        "WAF present, per raw extraction evidence",
        "extraction.waf_count > 0",
        note=(
            "Raw evidence. This is what the live engine uses; prefer it unless "
            "you specifically mean the phase 1 view."
        ),
    ),
    FieldSpec(
        "waf_count",
        "int",
        "Number of WAFs detected",
        "extraction.waf_count",
    ),
    FieldSpec(
        "waf_present_phase1",
        "bool",
        "WAF presence as recorded by phase 1, after exemption logic",
        "phase1.has_waf",
        note=(
            "None when phase 1 does not record it, which is currently always. "
            "Use has_waf unless you specifically need the phase 1 view."
        ),
    ),
    FieldSpec(
        "waf_exempt",
        "bool",
        "Work type is WAF-exempt",
        "phase1.waf_exempt",
    ),
    FieldSpec(
        "waf_exempt_reason",
        "str",
        "Exemption reason",
        "phase1.waf_exempt_reason",
    ),
    FieldSpec(
        "work_type",
        "str",
        "Detected work type",
        "phase1.work_type",
        note="Currently always None: phase 1 does not persist work_type.",
    ),
    # --- Line items -------------------------------------------------------
    FieldSpec(
        "line_items_mapped",
        "list",
        "Transformed line items",
        "transformer.line_items_mapped",
        operators=(
            "any_item_contains",
            "length_equals",
            "length_greater",
            "length_less",
        ),
    ),
    # --- Phase artifacts --------------------------------------------------
    FieldSpec(
        "phase1.rejection_template",
        "str",
        "Phase 1 rejection text",
        "phase1 artifact",
        group="Phase artifacts",
        operators=("contains", "not_contains", "regex_match"),
    ),
    FieldSpec(
        "phase1.validations",
        "list",
        "Array of step results",
        "phase1 artifact",
        group="Phase artifacts",
    ),
    FieldSpec(
        "phase2.rejection_template",
        "str",
        "Phase 2 rejection text",
        "phase2 artifact",
        group="Phase artifacts",
        operators=("contains", "not_contains", "regex_match"),
    ),
    FieldSpec(
        "phase3.rejection_template",
        "str",
        "Phase 3 rejection text",
        "phase3 artifact",
        group="Phase artifacts",
        operators=("contains", "not_contains", "regex_match"),
    ),
    FieldSpec(
        "phase3.rejection_reason",
        "str",
        "Phase 3 rejection reason",
        "phase3 artifact",
        group="Phase artifacts",
        operators=("contains", "not_contains", "regex_match"),
    ),
    FieldSpec(
        "phase4.rejection_template",
        "str",
        "Phase 4 rejection text",
        "phase4 artifact",
        group="Phase artifacts",
        operators=("contains", "not_contains", "regex_match"),
    ),
    # --- Invoice ----------------------------------------------------------
    FieldSpec(
        "invoice.vendor_name",
        "str",
        "Vendor name",
        "extraction.invoice",
        group="Invoice",
    ),
    FieldSpec(
        "invoice.customer_name",
        "str",
        "Customer name",
        "extraction.invoice",
        group="Invoice",
    ),
    FieldSpec(
        "invoice.vendor_tax_id",
        "str",
        "Vendor tax ID (e.g. ABN)",
        "extraction.invoice",
        group="Invoice",
    ),
    FieldSpec(
        "invoice.invoice_total_inc_tax",
        "float",
        "Total including tax",
        "extraction.invoice",
        group="Invoice",
    ),
    FieldSpec(
        "invoice.invoice_total_ex_tax",
        "float",
        "Total excluding tax",
        "extraction.invoice",
        group="Invoice",
    ),
    FieldSpec(
        "invoice.tax_amount",
        "float",
        "Tax amount",
        "extraction.invoice",
        group="Invoice",
    ),
    FieldSpec(
        "invoice.currency",
        "str",
        "Currency code (AUD, USD, EUR)",
        "extraction.invoice",
        group="Invoice",
    ),
    FieldSpec(
        "invoice.invoice_date",
        "str",
        "Invoice date (YYYY-MM-DD)",
        "extraction.invoice",
        group="Invoice",
    ),
    FieldSpec(
        "invoice.line_items",
        "list",
        "Line item objects",
        "extraction.invoice",
        group="Invoice",
        operators=(
            "any_item_contains",
            "length_equals",
            "length_greater",
            "length_less",
        ),
    ),
    FieldSpec(
        "invoice._tax_id_validation.valid",
        "bool",
        "Tax ID checksum result",
        "extraction.invoice",
        group="Invoice",
    ),
    # --- Raw artifacts ----------------------------------------------------
    FieldSpec(
        "extraction.invoice.line_items",
        "list",
        "Line item objects (raw)",
        "extraction artifact",
        group="Raw artifacts",
        operators=(
            "any_item_contains",
            "length_equals",
            "length_greater",
            "length_less",
        ),
    ),
    FieldSpec(
        "extraction.waf_count",
        "int",
        "Number of WAFs detected",
        "extraction artifact",
        group="Raw artifacts",
    ),
    FieldSpec(
        "extraction.extraction_failed",
        "bool",
        "Whether extraction failed",
        "extraction artifact",
        group="Raw artifacts",
    ),
    FieldSpec(
        "decision",
        "str",
        "Invoice Status from postprocessing output",
        "postprocessing",
        group="Raw artifacts",
    ),
)

_BY_PATH = {spec.path: spec for spec in FIELDS}

# Artifact keys that always exist as context roots, so a dotted path beneath one
# of them is plausible even if not individually registered.
ARTIFACT_ROOTS = (
    "classification",
    "extraction",
    "phase1",
    "phase2",
    "phase3",
    "phase4",
    "transformer",
    "decision",
    "audit_log",
    "postprocessing",
    "invoice",
    "work_authorization",
)


class UnknownFieldPath(ValueError):
    """A rule referenced a field path the context never provides."""


# ---------------------------------------------------------------------------
# The builder
# ---------------------------------------------------------------------------


def promote_convenience_fields(context: dict) -> None:
    """Flatten commonly-referenced nested values to the top level, in place.

    This is the single definition of how a context is derived from artifacts.
    """
    extraction = context.get("extraction")
    if extraction:
        context["invoice"] = extraction.get("invoice", {})
        context["work_authorization"] = extraction.get("work_authorization", {})
        context["waf_count"] = extraction.get("waf_count", 0)
        # Raw evidence. Deliberately NOT overridden by phase 1 -- see
        # waf_present_phase1 below and this module's docstring.
        context["has_waf"] = extraction.get("waf_count", 0) > 0

    phase1 = context.get("phase1")
    if phase1:
        context["decision_phase1"] = phase1.get("decision")
        context["work_type"] = phase1.get("work_type")
        # Phase 1's own view, exposed under a distinct name so a rule author
        # chooses explicitly instead of one builder silently overriding the
        # other. Currently always None: phase 1 does not record these.
        context["waf_present_phase1"] = phase1.get("has_waf")
        context["waf_exempt"] = phase1.get("waf_exempt")
        context["waf_exempt_reason"] = phase1.get("waf_exempt_reason")

    for phase in ("phase2", "phase3", "phase4"):
        artifact = context.get(phase)
        if artifact:
            context[f"decision_{phase}"] = artifact.get("decision")

    transformer = context.get("transformer")
    if transformer:
        context["line_items_mapped"] = transformer.get("line_items_mapped", [])


def build_case_context(artifacts: dict) -> dict:
    """Build a rule-evaluation context from stage artifacts.

    Pure: dict in, dict out. No filesystem access, so the same function serves
    the live engine, the impact assessor, and tests.
    """
    context = dict(artifacts)
    promote_convenience_fields(context)
    return context


# ---------------------------------------------------------------------------
# Introspection and validation
# ---------------------------------------------------------------------------


def describe_fields() -> str:
    """Render the field registry as the prompt block shown to a rule author.

    Generated, so it cannot advertise a field the builder does not set.
    """
    lines = ["=== AVAILABLE CONTEXT FIELDS (dot-notation paths) ==="]
    width = max(len(spec.path) for spec in FIELDS) + 2

    for group in dict.fromkeys(spec.group for spec in FIELDS):
        lines.append(f"\n{group}:")
        for spec in (s for s in FIELDS if s.group == group):
            entry = f"  {spec.path:<{width}} - {spec.description}"
            if spec.operators:
                entry += f" (use: {', '.join(spec.operators)})"
            lines.append(entry)
            if spec.note:
                lines.append(f"  {'':<{width}}   NOTE: {spec.note}")
    return "\n".join(lines)


def known_field_paths() -> set[str]:
    return set(_BY_PATH)


def validate_field_path(path: str, operator: str | None = None) -> None:
    """Raise UnknownFieldPath if a rule could never match on this field.

    An unregistered path is not automatically wrong -- artifacts carry more
    fields than are worth listing -- so a dotted path beneath a known artifact
    root is accepted. A bare unknown name is rejected: that is the case that
    silently never fires.
    """
    if not path or not isinstance(path, str):
        raise UnknownFieldPath(
            f"Field path must be a non-empty string, got {path!r}"
        )
    if ".." in path or path.startswith(".") or path.endswith("."):
        raise UnknownFieldPath(f"Malformed field path: {path!r}")

    spec = _BY_PATH.get(path)
    if spec is not None:
        if operator and spec.operators and operator not in spec.operators:
            raise UnknownFieldPath(
                f"Operator {operator!r} is not valid for {path!r} "
                f"(type {spec.type}); use one of: {', '.join(spec.operators)}"
            )
        return

    root = path.split(".", 1)[0]
    if root in ARTIFACT_ROOTS:
        return

    close = sorted(p for p in _BY_PATH if root in p or p.startswith(root[:4]))
    hint = f" Did you mean one of: {', '.join(close[:5])}?" if close else ""
    raise UnknownFieldPath(
        f"Unknown field path {path!r}. A condition on an unknown field always "
        f"evaluates to False, so the rule would never fire.{hint}"
    )


def validate_rule_base(doc: dict) -> None:
    """Refuse a rule base containing a condition that can never fire.

    An unresolvable field path returns None and the condition evaluates False,
    so the rule is silently inert -- approved, listed, and doing nothing. The
    rule writer has always checked this; a repository save had not, so a rule
    written through the API or a migration bypassed the check entirely.

    Disabled rules are checked too: a rule is disabled to be re-enabled later,
    and storing one that cannot work is storing a surprise for whoever does.
    """
    for rule in doc.get("rules") or []:
        rule_id = rule.get("id") or rule.get("rule_id") or "<unnamed>"
        for index, condition in enumerate(rule.get("conditions") or []):
            field = condition.get("field")
            if field is None:
                continue
            try:
                validate_field_path(field, condition.get("operator"))
            except UnknownFieldPath as exc:
                raise UnknownFieldPath(
                    f"Rule {rule_id}, condition {index}: {exc}"
                ) from exc
