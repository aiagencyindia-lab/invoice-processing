"""The written change-control policy exists and says what CI enforces."""

import re
from pathlib import Path

import tomllib
import yaml

REPO = Path(__file__).resolve().parent.parent


def test_dependabot_watches_every_ecosystem_weekly():
    config = yaml.safe_load((REPO / ".github" / "dependabot.yml").read_text())
    watched = {
        (u["package-ecosystem"], u["directory"]) for u in config["updates"]
    }
    assert watched == {
        ("uv", "/"),
        ("npm", "/frontend"),
        ("github-actions", "/"),
    }
    assert all(u["schedule"]["interval"] == "weekly" for u in config["updates"])


def test_every_path_has_an_owner():
    lines = (REPO / ".github" / "CODEOWNERS").read_text().splitlines()
    assert "* @vineetyad" in lines


def test_security_policy_says_how_to_report_and_what_ci_enforces():
    text = (REPO / "SECURITY.md").read_text()
    for phrase in (
        "Reporting a vulnerability",
        "aiagencyindia@gmail.com",
        "audit-allowlist.json",
        "pull request",
        "not enforced",
    ):
        assert phrase in text, phrase


def _floor(requirement: str) -> str:
    """'google-adk>=2.0,<3' -> '2.0'."""
    match = re.search(r">=\s*([0-9][^,;\s]*)", requirement)
    assert match, requirement
    return match.group(1)


def test_the_agent_stack_is_on_its_second_major():
    """google-adk 1.x pins Starlette below 1.0, where five advisories live.
    The floors keep a resolver from ever going back there, and the lock
    proves the installed Starlette is on the fixed line."""
    project = tomllib.loads((REPO / "pyproject.toml").read_text())
    by_name = {
        re.split(r"[<>=!~\[ ]", dep, maxsplit=1)[0]: dep
        for dep in project["project"]["dependencies"]
    }
    assert _floor(by_name["google-adk"]).startswith("2")
    assert _floor(by_name["google-cloud-aiplatform"]).startswith("2")

    lock = tomllib.loads((REPO / "uv.lock").read_text())
    versions = {pkg["name"]: pkg["version"] for pkg in lock["package"]}
    assert int(versions["starlette"].split(".")[0]) >= 1, versions["starlette"]
    assert "mcp" not in versions
