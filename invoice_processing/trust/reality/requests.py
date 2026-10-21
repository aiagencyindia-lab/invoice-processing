"""Evidence requests (FR-23.5, U4): an internal tracker. Filled in Task 4."""

from __future__ import annotations


class RequestStore:
    def __init__(self, url: str | None = None):
        self.url = url

    def proofs(self, tenant_id: str) -> dict[str, list[str]]:
        return {}
