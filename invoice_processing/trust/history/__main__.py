"""Import a history file from the shell.

    python -m invoice_processing.trust.history --tenant <id> --kind ap_invoice \\
        --source tally --file ap.csv --propose
    python -m invoice_processing.trust.history --tenant <id> --kind ap_invoice \\
        --source tally --file ap.csv --mapping '{"Bill No": "doc_number", ...}'
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ...ports import RunContext
from . import mapping as mp
from .importer import HistoryImporter
from .readers import read_table


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="trust-history")
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--kind", required=True, choices=mp.KINDS)
    parser.add_argument("--source", required=True)
    parser.add_argument("--file", required=True)
    parser.add_argument("--mapping", help="JSON object column -> field")
    parser.add_argument(
        "--propose",
        action="store_true",
        help="print a proposed mapping and stop",
    )
    parser.add_argument("--database-url", default=None)
    args = parser.parse_args(argv)

    data = Path(args.file).read_bytes()
    importer = HistoryImporter(args.database_url)
    if args.propose or not args.mapping:
        headers, _ = read_table(data, args.file)
        print(
            json.dumps(
                {
                    "headers": headers,
                    "proposed": mp.propose(args.kind, headers),
                    "saved": importer.saved_mapping(
                        args.tenant, args.source, args.kind
                    ),
                },
                indent=2,
            )
        )
        return 0
    ctx = RunContext(
        tenant_id=args.tenant, case_id="", run_id="cli", actor="cli"
    )
    try:
        out = importer.import_file(
            ctx,
            kind=args.kind,
            source_name=args.source,
            filename=Path(args.file).name,
            data=data,
            mapping=json.loads(args.mapping),
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(out.as_dict(), indent=2))
    return 0 if out.rows_ok or out.duplicate_of else 1


if __name__ == "__main__":
    raise SystemExit(main())
