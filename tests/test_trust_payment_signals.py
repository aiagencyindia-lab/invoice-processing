"""Payment Trust's pure signals: statistics, domains, mail bodies, stored mail."""

import json
from datetime import date
from decimal import Decimal

from invoice_processing.trust.payment import signals as sg


def test_robust_z_flags_five_times_the_median():
    history = [Decimal(x) for x in (100, 120, 80, 110, 90, 105)]
    z = sg.robust_z(Decimal(525), history)
    assert z > 3.0
    assert sg.robust_z(Decimal(100), history) < 1.0


def test_robust_z_zero_mad():
    flat = [Decimal(1000)] * 6
    assert sg.robust_z(Decimal(1000), flat) == 0.0
    assert sg.robust_z(Decimal(5000), flat) == float("inf")
    assert sg.robust_z(Decimal(10), flat) < 0


def test_cadence_gap_far_below_median_is_flagged():
    others = [date(2025, m, 1) for m in range(1, 8)]
    assert sg.nearest_gap_ratio(date(2025, 7, 4), others) < 0.25
    assert sg.nearest_gap_ratio(date(2025, 8, 1), others) >= 0.25
    assert sg.nearest_gap_ratio(date(2025, 8, 1), [date(2025, 1, 1)]) is None


def test_lookalike_folds_confusables():
    assert sg.lookalike("acme-steel.in", "acrne-steel.in", 0.8)
    assert sg.lookalike("acme.in", "acme.in.co", 0.8)
    assert sg.lookalike("globex.com", "g1obex.com", 0.8)
    assert sg.lookalike("globex.com", "glоbex.com", 0.8)  # Cyrillic о
    assert not sg.lookalike("acme.in", "acme.in", 0.8)
    assert not sg.lookalike("acme.in", "zebra-foods.com", 0.8)


def test_subdomain_is_vendor_domain():
    assert sg.same_domain("billing.acme.in", "acme.in")
    assert sg.same_domain("ACME.IN", "acme.in")
    assert not sg.same_domain("notacme.in", "acme.in")
    assert sg.domain_of("Accounts <ap@Acme.IN>") == "acme.in"
    assert sg.domain_of("nobody") is None


def test_urgency_words_found_with_excerpt():
    hit = sg.urgency("Hello,\nPlease pay URGENTLY today, our bank has changed.")
    assert hit and "URGENTLY" in hit
    assert sg.urgency("Please find attached invoice 42.") is None


def test_body_bank_details_differ_and_excerpt_is_masked():
    body = "Kindly remit to A/c No: 50100234567891, IFSC HDFC0001234."
    differ = sg.bank_details_differ(body, "11112222333344", "SBIN0000001")
    assert differ is not None
    assert "50100234567891" not in differ and "…7891" in differ
    assert sg.bank_details_differ(body, "50100234567891", "HDFC0001234") is None
    # No master account: nothing to differ from.
    assert sg.bank_details_differ(body, None, None) is None
    # A phone number is not an account.
    assert sg.bank_details_differ("Call 9876543210", "1", "SBIN0000001") is None


def test_mask_text_masks_every_long_number():
    out = sg.mask_text("a/c 123456789012 and 99887766554433")
    assert "123456789012" not in out and "…9012" in out and "…4433" in out


MIME = (
    b"From: Acme Accounts <ap@acme.in>\r\n"
    b"Reply-To: pay@acme-pay.co\r\n"
    b"Subject: Invoice 7\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
    b"Please pay urgently.\r\n"
)


def test_parse_raw_mime():
    m = sg.parse_email(MIME)
    assert m.sender == "ap@acme.in" and m.reply_to == "pay@acme-pay.co"
    assert "urgently" in m.body and m.subject == "Invoice 7"


def test_parse_postmark_json():
    raw = json.dumps(
        {
            "FromFull": {"Email": "AP@acme.in", "Name": "Acme"},
            "ReplyTo": "x@evil.co",
            "Subject": "Inv",
            "TextBody": "Pay today",
            "HtmlBody": "<p>Pay today</p>",
        }
    ).encode()
    m = sg.parse_email(raw)
    assert m.sender == "ap@acme.in" and m.reply_to == "x@evil.co"
    assert "Pay today" in m.body


def test_parse_postmark_raw_email():
    raw = json.dumps({"RawEmail": MIME.decode(), "TextBody": "x"}).encode()
    m = sg.parse_email(raw)
    assert m.sender == "ap@acme.in" and "urgently" in m.body


def test_parse_sendgrid_multipart():
    boundary = "xYzZY"
    parts = [("from", "Acme <ap@acme.in>"), ("text", "Wire it now")]
    parts.append(("headers", "Reply-To: r@other.co\nX-Foo: 1"))
    body = "".join(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'
        for k, v in parts
    )
    body += f"--{boundary}--\r\n"
    m = sg.parse_email(body.encode())
    assert m.sender == "ap@acme.in" and m.reply_to == "r@other.co"
    assert "Wire it now" in m.body


def test_parse_html_only_mime_body_is_text():
    raw = (
        b"From: a@b.in\r\nContent-Type: text/html\r\n\r\n"
        b"<p>Pay <b>immediately</b></p>"
    )
    assert "immediately" in sg.parse_email(raw).body


def test_parse_garbage_is_empty():
    m = sg.parse_email(b"\x00\x01not mail")
    assert m.sender == "" and m.reply_to == ""
    assert sg.parse_email(b"").body == ""
