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

from .. import paths
from ..adapters import JsonlAuditSink
from .queue import LocalQueue, get_queue
from .runner import ClaimStore, Worker


def main(argv: list[str] | None = None) -> int:
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

    worker = Worker(
        queue=queue,
        claims=ClaimStore(paths.DATA_DIR / "claims"),
        audit=JsonlAuditSink(),
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
