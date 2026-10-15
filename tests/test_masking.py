"""Bank numbers leave the API as the last four digits unless the caller may
verify a payment. One helper, so every surface masks the same way."""

from invoice_processing.masking import BANK_FIELDS, mask_bank, mask_row


def test_last_four_with_a_prefix():
    assert mask_bank("901031925572") == "…5572"


def test_spaces_do_not_count():
    assert mask_bank("12 34 56") == "…3456"


def test_short_values_are_never_returned_verbatim():
    """Four characters or fewer used to come back in full."""
    assert mask_bank("1234") == "…1234"
    assert mask_bank("12") == "…12"


def test_numbers_mask_too():
    assert mask_bank(123456) == "…3456"


def test_empty_is_none():
    assert mask_bank("") is None
    assert mask_bank(None) is None


def test_mask_row_touches_only_bank_fields_and_copies():
    row = {
        "vendor_name": "Acme",
        "bank_account_number": "901031925572",
        "bank_iban": "GB29NWBK60161331926819",
    }
    masked = mask_row(row)
    assert masked == {
        "vendor_name": "Acme",
        "bank_account_number": "…5572",
        "bank_iban": "…6819",
    }
    assert row["bank_account_number"] == "901031925572"
    assert set(BANK_FIELDS) == {"bank_account_number", "bank_iban"}


def test_a_structured_value_is_hidden_entirely():
    """Only a number or text has meaningful last digits; anything else would
    mask to a fragment of its repr."""
    assert mask_bank({"a": 1}) == "…"
    assert mask_bank(["901031925572"]) == "…"


def test_mask_text_masks_long_numbers_and_ifsc_codes():
    from invoice_processing.masking import mask_text  # noqa: PLC0415

    out = mask_text(
        "a/c 5010 0012 345678, IFSC HDFC0001234, ref 1234567, phone 98765"
    )
    assert out == "a/c …5678, IFSC …1234, ref 1234567, phone 98765"
