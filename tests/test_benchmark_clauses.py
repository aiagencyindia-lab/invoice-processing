"""Contract clause extraction, measured end to end (AC-19)."""

import json

from benchmark.history import clauses
from invoice_processing.trust.decide import FakeDecider


def test_the_corpus_is_deterministic_and_has_a_key(tmp_path):
    a, b = clauses.write(tmp_path / "a"), clauses.write(tmp_path / "b")
    assert a == b and sum(len(c["clauses"]) for c in a) >= 30
    first = a[0]
    pdf = (tmp_path / "a" / first["file"]).read_bytes()
    assert (
        pdf.startswith(b"%PDF")
        and pdf == (tmp_path / "b" / first["file"]).read_bytes()
    )
    key = json.loads((tmp_path / "a" / "contract_clauses.json").read_text())
    assert key == a


def test_the_keyword_decider_reads_only_the_prompt():
    prompt = (
        'Record:\n{"candidates": {"c0": "A rebate of 2% applies above INR 5,00,000.", '
        '"c1": "This agreement is governed by the laws of India."}, "page": 1}'
        "\nQuestions:\n[]"
    )
    reply, version = clauses.KeywordDecider()(prompt)
    answers = {a["id"]: a["value"] for a in json.loads(reply)["answers"]}
    assert answers["c0"] == "rebate" and answers["c1"] == "none"
    assert version == "keyword-baseline"


def test_coverage_goes_through_the_real_extraction_path(tmp_path, scratch_db):
    url = scratch_db("clauses.db")
    out = clauses.coverage(url, tmp_path, decider=clauses.KeywordDecider())
    assert out["planted"] >= 30
    assert out["coverage"] >= 0.6 and out["precision"] >= 0.9
    # The same corpus with the model down extracts nothing: the score comes
    # from the engine's extraction, not from the key.
    down = clauses.coverage(
        url, tmp_path / "down", decider=FakeDecider({}, fail=True)
    )
    assert down["coverage"] == 0.0
