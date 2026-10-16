"""trust/html.py: print-styled pages that never render raw input."""

from invoice_processing.trust import html


def test_page_escapes_everything_and_carries_print_css():
    out = html.page(
        "<b>Report</b>", html.kv([("Vendor", "<script>x</script>")])
    )
    assert "&lt;b&gt;Report&lt;/b&gt;" in out and "<script>" not in out
    assert "&lt;script&gt;x&lt;/script&gt;" in out
    assert "@media print" in out and "<!doctype html>" in out.lower()


def test_table_renders_columns_and_rows_in_order():
    out = html.table(
        ["Type", "₹"], [["LEAK-RATE", "1,000.00"], ["DUP-NEAR", "5"]]
    )
    assert out.index("<th>Type</th>") < out.index("<th>₹</th>")
    assert out.index("LEAK-RATE") < out.index("DUP-NEAR")
    assert out.count("<tr>") == 3
