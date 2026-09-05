#!/usr/bin/env bash
# Every repo gate, one table, output captured -- so "all green" in a report is
# a file somebody can reread, not a recollection.
#
#   bash benchmark/harness/gates.sh [output-dir]      (default: benchmark/runs/gates)
#
# Frontend e2e rebuilds .next against port 8100; rebuild with your own
# API_ORIGIN afterwards if you serve the frontend locally.
set -u
cd "$(dirname "$0")/../.."
OUT="${1:-benchmark/runs/gates}"
mkdir -p "$OUT"
PY=./.venv/bin/python
declare -a ROWS=()

gate() {  # name, command...
  local name="$1"; shift
  local log="$OUT/${name// /_}.log"
  local started=$(date +%s)
  if "$@" >"$log" 2>&1; then status=PASS; else status=FAIL; fi
  local secs=$(( $(date +%s) - started ))
  local row
  row="$(printf '%-28s %-4s %4ss  %s' "$name" "$status" "$secs" "$log")"
  ROWS+=("$row")
  echo "$row"
}

gate "pytest"              $PY -m pytest tests/ -q -p no:cacheprovider
gate "ruff check"          $PY -m ruff check .
gate "ruff format"         $PY -m ruff format --check .
gate "verify_phase0"       $PY scripts/verify_phase0.py
gate "verify_phase1"       $PY scripts/verify_phase1.py
gate "eval gate"           $PY eval/eval.py --ground-truth invoice_processing/exemplary_data \
                             --agent-output tests/fixtures/eval/agent_output --skip-llm --require-baseline
if [ "${SKIP_FRONTEND:-}" = "" ]; then
  gate "frontend lint"     bash -c 'cd frontend && npm run lint'
  gate "frontend tsc"      bash -c 'cd frontend && npx tsc --noEmit'
  gate "frontend e2e"      bash -c 'cd frontend && npm run test:e2e'
fi

{
  echo "# Gates -- $(date -u +%Y-%m-%dT%H:%M:%SZ) -- $(git rev-parse --short HEAD) ($(git branch --show-current))"
  echo
  printf '%s\n' "${ROWS[@]}"
} | tee "$OUT/summary.txt"
grep -q FAIL "$OUT/summary.txt" && exit 1 || exit 0
