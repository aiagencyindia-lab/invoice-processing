"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  api,
  ApiError,
  can,
  sheetQuery,
  type ExportJob,
  type InvoiceDetail,
  type Me,
  type SheetFacets,
  type SheetFilters,
  type SheetPage,
  type SheetSummary,
  type SheetView,
} from "@/lib/api";
import { usePoll } from "@/lib/hooks";
import { Grid, type ColumnLayout } from "@/components/sheet/Grid";
import { Drawer } from "@/components/sheet/Drawer";
import { BUILT_IN_VIEWS } from "@/lib/sheetViews";

const PAGE_SIZES = [50, 100, 250];

/** Column layout lives in the browser, per view.
 *
 *  Widths and order are a preference, not data: storing them server-side would
 *  mean a write on every drag, and one person's narrow vendor column becoming
 *  everyone's. A saved view carries its own layout when it is saved.
 */
function layoutKey(viewId: string) {
  return `sheet.layout.${viewId}`;
}

function loadLayout(viewId: string): ColumnLayout[] | null {
  try {
    const raw = window.localStorage.getItem(layoutKey(viewId));
    return raw ? (JSON.parse(raw) as ColumnLayout[]) : null;
  } catch {
    return null;
  }
}

function saveLayout(viewId: string, layout: ColumnLayout[]) {
  try {
    window.localStorage.setItem(layoutKey(viewId), JSON.stringify(layout));
  } catch {
    // A browser refusing storage is not a reason to break the grid.
  }
}

export default function SheetPage() {
  const { data: me } = usePoll<Me>("/api/me");

  const [filters, setFilters] = useState<SheetFilters>({ status: "approved" });
  const [search, setSearch] = useState("");
  const [sort, setSort] = useState("approved_at");
  const [order, setOrder] = useState<"asc" | "desc">("desc");
  const [limit, setLimit] = useState(100);
  const [offset, setOffset] = useState(0);
  const [custom, setCustom] = useState<Record<string, ColumnLayout[]>>({});
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [openCase, setOpenCase] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [showColumns, setShowColumns] = useState(false);
  const [activeView, setActiveView] = useState("all-approved");

  // Typing filters the sheet, but not on every keystroke: a request per
  // character would queue up behind itself and answer out of order.
  useEffect(() => {
    const timer = setTimeout(() => {
      setFilters((current) => ({ ...current, q: search || undefined }));
      setOffset(0);
    }, 300);
    return () => clearTimeout(timer);
  }, [search]);

  const query = useMemo(
    () => sheetQuery(filters, { sort, order, limit, offset }),
    [filters, sort, order, limit, offset],
  );

  const { data: page, error } = usePoll<SheetPage>(
    `/api/sheet/invoices?${query}`,
  );
  const { data: summary } = usePoll<SheetSummary>(
    `/api/sheet/summary?${sheetQuery(filters)}`,
  );
  const { data: facets } = usePoll<SheetFacets>("/api/sheet/facets");
  const { data: views, reload: reloadViews } = usePoll<{ views: SheetView[] }>(
    "/api/sheet/views",
  );

  // Polled only while something is in flight: a finished list does not change
  // on its own, and a timer that never stops is a request every two seconds
  // for as long as the tab is open.
  const [watchExports, setWatchExports] = useState(false);
  const { data: exportsList, reload: reloadExports } = usePoll<{
    exports: ExportJob[];
  }>("/api/sheet/exports", watchExports ? 2000 : 0);

  const { data: detail, loading: detailLoading } = usePoll<InvoiceDetail>(
    openCase ? `/api/invoices/${openCase}` : null,
  );

  // Memoised because the layout below depends on it: a fresh `[]` every render
  // would rebuild every column's width on every render.
  const columns = useMemo(() => page?.columns ?? [], [page]);
  const rows = page?.rows ?? [];

  // Derived, not stored in an effect: the layout is a function of the columns
  // the server offers, what this browser saved, and what the user has changed
  // in this session. Keeping it in state and syncing it would give the same
  // answer a render later, and a render where the two disagree.
  const layout = useMemo<ColumnLayout[]>(() => {
    if (!columns.length) return [];
    const saved =
      custom[activeView] ??
      (typeof window === "undefined" ? null : loadLayout(activeView));

    const build = (column: (typeof columns)[number]): ColumnLayout => {
      const stored = saved?.find((entry) => entry.key === column.key);
      return {
        key: column.key,
        width: stored?.width ?? (column.type === "text" ? 180 : 130),
        frozen: stored?.frozen ?? column.key === "vendor_name",
        hidden: stored?.hidden ?? !column.default,
      };
    };

    // Saved order first, so a reordered column stays where it was put; a
    // column added to the catalogue since then lands at the end rather than
    // disappearing.
    const known = new Map(columns.map((column) => [column.key, column]));
    const ordered = (saved ?? [])
      .map((entry) => known.get(entry.key))
      .filter((column): column is (typeof columns)[number] => Boolean(column));
    const rest = columns.filter(
      (column) => !ordered.some((entry) => entry.key === column.key),
    );
    return [...ordered, ...rest].map(build);
  }, [columns, custom, activeView]);

  const updateLayout = useCallback(
    (next: ColumnLayout[]) => {
      setCustom((current) => ({ ...current, [activeView]: next }));
      saveLayout(activeView, next);
    },
    [activeView],
  );

  const setFilter = (key: keyof SheetFilters, value: string | undefined) => {
    setFilters((current) => ({ ...current, [key]: value || undefined }));
    setOffset(0);
  };

  const toggleQuality = (flag: string) => {
    setFilters((current) => {
      const present = current.quality ?? [];
      const next = present.includes(flag)
        ? present.filter((f) => f !== flag)
        : [...present, flag];
      return { ...current, quality: next.length ? next : undefined };
    });
    setOffset(0);
  };

  const clearFilters = () => {
    setFilters({ status: filters.status });
    setSearch("");
    setOffset(0);
  };

  const applyView = (view: { id: string; definition: SheetFilters }) => {
    setActiveView(view.id);
    setFilters({ status: "approved", ...view.definition });
    setSearch(view.definition.q ?? "");
    setOffset(0);
  };

  const onSort = (key: string) => {
    if (sort === key) setOrder(order === "asc" ? "desc" : "asc");
    else {
      setSort(key);
      setOrder("desc");
    }
    setOffset(0);
  };

  const onSelect = (caseId: string) => {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(caseId)) next.delete(caseId);
      else next.add(caseId);
      return next;
    });
  };

  const requestExport = async (format: "csv" | "xlsx" | "json") => {
    setNotice(null);
    try {
      const visible = layout.filter((c) => !c.hidden).map((c) => c.key);
      const job = await api<ExportJob>("/api/sheet/exports", {
        method: "POST",
        body: JSON.stringify({
          format,
          status: filters.status ?? "approved",
          filters,
          columns: visible,
          case_ids: selected.size ? [...selected] : undefined,
          sort,
          order,
        }),
      });
      setNotice(
        job.status === "done"
          ? `Export ready: ${job.row_count} rows.`
          : "Export started. It will appear below when it is ready.",
      );
      setWatchExports(job.status !== "done");
      reloadExports();
    } catch (caught) {
      setNotice(caught instanceof ApiError ? caught.friendly : String(caught));
    }
  };

  const download = async (job: ExportJob) => {
    try {
      const response = await fetch(`/api/sheet/exports/${job.id}/download`, {
        headers: { Authorization: `Bearer ${await currentToken()}` },
      });
      if (!response.ok) throw new Error(await response.text());
      const url = URL.createObjectURL(await response.blob());
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `approved-invoices.${job.format}`;
      anchor.click();
      URL.revokeObjectURL(url);
    } catch (caught) {
      setNotice(String(caught));
    }
  };

  const total = page?.total ?? 0;
  const flagLabels = page?.quality_flags ?? {};

  return (
    <div className="stack" style={{ gap: 20 }}>
      <div className="row-between">
        <h1>Approved invoices</h1>
        <div style={{ display: "flex", gap: 8 }}>
          {can(me, "clerk") ? (
            <>
              <button
                type="button"
                className="btn-secondary"
                onClick={() => requestExport("csv")}
              >
                Export CSV
              </button>
              <button
                type="button"
                className="btn-primary"
                onClick={() => requestExport("xlsx")}
              >
                Export Excel
              </button>
            </>
          ) : null}
        </div>
      </div>

      {notice ? (
        <div className="panel" role="status" style={{ padding: "10px 14px" }}>
          {notice}
        </div>
      ) : null}

      {summary ? (
        <div className="tiles">
          <div className="tile">
            <span className="tile-value">{summary.total}</span>
            <span className="tile-label">Approved invoices</span>
          </div>
          {summary.by_currency.map((entry) => (
            <div className="tile" key={entry.currency}>
              <span className="tile-value">
                {entry.total.toLocaleString(undefined, {
                  maximumFractionDigits: 0,
                })}
              </span>
              <span className="tile-label">
                {entry.currency} approved ({entry.count})
              </span>
            </div>
          ))}
          <div className="tile">
            <span className="tile-value">{summary.approved_this_month}</span>
            <span className="tile-label">Approved this month</span>
          </div>
          <div className="tile">
            <span className="tile-value">{summary.vendors}</span>
            <span className="tile-label">Vendors</span>
          </div>
          <button
            type="button"
            className="tile"
            onClick={() => toggleQuality("duplicate_number")}
          >
            <span className="tile-value">
              {filters.quality?.includes("duplicate_number") ? "on" : "filter"}
            </span>
            <span className="tile-label">Duplicate numbers</span>
          </button>
        </div>
      ) : null}

      <div className="sheet-toolbar">
        <input
          className="input-field"
          placeholder="Search vendor, number, PO…"
          value={search}
          aria-label="Search invoices"
          onChange={(event) => setSearch(event.target.value)}
        />
        <select
          className="input-field"
          aria-label="Status"
          value={filters.status ?? "approved"}
          onChange={(event) => setFilter("status", event.target.value)}
        >
          <option value="approved">Approved</option>
          <option value="pending">Awaiting review</option>
          <option value="rejected">Rejected</option>
          <option value="all">All</option>
        </select>
        <select
          className="input-field"
          aria-label="Vendor"
          value={filters.vendor ?? ""}
          onChange={(event) => setFilter("vendor", event.target.value)}
        >
          <option value="">All vendors</option>
          {(facets?.vendors ?? []).map((vendor) => (
            <option key={vendor} value={vendor}>
              {vendor}
            </option>
          ))}
        </select>
        <select
          className="input-field"
          aria-label="Currency"
          value={filters.currency ?? ""}
          onChange={(event) => setFilter("currency", event.target.value)}
        >
          <option value="">Any currency</option>
          {(facets?.currencies ?? []).map((currency) => (
            <option key={currency} value={currency}>
              {currency}
            </option>
          ))}
        </select>
        <input
          className="input-field"
          type="date"
          aria-label="Approved from"
          value={filters.approved_from ?? ""}
          onChange={(event) => setFilter("approved_from", event.target.value)}
        />
        <input
          className="input-field"
          type="date"
          aria-label="Approved to"
          value={filters.approved_to ?? ""}
          onChange={(event) => setFilter("approved_to", event.target.value)}
        />
        <div className="menu-anchor">
          <button
            type="button"
            className="btn-chip"
            aria-expanded={showColumns}
            onClick={() => setShowColumns(!showColumns)}
          >
            Columns
          </button>
          {showColumns ? (
            <div className="col-menu">
              {layout.map((entry) => {
                const column = columns.find((c) => c.key === entry.key);
                if (!column) return null;
                return (
                  <label key={entry.key}>
                    <input
                      type="checkbox"
                      checked={!entry.hidden}
                      onChange={() =>
                        updateLayout(
                          layout.map((item) =>
                            item.key === entry.key
                              ? { ...item, hidden: !item.hidden }
                              : item,
                          ),
                        )
                      }
                    />
                    {column.label}
                    <button
                      type="button"
                      className="btn-chip"
                      style={{ marginLeft: "auto" }}
                      onClick={() =>
                        updateLayout(
                          layout.map((item) =>
                            item.key === entry.key
                              ? { ...item, frozen: !item.frozen }
                              : item,
                          ),
                        )
                      }
                    >
                      {entry.frozen ? "unfreeze" : "freeze"}
                    </button>
                  </label>
                );
              })}
            </div>
          ) : null}
        </div>
        <button type="button" className="btn-chip" onClick={clearFilters}>
          Clear filters
        </button>
      </div>

      <div className="chip-row">
        {BUILT_IN_VIEWS.map((view) => (
          <button
            key={view.id}
            type="button"
            className="btn-chip"
            aria-pressed={activeView === view.id}
            onClick={() => applyView(view)}
          >
            {view.name}
          </button>
        ))}
        {(views?.views ?? []).map((view) => (
          <button
            key={view.id}
            type="button"
            className="btn-chip"
            aria-pressed={activeView === view.id}
            onClick={() =>
              applyView({ id: view.id, definition: view.definition.filters ?? {} })
            }
          >
            {view.name}
            {view.shared ? " (shared)" : ""}
          </button>
        ))}
        {can(me, "clerk") ? (
          <button
            type="button"
            className="btn-chip"
            onClick={async () => {
              const name = window.prompt("Name this view");
              if (!name) return;
              await api("/api/sheet/views", {
                method: "POST",
                body: JSON.stringify({
                  name,
                  definition: { filters, sort: { key: sort, order }, columns: layout },
                }),
              });
              reloadViews();
            }}
          >
            Save view
          </button>
        ) : null}
      </div>

      <div className="chip-row">
        {Object.entries(flagLabels).map(([flag, description]) => (
          <button
            key={flag}
            type="button"
            className="btn-chip"
            title={description}
            aria-pressed={filters.quality?.includes(flag) ?? false}
            onClick={() => toggleQuality(flag)}
          >
            {flag.replace(/_/g, " ")}
          </button>
        ))}
      </div>

      {error ? <div className="empty-state">{error}</div> : null}

      {rows.length === 0 && !error ? (
        <div className="empty-state">
          No invoices match. Approved invoices appear here once someone approves
          them in the workbench — nothing is added automatically.
        </div>
      ) : (
        <Grid
          columns={columns}
          layout={layout}
          rows={rows}
          sort={sort}
          order={order}
          selected={selected}
          flagLabels={flagLabels}
          onSort={onSort}
          onResize={(key, width) =>
            updateLayout(
              layout.map((entry) =>
                entry.key === key ? { ...entry, width } : entry,
              ),
            )
          }
          onReorder={(from, to) => {
            const next = [...layout];
            const fromIndex = next.findIndex((entry) => entry.key === from);
            const toIndex = next.findIndex((entry) => entry.key === to);
            const [moved] = next.splice(fromIndex, 1);
            next.splice(toIndex, 0, moved);
            updateLayout(next);
          }}
          onSelect={onSelect}
          onOpen={setOpenCase}
        />
      )}

      <div className="row-between">
        <span className="muted">
          {total === 0
            ? "No rows"
            : `${offset + 1}–${Math.min(offset + limit, total)} of ${total}`}
          {selected.size ? ` · ${selected.size} selected` : ""}
        </span>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <select
            className="input-field"
            aria-label="Rows per page"
            value={limit}
            onChange={(event) => {
              setLimit(Number(event.target.value));
              setOffset(0);
            }}
          >
            {PAGE_SIZES.map((size) => (
              <option key={size} value={size}>
                {size} per page
              </option>
            ))}
          </select>
          <button
            type="button"
            className="btn-chip"
            disabled={offset === 0}
            onClick={() => setOffset(Math.max(0, offset - limit))}
          >
            Previous
          </button>
          <button
            type="button"
            className="btn-chip"
            disabled={offset + limit >= total}
            onClick={() => setOffset(offset + limit)}
          >
            Next
          </button>
        </div>
      </div>

      {exportsList?.exports?.length ? (
        <section className="panel" style={{ padding: 12 }}>
          <div className="eyebrow">Exports</div>
          <div className="table-scroll">
            <table className="data-table">
              <thead>
                <tr>
                  <th>Requested</th>
                  <th>Format</th>
                  <th>Rows</th>
                  <th>Status</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {exportsList.exports.map((job) => (
                  <tr key={job.id}>
                    <td className="faint">{(job.created_at ?? "").slice(0, 16)}</td>
                    <td>{job.format.toUpperCase()}</td>
                    <td>{job.row_count ?? "—"}</td>
                    <td>
                      <span
                        className={`badge ${
                          job.status === "done"
                            ? "badge-ok"
                            : job.status === "failed"
                              ? "badge-error"
                              : "badge-neutral"
                        }`}
                      >
                        {job.status}
                      </span>
                      {job.error ? (
                        <div className="evidence">{job.error}</div>
                      ) : null}
                    </td>
                    <td>
                      {job.status === "done" ? (
                        <button
                          type="button"
                          className="btn-chip"
                          onClick={() => download(job)}
                        >
                          Download
                        </button>
                      ) : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      ) : null}

      {openCase ? (
        <Drawer
          detail={detail ?? null}
          loading={detailLoading}
          onClose={() => setOpenCase(null)}
          onPrevious={() => {
            const index = rows.findIndex((row) => row.case_id === openCase);
            if (index > 0) setOpenCase(rows[index - 1].case_id);
          }}
          onNext={() => {
            const index = rows.findIndex((row) => row.case_id === openCase);
            if (index >= 0 && index < rows.length - 1)
              setOpenCase(rows[index + 1].case_id);
          }}
        />
      ) : null}
    </div>
  );
}

/** The bearer token, fetched per request like every other call. */
async function currentToken(): Promise<string> {
  const { currentIdToken } = await import("@/lib/auth");
  return (await currentIdToken()) ?? "";
}
