"""What each governed route calls just before its write. Filled by the policy
branch (v2/w4-policy): read the `agents` mode (off → None, no agent query),
find the agent behind the principal, evaluate, record, and in enforcing
refuse a BLOCK with a 403."""

from __future__ import annotations


def check(principal, action, *, settings_store=None, now=None, store=None):
    return None
