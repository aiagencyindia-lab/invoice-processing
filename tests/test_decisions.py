"""Gemini Decision 1 answers the closed-set questions; Gemini stays the fallback."""

from invoice_processing import decisions
from invoice_processing.shared_libraries.acting import (
    general_invoice_agent as gia,
)
from invoice_processing.trust import decide as d


def _stub(monkeypatch, answer_for, fail=False):
    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    asked = []

    def ask(input_, questions, timeout=30):
        asked.append(questions)
        if fail:
            raise RuntimeError("down")
        return {k: answer_for(k, q) for k, q in questions.items()}, 1000

    monkeypatch.setattr(decisions, "ask", ask)
    return asked


def test_every_document_type_has_a_description():
    assert set(gia._DOC_TYPE_HINTS) == set(gia.DOCUMENT_TYPES)
    assert set(gia._ITEM_CODE_HINTS) == set(
        gia.TransformerAgent.VALID_ITEM_CODES
    )


def test_document_analysis(monkeypatch):
    def answer(k, q):
        if q["type"] == "noul":
            return {"noul": 0.9 if k == "has_invoice" else 0.1}
        if k == "invoice_count":
            return {"choice": "n2", "confidence": 0.8}
        return {"choice": "credit_note", "confidence": 0.7}

    _stub(monkeypatch, answer)
    before = gia._get_metrics()["total_cost_usd"]
    doc = gia.ClassifierAgent._analyze_with_decisions("CREDIT NOTE")
    assert (doc.has_invoice, doc.has_work_authorization) == (True, False)
    assert (doc.invoice_count, doc.document_type) == (2, "credit_note")
    assert round(gia._get_metrics()["total_cost_usd"] - before, 7) == 0.0001


def test_item_codes_and_fallback(monkeypatch):
    agent = gia.TransformerAgent.__new__(gia.TransformerAgent)
    _stub(
        monkeypatch, lambda k, q: {"choice": "PARTS" if k == "l0" else "LABOUR"}
    )
    assert agent._classify_items_llm(["filter", "repair"]) == [
        "PARTS",
        "LABOUR",
    ]

    _stub(monkeypatch, None, fail=True)
    monkeypatch.setattr(
        gia,
        "call_gemini",
        lambda *a, **k: (
            '{"classifications": [{"item_number": 1, "item_code": "HIRE"}]}',
            None,
        ),
    )
    assert agent._classify_items_llm(["crane"]) == ["HIRE"]


def test_trust_questions(monkeypatch):
    def answer(k, q):
        return {
            "choice": {"choice": "o1", "confidence": 0.8},
            "noul": {"noul": 0.2},
            "score": {"score": 3.0, "confidence": 0.6},
        }[q["type"]]

    _stub(monkeypatch, answer)
    qs = [
        d.Question("kind", "choice", "?", "none", options=("rate", "tier")),
        d.Question("same", "yes_no", "?", False),
        d.Question("sure", "score", "?", 0.0),
    ]
    got = {
        a.id: (a.value, round(a.probability, 2), a.fallback)
        for a in d.decide({}, qs, d.GeminiDecider())
    }
    assert got == {
        "kind": ("tier", 0.8, False),
        "same": (False, 0.8, False),
        "sure": (0.75, 0.6, False),
    }


def test_trust_falls_back_to_gemini(monkeypatch):
    _stub(monkeypatch, None, fail=True)
    monkeypatch.setattr(
        d.GeminiDecider,
        "__call__",
        lambda self, prompt: (
            '{"answers": [{"id": "same", "value": true, "probability": 0.9}]}',
            "g",
        ),
    )
    [a] = d.decide(
        {}, [d.Question("same", "yes_no", "?", False)], d.GeminiDecider()
    )
    assert (a.value, a.model_version) == (True, "g")


def test_off_without_a_key(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "")
    assert not decisions.enabled()
