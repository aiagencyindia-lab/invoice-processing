"""
Rule validation and display.

It used to write rules too, straight into `rule_base.json`: load the whole
document, mutate a list, write it back, with a timestamped copy beside it and
no lock or version check anywhere. Two people teaching a rule at the same time
each read the same document, each allocated the same `ALF-00N` by scanning for
the highest, and the second write silently dropped the first rule.

Rules are stored per tenant now, through a repository with optimistic
concurrency, and approving one is a status change on a single row. What is
left here is the part that was always pure: does this rule refer to fields
that exist, does it use operators the engine implements, and how does it read
to a person.
"""

import json
from dataclasses import dataclass

from ..context import UnknownFieldPath, validate_field_path
from ..shared_libraries.alf_engine import (
    SUPPORTED_ACTION_TYPES,
    SUPPORTED_CONDITION_OPERATORS,
)

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class WriteResult:
    """Structured output from RuleWriterAgent."""

    success: bool
    rule_id: str
    mode: str  # "add" or "update"
    backup_path: str = ""
    total_rules: int = 0
    message: str = ""


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class RuleWriterAgent:
    """
    Sub-agent: Manages rule_base.json read/write operations.
    """

    def validate_rule(self, rule_dict: dict) -> list[str]:
        """
        Validate a rule dict against the schema.

        Returns list of error messages (empty if valid).
        """
        errors = []

        # Required fields
        for field in ["id", "name", "conditions", "actions"]:
            if field not in rule_dict:
                errors.append(f"Missing required field: '{field}'")

        # Validate conditions
        for i, cond in enumerate(rule_dict.get("conditions", [])):
            if "field" not in cond:
                errors.append(f"Condition {i}: missing 'field'")
            if "operator" not in cond:
                errors.append(f"Condition {i}: missing 'operator'")
            elif cond["operator"] not in SUPPORTED_CONDITION_OPERATORS:
                errors.append(
                    f"Condition {i}: unsupported operator '{cond['operator']}'. "
                    f"Supported: {sorted(SUPPORTED_CONDITION_OPERATORS)}"
                )

            # A condition on an unknown field resolves to None and evaluates to
            # False, so the rule silently never fires. Catch it at write time
            # rather than letting an SME approve a rule that does nothing.
            if "field" in cond:
                try:
                    validate_field_path(cond["field"], cond.get("operator"))
                except UnknownFieldPath as exc:
                    errors.append(f"Condition {i}: {exc}")

        # Validate actions
        for i, act in enumerate(rule_dict.get("actions", [])):
            if "type" not in act:
                errors.append(f"Action {i}: missing 'type'")
            elif act["type"] not in SUPPORTED_ACTION_TYPES:
                errors.append(
                    f"Action {i}: unsupported type '{act['type']}'. "
                    f"Supported: {sorted(SUPPORTED_ACTION_TYPES)}"
                )

        return errors

    def _format_conditions(self, rule_dict: dict, lines: list) -> None:
        """Append formatted conditions to display lines."""
        lines.append("Conditions:")
        for i, c in enumerate(rule_dict.get("conditions", []), 1):
            desc = c.get("description", "")
            val_display = c.get("value", "")
            if isinstance(val_display, list):
                val_display = json.dumps(val_display)
            elif val_display is None:
                val_display = "(null)"
            lines.append(
                f"  {i}. {c.get('field', '?')} {c.get('operator', '?')} {val_display}"
            )
            if desc:
                lines.append(f"     ({desc})")

    def _format_actions(self, rule_dict: dict, lines: list) -> None:
        """Append formatted actions to display lines."""
        lines.append("Actions:")
        for i, a in enumerate(rule_dict.get("actions", []), 1):
            action_type = a.get("type", "?")
            if action_type == "llm_continue_processing":
                lines.append(
                    f"  {i}. {action_type} from {a.get('resume_from', '?')}"
                )
                ctx = a.get("correction_context", "")
                if ctx:
                    # Show first 200 chars
                    lines.append(f"     Context: {ctx[:200]}...")
            elif action_type == "llm_patch_fields":
                fields = a.get("target_fields", [])
                lines.append(f"  {i}. {action_type}: {', '.join(fields)}")
            else:
                lines.append(f"  {i}. {action_type}")

    def _format_metadata(self, rule_dict: dict, lines: list) -> None:
        """Append formatted metadata to display lines if present."""
        meta = rule_dict.get("metadata", {})
        if meta:
            lines.append("")
            lines.append("Metadata:")
            if meta.get("severity"):
                lines.append(f"  Severity: {meta['severity']}")
            if meta.get("root_cause"):
                lines.append(f"  Root cause: {meta['root_cause']}")
            if meta.get("rules_book_section"):
                lines.append(f"  Rules book: {meta['rules_book_section']}")

    def format_rule_display(self, rule_dict: dict) -> str:
        """Format a rule dict for terminal display."""
        lines = []
        lines.append(f"=== Proposed Rule: {rule_dict.get('id', '?')} ===")
        lines.append(f"Name: {rule_dict.get('name', '?')}")
        lines.append(f"Description: {rule_dict.get('description', '?')}")
        lines.append(f"Scope: {rule_dict.get('scope', '?')}")
        lines.append(f"Priority: {rule_dict.get('priority', '?')}")
        lines.append("")

        self._format_conditions(rule_dict, lines)
        lines.append("")
        self._format_actions(rule_dict, lines)
        self._format_metadata(rule_dict, lines)

        return "\n".join(lines)
