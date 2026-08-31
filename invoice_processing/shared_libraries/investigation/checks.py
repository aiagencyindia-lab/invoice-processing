"""Deterministic critic checks (Tier 0).

The investigation layer's "Layer 1" was described as deterministic but was not:
it made six `gemini-2.5-pro` calls per case, one per validator. Two things made
those calls close to worthless:

  * Every prompt is prefixed with INVESTIGATOR_GUIDELINES, which instructs the
    model to "mark as COMPLIANT (valid=true)" whenever in doubt, to give
    "MAXIMUM BENEFIT OF THE DOUBT", and to return compliant below 90%
    confidence. The output is therefore close to a constant.
  * Each validator's exception handler also returns ``valid: True``, so a failed
    call is indistinguishable from a passing one.

The checks in this module compute the same verdicts arithmetically. They are
free, instant, reproducible, and -- because they cite the exact values they
compared -- produce a *better* audit record than a model's narrative. That
matters: the product's differentiator is the explanation, not the fact that a
large model was consulted.

Each function returns the same dict shape as the LLM validator it replaces, so
callers need no changes.

Judgment that genuinely needs a model (vendor trading names, work-type
semantics) stays on the LLM path.
"""

from typing import Any

try:
    from ... import tax_ids
except ImportError:  # executed as a script
    from invoice_processing import tax_ids

# Australian Business Number: 11 digits, weighted mod-89 checksum.
_ABN_LENGTH = 11

# Matches the acting pipeline's hours_tolerance default.
_DEFAULT_HOURS_TOLERANCE = 0.5


def normalize_tax_id(value: Any) -> str:
    """Strip formatting so '51 824 753 556' and '51824753556' compare equal."""
    return "".join(ch for ch in str(value or "") if ch.isalnum())


def abn_checksum_valid(abn: Any) -> bool | None:
    """Return True/False for a well-formed ABN, or None if not checkable.

    None means "no opinion" (empty or wrong length) and must not be reported as
    a failure -- that distinction is what stops a missing value looking like a
    violation. The checksum itself comes from the tax_ids registry, so the
    critic and the acting pipeline cannot disagree about whether an ABN is
    valid.
    """
    digits = normalize_tax_id(abn)
    if len(digits) != _ABN_LENGTH or not digits.isdigit():
        return None
    valid, _ = tax_ids.validate_abn(digits)
    return valid


def hamming_distance(left: Any, right: Any) -> int | None:
    """Digit differences between two equal-length identifiers, else None."""
    a, b = normalize_tax_id(left), normalize_tax_id(right)
    if not a or not b or len(a) != len(b):
        return None
    return sum(1 for x, y in zip(a, b, strict=True) if x != y)


def check_abn_matching(
    extracted_abn: str,
    reference_abn: str,
    abn_validation_metadata: dict,
    currency: str,
    agent_decision: str,
) -> dict:
    """Deterministic replacement for LLMValidator.validate_abn_matching.

    Verifies that the agent's tax-ID verdict matches what the checksum and any
    reference comparison actually say. No model call.
    """
    metadata = abn_validation_metadata or {}
    checksum_ok = abn_checksum_valid(extracted_abn)

    # The extractor records its own verdict; prefer it when present so the
    # critic checks the agent's recorded reasoning rather than recomputing a
    # different answer from possibly different input.
    recorded = metadata.get("valid")
    if (
        recorded is not None
        and checksum_ok is not None
        and recorded != checksum_ok
    ):
        return {
            "valid": False,
            "checksum_validated": True,
            "checksum_passed": checksum_ok,
            "reference_matching_correct": True,
            "hamming_distance": None,
            "ocr_error_suspected": False,
            "expected_decision": "REJECT" if not checksum_ok else "ACCEPT",
            "agent_decision": agent_decision,
            "violated_rules": ["Step 3.2"],
            "severity": "HIGH",
            "confidence": 1.0,
            "is_borderline_case": False,
            "deterministic": True,
            "reasoning": (
                f"Recorded tax ID validation ({recorded}) disagrees with the "
                f"recomputed checksum ({checksum_ok}) for {extracted_abn!r}."
            ),
        }

    distance = hamming_distance(extracted_abn, reference_abn)
    reference_matches = distance == 0 if distance is not None else None

    if checksum_ok is False:
        expected = "REJECT"
    elif reference_matches is False:
        # One digit out is more likely an OCR error than a different entity.
        expected = "SET_ASIDE" if distance == 1 else "REJECT"
    else:
        expected = "ACCEPT"

    return {
        "valid": True,
        "checksum_validated": checksum_ok is not None,
        "checksum_passed": checksum_ok,
        "reextraction_attempted": bool(metadata.get("reextraction_attempted")),
        "reference_matching_correct": True,
        "hamming_distance": distance,
        "ocr_error_suspected": distance == 1,
        "expected_decision": expected,
        "agent_decision": agent_decision,
        "violated_rules": [],
        "severity": None,
        "confidence": 1.0,
        "is_borderline_case": False,
        "deterministic": True,
        "reasoning": _abn_reasoning(
            extracted_abn, reference_abn, checksum_ok, distance, expected
        ),
    }


def _abn_reasoning(
    extracted, reference, checksum_ok, distance, expected
) -> str:
    if checksum_ok is None:
        detail = f"Tax ID {extracted!r} is not a checkable 11-digit ABN"
    elif checksum_ok:
        detail = f"ABN {extracted!r} passes the mod-89 checksum"
    else:
        detail = f"ABN {extracted!r} fails the mod-89 checksum"

    if not normalize_tax_id(reference):
        detail += "; no reference ABN to compare against"
    elif distance == 0:
        detail += "; matches the reference exactly"
    else:
        detail += f"; differs from the reference in {distance} digit(s)"
    return f"{detail}. Step 22 expected decision: {expected}."


def check_waf_hours(
    work_type: str,
    invoice_labour_hours: float,
    waf_total_hours: float,
    agent_decision: str,
    agent_reason: str | None = None,
    agent_step_passed: bool | None = None,
    agent_step_evidence: str | None = None,
    bypass_info: dict[str, Any] | None = None,
    tolerance_info: dict[str, Any] | None = None,
    exempt_work_types: tuple[str, ...] = ("PREVENTATIVE", "CLEANING"),
) -> dict:
    """Deterministic replacement for LLMValidator.validate_waf_hours.

    Compares billed labour hours against authorized hours plus tolerance. This
    is arithmetic, not judgment.
    """
    tolerance = _resolve_tolerance(tolerance_info)
    invoice_hours = _as_float(invoice_labour_hours)
    authorized_hours = _as_float(waf_total_hours)

    exempt = str(work_type or "").upper() in {
        t.upper() for t in exempt_work_types
    }

    if exempt:
        expected = "Skip"
        within = None
    elif authorized_hours <= 0:
        # Labour billed with no authorization at all.
        within = invoice_hours <= 0
        expected = "Continue" if within else "Reject"
    else:
        within = invoice_hours <= authorized_hours + tolerance
        expected = "Continue" if within else "Reject"

    # The agent is only wrong if its recorded step result contradicts the
    # arithmetic. A missing step result is not evidence of a violation.
    agent_correct = True
    if agent_step_passed is not None and within is not None:
        agent_correct = agent_step_passed == within

    return {
        "valid": agent_correct,
        "agent_step_found": agent_step_passed is not None,
        "agent_bypassed": bool((bypass_info or {}).get("bypassed")),
        "bypass_valid": None,
        "work_type_exempt": exempt,
        "exemption_correctly_applied": True,
        "hours_within_tolerance": within,
        "hours_within_investigator_tolerance": within,
        "effective_invoice_hours": invoice_hours,
        "tolerance_calculation": (
            f"{invoice_hours}h billed vs {authorized_hours}h authorized "
            f"(+{tolerance}h tolerance)"
        ),
        "expected_decision": expected,
        "agent_decision": agent_decision,
        "agent_decision_correct": agent_correct,
        "violated_rules": [] if agent_correct else ["Step 4.3"],
        "severity": None if agent_correct else "MEDIUM",
        "confidence": 1.0,
        "is_borderline_case": (
            within is not None
            and authorized_hours > 0
            and abs(invoice_hours - authorized_hours) <= tolerance
        ),
        "deterministic": True,
        "reasoning": (
            f"Work type {work_type!r}"
            + (
                " is WAF-exempt, so the hours check is skipped. "
                if exempt
                else ". "
            )
            + f"Billed {invoice_hours}h against {authorized_hours}h authorized "
            f"(tolerance {tolerance}h). Expected decision: {expected}."
        ),
    }


def _resolve_tolerance(tolerance_info: dict[str, Any] | None) -> float:
    for key in ("tolerance", "hours_tolerance", "value"):
        value = (tolerance_info or {}).get(key)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return _DEFAULT_HOURS_TOLERANCE


def _as_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0
