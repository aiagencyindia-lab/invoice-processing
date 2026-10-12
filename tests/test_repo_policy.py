"""The written change-control policy exists and says what CI enforces."""

from pathlib import Path

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
