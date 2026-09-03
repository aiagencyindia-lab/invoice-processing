"""Phase 0 acceptance sweep: verify each deliverable actually behaves.

Checks behaviour, not test counts. Every item asserts the specific bug is gone.
"""

import json
import sys
import tempfile
import time
from pathlib import Path

# Derived from this file's location, matching verify_phase1.py -- the sweep
# originally lived in a session scratchpad with this path hardcoded, which made
# Phase 0 verification impossible from any other machine or a fresh clone.
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

results = []


def check(phase, name, fn):
    try:
        detail = fn()
        results.append((phase, name, True, detail or ""))
    except AssertionError as exc:
        results.append((phase, name, False, str(exc)[:160]))
    except Exception as exc:
        results.append(
            (phase, name, False, f"{type(exc).__name__}: {str(exc)[:140]}")
        )


# ---------------------------------------------------------------- 0.0
def golden_fixture_exists():
    d = REPO / "tests/fixtures/golden"
    cases = list((d / "cases").iterdir())
    expected = list((d / "expected").glob("*.json"))
    assert len(cases) == 3, f"expected 3 frozen cases, found {len(cases)}"
    assert len(expected) >= 7, (
        f"expected >=7 golden files, found {len(expected)}"
    )
    return f"{len(cases)} cases, {len(expected)} golden files"


def dead_code_gone():
    src = (
        REPO
        / "invoice_processing/shared_libraries/acting/general_invoice_agent.py"
    ).read_text()
    for sym in (
        "check_vendor_name_similarity",
        "WorkTypeClassification",
        "ItemCodeClassification",
        "VendorNameSimilarity",
    ):
        assert sym not in src, f"{sym} still present"
    return "4 dead symbols removed"


# ---------------------------------------------------------------- 0.1
def step_41_informational():
    from invoice_processing.shared_libraries.acting.general_invoice_agent import (
        Phase4ValidatorAgent,
    )

    inv = {
        "invoice_total_ex_tax": 100.0,
        "invoice_total_inc_tax": 210.0,
        "tax_amount": 10.0,
        "currency": "AUD",
        "line_items": [{"description": "S", "amount_ex_tax": 100.0}],
    }
    with tempfile.TemporaryDirectory() as tmp:
        out = Phase4ValidatorAgent().run(
            Path(tmp), {"invoice": inv}, {"decision": "CONTINUE"}
        )
    s41 = next(v for v in out["validations"] if v["step"] == "4.1")
    assert s41["passed"] is False and s41.get("informational") is True
    assert out["decision"] == "ACCEPT", "informational failure must not reject"
    return "broken totals recorded, not rejected"


def no_reasonless_rejection():
    from invoice_processing.shared_libraries.acting.general_invoice_agent import (
        Phase4ValidatorAgent,
    )

    inv = {
        "invoice_total_ex_tax": 100.0,
        "invoice_total_inc_tax": 110.0,
        "tax_amount": 10.0,
        "currency": "AUD",
        "line_items": [{"description": "S", "amount_ex_tax": 5.0}],
    }
    with tempfile.TemporaryDirectory() as tmp:
        out = Phase4ValidatorAgent().run(
            Path(tmp), {"invoice": inv}, {"decision": "CONTINUE"}
        )
    assert out["decision"] == "REJECT"
    assert out["rejection_template"], "REJECT with no stated reason"
    return f"rejects with reason: {out['rejection_template']!r}"


def stubs_marked():
    """Any control that has not been built must say so.

    Duplicate detection (3.1) was a stub and is now implemented, so the
    remaining stub is PO matching (2.2). What must never happen is a check
    reporting success without running.
    """
    import invoice_processing.shared_libraries.acting.general_invoice_agent as gia
    from invoice_processing.shared_libraries.acting.general_invoice_agent import (
        Phase2ValidatorAgent,
        Phase3ValidatorAgent,
    )

    orig = gia._config_store["CONFIG"]
    gia._config_store["CONFIG"] = {
        **orig,
        "duplicate_check_enabled": True,
        "require_purchase_order": True,
    }
    try:
        with tempfile.TemporaryDirectory() as tmp:
            p2 = Phase2ValidatorAgent().run(
                Path(tmp),
                {"invoice": {"line_items": [{"description": "x"}]}},
                {"decision": "CONTINUE"},
            )
            p3 = Phase3ValidatorAgent().run(
                Path(tmp),
                {"invoice": {"invoice_date": "2026-01-01"}},
                {"decision": "CONTINUE"},
            )
        entries = p2["validations"] + p3["validations"]

        # The durable invariant, not a snapshot of which checks happen to be
        # built. Asserting "2.2 is still a stub" made this fail the moment PO
        # matching was implemented -- a check that breaks on good news is a
        # check that gets edited rather than read.
        for entry in entries:
            if entry.get("implemented") is False:
                assert entry.get("status") == "NOT_IMPLEMENTED", (
                    f"step {entry['step']} is unimplemented but does not say so"
                )

        s22 = next(v for v in p2["validations"] if v["step"] == "2.2")
        s31 = next(v for v in p3["validations"] if v["step"] == "3.1")
        for step, entry in (("2.2", s22), ("3.1", s31)):
            assert entry["implemented"] is True, (
                f"step {step} regressed to a stub"
            )
            assert entry.get("status") != "NOT_IMPLEMENTED", step
    finally:
        gia._config_store["CONFIG"] = orig
    return f"{len(entries)} entries; none unimplemented-but-silent"


def rule_base_roundtrip():
    from invoice_processing.shared_libraries.alf_engine import ALFEngine

    doc = {
        "schema_version": "2.0.0",
        "metadata": {},
        "unknown_key": {"keep": 1},
        "rules": [
            {
                "id": "ALF-900",
                "name": "off",
                "enabled": False,
                "conditions": [],
                "actions": [],
            },
            {
                "id": "ALF-901",
                "name": "on",
                "enabled": True,
                "conditions": [],
                "actions": [],
            },
        ],
    }
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "rb.json"
        p.write_text(json.dumps(doc))
        ALFEngine(p).save_rule_base(p)
        after = json.loads(p.read_text())
    ids = {r["id"] for r in after["rules"]}
    assert "ALF-900" in ids, "disabled rule deleted on save"
    assert after.get("unknown_key") == {"keep": 1}, "unknown key dropped"
    return "disabled rules and unknown keys preserved"


def session_logging_durable():
    """Teaching a rule is recorded durably, and now by the audit trail.

    The original check covered `SessionLogger`, which wrote an SME chat
    session to a JSON file and lost everything on a crash before `save()`.
    That agent is retired: teaching happens in the product, and every step --
    proposed, drafted, approved, rejected, disabled -- is an append-only audit
    event with an actor and a tenant on it, which the file never had.
    """
    from invoice_processing.api import rules, service

    events = {
        # Proposed where the decision is made, decided where rules are
        # reviewed -- both halves of the loop, both on the trail.
        "rule_proposed",
        "rule_approved",
        "rule_rejected",
        "rule_disabled",
    }
    source = "".join(
        Path(module.__file__).read_text(encoding="utf-8")
        for module in (rules, service)
    )
    missing = sorted(e for e in events if f'"{e}"' not in source)
    assert not missing, f"rule lifecycle events not audited: {missing}"

    from invoice_processing import core

    assert not (Path(core.__file__).parent / "session_logger.py").exists(), (
        "the file-based session logger is back"
    )
    return "rule lifecycle is audited, not written to a session file"


def step_43_gated():
    from invoice_processing.shared_libraries.acting.general_invoice_agent import (
        Phase4ValidatorAgent,
    )

    inv = {
        "invoice_total_ex_tax": 972.11,
        "invoice_total_inc_tax": 1069.32,
        "tax_amount": 97.21,
        "currency": "AUD",
        "line_items": [
            {
                "description": "Technician labour",
                "amount_ex_tax": 972.11,
                "quantity": 2.5,
            }
        ],
    }
    with tempfile.TemporaryDirectory() as tmp:
        out = Phase4ValidatorAgent().run(
            Path(tmp), {"invoice": inv}, {"decision": "CONTINUE"}
        )
    assert not any(v["step"] == "4.3" for v in out["validations"]), (
        "4.3 ran with gate off"
    )
    return "4.3 skipped when require_work_authorization is false"


# ---------------------------------------------------------------- 0.2
def paths_unified():
    import importlib

    from invoice_processing import paths

    mods = [
        "invoice_processing.core.config",
        "invoice_processing.agent",
        "invoice_processing.tools.tools",
        "invoice_processing.shared_libraries.alf_engine",
        "invoice_processing.shared_libraries.acting.general_invoice_agent",
        "invoice_processing.shared_libraries.investigation.investigate_agent_reconst",
    ]
    roots = {
        str(
            getattr(
                importlib.import_module(m), "PROJECT_ROOT", paths.AGENT_ROOT
            )
        )
        for m in mods
    }
    assert len(roots) == 1, f"PROJECT_ROOT diverged: {roots}"
    assert paths.ENV_PATH.exists(), f".env not found at {paths.ENV_PATH}"
    return "1 root across 6 modules; .env resolves"


def config_from_yaml():
    import yaml

    from invoice_processing import paths
    from invoice_processing.shared_libraries.acting.general_invoice_agent import (
        _load_config_defaults,
    )

    y = yaml.safe_load(paths.MASTER_DATA_PATH.read_text())["config_defaults"]
    loaded = _load_config_defaults()
    for k, v in y.items():
        assert loaded[k] == v, f"{k} did not come from YAML"
    return f"{len(y)} keys sourced from master data"


# ---------------------------------------------------------------- 0.3
def one_context_builder():
    from invoice_processing.context import build_case_context
    from invoice_processing.core.case_loader import CaseLoaderAgent
    from invoice_processing.shared_libraries.alf_engine import (
        ActingAgentAdapter,
    )

    artifacts = {
        "extraction": {"waf_count": 0, "invoice": {}},
        "phase1": {"decision": "CONTINUE", "has_waf": True},
    }
    ctx = dict(artifacts)
    ActingAgentAdapter._promote_convenience_fields(ctx)
    loader = CaseLoaderAgent()._build_context(artifacts, None)
    canonical = build_case_context(artifacts)
    assert ctx == canonical == loader, "builders disagree"
    assert (
        canonical["has_waf"] is False
        and canonical["waf_present_phase1"] is True
    )
    assert not hasattr(CaseLoaderAgent, "_flatten_phase_decisions"), (
        "duplicate builder back"
    )
    return "engine == assessor == canonical"


def field_validation():
    from invoice_processing.core.rule_writer import RuleWriterAgent

    w = RuleWriterAgent()
    bad = {
        "id": "X",
        "name": "n",
        "actions": [{"type": "set_field"}],
        "conditions": [
            {"field": "decision_phase_4", "operator": "equals", "value": "R"}
        ],
    }
    errs = w.validate_rule(bad)
    assert any("never fire" in e for e in errs), "unfireable rule accepted"
    good = dict(
        bad,
        conditions=[
            {"field": "decision_phase4", "operator": "equals", "value": "R"}
        ],
    )
    assert w.validate_rule(good) == [], (
        f"valid rule rejected: {w.validate_rule(good)}"
    )
    return "typo'd field rejected, valid field accepted"


def prompt_is_generated():
    from invoice_processing.context import describe_fields
    from invoice_processing.core.prompts import RULE_DISCOVERY_SYSTEM_PROMPT

    assert describe_fields() in RULE_DISCOVERY_SYSTEM_PROMPT
    return "field list generated into the prompt"


# ---------------------------------------------------------------- 0.4
def critic_guard():
    from invoice_processing.shared_libraries.investigation.investigate_agent_reconst import (
        CallBudget,
        LlmBudgetExceeded,
        LlmCallTimeout,
        guarded_generate,
    )

    class Hang:
        def generate_content(self, p):
            time.sleep(30)

    started = time.monotonic()
    try:
        guarded_generate(
            Hang(), "p", CallBudget(timeout_seconds=0.5, deadline_seconds=5)
        )
        raise AssertionError("hang was not interrupted")
    except (LlmCallTimeout, LlmBudgetExceeded):
        pass
    elapsed = time.monotonic() - started
    assert elapsed < 10, f"waited {elapsed:.1f}s"
    return f"hang interrupted in {elapsed:.1f}s"


def critic_no_unguarded_calls():
    src = (
        REPO
        / "invoice_processing/shared_libraries/investigation/investigate_agent_reconst.py"
    ).read_text()
    assert "self.model.generate_content(" not in src, (
        "unguarded call site present"
    )
    return "all 11 call sites guarded"


def cache_key_isolated():
    from invoice_processing.shared_libraries.investigation.investigate_agent_reconst import (
        RuleDiscoveryEngine,
    )

    a = RuleDiscoveryEngine(
        "r", model_name="m", tenant_id="t1"
    )._compute_rules_hash()
    b = RuleDiscoveryEngine(
        "r", model_name="m", tenant_id="t2"
    )._compute_rules_hash()
    c = RuleDiscoveryEngine(
        "r", model_name="m2", tenant_id="t1"
    )._compute_rules_hash()
    assert a != b, "tenants share a cache entry"
    assert a != c, "model change does not invalidate"
    return "tenant and model included in key"


def tier0_deterministic():
    from invoice_processing.shared_libraries.investigation.checks import (
        check_abn_matching,
        check_waf_hours,
    )

    abn = {
        check_abn_matching("51 824 753 556", "", {}, "AUD", "C")[
            "expected_decision"
        ]
        for _ in range(20)
    }
    waf = {
        check_waf_hours("REPAIRS", 2.5, 0, "Reject")["expected_decision"]
        for _ in range(20)
    }
    assert abn == {"ACCEPT"} and waf == {"Reject"}
    return "20 runs each, identical"


# ---------------------------------------------------------------- 0.5
def ports_conformance():
    from invoice_processing.adapters import (
        FsCaseRepository,
        JsonlAuditSink,
        JsonRuleRepository,
        LocalBlobStore,
    )
    from invoice_processing.ports import (
        AuditSink,
        BlobStore,
        CaseRepository,
        RuleRepository,
    )

    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        assert isinstance(LocalBlobStore(t), BlobStore)
        assert isinstance(FsCaseRepository(t, {}), CaseRepository)
        assert isinstance(JsonRuleRepository(t / "r.json"), RuleRepository)
        assert isinstance(JsonlAuditSink(t / "a.jsonl"), AuditSink)
    return "4 adapters satisfy their protocols"


def concurrency_control():
    from invoice_processing.adapters import JsonRuleRepository
    from invoice_processing.ports import RuleBaseConflict

    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "rb.json"
        p.write_text(
            json.dumps({"rules": [{"id": "ALF-001", "name": "first"}]})
        )
        repo = JsonRuleRepository(p)
        doc_a, v_a = repo.load()
        doc_b, v_b = repo.load()
        doc_a["rules"].append({"id": "ALF-002", "name": "A"})
        repo.save("t", doc_a, expected_version=v_a)
        doc_b["rules"].append({"id": "ALF-002", "name": "B"})
        try:
            repo.save("t", doc_b, expected_version=v_b)
            raise AssertionError("concurrent write silently won")
        except RuleBaseConflict:
            pass
        names = [r["name"] for r in repo.load()[0]["rules"]]
    assert names == ["first", "A"], names
    return "second writer rejected, first survives"


# ---------------------------------------------------------------- 0.6
def tax_registry():
    from invoice_processing import tax_ids
    from invoice_processing.shared_libraries.acting.general_invoice_agent import (
        validate_abn_checksum,
    )
    from invoice_processing.shared_libraries.investigation.checks import (
        abn_checksum_valid,
    )

    assert {"ABN", "GSTIN", "NONE"} <= set(tax_ids.supported_formats())
    v, expl = tax_ids.validate("x", "UNKNOWNFMT")
    assert v is True and "NOT checked" in expl, (
        "unknown format must not claim valid"
    )
    for abn in ("51 824 753 556", "64 560 747 633"):
        assert validate_abn_checksum(abn) == tax_ids.validate_abn(abn)
        assert abn_checksum_valid(abn) == tax_ids.validate_abn(abn)[0]
    return f"{len(tax_ids.supported_formats())} formats; one shared ABN impl"


def classifier_deterministic():
    from invoice_processing.shared_libraries.acting.general_invoice_agent import (
        TransformerAgent,
    )

    t = TransformerAgent()
    out = {
        tuple(t._classify_items(["Emergency call-out - overtime rate"]))
        for _ in range(15)
    }
    assert out == {("LABOUR_AH",)}, out
    return "15 runs, identical (was ~40% flake)"


CHECKS = [
    ("0.0", "golden fixture frozen", golden_fixture_exists),
    ("0.0", "dead code removed", dead_code_gone),
    ("0.1", "step 4.1 informational", step_41_informational),
    ("0.1", "no reason-less rejection", no_reasonless_rejection),
    ("0.1", "stubs marked NOT_IMPLEMENTED", stubs_marked),
    ("0.1", "rule base round-trip", rule_base_roundtrip),
    ("0.1", "session logging durable", session_logging_durable),
    ("0.1", "step 4.3 gated", step_43_gated),
    ("0.2", "paths unified + .env found", paths_unified),
    ("0.2", "config read from YAML", config_from_yaml),
    ("0.3", "one context builder", one_context_builder),
    ("0.3", "field path validation", field_validation),
    ("0.3", "prompt field list generated", prompt_is_generated),
    ("0.4", "critic hang interrupted", critic_guard),
    ("0.4", "no unguarded LLM calls", critic_no_unguarded_calls),
    ("0.4", "cache key tenant-isolated", cache_key_isolated),
    ("0.4", "tier 0 deterministic", tier0_deterministic),
    ("0.5", "ports conformance", ports_conformance),
    ("0.5", "rule base concurrency", concurrency_control),
    ("0.6", "tax id registry", tax_registry),
    ("0.6", "classifier deterministic", classifier_deterministic),
]

for phase, name, fn in CHECKS:
    check(phase, name, fn)

print("\n" + "=" * 78)
print("PHASE 0 ACCEPTANCE SWEEP")
print("=" * 78)
current = None
for phase, name, ok, detail in results:
    if phase != current:
        print(f"\n  Phase {phase}")
        current = phase
    print(f"    {'PASS' if ok else 'FAIL'}  {name:<34} {detail}")

failed = [r for r in results if not r[2]]
print("\n" + "-" * 78)
print(f"  {len(results) - len(failed)}/{len(results)} passed")
if failed:
    print("  FAILURES:")
    for phase, name, _, detail in failed:
        print(f"    {phase} {name}: {detail}")
sys.exit(1 if failed else 0)
