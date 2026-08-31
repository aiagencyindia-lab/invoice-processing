"""Characterization tests: freeze current behaviour before the library refactor.

These tests assert what the system does *today*, not what it should do. They are
the regression net for Phase 0 of the platform migration: any change to context
building, rule evaluation, or ALF output must either keep these green or update
the golden files deliberately.

Regenerate the golden files after an intentional behaviour change:

    UPDATE_GOLDEN=1 pytest tests/test_golden_characterization.py

Then review the diff before committing. An unreviewed regeneration defeats the
entire purpose of these tests.
"""

import json
import os
import shutil
from pathlib import Path

import pytest

from invoice_processing.core import case_loader as case_loader_module
from invoice_processing.core.case_loader import CaseLoaderAgent
from invoice_processing.shared_libraries.alf_engine import (
    ActingAgentAdapter,
    ALFEngine,
    run_alf_on_case,
)

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "golden"
CASES_DIR = FIXTURE_DIR / "cases"
RULE_BASE = FIXTURE_DIR / "rule_base.json"
EXPECTED_DIR = FIXTURE_DIR / "expected"

CASE_IDS = ("case_001", "case_002", "case_005")

# Keys whose values legitimately change on every run.
VOLATILE_AUDIT_KEYS = {"evaluation_timestamp"}

# Context keys the rules actually reference. Snapshotting the whole context
# would bury the signal under the raw artifacts; these are the promoted fields
# that rule conditions bind to, and the ones the Phase 0.3 unification touches.
TRACKED_CONTEXT_KEYS = (
    "decision",
    "decision_phase1",
    "decision_phase2",
    "decision_phase3",
    "decision_phase4",
    "has_waf",
    "waf_count",
    "waf_exempt",
    "waf_exempt_reason",
    "work_type",
)


def _updating() -> bool:
    return os.environ.get("UPDATE_GOLDEN") == "1"


def _assert_matches_golden(name: str, actual: dict) -> None:
    """Compare against the golden file, or write it when regenerating."""
    EXPECTED_DIR.mkdir(parents=True, exist_ok=True)
    path = EXPECTED_DIR / f"{name}.json"
    serialized = json.dumps(actual, indent=2, sort_keys=True, default=str)

    if _updating():
        path.write_text(serialized + "\n")
        pytest.skip(f"Regenerated golden file {path.name} — review the diff")

    assert path.exists(), (
        f"Missing golden file {path}. Generate it with UPDATE_GOLDEN=1 pytest"
    )
    expected = json.loads(path.read_text())
    assert json.loads(serialized) == expected, (
        f"Behaviour changed for {name}. If this change is intentional, "
        f"regenerate with UPDATE_GOLDEN=1 and review the diff carefully."
    )


def _tracked(context: dict) -> dict:
    """Project a case context down to the fields rules bind to."""
    return {key: context.get(key) for key in TRACKED_CONTEXT_KEYS}


def _loader_context(case_id: str) -> dict:
    """Build a context the way the impact assessor does.

    CaseLoaderAgent reads from the module-level AGENTIC_FLOW_OUT constant, so
    pointing it at the fixture means patching that global. Phase 0.5 replaces
    this with an injected CaseRepository and this helper goes away.
    """
    original = case_loader_module.AGENTIC_FLOW_OUT
    case_loader_module.AGENTIC_FLOW_OUT = CASES_DIR
    try:
        return CaseLoaderAgent().run(case_id).context
    finally:
        case_loader_module.AGENTIC_FLOW_OUT = original


@pytest.fixture(scope="module")
def engine() -> ALFEngine:
    return ALFEngine(RULE_BASE)


@pytest.mark.parametrize("case_id", CASE_IDS)
def test_alf_output_is_stable(tmp_path, engine, case_id):
    """run_alf_on_case must produce identical output for a frozen case."""
    # Copy into tmp_path so the engine cannot mutate the checked-in fixture.
    case_dir = tmp_path / case_id
    shutil.copytree(CASES_DIR / case_id, case_dir)

    revised, audit = run_alf_on_case(
        rule_base_path=RULE_BASE,
        output_folder=case_dir,
        save_to_alf_out=False,
        engine=engine,
    )

    _assert_matches_golden(
        f"{case_id}_alf",
        {
            "revised_postprocessing": revised,
            "audit": {
                k: v for k, v in audit.items() if k not in VOLATILE_AUDIT_KEYS
            },
        },
    )


@pytest.mark.parametrize("case_id", CASE_IDS)
def test_context_builders_are_stable(case_id):
    """Freeze BOTH context builders.

    `ActingAgentAdapter` (the live engine) and `CaseLoaderAgent` (the impact
    assessor) build the same context through two independent code paths that can
    disagree -- see test_documents_known_context_divergence. Snapshotting both
    means the Phase 0.3 unification can be verified rather than assumed.
    """
    case_dir = CASES_DIR / case_id

    engine_context = ActingAgentAdapter.build_case_context(case_dir)
    loader_context = _loader_context(case_id)

    _assert_matches_golden(
        f"{case_id}_context",
        {
            "engine_builder": _tracked(engine_context),
            "loader_builder": _tracked(loader_context),
        },
    )


def test_documents_known_context_divergence():
    """The engine and the impact assessor must agree on every case.

    They used to build the context through two independent code paths that had
    already drifted on `has_waf`. Phase 0.3 deleted the duplicate, so both now
    call one builder and this can only fail if someone reintroduces a copy --
    which is exactly what it is here to catch.

    See test_context_unification.py for the unification's own tests.

    Note: `work_type` is always None -- phase 1 does not persist it, so any
    rule conditioning on it silently never matches.
    """
    divergences = {}
    for case_id in CASE_IDS:
        case_dir = CASES_DIR / case_id
        engine_context = ActingAgentAdapter.build_case_context(case_dir)
        loader_context = _loader_context(case_id)

        differing = {
            key: {
                "engine": engine_context.get(key),
                "loader": loader_context.get(key),
            }
            for key in TRACKED_CONTEXT_KEYS
            if engine_context.get(key) != loader_context.get(key)
        }
        if differing:
            divergences[case_id] = differing

    _assert_matches_golden("context_divergence", divergences)
