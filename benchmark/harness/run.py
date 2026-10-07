"""Run the benchmark: three company stacks in parallel, then score.

    python -m benchmark.harness.run --run baseline --profile benchmark/harness/profiles/baseline.json
        [--companies A,B,C] [--subset-file ids.txt] [--fresh] [--no-score]

Each company is a separate process because the pipeline keeps its config and
metrics in module globals: two tenants in one process would validate each
other's invoices.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time

from . import dataset, score

# One real call through the pipeline's own guard, in a child process so the
# product's load_dotenv() cannot leak .env into the stacks' inherited
# environment. It costs a fraction of a cent; skipping it cost a 300-invoice
# run that scored 85% while Vertex refused every call for want of billing.
_PREFLIGHT = """
from invoice_processing.shared_libraries.acting import general_invoice_agent as g
g._ensure_gcp_initialized()
g._generate(g._model_for(g._gcp_config.GEMINI_FLASH_MODEL, 0), "Reply with OK.")
"""


def preflight() -> str | None:
    """Why the model cannot be used, or None if it answered."""
    done = subprocess.run(
        [sys.executable, "-c", _PREFLIGHT],
        cwd=dataset.REPO,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    if done.returncode == 0:
        return None
    return (done.stderr.strip().splitlines() or ["no output"])[-1]


def undecided(out) -> int:
    """Invoices the pipeline finished without deciding (ERROR or NONE)."""
    results = json.loads((out / "results.json").read_text(encoding="utf-8"))
    return results["decision"]["counts"]["errors_or_no_decision"]


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--run",
        required=True,
        help="run name; results land in benchmark/runs/<run>",
    )
    p.add_argument("--profile", required=True)
    p.add_argument("--companies", default=",".join(dataset.COMPANIES))
    p.add_argument("--subset-file")
    p.add_argument("--fresh", action="store_true")
    p.add_argument("--no-score", action="store_true")
    args = p.parse_args(argv)

    reason = preflight()
    if reason:
        print(
            f"PREFLIGHT FAILED -- the model is not usable, nothing run:\n  {reason}"
        )
        return 2

    out = dataset.REPO / "benchmark" / "runs" / args.run
    (out / "logs").mkdir(parents=True, exist_ok=True)
    started = time.time()
    procs = {}
    for company in args.companies.split(","):
        cmd = [
            sys.executable,
            "-m",
            "benchmark.harness.stack_main",
            "--company",
            company,
            "--out",
            str(out),
            "--profile",
            args.profile,
        ]
        if args.subset_file:
            cmd += ["--subset-file", args.subset_file]
        if args.fresh:
            cmd.append("--fresh")
        log = open(out / "logs" / f"{company}.log", "a", encoding="utf-8")
        procs[company] = (
            subprocess.Popen(
                cmd, cwd=dataset.REPO, stdout=log, stderr=subprocess.STDOUT
            ),
            log,
        )
        print(f"started {company}: {' '.join(cmd)}")

    failed = []
    for company, (proc, log) in procs.items():
        code = proc.wait()
        log.close()
        print(f"{company} exited {code} after {time.time() - started:.0f}s")
        if code:
            failed.append(company)
    (out / "wall_seconds.txt").write_text(f"{time.time() - started:.0f}\n")
    if failed:
        print(f"FAILED: {failed} -- see {out / 'logs'}")
        return 1
    if not args.no_score:
        score.main([str(out), "--profile", args.profile])
        # A decision the pipeline could not make is a hole in the measurement,
        # not a data point: exit non-zero so nobody reads the report as a run.
        missing = undecided(out)
        if missing:
            print(
                f"INVALID RUN: {missing} invoice(s) have no decision -- "
                f"see {out / 'logs'}"
            )
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
