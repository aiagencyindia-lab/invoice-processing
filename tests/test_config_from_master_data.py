"""Pipeline config must come from master data, not a second hardcoded copy.

`general_invoice_agent.DEFAULT_CONFIG` was a verbatim duplicate of
`config_defaults` in invoice_master_data.yaml, and nothing kept the two in sync.
The README's claim that swapping the YAML adapts the pipeline to a new domain
was false for this section: editing the YAML changed nothing, because the agent
never read it.

These tests assert the YAML is genuinely authoritative, and that the hardcoded
fallback still protects startup when master data is unavailable.
"""

import yaml

from invoice_processing import paths
from invoice_processing.shared_libraries import (
    master_data_loader as loader_module,
)
from invoice_processing.shared_libraries.acting import general_invoice_agent
from invoice_processing.shared_libraries.acting.general_invoice_agent import (
    _FALLBACK_CONFIG,
    _load_config_defaults,
)


def _yaml_config_defaults() -> dict:
    data = yaml.safe_load(paths.MASTER_DATA_PATH.read_text())
    return data["config_defaults"]


def test_config_is_loaded_from_master_data():
    """Every value the YAML declares must reach the agent."""
    loaded = _load_config_defaults()
    for key, expected in _yaml_config_defaults().items():
        assert loaded[key] == expected, f"{key} did not come from master data"


def test_yaml_and_fallback_have_not_drifted():
    """Two copies of the same config will drift; catch it while they agree."""
    yaml_defaults = _yaml_config_defaults()

    assert set(yaml_defaults) == set(_FALLBACK_CONFIG), (
        "master data and the fallback declare different keys"
    )
    mismatched = {
        key: (_FALLBACK_CONFIG[key], yaml_defaults[key])
        for key in yaml_defaults
        if _FALLBACK_CONFIG[key] != yaml_defaults[key]
    }
    assert not mismatched, f"fallback drifted from master data: {mismatched}"


def test_fallback_used_when_master_data_unavailable(monkeypatch):
    """A missing or broken YAML must not stop the agent from starting."""

    def _boom():
        raise FileNotFoundError("master data missing")

    monkeypatch.setattr(
        general_invoice_agent,
        "_load_config_defaults",
        general_invoice_agent._load_config_defaults,
    )
    monkeypatch.setattr(loader_module, "load_master_data", _boom)

    config = _load_config_defaults()
    assert config == _FALLBACK_CONFIG
    assert config is not _FALLBACK_CONFIG, (
        "must return a copy, not the original"
    )


def test_default_config_is_not_the_fallback_object():
    """Mutating the live config must never corrupt the fallback."""
    assert general_invoice_agent.DEFAULT_CONFIG is not _FALLBACK_CONFIG


def test_config_covers_every_key_the_pipeline_reads():
    """A YAML missing a key still yields a complete config via the fallback."""
    config = _load_config_defaults()
    for key in _FALLBACK_CONFIG:
        assert key in config, f"{key} missing from the resolved config"
