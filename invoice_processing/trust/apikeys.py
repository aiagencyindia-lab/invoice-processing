"""Per-tenant API keys for the /v2 API. The v2/w3-api branch fills
`resolve`; `api.service` registers it with `auth.set_api_key_resolver`."""

from __future__ import annotations

PREFIX = "ik_"


def resolve(token: str):
    """The Principal a key stands for, or None when there is no such key."""
    return None
