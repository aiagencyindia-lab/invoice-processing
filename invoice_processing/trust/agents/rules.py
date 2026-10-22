"""Structural controls (FR-28.3): each `fn(action, agent, mandate, store,
settings, now, decider) -> Reason | None`, in severity order. Filled by the
policy branch (v2/w4-policy)."""

from __future__ import annotations

RULES: tuple = ()
