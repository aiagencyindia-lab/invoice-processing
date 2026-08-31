"""Tests for eval comparators and path resolution."""

import sys
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(AGENT_DIR / "eval"))

from eval import AGENT_DIR as EVAL_AGENT_DIR  # noqa: E402
from eval import (  # noqa: E402
    EVAL_OUTPUT_DIR,
    compare_field_group,
    normalize_identifier,
)

VENDOR_GROUP = {
    "section": "Vendor Information",
    "fields": [
        {"name": "Vendor Name", "type": "string"},
        {"name": "Tax ID", "type": "identifier"},
    ],
}


class TestNormalizeIdentifier:
    """Tax IDs print with grouping spaces but are stored unspaced."""

    def test_spaced_and_unspaced_abn_are_equal(self):
        assert normalize_identifier("51 824 753 556") == normalize_identifier(
            "51824753556"
        )

    def test_distinct_identifiers_still_differ(self):
        assert normalize_identifier("51824753556") != normalize_identifier(
            "64560747633"
        )

    def test_none_and_empty(self):
        assert normalize_identifier(None) == ""
        assert normalize_identifier("  ") == ""


class TestCompareFieldGroup:
    """The schema-driven comparator must honour the identifier type."""

    def test_spaced_tax_id_matches_unspaced_ground_truth(self):
        gt = {
            "Vendor Information": {
                "Vendor Name": "QuickFix",
                "Tax ID": "51824753556",
            }
        }
        agent = {
            "Vendor Information": {
                "Vendor Name": "QuickFix",
                "Tax ID": "51 824 753 556",
            }
        }
        result = compare_field_group(gt, agent, VENDOR_GROUP, 0.02)
        assert result["Tax ID"]["match"] is True
        assert result["all_match"] is True

    def test_wrong_tax_id_still_fails(self):
        gt = {
            "Vendor Information": {
                "Vendor Name": "QuickFix",
                "Tax ID": "51824753556",
            }
        }
        agent = {
            "Vendor Information": {
                "Vendor Name": "QuickFix",
                "Tax ID": "64 560 747 633",
            }
        }
        result = compare_field_group(gt, agent, VENDOR_GROUP, 0.02)
        assert result["Tax ID"]["match"] is False
        assert result["all_match"] is False

    def test_string_fields_keep_internal_spaces_significant(self):
        group = {
            "section": "Invoice Processing",
            "fields": [{"name": "Invoice Status", "type": "string"}],
        }
        gt = {"Invoice Processing": {"Invoice Status": "Pending Payment"}}
        agent = {"Invoice Processing": {"Invoice Status": "PendingPayment"}}
        assert compare_field_group(gt, agent, group, 0.02)["all_match"] is False


class TestPathResolution:
    """Relative paths resolve against the agent root, not the repo root."""

    def test_agent_dir_contains_the_package(self):
        assert (EVAL_AGENT_DIR / "invoice_processing").is_dir()
        assert (
            EVAL_AGENT_DIR / "invoice_processing" / "exemplary_data"
        ).is_dir()

    def test_eval_output_dir_lives_inside_the_package(self):
        assert EVAL_OUTPUT_DIR == (
            EVAL_AGENT_DIR / "invoice_processing" / "data" / "eval_results"
        )


def test_master_data_declares_tax_id_as_identifier():
    """The comparator fix only takes effect if the config opts Tax ID in."""
    import yaml  # noqa: PLC0415

    path = (
        AGENT_DIR
        / "invoice_processing"
        / "shared_libraries"
        / "invoice_master_data.yaml"
    )
    md = yaml.safe_load(path.read_text())
    groups = md["eval_schema"]["comparison_groups"]
    vendor = next(g for g in groups if g["id"] == "vendor")
    tax_id = next(f for f in vendor["fields"] if f["name"] == "Tax ID")
    assert tax_id["type"] == "identifier"
