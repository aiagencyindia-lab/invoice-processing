"""Benchmark fault families, one module per engine (Wave 1 onwards).

A module `benchmark/history/faults/<engine>.py` exports:

* `inject(rng, rows, faults, *, start, months) -> None` — append or change
  rows (keyed by history kind) and append one fault per injected case:
  `{"type", "doc_number", "vendor_key", "expected_impact"}`.
* `GATES: dict[type, {"precision": float, "recall": float, "impact_error": float}]`.
* optional `seed_graph(ctx, graph, data_dir)` for state that is not a CSV row,
  and `provider_scripts: dict[capability, script]` for the fake providers.

Discovered, never listed, so engines built in parallel never edit this file.
"""

from __future__ import annotations

import importlib
import pkgutil


def modules() -> list:
    return [
        importlib.import_module(f"{__name__}.{info.name}")
        for info in sorted(pkgutil.iter_modules(__path__), key=lambda i: i.name)
        if not info.name.startswith("_")
    ]
