"""The classifier reads a PDF's own text and pays Gemini only for a scan."""

from types import SimpleNamespace

from invoice_processing.shared_libraries.acting import (
    general_invoice_agent as gia,
)

DIGITAL = "## Page 1\n\nTAX INVOICE  INV-001  GSTIN 27ABCDE1234F1Z5  Total 11,800.00\n---\n"
SCANNED = "## Page 1\n\n## Page 2\n"


def _pdf(monkeypatch, local, gemini_text="TRANSCRIBED " * 20):
    calls = []
    monkeypatch.setattr(gia, "extract_pdf_to_markdown", lambda p: local)
    monkeypatch.setattr(
        gia,
        "extract_pdf_with_gemini",
        lambda p: calls.append(p) or gemini_text,
    )
    monkeypatch.setattr(gia, "_ensure_gcp_initialized", lambda: None)
    monkeypatch.setattr(gia._gcp_config, "PROJECT_ID", "p")
    return calls


def test_a_digital_pdf_never_reaches_gemini(monkeypatch):
    calls = _pdf(monkeypatch, DIGITAL)
    assert gia.extract_pdf_with_fallback("x.pdf") == (DIGITAL, "pdfplumber")
    assert calls == []


def test_a_scan_or_an_unreadable_pdf_is_transcribed(monkeypatch):
    for local in (SCANNED, "[PDF extraction failed: " + "x" * 80 + "]"):
        calls = _pdf(monkeypatch, local)
        assert gia.extract_pdf_with_fallback("x.pdf")[1] == "gemini"
        assert calls == ["x.pdf"]


def test_the_transcription_call_is_counted(monkeypatch):
    """It was the one Gemini call that recorded no tokens or cost."""
    response = SimpleNamespace(
        text="t",
        usage_metadata=SimpleNamespace(
            prompt_token_count=1_000_000,
            candidates_token_count=0,
            thoughts_token_count=0,
        ),
    )
    monkeypatch.setattr(gia, "_generate", lambda model, contents: response)
    monkeypatch.setattr(
        gia._gcp_config, "GEMINI_FLASH_MODEL", "gemini-3.5-flash"
    )
    monkeypatch.setattr(gia._gcp_config, "API_CALL_DELAY_SECONDS", 0)
    metrics = gia._get_metrics()
    before = metrics["llm_calls"], metrics["total_cost_usd"]
    gia.extract_pdf_with_gemini(__file__)  # any readable file stands in
    assert metrics["llm_calls"] == before[0] + 1
    assert round(metrics["total_cost_usd"] - before[1], 6) == 1.50
