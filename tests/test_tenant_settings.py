import pytest

from invoice_processing import tenant_settings
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
