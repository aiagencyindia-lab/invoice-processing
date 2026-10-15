"""Pick fake or real per tenant, call it, and keep the raw answer as evidence.

# ponytail: evidence is a graph node, not its own table; add one if evidence
# volume outgrows graph_nodes.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from datetime import datetime, timezone

from ... import masking
from ...ports import RunContext
from ..modes import CAPABILITIES, provider_name
from .base import METHOD, ProviderResult

logger = logging.getLogger(__name__)


class Registry:
    def __init__(self, graph):
        self.graph = graph
        self._factories: dict[tuple[str, str], Callable[[], object]] = {}

    def register(
        self, capability: str, name: str, factory: Callable[[], object]
    ) -> None:
        if capability not in CAPABILITIES:
            raise ValueError(f"No such capability: {capability}")
        self._factories[(capability, name)] = factory

    def call(
        self,
        ctx: RunContext,
        capability: str,
        *args,
        settings_store=None,
        **kwargs,
    ) -> ProviderResult:
        if capability not in CAPABILITIES:
            raise ValueError(f"No such capability: {capability}")
        name = provider_name(ctx.tenant_id, capability, settings_store)
        factory = self._factories.get((capability, name))
        if factory is None:
            raise ValueError(
                f"No provider {name!r} registered for {capability}."
            )
        observed_at = datetime.now(timezone.utc)
        try:
            data = getattr(factory(), METHOD[capability])(*args, **kwargs)
            status = "ok"
        except (
            Exception
        ) as exc:  # the provider's trouble is evidence, not a pass
            # A verifier's error can echo the request, account and all.
            data = {"error": masking.mask_text(str(exc))[:500]}
            status = "unavailable"
            logger.warning(
                "provider_unavailable",
                extra={"capability": capability, "provider": name},
            )
        if not isinstance(data, dict):
            data = {"items": data}
        raw = json.dumps(data, sort_keys=True, default=str)
        sha = hashlib.sha256(raw.encode()).hexdigest()
        node = self.graph.upsert_node(
            ctx,
            kind="provider_evidence",
            natural_key=f"{capability}:{sha}",
            attrs={
                "capability": capability,
                "provider": name,
                "status": status,
                "raw_sha256": sha,
                "observed_at": observed_at.isoformat(),
                "data": masking.mask_row(data),
            },
            at=observed_at,
        )
        return ProviderResult(
            status, data, sha, name, capability, observed_at, node
        )
