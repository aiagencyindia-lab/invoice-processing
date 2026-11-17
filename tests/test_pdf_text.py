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


def test_a_scan_is_read_by_vision_before_gemini(monkeypatch):
    calls = _pdf(monkeypatch, SCANNED)
    monkeypatch.setattr(
        gia, "extract_pdf_with_vision", lambda p: "## Page 1\n\n" + "OCR " * 30
    )
    assert gia.extract_pdf_with_fallback("x.pdf")[1] == "vision"
    assert calls == []


def test_vision_failure_falls_back_to_gemini(monkeypatch):
    calls = _pdf(monkeypatch, SCANNED)

    def down(p):
        raise RuntimeError("Vision API disabled")

    monkeypatch.setattr(gia, "extract_pdf_with_vision", down)
    assert gia.extract_pdf_with_fallback("x.pdf")[1] == "gemini"
    assert calls == ["x.pdf"]


def test_vision_reads_every_page_in_batches_of_five(monkeypatch, tmp_path):
    """Seven pages: two requests (1-5, 6-7), each page billed once."""
    monkeypatch.setenv("GOOGLE_VISION_OCR", "on")
    monkeypatch.setattr(gia._gcp_config, "PROJECT_ID", "p")
    sent = []

    class Session:
        def __init__(self, creds):
            pass

        def post(self, url, headers, json, timeout):
            pages = json["requests"][0]["pages"]
            sent.append(pages)
            body = {
                "responses": [
                    {
                        "responses": [
                            {"fullTextAnnotation": {"text": f"p{n}"}}
                            for n in pages
                        ]
                    }
                ]
            }
            return SimpleNamespace(
                raise_for_status=lambda: None, json=lambda: body
            )

    import google.auth  # noqa: PLC0415
    from google.auth.transport import requests as gar  # noqa: PLC0415

    monkeypatch.setattr(google.auth, "default", lambda scopes: (None, "p"))
    monkeypatch.setattr(gar, "AuthorizedSession", Session)

    class Pdf:
        pages = [None] * 7

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(gia.pdfplumber, "open", lambda p: Pdf())
    path = tmp_path / "seven.pdf"
    path.write_bytes(b"%PDF-1.4 scan")
    metrics = gia._get_metrics()
    before = metrics["total_cost_usd"]
    text = gia.extract_pdf_with_vision(path)
    assert sent == [[1, 2, 3, 4, 5], [6, 7]]
    assert "## Page 7\n\np7" in text
    assert round(metrics["total_cost_usd"] - before, 6) == 0.0105
