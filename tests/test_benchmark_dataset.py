from benchmark.harness import dataset


def test_vendor_master_carries_bank_columns():
    rec = next(
        r
        for r in dataset.vendor_records("A")
        if r["name"].startswith("Cloudhaven")
    )
    assert rec["bank_code"] == "KCBL0230056"
    assert rec["email_domain"] == "cloudhaven.example"
