import pytest

from invoice_processing import po_book, tenant_settings
from invoice_processing.api import service
from invoice_processing.shared_libraries.acting import general_invoice_agent


def test_bank_change_recent_days_is_settable_and_defaults_to_30():
    assert tenant_settings.validate({"bank_change_recent_days": 45}) == {
        "bank_change_recent_days": 45
    }
    assert (
        tenant_settings.effective("nobody", store={})["bank_change_recent_days"]
        == 30
    )


def test_bank_change_recent_days_cannot_be_negative():
    with pytest.raises(tenant_settings.SettingsError):
        tenant_settings.validate({"bank_change_recent_days": -1})
    assert tenant_settings.validate({"bank_change_recent_days": 0}) == {
        "bank_change_recent_days": 0
    }


def test_apply_to_pipeline_injects_vendor_records(monkeypatch):
    # apply_to_pipeline overwrites this module global; setitem makes
    # monkeypatch restore it afterwards.
    monkeypatch.setitem(
        general_invoice_agent._config_store,
        "CONFIG",
        dict(general_invoice_agent.DEFAULT_CONFIG),
    )

    class Repo:
        def load_records(self, tenant_id):
            return [{"name": "Acme", "tax_id": "X", "kind": ""}]

    monkeypatch.setattr(service, "get_vendors", Repo)
    config = tenant_settings.apply_to_pipeline("t1", store={})
    assert config["vendor_master_records"] == [
        {"name": "Acme", "tax_id": "X", "kind": ""}
    ]


def test_apply_to_pipeline_without_a_vendor_table_injects_none(monkeypatch):
    monkeypatch.setitem(
        general_invoice_agent._config_store,
        "CONFIG",
        dict(general_invoice_agent.DEFAULT_CONFIG),
    )
    monkeypatch.setattr(service, "get_vendors", lambda: None)
    assert (
        tenant_settings.apply_to_pipeline("t1", store={})[
            "vendor_master_records"
        ]
        is None
    )


def test_approval_sla_days_is_settable_and_defaults_to_3():
    assert tenant_settings.validate({"approval_sla_days": 5}) == {
        "approval_sla_days": 5
    }
    assert (
        tenant_settings.effective("nobody", store={})["approval_sla_days"] == 3
    )


def test_approval_sla_days_cannot_be_negative():
    with pytest.raises(tenant_settings.SettingsError):
        tenant_settings.validate({"approval_sla_days": -1})


@pytest.mark.parametrize("value", [0, 1.5, True, "3", -2])
def test_approval_sla_days_is_a_whole_number_of_at_least_one(value):
    with pytest.raises(
        tenant_settings.SettingsError, match="approval_sla_days"
    ):
        tenant_settings.validate({"approval_sla_days": value})


def test_approval_sla_days_keeps_its_integer():
    assert tenant_settings.validate({"approval_sla_days": 1}) == {
        "approval_sla_days": 1
    }
    assert isinstance(
        tenant_settings.validate({"approval_sla_days": 7})["approval_sla_days"],
        int,
    )
    assert (
        "whole days"
        in tenant_settings.DESCRIPTIONS["approval_sla_days"].lower()
    )


@pytest.mark.parametrize("value", [0, 1.5])
def test_three_way_qty_tolerance_accepts_non_negative_numbers(value):
    assert tenant_settings.validate({"three_way_qty_tolerance": value}) == {
        "three_way_qty_tolerance": value
    }
    assert (
        tenant_settings.effective("nobody", store={})["three_way_qty_tolerance"]
        == 0
    )


@pytest.mark.parametrize(
    "value", [-1, True, float("nan"), float("inf"), float("-inf")]
)
def test_three_way_qty_tolerance_refuses_negative_bool_and_non_finite(value):
    with pytest.raises(
        tenant_settings.SettingsError, match="three_way_qty_tolerance must be"
    ):
        tenant_settings.validate({"three_way_qty_tolerance": value})


@pytest.mark.parametrize("has_orders", [True, False])
def test_apply_to_pipeline_picks_the_db_book_only_when_the_tenant_has_orders(
    monkeypatch, has_orders
):
    monkeypatch.setitem(
        general_invoice_agent._config_store,
        "CONFIG",
        dict(general_invoice_agent.DEFAULT_CONFIG),
    )
    monkeypatch.setattr(service, "get_vendors", lambda: None)

    class Repo:
        def has_orders(self, tenant_id):
            assert tenant_id == "t1"
            return has_orders

    monkeypatch.setattr(service, "get_purchase_orders", Repo)
    book = tenant_settings.apply_to_pipeline("t1", store={})[
        "purchase_order_book"
    ]
    if has_orders:
        assert isinstance(book, po_book.DbBook) and book.tenant_id == "t1"
    else:
        assert book is None


def test_apply_to_pipeline_without_a_po_table_uses_the_file(monkeypatch):
    monkeypatch.setitem(
        general_invoice_agent._config_store,
        "CONFIG",
        dict(general_invoice_agent.DEFAULT_CONFIG),
    )
    monkeypatch.setattr(service, "get_vendors", lambda: None)
    monkeypatch.setattr(service, "get_purchase_orders", lambda: None)
    config = tenant_settings.apply_to_pipeline("t1", store={})
    assert config["purchase_order_book"] is None
    assert type(general_invoice_agent._load_purchase_orders()).__name__ == (
        "FileBook"
    )


@pytest.mark.parametrize(
    "key",
    [
        "po_tolerance_pct",
        "po_tolerance_abs",
        "high_value_threshold",
        "tax_rate_tolerance",
        "tax_rate_tolerance_pct",
        "bank_change_recent_days",
    ],
)
@pytest.mark.parametrize("value", [float("nan"), float("inf")])
def test_non_finite_tolerances_are_refused(key, value):
    with pytest.raises(tenant_settings.SettingsError, match=key):
        tenant_settings.validate({key: value})
