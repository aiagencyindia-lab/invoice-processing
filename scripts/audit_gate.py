"""Fail the build when a production dependency has a known serious advisory.

Run in CI as `python3 scripts/audit_gate.py`. Standard library only, so the
job needs no project install to run it.

Two audits, one rule. pip-audit reads the locked production requirements;
npm audit reads the frontend lockfile. A high or critical finding fails the
build unless `security/audit-allowlist.json` waives it with a reason and an
expiry date. A waiver past its expiry fails the build too, matched or not:
an exception that is never revisited is not an exception, it is policy.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ALLOWLIST = REPO / "security" / "audit-allowlist.json"
BLOCKING = {"high", "critical"}
PIP_AUDIT = "pip-audit==2.10.1"


class AuditError(RuntimeError):
    """The gate cannot give an answer, which is itself a failure."""


@dataclass(frozen=True)
class Finding:
    ecosystem: str
    package: str
    version: str
    advisory: str
    aliases: tuple[str, ...]
    severity: str
    fixed: str


def findings_from_pip_audit(report: dict) -> list[Finding]:
    seen: set[tuple[str, str]] = set()
    out = []
    for dep in report.get("dependencies") or []:
        for vuln in dep.get("vulns") or []:
            key = (dep["name"], vuln["id"])
            if key in seen:  # pip-audit repeats an advisory per source
                continue
            seen.add(key)
            out.append(
                Finding(
                    ecosystem="python",
                    package=dep["name"],
                    version=dep.get("version", ""),
                    advisory=vuln["id"],
                    aliases=tuple(vuln.get("aliases") or ()),
                    # pip-audit's JSON has no severity: fail closed.
                    severity="high",
                    fixed=", ".join(vuln.get("fix_versions") or []) or "none",
                )
            )
    return out


def findings_from_npm_audit(report: dict) -> list[Finding]:
    seen: set[tuple[str, str]] = set()
    out = []
    for name, entry in (report.get("vulnerabilities") or {}).items():
        for via in entry.get("via") or []:
            # A string is a pointer to another package's entry, which lists
            # the advisory itself; counting it here would count it twice.
            if not isinstance(via, dict):
                continue
            url = via.get("url") or ""
            if not url and via.get("source") is None:
                # Unwaivable and unnamed: refuse rather than invent an id.
                raise AuditError(f"npm advisory for {name} has no id: {via!r}")
            advisory = url.rsplit("/", 1)[-1] if url else str(via["source"])
            package = via.get("name") or name
            if (package, advisory) in seen:
                continue
            seen.add((package, advisory))
            out.append(
                Finding(
                    ecosystem="npm",
                    package=package,
                    version=via.get("range", ""),
                    advisory=advisory,
                    aliases=(),
                    severity=str(via.get("severity") or "high").lower(),
                    fixed="see npm audit",
                )
            )
    return out


def load_allowlist(text: str) -> list[dict]:
    try:
        entries = json.loads(text)
    except ValueError as exc:
        raise AuditError(f"{ALLOWLIST.name} is not JSON: {exc}") from exc
    if not isinstance(entries, list):
        raise AuditError(f"{ALLOWLIST.name} must be a JSON list of entries.")
    for item in entries:
        if not isinstance(item, dict) or not item.get("id"):
            raise AuditError(f"Allowlist entry without an id: {item!r}")
        if not str(item.get("reason") or "").strip():
            raise AuditError(f"Allowlist entry {item['id']} gives no reason.")
        try:
            if not isinstance(item.get("expires"), str):
                raise ValueError("not text")
            date.fromisoformat(item["expires"])
        except ValueError as exc:
            raise AuditError(
                f"Allowlist entry {item['id']} needs expires as YYYY-MM-DD."
            ) from exc
    return entries


def gate(
    findings: list[Finding], allowlist: list[dict], today: date
) -> tuple[list[str], bool]:
    by_id = {item["id"]: item for item in allowlist}
    lines: list[str] = []
    failed = False
    for item in allowlist:
        if date.fromisoformat(item["expires"]) < today:
            lines.append(
                f"FAIL  allowlist entry {item['id']} expired {item['expires']}"
            )
            failed = True
    for f in findings:
        label = (
            f"{f.ecosystem} {f.package} {f.version} {f.advisory} "
            f"[{f.severity}] fixed in: {f.fixed}"
        )
        if f.severity not in BLOCKING:
            lines.append(f"ok    {label}")
            continue
        waiver = next(
            (by_id[i] for i in (f.advisory, *f.aliases) if i in by_id), None
        )
        if waiver is None:
            lines.append(f"FAIL  {label}")
            failed = True
        elif date.fromisoformat(waiver["expires"]) < today:
            lines.append(f"FAIL  {label} (waiver expired {waiver['expires']})")
            failed = True
        else:
            lines.append(f"waived until {waiver['expires']}  {label}")
    return lines, failed


def parse_tool_output(
    name: str, returncode: int, stdout: str, required_key: str
) -> dict:
    # Both tools exit 1 when they find something; anything else means they
    # could not answer (network, lockfile), which must not read as "clean".
    if returncode not in (0, 1):
        raise AuditError(f"{name} exited {returncode}; no answer to gate on.")
    try:
        report = json.loads(stdout)
    except ValueError as exc:
        raise AuditError(
            f"{name} printed no JSON report: {stdout[:200]!r}"
        ) from exc
    if not isinstance(report, dict):
        raise AuditError(f"{name} printed an unexpected report shape.")
    # npm answers a registry failure with exit 0 and {"error": ...}: a
    # report without its findings key is a tool that could not answer.
    if "error" in report or required_key not in report:
        detail = (
            report.get("message") or report.get("error") or "no findings key"
        )
        raise AuditError(f"{name} could not audit: {str(detail)[:200]}")
    return report


def _run(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, cwd=cwd, capture_output=True, text=True, check=False
    )


def _python_findings() -> list[Finding]:
    export = _run(
        [
            "uv",
            "export",
            "--frozen",
            "--no-dev",
            "--format",
            "requirements-txt",
            "--no-emit-project",
        ],
        REPO,
    )
    if export.returncode != 0:
        raise AuditError(f"uv export failed: {export.stderr[-500:]}")
    with tempfile.NamedTemporaryFile(
        "w", suffix=".txt", delete=False
    ) as handle:
        handle.write(export.stdout)
        requirements = handle.name
    try:
        done = _run(
            [
                "uvx",
                PIP_AUDIT,
                "-r",
                requirements,
                "--disable-pip",
                "--require-hashes",
                "-f",
                "json",
            ],
            REPO,
        )
    finally:
        Path(requirements).unlink(missing_ok=True)
    return findings_from_pip_audit(
        parse_tool_output(
            "pip-audit", done.returncode, done.stdout, "dependencies"
        )
    )


def _npm_findings() -> list[Finding]:
    done = _run(["npm", "audit", "--omit=dev", "--json"], REPO / "frontend")
    return findings_from_npm_audit(
        parse_tool_output(
            "npm audit", done.returncode, done.stdout, "vulnerabilities"
        )
    )


def main() -> int:
    try:
        allowlist = load_allowlist(ALLOWLIST.read_text(encoding="utf-8"))
        findings = _python_findings() + _npm_findings()
    except (AuditError, OSError) as exc:
        print(f"FAIL  {exc}")
        return 1
    lines, failed = gate(findings, allowlist, date.today())
    print("\n".join(lines) or "No advisories in production dependencies.")
    print("Dependency audit: FAILED" if failed else "Dependency audit: passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
