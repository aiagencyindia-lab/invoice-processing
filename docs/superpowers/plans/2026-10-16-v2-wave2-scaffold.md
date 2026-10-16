# Wave 2 Scaffold Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans. Steps use checkbox syntax.

**Goal:** Every shared file the three Wave 2 branches need, so they never touch the same file.
**Architecture:** Migration + models; FindingStore filters/summary/events; findings, node, lineage, contract-file and clause routes; the `trust.audit.run` job type and an `on_progress` hook; frontend types, nav, placeholders, EvidenceViewer; benchmark hook; docs.
**Tech stack:** Python 3.12, FastAPI, SQLAlchemy 2, Alembic, pytest (TDD), Next.js 16, Playwright.
**Spec:** `docs/superpowers/specs/2026-10-16-v2-wave2-scaffold-design.md`.

## Global constraints
- §3.1 contracts; v1 behaviour unchanged; no route takes a tenant; every new route a cross-tenant probe; masked attrs; Decimal money; no ids in log `extra`.
- One Alembic revision `f4a5b6c7d8e9`, single head.
- Each task: write the failing test, watch it fail, implement, watch it pass, run the file, commit explicit paths.

## Review focus
1. Vendor filter joins the open `issued_by` edge; a re-versioned invoice node still lists under its vendor.
2. `transition` route role by target status; a clerk cannot reach a money status.
3. Lineage contract stage: contradiction when the governing contract's vendor differs.
4. `on_progress` is called for every non-off engine, including failed/missing ones.
5. Contract file route returns only this tenant's contract documents.

## Tasks
- **S1** Migration + models + schema/migration/head-pin tests.
- **S2** FindingStore `list` filters, `count`, `summary`, `events`.
- **S3** `service.get_finding_store()`, stub packages/routers, `source_of`/`vendor_of`, `trust/html.py`.
- **S4** Findings/node/lineage/contract-file/clauses routes; lineage contract fix; isolation probes + `EXTRA_PROBE_MODULES`.
- **S5** `trust.audit.run` job type + `evaluate(on_progress=)`.
- **S6** Frontend types, `trust.ts`, NAV, placeholder pages, `EvidenceViewer`, CSS, e2e nav list, VIEWS, e2e API seed finding.
- **S7** Benchmark explain hook, PENDING, memory.

Each task's exact signatures, columns and route shapes are in the spec; tests are named in the program plan's testing lines.
