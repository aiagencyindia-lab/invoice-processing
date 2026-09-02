"""Run the worker.

python -m invoice_processing.worker            # drain forever
python -m invoice_processing.worker --once     # one pass, for a cron or a test
python -m invoice_processing.worker --dead     # inspect the dead letters
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from ..api import service
from ..gcp_credentials import materialise_credentials
from .queue import LocalQueue, get_queue
from .runner import AttemptLog, Worker, _worker_id


def main(argv: list[str] | None = None) -> int:
    # Before anything can construct a Google client. A platform that lets a
    # service set its own start command replaces the image's entrypoint with
    # it, so a credential bridged only there is not there at all.
    materialise_credentials()

    parser = argparse.ArgumentParser(prog="worker")
    parser.add_argument(
        "--once", action="store_true", help="One pass, then exit."
    )
    parser.add_argument(
        "--dead", action="store_true", help="List dead letters."
    )
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    queue = get_queue()

    if args.dead:
        if not isinstance(queue, LocalQueue):
            print("Dead letters live in the Pub/Sub dead-letter topic.")
            return 0
        print(json.dumps(queue.dead_letters(), indent=2))
        return 0

    # The worker can be the first process up after a deploy, and it writes to
    # every table the API reads. The advisory lock inside means whichever of
    # them gets there first migrates and the rest wait.
    if service._database_configured():
        from ..db.migrate import upgrade_to_head  # noqa: PLC0415

        upgrade_to_head()

    worker = Worker(
        queue=queue,
        # Resolved, not hardcoded: a claim on container-local disk excludes
        # only the processes sharing that disk, which across two containers
        # is none of the ones that matter.
        claims=service.get_claim_store(worker_id=_worker_id()),
        # The resolved sink, not a hardcoded file: a worker writing its audit
        # trail to container-local disk loses it on every restart, which is
        # exactly the events an auditor would ask about.
        audit=service.get_audit(),
        # Counted through the case store rather than trusted from the queue:
        # Pub/Sub reports every redelivery as attempt 1 unless the
        # subscription has a dead-letter policy, and this one does not.
        attempts=AttemptLog(service.get_cases()),
    )
    if args.once:
        for outcome in worker.run_once():
            print(outcome)
        return 0

    try:
        worker.run_forever(poll_seconds=args.poll_seconds)
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
