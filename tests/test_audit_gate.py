"""The dependency gate: what fails a build, and what a waiver may excuse."""

import importlib.util
import json
import sys
from datetime import date
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parent.parent / "scripts" / "audit_gate.py"
_spec = importlib.util.spec_from_file_location("audit_gate", _PATH)
audit_gate = importlib.util.module_from_spec(_spec)
# Registered before running: @dataclass resolves the module by name.
sys.modules["audit_gate"] = audit_gate
_spec.loader.exec_module(audit_gate)

TODAY = date(2026, 10, 13)

PIP = {
    "dependencies": [
        {"name": "aiohappyeyeballs", "version": "2.6.1", "vulns": []},
        {
            "name": "starlette",
            "version": "0.52.1",
            "vulns": [
                {
                    "id": "PYSEC-2026-161",
                    "fix_versions": ["1.0.1"],
                    "aliases": ["GHSA-86qp-5c8j-p5mr", "CVE-2026-48710"],
                },
                # pip-audit really does repeat an advisory.
                {
                    "id": "PYSEC-2026-161",
                    "fix_versions": ["1.0.1"],
                    "aliases": ["GHSA-86qp-5c8j-p5mr"],
                },
            ],
        },
    ]
}

NPM = {
    "vulnerabilities": {
        "next": {
            "severity": "critical",
            "via": [
                {
                    "source": 1,
                    "name": "next",
                    "url": "https://github.com/advisories/GHSA-2xp9-vwfh-vxw4",
                    "severity": "critical",
                    "range": ">=16.0.0 <16.3.8",
                },
            ],
        },
        "firebase": {"severity": "high", "via": ["@firebase/firestore"]},
        "brace-expansion": {
            "severity": "moderate",
            "via": [
                {
                    "source": 2,
                    "name": "brace-expansion",
                    "url": "https://github.com/advisories/GHSA-mod1",
                    "severity": "moderate",
                    "range": "<2.0.2",
                },
            ],
        },
    }
}


def entry(id_, expires="2026-12-12", reason="waiting on upstream"):
    return {"id": id_, "reason": reason, "expires": expires}


class TestReading:
    def test_pip_audit_findings_are_deduplicated_and_high(self):
        [finding] = audit_gate.findings_from_pip_audit(PIP)
        assert (finding.package, finding.advisory, finding.severity) == (
            "starlette",
            "PYSEC-2026-161",
            "high",
        )
        assert "GHSA-86qp-5c8j-p5mr" in finding.aliases
        assert finding.fixed == "1.0.1"

    def test_npm_findings_come_from_advisories_not_pointers(self):
        findings = audit_gate.findings_from_npm_audit(NPM)
        assert sorted(
            (f.package, f.advisory, f.severity) for f in findings
        ) == [
            ("brace-expansion", "GHSA-mod1", "moderate"),
            ("next", "GHSA-2xp9-vwfh-vxw4", "critical"),
        ]


class TestGate:
    def test_a_high_finding_fails(self):
        _, failed = audit_gate.gate(
            audit_gate.findings_from_pip_audit(PIP), [], TODAY
        )
        assert failed

    def test_a_moderate_finding_passes(self):
        moderate = [
            f
            for f in audit_gate.findings_from_npm_audit(NPM)
            if f.severity == "moderate"
        ]
        _, failed = audit_gate.gate(moderate, [], TODAY)
        assert not failed

    def test_an_allowlisted_id_passes(self):
        lines, failed = audit_gate.gate(
            audit_gate.findings_from_pip_audit(PIP),
            [entry("PYSEC-2026-161")],
            TODAY,
        )
        assert not failed
        assert any("waived until 2026-12-12" in line for line in lines)

    def test_an_alias_matches_too(self):
        _, failed = audit_gate.gate(
            audit_gate.findings_from_pip_audit(PIP),
            [entry("GHSA-86qp-5c8j-p5mr")],
            TODAY,
        )
        assert not failed

    def test_an_expired_entry_fails_even_when_it_matches(self):
        lines, failed = audit_gate.gate(
            audit_gate.findings_from_pip_audit(PIP),
            [entry("PYSEC-2026-161", expires="2026-10-01")],
            TODAY,
        )
        assert failed
        assert any("expired" in line for line in lines)

    def test_an_expired_entry_fails_even_when_nothing_matches(self):
        """A waiver must be removed, not forgotten."""
        _, failed = audit_gate.gate(
            [], [entry("GHSA-gone", expires="2026-10-01")], TODAY
        )
        assert failed


class TestAllowlistFile:
    def test_a_valid_file_loads(self):
        assert audit_gate.load_allowlist(json.dumps([entry("X-1")])) == [
            entry("X-1")
        ]

    @pytest.mark.parametrize(
        "text",
        [
            "not json",
            json.dumps({"id": "X"}),
            json.dumps([{"id": "X", "expires": "2026-12-12"}]),
            json.dumps([{"id": "X", "reason": "", "expires": "2026-12-12"}]),
            json.dumps([{"id": "X", "reason": "r", "expires": "someday"}]),
        ],
    )
    def test_a_malformed_file_is_refused(self, text):
        with pytest.raises(audit_gate.AuditError):
            audit_gate.load_allowlist(text)


class TestToolOutput:
    @pytest.mark.parametrize("code", [0, 1])
    def test_findings_or_none_are_both_answers(self, code):
        assert audit_gate.parse_tool_output(
            "npm audit", code, '{"vulnerabilities": {}}', "vulnerabilities"
        ) == {"vulnerabilities": {}}

    @pytest.mark.parametrize(
        ("code", "stdout"),
        [(2, "{}"), (1, ""), (0, "ERROR: network unreachable"), (1, "[]")],
    )
    def test_a_tool_that_could_not_answer_fails(self, code, stdout):
        with pytest.raises(audit_gate.AuditError):
            audit_gate.parse_tool_output(
                "pip-audit", code, stdout, "dependencies"
            )


class TestFailClosed:
    def test_an_npm_registry_error_is_not_a_clean_report(self):
        """npm audit answers a registry failure with exit 0 and an error
        object; reading that as "no vulnerabilities" passed the gate."""
        payload = json.dumps(
            {
                "message": "request to https://registry.npmjs.org failed, reason: ECONNREFUSED",
                "error": {"code": "ECONNREFUSED"},
            }
        )
        with pytest.raises(audit_gate.AuditError):
            audit_gate.parse_tool_output(
                "npm audit", 0, payload, "vulnerabilities"
            )

    def test_a_pip_audit_report_needs_its_key_too(self):
        with pytest.raises(audit_gate.AuditError):
            audit_gate.parse_tool_output("pip-audit", 0, "{}", "dependencies")

    def test_an_expiry_must_be_text(self):
        with pytest.raises(audit_gate.AuditError):
            audit_gate.load_allowlist(
                json.dumps([{"id": "X", "reason": "r", "expires": 20261212}])
            )

    def test_an_npm_advisory_without_url_or_source_is_refused(self):
        report = {
            "vulnerabilities": {
                "x": {"via": [{"name": "x", "severity": "high"}]}
            }
        }
        with pytest.raises(audit_gate.AuditError):
            audit_gate.findings_from_npm_audit(report)
