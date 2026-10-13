# google-adk 2.x Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the Python stack to google-adk 2.x / google-cloud-aiplatform 2.x so Starlette 1.x can be installed and the five Starlette waivers in `security/audit-allowlist.json` can be deleted before they expire on 2026-12-12.

**Architecture:** A dependency bump with no runtime code change. ADK is one `LlmAgent` used by `adk web`; every Gemini call still goes through `vertexai.generative_models`, which aiplatform 2.4.0 still ships. The gate is a 14-invoice live smoke on GCP project `rasayan` plus a full 300-record replay that must score 98.0%.

**Tech Stack:** Python 3.12, uv 0.9.28 (`uv.lock`), pytest, ruff; Next.js 16 + Playwright for the e2e check; `gh` for Dependabot PRs.

**Spec:** `docs/superpowers/specs/2026-10-13-adk-2-migration-design.md`

## Global Constraints

- Always `./.venv/bin/python`; tests run with `-p no:cacheprovider`.
- Never `git commit -a`; stage explicit paths (`.impeccable/hook.cache.json` is a modified, never-committed file).
- `pyproject.toml` floors after this work: `google-adk>=2.0,<3`, `google-cloud-aiplatform>=2.0,<3`, `fastapi>=0.141`. No other floor changes; `vertexai>=1.38.0` stays.
- `uv.lock` after this work: starlette 1.7.x; no `mcp`; no `sse-starlette`.
- `security/audit-allowlist.json` after this work: `[]`.
- The SDK deprecation warning on `GenerativeModel()` stays visible (spec ruling).
- No Railway, GCP Cloud SQL, GCS or Dockerfile changes. Local LLM calls use project `rasayan` with `CLOUD_SQL_INSTANCE="" GCS_BUCKET="" PUBSUB_TOPIC=""` (empty, not unset).
- Commit messages: imperative subject that says why, body, trailer `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- Ledger rulings in `.superpowers/sdd/2026-10-13-adk-2-migration/progress.md` (excluded via `.git/info/exclude`; delete it at the end).

## Review Focus

1. A request with a multipart upload (PO CSV, supporting-document scan) under Starlette 1.x: the route must still read the file and return the same status codes. Pinned by the existing `tests/test_supporting_docs.py` and `tests/test_po_*.py` suites in Task 2, and by the browser upload in Task 9.
2. The `_identify_request` middleware catching an unhandled exception under Starlette 1.x: still a 500 with the security headers, not a re-raised exception in TestClient. Pinned by `tests/test_security_headers.py` in Task 2.
3. `uv sync --frozen` in the Dockerfile with uv 0.5.11 reading a lock written by 0.9.28: out of scope (production down), noted in Task 7's README line so the restore runbook says it.
4. The e2e scanned-document test reading the iframe `src` before the blob URL resolves (null → `toMatch` throws). Pinned in Task 4 by replacing the non-waiting read with `toHaveAttribute`.
5. `pip-audit` reporting a *new* high finding in the upgraded tree (the allowlist is now empty, so anything fails the build). Task 3 runs the gate and, if a finding appears, it is either upgraded away in the same task or waived with a reason and expiry, never ignored.

---

### Task 1: Pin the policy, bump the dependencies

**Files:**
- Modify: `tests/test_repo_policy.py` (append)
- Modify: `pyproject.toml:15-18` (the `google-adk`, `google-cloud-aiplatform`, `fastapi` lines in `dependencies`)
- Modify: `uv.lock` (regenerated)
- Modify: `invoice_processing/requirements.txt:1-2`

**Interfaces:**
- Produces: the new lock every later task runs against.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_repo_policy.py`:

```python
import re
import tomllib


def _floor(requirement: str) -> str:
    """'google-adk>=2.0,<3' -> '2.0'."""
    match = re.search(r">=\s*([0-9][^,;\s]*)", requirement)
    assert match, requirement
    return match.group(1)


def test_the_agent_stack_is_on_its_second_major():
    """google-adk 1.x pins Starlette below 1.0, where five advisories live.
    The floors keep a resolver from ever going back there, and the lock
    proves the installed Starlette is on the fixed line."""
    project = tomllib.loads((REPO / "pyproject.toml").read_text())
    by_name = {
        re.split(r"[<>=!~\[ ]", dep, 1)[0]: dep
        for dep in project["project"]["dependencies"]
    }
    assert _floor(by_name["google-adk"]).startswith("2")
    assert _floor(by_name["google-cloud-aiplatform"]).startswith("2")

    lock = tomllib.loads((REPO / "uv.lock").read_text())
    versions = {pkg["name"]: pkg["version"] for pkg in lock["package"]}
    assert versions["starlette"].split(".")[0] >= "1", versions["starlette"]
    assert "mcp" not in versions
```

- [ ] **Step 2: Run it to verify it fails**

Run: `./.venv/bin/python -m pytest tests/test_repo_policy.py -q -p no:cacheprovider`
Expected: 1 failed (`assert '1.0.0'.startswith('2')`), 3 passed.

- [ ] **Step 3: Prove it fails on `main` too**

```bash
git worktree add /private/tmp/claude-501/adk2-main ea11ab1
cp tests/test_repo_policy.py /private/tmp/claude-501/adk2-main/tests/
(cd /private/tmp/claude-501/adk2-main && /Users/vineet/Downloads/ADK/invoice-processing/.venv/bin/python -m pytest tests/test_repo_policy.py -q -p no:cacheprovider)
git worktree remove --force /private/tmp/claude-501/adk2-main && git worktree prune
```
Expected: the same 1 failed. Record the result in the ledger.

- [ ] **Step 4: Change the floors**

In `pyproject.toml` `dependencies`:

```toml
    "google-adk>=2.0,<3",
    ...
    "google-cloud-aiplatform>=2.0,<3",
    ...
    "fastapi>=0.141",
```

In `invoice_processing/requirements.txt` replace the first two lines with:

```
google-adk>=2.0,<3
google-cloud-aiplatform>=2.0,<3
```

- [ ] **Step 5: Regenerate the lock and sync**

```bash
uv lock --upgrade-package fastapi --upgrade-package starlette --upgrade-package google-adk --upgrade-package mcp --upgrade-package sse-starlette --upgrade-package google-cloud-aiplatform
uv sync --frozen
grep -n -A1 '^name = "starlette"' uv.lock
grep -c '^name = "mcp"' uv.lock
./.venv/bin/python -c "import google.adk, starlette, fastapi; print(google.adk.__version__, starlette.__version__, fastapi.__version__)"
```
Expected: starlette `1.7.0`; `grep -c` prints `0`; the import line prints `2.11.0 1.7.0 0.141.1`. If `uv lock` changes anything outside the dry-run set listed in the spec (e.g. an unrelated major), stop and ledger it before continuing.

- [ ] **Step 6: Run the policy test**

Run: `./.venv/bin/python -m pytest tests/test_repo_policy.py -q -p no:cacheprovider`
Expected: 4 passed.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml uv.lock invoice_processing/requirements.txt tests/test_repo_policy.py
git commit -m "Move to google-adk 2.x so Starlette 1.x can be installed

google-adk 1.x pins Starlette below 1.0, where the five advisories waived
in security/audit-allowlist.json live. aiplatform 2.4.0 still ships
vertexai.generative_models, so the Gemini call sites are untouched. The
lock drops mcp, sse-starlette and about twenty Google client packages
nothing imports. A policy test keeps both majors floored at 2.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: Run the Python suite on the new lock and fix only what breaks

**Files:**
- Modify: only what fails (expected: nothing).
- Test: `tests/`

**Interfaces:**
- Consumes: the lock from Task 1.

- [ ] **Step 1: Run the full suite**

Run: `./.venv/bin/python -m pytest tests/ -q -p no:cacheprovider 2>&1 | tail -15`
Expected: `2679 passed, 4 skipped` (2678 + the policy test). Note any `DeprecationWarning` lines; they are expected and stay.

- [ ] **Step 2: If anything fails, find the root cause before editing**

For each failure: read the traceback to the first frame in `invoice_processing/`, then `grep -rn` every caller of that function. Known places Starlette 1.x could show up: `api/service.py` `_identify_request` (middleware exception handling), `api/po_routes.py` (`UploadFile`, `run_in_threadpool`), any `TestClient` call that passes `files=`. Fix in the shared function, add the failing test first if the failure is not already covered by an existing test, and ledger the ruling.

- [ ] **Step 3: Lint**

Run: `./.venv/bin/python -m ruff check . && ./.venv/bin/python -m ruff format --check .`
Expected: `All checks passed!` and `N files already formatted`.

- [ ] **Step 4: Confirm the ADK wrapper still builds**

Run: `./.venv/bin/python -c "from invoice_processing import root_agent; print(sorted(t.__name__ if callable(t) else t.name for t in root_agent.tools))"`
Expected: `['list_inference_cases', 'run_inference']` (matches `tests/test_alf_in_the_worker.py:265`).

- [ ] **Step 5: Commit (only if Step 2 changed anything)**

```bash
git add <the files changed in step 2>
git commit -m "<imperative subject saying what Starlette 1.x changed and why the fix is where it is>

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: Empty the audit allowlist and run the gate

**Files:**
- Modify: `security/audit-allowlist.json`
- Modify: `scripts/audit_gate.py` (only if a comment names the Starlette waiver)
- Modify: `SECURITY.md` (only if it names the Starlette waiver; today it describes the mechanism only)

**Interfaces:**
- Consumes: the lock from Task 1.

- [ ] **Step 1: Empty the allowlist**

Write `security/audit-allowlist.json` as exactly:

```json
[]
```

- [ ] **Step 2: Remove any sentence that names the waiver**

Run: `grep -n -i 'starlette' scripts/audit_gate.py SECURITY.md README.md deployment/README.md`
Expected: no hits that describe the waiver as current. Delete any sentence that does; keep text that explains the allowlist mechanism.

- [ ] **Step 3: Run the gate's unit tests**

Run: `./.venv/bin/python -m pytest tests/test_audit_gate.py tests/test_repo_policy.py -q -p no:cacheprovider`
Expected: all passed.

- [ ] **Step 4: Run the real gate (needs network)**

Run: `python3 scripts/audit_gate.py`
Expected: no `waived` lines, no `FAIL` lines, last line `Dependency audit: passed`.
If a `FAIL` appears for a package the new lock pulled in: try `uv lock --upgrade-package <name>` and re-run `uv sync --frozen` and the suite; only if no fixed version resolves, add an allowlist entry with the advisory id, a reason naming the blocker, and an expiry no later than 2026-12-12, and ledger it.

- [ ] **Step 5: Commit**

```bash
git add security/audit-allowlist.json
git commit -m "Drop the Starlette waivers now that Starlette 1.7 is installed

The five PYSEC entries were waived until 2026-12-12 for want of google-adk
2.x. The gate now fails on any high finding, with nothing excused.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```
(Add `scripts/audit_gate.py` or `SECURITY.md` to `git add` if Step 2 changed them.)

---

### Task 4: Make the scanned-document e2e test wait for the blob URL

**Files:**
- Modify: `frontend/e2e/golden-path.spec.ts:499-500`
- Test: the same file

**Interfaces:**
- none

**Why:** `SupportingReview.tsx:274` renders `<iframe src={scanUrl ?? undefined}>` as soon as the detail loads, and `scanUrl` is set later when `objectUrl()` resolves. `locator.getAttribute("src")` does not wait for the attribute, so it can return `null`, which is the `toMatch` "received value must be a string" failure seen on Dependabot's #32 run. This is a timing bug in the test, not a Starlette change.

- [ ] **Step 1: Replace the non-waiting read**

Change lines 499-500 from:

```ts
  const scan = await drawer.locator("iframe").getAttribute("src");
  expect(scan).toMatch(/^blob:/);
```

to:

```ts
  const frame = drawer.locator("iframe");
  await expect(frame).toHaveAttribute("src", /^blob:/);
  const scan = await frame.getAttribute("src");
```

Leave the following `page.evaluate(... scan!)` line as it is.

- [ ] **Step 2: Lint and type-check the frontend**

Run: `cd frontend && npm run lint && npx tsc --noEmit`
Expected: both clean.

- [ ] **Step 3: Run the one test three times, then the whole e2e suite once**

```bash
cd frontend && npm run test:e2e -- -g "corrects a scanned receipt" --repeat-each 3
cd frontend && npm run test:e2e
```
Expected: 3/3 passed, then 38 passed. (`npm run test:e2e` rebuilds Next first; a bare `npx playwright test` would use a stale build.)

- [ ] **Step 4: Run the Python suite's frontend-string guard**

Run: `./.venv/bin/python -m pytest tests/test_frontend_reaches_the_flow.py -q -p no:cacheprovider`
Expected: passed (it greps frontend source for exact strings).

- [ ] **Step 5: Commit**

```bash
git add frontend/e2e/golden-path.spec.ts
git commit -m "Wait for the scan's blob URL before reading the iframe src

The iframe renders before objectUrl() resolves, so getAttribute could
return null and toMatch threw instead of failing. toHaveAttribute waits.
Seen once on CI (Dependabot #32, run 38299346015).

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: All nine gates

**Files:** none modified.

- [ ] **Step 1: Run the gate script**

Run: `bash benchmark/harness/gates.sh benchmark/runs/gates-adk2`
Expected: `benchmark/runs/gates-adk2/summary.txt` shows all 9 rows passing (pytest, ruff check, ruff format, verify_phase0, verify_phase1, eval gate, frontend lint, frontend tsc, frontend e2e).

- [ ] **Step 2: Read the summary yourself**

Run: `cat benchmark/runs/gates-adk2/summary.txt`
Expected: nine passes; if not, the failing gate's log is in the same directory. Fix at the root, with a failing test first, and re-run the script.

---

### Task 6: Postgres parity

**Files:** none modified.

- [ ] **Step 1: Start a throwaway Postgres (never touch `bsi-platform-db-1` on 5433)**

```bash
docker run -d --rm --name pg-x -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=t -p 55432:5432 postgres:16
```

- [ ] **Step 2: Run the CI Postgres list**

```zsh
cd /Users/vineet/Downloads/ADK/invoice-processing
files=(${(f)"$(awk '/DB-backed tests against Postgres/{f=1} f&&/tests\//{for(i=1;i<=NF;i++) if($i ~ /tests\//){gsub(/[\\"]/,"",$i); print $i}} f&&/-q$/{exit}' .github/workflows/ci.yml)"})
TEST_DATABASE_URL=postgresql+pg8000://postgres:postgres@localhost:55432/t ./.venv/bin/python -m pytest "${files[@]}" -q -p no:cacheprovider 2>&1 | tail -5
```
Expected: ~818 passed, 0 failed (about 6 minutes).

- [ ] **Step 3: Alembic round trip on a fresh database**

```bash
docker exec pg-x psql -U postgres -c 'CREATE DATABASE fresh;'
DATABASE_URL=postgresql+pg8000://postgres:postgres@localhost:55432/fresh ./.venv/bin/python -m alembic upgrade head
DATABASE_URL=postgresql+pg8000://postgres:postgres@localhost:55432/fresh ./.venv/bin/python -m alembic downgrade base
docker stop pg-x
```
Expected: both commands exit 0; head is `c9d5e2f3a4b6`.

---

### Task 7: Documentation and the Dependabot PRs

**Files:**
- Modify: `deployment/README.md:53,57`
- Modify: `README.md` (only if it states a google-adk version)

- [ ] **Step 1: Name the new floors where the README states versions**

`deployment/README.md:53` and `:57` say `google-adk[vertexai]`. Check whether the extra still exists:

Run: `./.venv/bin/python -c "import importlib.metadata as m; print(m.metadata('google-adk').get_all('Provides-Extra'))"`

If `vertexai` is listed, change both lines to `"google-adk[vertexai]>=2,<3"`; if not, to `"google-adk>=2,<3" "google-cloud-aiplatform>=2,<3"`. Add one sentence under the Release section: "The Dockerfile pins uv 0.5.11; the lock is written by uv 0.9.28. Check `uv sync --frozen` builds before the next deploy."

- [ ] **Step 2: Close the three superseded Dependabot PRs**

```bash
gh pr close 32 --comment "Superseded by phase3/adk-2-migration, which bumps google-adk and google-cloud-aiplatform together and drops the Starlette waivers."
gh pr close 31 --comment "Superseded by phase3/adk-2-migration, which bumps google-adk and google-cloud-aiplatform together and drops the Starlette waivers."
gh pr close 30 --comment "Closing: this grouped resolve downgraded google-adk to 1.14.1 (PYSEC-2026-344, high) and failed the audit gate. phase3/adk-2-migration moves the stack forward instead."
gh pr list --state open | wc -l
```
Expected: the last line prints `9` (#21–#29 remain; they are a separate triage).

- [ ] **Step 3: Commit**

```bash
git add deployment/README.md
git commit -m "Say which google-adk line the deploy docs install

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 8: The live smoke and the full replay (the gate)

**Files:** none modified; results in `benchmark/runs/smoke-adk2/` and `benchmark/runs/replay-adk2/` (git-ignored).

- [ ] **Step 1: Live smoke on `rasayan`**

```bash
cd /Users/vineet/Downloads/ADK/invoice-processing
CLOUD_SQL_INSTANCE="" GCS_BUCKET="" PUBSUB_TOPIC="" PROJECT_ID=rasayan GOOGLE_CLOUD_PROJECT=rasayan \
  ./.venv/bin/python -m benchmark.harness.run --run smoke-adk2 --profile benchmark/harness/profiles/after.json --subset-file benchmark/harness/smoke.txt --fresh
```
Expected: no `PREFLIGHT FAILED`; `benchmark/runs/smoke-adk2/report.md` says 14 of 14 submissions recorded, `errors_or_no_decision` 0. If preflight fails, the last stderr line names why (billing, ADC, API not enabled); fix the environment, not the code.

- [ ] **Step 2: Compare the 14 decisions with the recording**

```bash
./.venv/bin/python - <<'EOF'
import json, glob
def load(run):
    out = {}
    for f in glob.glob(f"benchmark/runs/{run}/raw/*.jsonl"):
        for line in open(f):
            r = json.loads(line); out[r["invoice_id"]] = (r["final_decision"], r.get("first_failing_step"))
    return out
new, old = load("smoke-adk2"), load("after-9f8c923")
ids = [l.strip() for l in open("benchmark/harness/smoke.txt") if l.strip()]
diff = [(i, old.get(i), new.get(i)) for i in ids if old.get(i) != new.get(i)]
print(len(ids), "compared;", len(diff), "differ"); print(*diff, sep="\n")
EOF
```
Expected: `14 compared; 0 differ`. Any difference: open both records' `findings` and decide whether it is model non-determinism on a known-flaky case (ledger it, with the evidence) or a pipeline change (stop; escalate to the full 300-invoice run `--run after-adk2` without `--subset-file`, ~87 min).

- [ ] **Step 3: Full replay over the 300-record run**

```bash
CLOUD_SQL_INSTANCE="" GCS_BUCKET="" PUBSUB_TOPIC="" \
  ./.venv/bin/python -m benchmark.harness.replay --run after-9f8c923 --profile benchmark/harness/profiles/after.json --out replay-adk2
./.venv/bin/python -m benchmark.harness.score benchmark/runs/replay-adk2 --profile benchmark/harness/profiles/after.json
grep -n 'decision accuracy' benchmark/runs/replay-adk2/report.md benchmark/runs/replay-three-way-final/report.md
```
Expected: both lines show `98.0%`. A different number means a validation phase changed behaviour under the new libraries: diff `results.csv` between the two runs, find the first differing invoice, and trace it before touching anything.

- [ ] **Step 4: Record the gate in the benchmark changelog**

Append to `benchmark/CHANGELOG.md` a dated entry, in the file's existing voice, giving: the lock versions (google-adk 2.11.0, aiplatform 2.4.0, starlette 1.7.0), the smoke result (14/14, 0 differ), the replay result (98.0%), and the run names.

```bash
git add benchmark/CHANGELOG.md
git commit -m "Record the google-adk 2.x smoke and replay

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 9: Browser check on the multipart path

**Files:**
- Modify (temporarily, restored after): `.claude/launch.json`

- [ ] **Step 1: Add temporary launch configs**

```bash
cp .claude/launch.json /private/tmp/claude-501/launch.json.bak
```
Add two configurations (copy `manual-a-api-worker`'s command without the worker, `--port 8200`, name `check-api-8200`, port 8200; and `check-frontend-3200`: `cd frontend && API_ORIGIN=http://127.0.0.1:8200 npx next dev -p 3200`, port 3200).

- [ ] **Step 2: Start both and check**

Use `preview_start {name: "check-api-8200"}` then `preview_start {name: "check-frontend-3200"}`. In the browser pane:
1. `/login` renders (the "no Identity Platform configuration" notice is expected locally).
2. `/purchase-orders` as `alice@e2e.test:clerk` via the dev bypass: upload a PO CSV (any file from `tests/fixtures/` the page accepts) and a scanned document; both succeed and the document card appears.
3. Open an invoice detail; `read_console_messages` shows no errors.

```bash
curl -s -o /dev/null -w '%{http_code}\n' -H 'X-Dev-Principal: clerk@bench.test:clerk' http://127.0.0.1:8200/api/invoices
curl -s -I http://127.0.0.1:8200/api/invoices | grep -i -E 'x-frame-options|cache-control'
```
Expected: `200`; `X-Frame-Options: DENY` and `Cache-Control: no-store`.

- [ ] **Step 3: Restore the launch file**

```bash
cp /private/tmp/claude-501/launch.json.bak .claude/launch.json
git status --short .claude
```
Expected: nothing (the file is git-excluded anyway).

---

### Task 10: Whole-branch review, push, PR

- [ ] **Step 1: Whole-branch review on the `fable` model** (superpowers:requesting-code-review), diff `main...phase3/adk-2-migration`. One fix pass; every fix gets its failing test first.

- [ ] **Step 2: Re-run the fast gates after any fix**

```bash
./.venv/bin/python -m pytest tests/ -q -p no:cacheprovider | tail -3
./.venv/bin/python -m ruff check . && ./.venv/bin/python -m ruff format --check .
python3 scripts/audit_gate.py | tail -1
```

- [ ] **Step 3: Push and open the PR** (only when the user picks "push + PR" in the finishing options)

```bash
git push -u origin phase3/adk-2-migration
gh pr create --title "Move to google-adk 2.x and drop the Starlette waivers" --body-file /private/tmp/claude-501/pr-body.md
```
The body lists: what changed, the gate results (smoke 14/14 with 0 differences, replay 98.0%, Postgres parity, 9 gates), the rulings, and ends with `🤖 Generated with [Claude Code](https://claude.com/claude-code)`. Then `mcp__ccd_pr__get_status`; bind with `bind_pr` if needed. Never merge without the user's explicit "merge PR N".

- [ ] **Step 4: Clean up**

```bash
rm -rf .superpowers/sdd/2026-10-13-adk-2-migration
git worktree list
```
Update memory `latest-handoff.md` with the PR number and the branch state.
