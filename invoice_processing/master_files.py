"""Reference data a tenant loads: vendors, work authorizations.

One reader for all of them, because the purchase-order loader had already been
written twice -- once in the pipeline and once in the settings API -- and the
two disagreed about whether a bare list was acceptable.

The contract each caller depends on:

* **A missing file means "nothing is known", never "everything is fine".** A
  check that cannot see its reference data must say so; the callers here turn
  that into "not found" evidence rather than a silent pass.
* **Unreadable JSON is reported, not swallowed.** A truncated export is the
  most likely way this data goes wrong, and it would otherwise look identical
  to a tenant that has loaded nothing.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def read_records(path: Path | str | None, key: str) -> list[dict]:
    """Records from `{key: [...]}`, a bare list, or a dict of records."""
    if not path:
        return []
    path = Path(path)
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning(
            "%s could not be read (%s); treating it as empty.", path.name, exc
        )
        return []
    records = raw.get(key, raw) if isinstance(raw, dict) else raw
    if isinstance(records, dict):
        records = list(records.values())
    return [r for r in (records or []) if isinstance(r, dict)]


def write_records(path: Path, key: str, records: list[dict]) -> None:
    """Replace the file atomically.

    Replace rather than merge: this is an export from the customer's own
    system, and merging would keep records their system has since removed --
    an invoice would then match a purchase order or authorization that no
    longer exists. Written to a temporary file first so a crash mid-write
    cannot leave the checks reading half a master.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({key: records}, indent=2), encoding="utf-8")
    tmp.replace(path)
