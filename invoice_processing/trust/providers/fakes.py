"""Deterministic providers for tests and demos."""

from __future__ import annotations

from ..modes import CAPABILITIES
from .base import METHOD


class FakeProvider:
    """Answers scripted by the first positional argument; an Exception value
    is raised."""

    def __init__(
        self, script: dict[str, dict | list | Exception] | None = None
    ):
        self.script = dict(script or {})
        self.calls: list[tuple] = []

    def _answer(self, *args, **kwargs):
        self.calls.append(args)
        key = str(args[0]) if args else ""
        value = self.script.get(key, {"result": "unknown"})
        if isinstance(value, Exception):
            raise value
        return value

    verify = lookup = check = fetch = search = hours = track = _answer


def install(registry) -> None:
    for capability in CAPABILITIES:
        registry.register(capability, "fake", FakeProvider)
    missing = [m for m in METHOD.values() if not hasattr(FakeProvider, m)]
    if missing:
        raise RuntimeError(f"FakeProvider lacks {missing}")
