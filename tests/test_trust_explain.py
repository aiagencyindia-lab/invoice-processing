"""The Causal Why engine: model, counterfactuals, templates, pack, ratings."""

from __future__ import annotations

import pytest

from invoice_processing.trust.explain import model as xm

TXN = "n-txn"


def _explanation(**over):
    base = {
        "finding_id": "f1",
        "type": "LEAK-RATE",
        "version": xm.EXPLAIN_VERSION,
        "what_happened": "Billed above the contract rate.",
        "why": (xm.Step("The contract says 50.", "n-clause"),),
        "caused_by": (xm.Actor("vendor", "Acme", None),),
        "evidence": (
            xm.Evidence("n-clause", "contract_clause", "Clause", "50", None),
        ),
        "confidence": 0.9,
        "confidence_note": "High.",
        "next_action": "Short-pay.",
        "would_change": ("A reviewed clause.",),
        "counterfactual": None,
        "versions": {"explain_version": xm.EXPLAIN_VERSION},
        "transaction_node": TXN,
    }
    base.update(over)
    return xm.Explanation(**base)


def test_check_accepts_steps_on_cited_nodes_and_the_transaction():
    x = _explanation(
        why=(xm.Step("a", "n-clause"), xm.Step("b", TXN)),
        caused_by=(
            xm.Actor("vendor", "Acme", TXN),
            xm.Actor("engine", "e", None),
        ),
    )
    x.check({"n-clause"})  # no raise


def test_check_rejects_uncited_step():
    x = _explanation(why=(xm.Step("a", "n-other"),))
    with pytest.raises(xm.Uncited):
        x.check({"n-clause"})
    x = _explanation(caused_by=(xm.Actor("person", "p", "n-other"),))
    with pytest.raises(xm.Uncited):
        x.check({"n-clause"})
    x = _explanation(evidence=(xm.Evidence("n-other", "k", "l", None, None),))
    with pytest.raises(xm.Uncited):
        x.check({"n-clause"})


def test_check_rejects_empty_evidence_and_blank_step():
    with pytest.raises(xm.Uncited):
        _explanation(evidence=()).check({"n-clause"})
    with pytest.raises(xm.Uncited):
        _explanation(why=(xm.Step("  ", "n-clause"),)).check({"n-clause"})
    with pytest.raises(xm.Uncited):
        _explanation(why=()).check({"n-clause"})


def test_as_dict_matches_the_frontend_contract():
    d = _explanation().as_dict()
    assert set(d) == {
        "finding_id", "type", "version", "what_happened", "why", "caused_by",
        "evidence", "confidence", "confidence_note", "next_action",
        "would_change", "counterfactual", "versions", "transaction_node",
    }  # fmt: skip
    assert d["why"] == [
        {"text": "The contract says 50.", "node_id": "n-clause"}
    ]
    assert d["would_change"] == ["A reviewed clause."]
