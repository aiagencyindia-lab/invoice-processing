# SDLC Evidence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** CI fails any change that ships a known high/critical vulnerable dependency, today's findings are fixed or waived with an expiry, Dependabot proposes upgrades weekly, ruff's security rules guard the code, and the change process is written down.

**Architecture:** A stdlib-only `scripts/audit_gate.py` runs pip-audit (via `uvx`, on the locked production requirements) and `npm audit --omit=dev`, normalises both into `Finding`s, and applies `security/audit-allowlist.json` (id + reason + expiry). Pure functions carry the logic and are unit-tested on canned JSON; a new CI job runs the script. Upgrades land in `uv.lock` and `frontend/package-lock.json`; ruff's `S` rules are enabled with test-only ignores and per-site fixes in product code.

**Tech Stack:** Python 3.12 (stdlib only for the gate), uv, pip-audit 2.10.1 via uvx, npm 10, ruff, GitHub Actions, Dependabot.

**Spec:** `docs/superpowers/specs/2026-10-13-sdlc-evidence-design.md`

## Global Constraints

- Always `./.venv/bin/python`; run from `/Users/vineet/Downloads/ADK/invoice-processing`; tests with `-q -p no:cacheprovider`.
- `scripts/audit_gate.py` imports only the standard library (CI runs it with the runner's `python3`).
- Blocking severities: `{"high", "critical"}`. Every Python finding is treated as `high` (pip-audit's JSON has no severity).
- Allowlist file `security/audit-allowlist.json`: a JSON list of `{"id", "reason", "expires"}`; `reason` non-empty; `expires` an ISO date. An entry matches a finding by its advisory id or any alias. An entry past its expiry fails the build whether or not it matches anything. A malformed file fails the build.
- pip-audit is pinned: `uvx pip-audit==2.10.1`.
- Allow-listed today, expiring `2026-12-12`: Starlette `PYSEC-2026-161`, `PYSEC-2026-248`, `PYSEC-2026-249`, `PYSEC-2026-2280`, `PYSEC-2026-2281` (reason: needs Starlette 1.x, which needs the google-adk 2.x / aiplatform 2.x migration); PyJWT `PYSEC-2026-4146` (reason: no fixed version published).
- Upgrades: `next` and `eslint-config-next` → `16.3.8`; npm `overrides` `"@grpc/grpc-js": "^1.14.6"`; Python `uv lock --upgrade-package` for aiohttp, anyio, authlib, cryptography, httplib2, idna, mako, mcp, multidict, pillow, pyasn1, pydantic-settings, pyjwt, python-multipart, sqlparse, urllib3. No change to `pyproject.toml` dependency specifiers.
- `tests/test_frontend_reaches_the_flow.py` greps frontend source for exact strings: run the full Python suite after any frontend change.
- Every `# noqa` carries its reason on the same line. Comments explain why; ruff check + format clean; commit subjects imperative and say why; trailer `Co-Authored-By: Claude <model that wrote it> <noreply@anthropic.com>`.

## Review Focus

1. pip-audit lists the same advisory twice for one package (the real report does) — the gate must count it once. Test in Task 1.
2. An npm `via` entry that is a plain string (a transitive pointer, not an advisory) must not become a finding or crash. Test in Task 1.
3. pip-audit or npm exiting with an unexpected code or printing non-JSON (network down) must fail the gate with a clear message, not pass. Test in Task 1.
4. An allowlist entry matched through an alias (GHSA id on a PYSEC finding) must waive it. Test in Task 1.
5. The upgrades must not change app behaviour: the full Python suite, the eval gate and e2e must pass after Task 2.

---

### Task 1: The audit gate script

**Files:**
- Create: `scripts/audit_gate.py`, `tests/test_audit_gate.py`

**Interfaces:**
- Produces (module `scripts/audit_gate.py`):
  - `Finding` (frozen dataclass): `ecosystem: str`, `package: str`, `version: str`, `advisory: str`, `aliases: tuple[str, ...]`, `severity: str`, `fixed: str`
  - `class AuditError(RuntimeError)` — malformed allowlist or an audit tool that could not answer
  - `findings_from_pip_audit(report: dict) -> list[Finding]`
  - `findings_from_npm_audit(report: dict) -> list[Finding]`
  - `load_allowlist(text: str) -> list[dict]` (raises `AuditError`)
  - `gate(findings: list[Finding], allowlist: list[dict], today: date) -> tuple[list[str], bool]` — report lines and `True` when the build must fail
  - `parse_tool_output(name: str, returncode: int, stdout: str) -> dict` (raises `AuditError` unless returncode in {0, 1} and stdout is a JSON object)
  - `main() -> int`

- [ ] **Step 1: Failing tests** — `tests/test_audit_gate.py`:

```python
"""The dependency gate: what fails a build, and what a waiver may excuse."""

import importlib.util
import json
from datetime import date
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parent.parent / "scripts" / "audit_gate.py"
_spec = importlib.util.spec_from_file_location("audit_gate", _PATH)
audit_gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(audit_gate)

TODAY = date(2026, 10, 13)

PIP = {
    "dependencies": [
        {"name": "aiohappyeyeballs", "version": "2.6.1", "vulns": []},
        {
            "name": "starlette",
            "version": "0.52.1",
            "vulns": [
                {"id": "PYSEC-2026-161", "fix_versions": ["1.0.1"],
                 "aliases": ["GHSA-86qp-5c8j-p5mr", "CVE-2026-48710"]},
                # pip-audit really does repeat an advisory.
                {"id": "PYSEC-2026-161", "fix_versions": ["1.0.1"],
                 "aliases": ["GHSA-86qp-5c8j-p5mr"]},
            ],
        },
    ]
}

NPM = {
    "vulnerabilities": {
        "next": {
            "severity": "critical",
            "via": [
                {"source": 1, "name": "next", "url": "https://github.com/advisories/GHSA-2xp9-vwfh-vxw4",
                 "severity": "critical", "range": ">=16.0.0 <16.3.8"},
            ],
        },
        "firebase": {"severity": "high", "via": ["@firebase/firestore"]},
        "brace-expansion": {
            "severity": "moderate",
            "via": [
                {"source": 2, "name": "brace-expansion", "url": "https://github.com/advisories/GHSA-mod1",
                 "severity": "moderate", "range": "<2.0.2"},
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
            "starlette", "PYSEC-2026-161", "high"
        )
        assert "GHSA-86qp-5c8j-p5mr" in finding.aliases
        assert finding.fixed == "1.0.1"

    def test_npm_findings_come_from_advisories_not_pointers(self):
        findings = audit_gate.findings_from_npm_audit(NPM)
        assert sorted((f.package, f.advisory, f.severity) for f in findings) == [
            ("brace-expansion", "GHSA-mod1", "moderate"),
            ("next", "GHSA-2xp9-vwfh-vxw4", "critical"),
        ]


class TestGate:
    def test_a_high_finding_fails(self):
        _, failed = audit_gate.gate(audit_gate.findings_from_pip_audit(PIP), [], TODAY)
        assert failed

    def test_a_moderate_finding_passes(self):
        moderate = [f for f in audit_gate.findings_from_npm_audit(NPM) if f.severity == "moderate"]
        _, failed = audit_gate.gate(moderate, [], TODAY)
        assert not failed

    def test_an_allowlisted_id_passes(self):
        lines, failed = audit_gate.gate(
            audit_gate.findings_from_pip_audit(PIP), [entry("PYSEC-2026-161")], TODAY
        )
        assert not failed
        assert any("waived until 2026-12-12" in line for line in lines)

    def test_an_alias_matches_too(self):
        _, failed = audit_gate.gate(
            audit_gate.findings_from_pip_audit(PIP), [entry("GHSA-86qp-5c8j-p5mr")], TODAY
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
        _, failed = audit_gate.gate([], [entry("GHSA-gone", expires="2026-10-01")], TODAY)
        assert failed


class TestAllowlistFile:
    def test_a_valid_file_loads(self):
        assert audit_gate.load_allowlist(json.dumps([entry("X-1")])) == [entry("X-1")]

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
        assert audit_gate.parse_tool_output("npm audit", code, '{"vulnerabilities": {}}') == {
            "vulnerabilities": {}
        }

    @pytest.mark.parametrize(
        ("code", "stdout"),
        [(2, "{}"), (1, ""), (0, "ERROR: network unreachable"), (1, "[]")],
    )
    def test_a_tool_that_could_not_answer_fails(self, code, stdout):
        with pytest.raises(audit_gate.AuditError):
            audit_gate.parse_tool_output("pip-audit", code, stdout)
```

Run: `./.venv/bin/python -m pytest tests/test_audit_gate.py -q -p no:cacheprovider` — Expected: collection error (`scripts/audit_gate.py` missing).

- [ ] **Step 2: Implement `scripts/audit_gate.py`**:

```python
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
            advisory = url.rsplit("/", 1)[-1] if url else str(via.get("source"))
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
            date.fromisoformat(str(item.get("expires")))
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
            lines.append(f"FAIL  allowlist entry {item['id']} expired {item['expires']}")
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


def parse_tool_output(name: str, returncode: int, stdout: str) -> dict:
    # Both tools exit 1 when they find something; anything else means they
    # could not answer (network, lockfile), which must not read as "clean".
    if returncode not in (0, 1):
        raise AuditError(f"{name} exited {returncode}; no answer to gate on.")
    try:
        report = json.loads(stdout)
    except ValueError as exc:
        raise AuditError(f"{name} printed no JSON report: {stdout[:200]!r}") from exc
    if not isinstance(report, dict):
        raise AuditError(f"{name} printed an unexpected report shape.")
    return report


def _run(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False)


def _python_findings() -> list[Finding]:
    export = _run(
        ["uv", "export", "--frozen", "--no-dev", "--format", "requirements-txt",
         "--no-emit-project"],
        REPO,
    )
    if export.returncode != 0:
        raise AuditError(f"uv export failed: {export.stderr[-500:]}")
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as handle:
        handle.write(export.stdout)
        requirements = handle.name
    try:
        done = _run(
            ["uvx", PIP_AUDIT, "-r", requirements, "--disable-pip",
             "--require-hashes", "-f", "json"],
            REPO,
        )
    finally:
        Path(requirements).unlink(missing_ok=True)
    return findings_from_pip_audit(
        parse_tool_output("pip-audit", done.returncode, done.stdout)
    )


def _npm_findings() -> list[Finding]:
    done = _run(["npm", "audit", "--omit=dev", "--json"], REPO / "frontend")
    return findings_from_npm_audit(
        parse_tool_output("npm audit", done.returncode, done.stdout)
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
```

Run Step 1 — Expected: PASS. `ruff format scripts/audit_gate.py tests/test_audit_gate.py`; `ruff check .` clean.

- [ ] **Step 3: Commit.**

```bash
git add scripts/audit_gate.py tests/test_audit_gate.py
git commit -m "Gate builds on high and critical dependency advisories, with expiring waivers

Co-Authored-By: Claude <model> <noreply@anthropic.com>"
```

---

### Task 2: Fix today's findings, add the allowlist and the CI job

**Files:**
- Create: `security/audit-allowlist.json`
- Modify: `uv.lock`, `frontend/package.json`, `frontend/package-lock.json`, `.github/workflows/ci.yml`

**Interfaces:**
- Consumes: `scripts/audit_gate.py main()` from Task 1.

- [ ] **Step 1: RED — the gate fails today.** Run `python3 scripts/audit_gate.py; echo exit $?` — Expected: `FAIL` lines for the 17 Python packages, `next` (critical) and `@grpc/grpc-js` (high), then exit 1 with a missing-allowlist error first (`security/audit-allowlist.json` does not exist yet → `FAIL  [Errno 2] …`). Create the allowlist (Step 2) and re-run to see the full list of `FAIL` lines.

- [ ] **Step 2: Allowlist** — `security/audit-allowlist.json`:

```json
[
  {"id": "PYSEC-2026-161", "reason": "starlette: fix needs Starlette 1.x, which needs the google-adk 2.x / google-cloud-aiplatform 2.x migration (own sub-project)", "expires": "2026-12-12"},
  {"id": "PYSEC-2026-248", "reason": "starlette: fix needs Starlette 1.x, which needs the google-adk 2.x / google-cloud-aiplatform 2.x migration (own sub-project)", "expires": "2026-12-12"},
  {"id": "PYSEC-2026-249", "reason": "starlette: fix needs Starlette 1.x, which needs the google-adk 2.x / google-cloud-aiplatform 2.x migration (own sub-project)", "expires": "2026-12-12"},
  {"id": "PYSEC-2026-2280", "reason": "starlette: fix needs Starlette 1.x, which needs the google-adk 2.x / google-cloud-aiplatform 2.x migration (own sub-project)", "expires": "2026-12-12"},
  {"id": "PYSEC-2026-2281", "reason": "starlette: fix needs Starlette 1.x, which needs the google-adk 2.x / google-cloud-aiplatform 2.x migration (own sub-project)", "expires": "2026-12-12"},
  {"id": "PYSEC-2026-4146", "reason": "pyjwt: no fixed version published yet", "expires": "2026-12-12"}
]
```

- [ ] **Step 3: Python upgrades.**

```bash
uv lock --upgrade-package aiohttp --upgrade-package anyio --upgrade-package authlib --upgrade-package cryptography --upgrade-package httplib2 --upgrade-package idna --upgrade-package mako --upgrade-package mcp --upgrade-package multidict --upgrade-package pillow --upgrade-package pyasn1 --upgrade-package pydantic-settings --upgrade-package pyjwt --upgrade-package python-multipart --upgrade-package sqlparse --upgrade-package urllib3
uv sync --frozen
```

Expected (dry-run on 2026-10-13): aiohttp 3.14.5, anyio 4.14.2, authlib 1.8.0 (+ joserfc), cryptography 50.0.2, httplib2 0.32.0, idna 3.20, mako 1.4.3, mcp 1.30.0, multidict 7.1.0, pillow 12.3.0, pyasn1 0.6.4, pydantic-settings 2.15.0, pyjwt 2.15.1, pyopenssl 26.4.0, python-multipart 0.0.32, sqlparse 0.6.0, urllib3 2.8.0, yarl 1.25.1. Starlette stays 0.52.1.

- [ ] **Step 4: Frontend upgrades.** In `frontend/package.json` set `"next": "16.3.8"` and `"eslint-config-next": "16.3.8"`, and add a top-level:

```json
  "overrides": {
    "@grpc/grpc-js": "^1.14.6"
  },
```

Then `cd frontend && npm install` (updates `package-lock.json`); confirm `npm ls @grpc/grpc-js` shows 1.14.x and `npm ls next` shows 16.3.8.

- [ ] **Step 5: GREEN.** `python3 scripts/audit_gate.py; echo exit $?` — Expected: only `waived until 2026-12-12` lines for the six allow-listed ids (plus any `ok` moderate npm lines), `Dependency audit: passed`, exit 0. If anything else still FAILs, read it: upgrade it if a fix resolves within constraints, otherwise add an allowlist entry with the reason, and record a ruling.

- [ ] **Step 6: Prove nothing broke.** Full Python suite (`./.venv/bin/python -m pytest tests/ -q -p no:cacheprovider`, all pass) and `bash benchmark/harness/gates.sh` (9 PASS, which includes lint, tsc, e2e 38 on the Next 16.3.8 build and the eval gate).

- [ ] **Step 7: CI job.** In `.github/workflows/ci.yml` add a job after `frontend:` (same indentation as the other jobs):

```yaml
  # Known-vulnerable production dependencies fail the build. Waivers live in
  # security/audit-allowlist.json with a reason and an expiry date.
  audit:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@v4

      - name: Install uv
        uses: astral-sh/setup-uv@v5

      - uses: actions/setup-node@v4
        with:
          node-version: "22"

      - name: Dependency audit
        run: python3 scripts/audit_gate.py
```

(`npm audit` reads `frontend/package-lock.json` and needs no install; `uvx` fetches pip-audit; the script is stdlib only.)

- [ ] **Step 8: Commit.**

```bash
git add security/audit-allowlist.json uv.lock frontend/package.json frontend/package-lock.json .github/workflows/ci.yml
git commit -m "Upgrade vulnerable dependencies and fail CI on new advisories

Next.js 16.3.8 leaves the RCE range; @grpc/grpc-js is pinned past its fix by
override (Firestore's transport, unused by the app); sixteen Python packages
upgrade within today's constraints. Starlette waits for the google-adk 2.x
migration and PyJWT's last advisory has no fix: both are waived until
2026-12-12.

Co-Authored-By: Claude <model> <noreply@anthropic.com>"
```

---

### Task 3: Ruff security rules

**Files:**
- Modify: `pyproject.toml` (`[tool.ruff.lint] select`, `per-file-ignores`), `invoice_processing/api/ingest.py` (~360, ~419-431), `invoice_processing/core/config.py` (~76-81), `invoice_processing/core/impact_assessor.py` (~91-103, ~168), `invoice_processing/shared_libraries/acting/general_invoice_agent.py` (~554-561), `invoice_processing/shared_libraries/alf_engine.py` (~205-210), `invoice_processing/db/po_lock.py` (~24), `invoice_processing/confidence.py` (~244, ~320-321), `invoice_processing/gcp_credentials.py` (~82), `invoice_processing/db/session.py` (~177)

**Interfaces:** none produced; lint only.

- [ ] **Step 1: RED.** Add `"S",    # flake8-bandit: security anti-patterns` to `select`. Run `./.venv/bin/python -m ruff check --select S --output-format concise invoice_processing` — Expected: 16 findings (15 listed in the spec + the `assert` in `db/session.py`).

- [ ] **Step 2: Test and tooling ignores.** In `[tool.ruff.lint.per-file-ignores]` add:

```toml
# Tests, the benchmark harness and maintenance scripts: assert is the test
# idiom, fixture credentials are fake, subprocess calls run our own tools,
# and SQL is built from fixture literals. Product code gets no such pass.
"tests/**" = ["S101", "S105", "S106", "S107", "S108", "S603", "S607", "S608"]
"benchmark/**" = ["S101", "S603", "S607", "S608"]
"scripts/**" = ["S101", "S603", "S607"]
```

(merge with the existing `scripts/verify_phase*.py` and `benchmark/harness/*.py` entries — ruff applies every matching pattern.)

- [ ] **Step 3: Product code, site by site.**
  - `api/ingest.py` attachment loop: log what was skipped, then continue:
    ```python
            except Exception as exc:
                # One undecodable part must not lose the others.
                logger.warning("Skipped an attachment that would not decode: %s", exc)
                continue
    ```
  - `api/ingest.py _parse_mime`, the three guards: replace each `pass` with `logger.debug("Inbound mail: <what> unreadable: %s", exc)` (`except Exception as exc:`), naming "message headers", "From", "Reply-To" respectively.
  - `core/config.py` and `shared_libraries/alf_engine.py` `google.auth.default()` fallback: add `import logging` / `logger = logging.getLogger(__name__)` to `core/config.py` (alf_engine has one), then `except Exception as exc: logger.debug("No ambient Google project: %s", exc)`.
  - `core/impact_assessor.py` both loader guards: add a module logger; `except Exception as exc: logger.warning("Batch case load failed, loading one at a time: %s", exc)` and `logger.warning("Skipped case %s: %s", cid, exc)`. The `random.Random(seed)` line: `# noqa: S311 -- deterministic sampling, not cryptography`.
  - `shared_libraries/acting/general_invoice_agent.py` master-data fallback: add a module logger; `except Exception as exc: logger.warning("Master data unavailable, using built-in defaults: %s", exc)`.
  - `db/po_lock.py`: `hashlib.sha1(key.encode(), usedforsecurity=False).digest()` with comment `# A lock key, not a secret; the digest is unchanged.`
  - `confidence.py` tesseract call: `done = subprocess.run(  # noqa: S603 -- fixed argv, no shell, path from shutil.which`; the two token constants: `# noqa: S105 -- regex tokens, not passwords`.
  - `gcp_credentials.py`: `or "/tmp/gcp-credentials.json"  # noqa: S108 -- written mode 600; GOOGLE_CREDENTIALS_PATH overrides`.
  - `db/session.py`: `assert _Session is not None` →
    ```python
    if _Session is None:  # asserts vanish under python -O
        raise RuntimeError("No session factory: get_engine() did not run.")
    ```
  Check each noqa line stays ≤ 80 columns after `ruff format`; if not, put the reason on the line above as a comment and keep `# noqa: SXXX` on the code line.

- [ ] **Step 4: GREEN.** `./.venv/bin/python -m ruff check .` — Expected: `All checks passed!`. `ruff format --check .` clean. Full Python suite — Expected: all pass (behaviour unchanged; the po_lock digest is identical).

- [ ] **Step 5: Commit.**

```bash
git add pyproject.toml invoice_processing
git commit -m "Enable ruff's security rules and stop swallowing errors silently

Co-Authored-By: Claude <model> <noreply@anthropic.com>"
```

---

### Task 4: Dependabot, CODEOWNERS, SECURITY.md

**Files:**
- Create: `.github/dependabot.yml`, `.github/CODEOWNERS`, `SECURITY.md`, `tests/test_repo_policy.py`

- [ ] **Step 1: Failing test** — `tests/test_repo_policy.py`:

```python
"""The written change-control policy exists and says what CI enforces."""

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent


def test_dependabot_watches_every_ecosystem_weekly():
    config = yaml.safe_load((REPO / ".github" / "dependabot.yml").read_text())
    watched = {(u["package-ecosystem"], u["directory"]) for u in config["updates"]}
    assert watched == {("uv", "/"), ("npm", "/frontend"), ("github-actions", "/")}
    assert all(u["schedule"]["interval"] == "weekly" for u in config["updates"])


def test_every_path_has_an_owner():
    lines = (REPO / ".github" / "CODEOWNERS").read_text().splitlines()
    assert "* @vineetyad" in lines


def test_security_policy_says_how_to_report_and_what_ci_enforces():
    text = (REPO / "SECURITY.md").read_text()
    for phrase in ("Reporting a vulnerability", "aiagencyindia@gmail.com",
                   "audit-allowlist.json", "pull request", "not enforced"):
        assert phrase in text, phrase
```

Run it — Expected: FAIL (files missing).

- [ ] **Step 2: `.github/dependabot.yml`**:

```yaml
# Weekly upgrade proposals. Security updates arrive as their own PRs
# regardless of this schedule; the audit gate in CI blocks the rest.
version: 2
updates:
  - package-ecosystem: uv
    directory: /
    schedule:
      interval: weekly
    open-pull-requests-limit: 5
    groups:
      minor-and-patch:
        update-types: [minor, patch]
  - package-ecosystem: npm
    directory: /frontend
    schedule:
      interval: weekly
    open-pull-requests-limit: 5
    groups:
      minor-and-patch:
        update-types: [minor, patch]
  - package-ecosystem: github-actions
    directory: /
    schedule:
      interval: weekly
    open-pull-requests-limit: 5
    groups:
      minor-and-patch:
        update-types: [minor, patch]
```

- [ ] **Step 3: `.github/CODEOWNERS`**:

```
# Every path is owned by the maintainer. Review requests follow from this
# once branch protection is available (see SECURITY.md).
* @vineetyad
```

- [ ] **Step 4: `SECURITY.md`**:

```markdown
# Security

## Reporting a vulnerability

Email aiagencyindia@gmail.com with what you found and how to reproduce it.
Please do not open a public issue. You will get an answer within five
working days.

## How changes reach `main`

Every change lands by pull request. A pull request merges only when CI is
green on its head commit:

- **test**: lint (including ruff's security rules), format, the full test
  suite, the tenant-isolation suite, the evaluation gate and the phase-1
  acceptance sweep;
- **postgres**: migrations up and down, and the database tests against
  Postgres with forced row-level security;
- **frontend**: lint and a production build;
- **e2e**: the browser suite against a production build;
- **audit**: `scripts/audit_gate.py` fails on any high or critical advisory
  in production dependencies. A waiver needs an entry in
  `security/audit-allowlist.json` with a reason and an expiry date, and an
  expired waiver fails the build.

Dependabot proposes dependency upgrades weekly and security fixes as they
are published.

The merge history is the record: every change since the first pull request
is a merge commit naming its pull request, with its CI run attached.

## Known gap

These rules are followed and evidenced but not enforced by GitHub: branch
protection on a private repository owned by a personal account needs a paid
plan. Until the repository moves to a paid plan or an organisation, nothing
technically prevents a direct push to `main`. CODEOWNERS
(`.github/CODEOWNERS`) is in place for when protection is turned on.
```

- [ ] **Step 5: GREEN.** Run the test — Expected: PASS. Full suite once, ruff clean.

- [ ] **Step 6: Commit.**

```bash
git add .github/dependabot.yml .github/CODEOWNERS SECURITY.md tests/test_repo_policy.py
git commit -m "Write down how changes reach main, and propose upgrades weekly

Co-Authored-By: Claude <model> <noreply@anthropic.com>"
```

---

### Finishing checks (controller)

- Full suite, ruff, `bash benchmark/harness/gates.sh` (9 gates); `python3 scripts/audit_gate.py` exits 0.
- Pre-feature proof: `tests/test_audit_gate.py` and `tests/test_repo_policy.py` fail on `main` (missing script and files); the RED gate run from Task 2 Step 1 is the proof for the upgrades.
- The PR's own CI run must show the new `audit` job green.
