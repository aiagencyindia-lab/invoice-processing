"""One final approval at a time per purchase order.

Step 2.3 counts as billed only invoices already approved, and an approval
lands in that count when its workflow is written and projected. Two final
approvals on the same PO that each re-check before either is written would
both pass against the same received units. This lock serialises them from the
re-check to the projection, so the second re-checks against the first.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import text

from .. import po_matching
from .session import get_engine, is_postgres


def _digest(tenant_id: str, po_number) -> bytes:
    key = f"{tenant_id}\x00{po_matching.normalize_po_number(po_number)}"
    # A lock key, not a secret; the digest is unchanged.
    return hashlib.sha1(key.encode(), usedforsecurity=False).digest()


@contextmanager
def po_lock(
    tenant_id: str, po_number, url: str | None = None
) -> Iterator[None]:
    # ponytail: one lock per PO, so approvals on unrelated lines of the same
    # PO also wait for each other; lock per PO line if a PO sees heavy traffic.
    digest = _digest(tenant_id, po_number)
    engine = get_engine(url)
    if is_postgres(engine):
        key = int.from_bytes(digest[:8], "big", signed=True)
        # A dedicated connection: a session-level advisory lock is held by
        # the connection, not by any transaction the approval runs.
        with engine.connect() as connection:
            connection.execute(text("select pg_advisory_lock(:k)"), {"k": key})
            connection.commit()
            try:
                yield
            finally:
                connection.execute(
                    text("select pg_advisory_unlock(:k)"), {"k": key}
                )
                connection.commit()
        return
    database = engine.url.database
    if not database or database == ":memory:":
        yield  # private to one connection: nothing to share
        return
    # ponytail: fcntl is POSIX only, as in migrate.py.
    import fcntl  # noqa: PLC0415

    with open(f"{database}.po-{digest.hex()}.lock", "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
