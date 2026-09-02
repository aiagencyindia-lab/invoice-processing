"use client";

import { useCallback, useRef } from "react";
import type { SheetColumn, SheetRow } from "@/lib/api";

/** The grid itself: sort, resize, reorder, freeze, select.
 *
 *  Hand-rolled rather than a library. The whole feature set here is a click
 *  handler on a header, a drag on its edge, and `position: sticky` -- adding a
 *  grid dependency would be more code to carry than the grid.
 *
 *  Sorting and paging are the server's job. A page is a hundred rows, so the
 *  browser is never asked to hold ten thousand.
 */

export type ColumnLayout = {
  key: string;
  width: number;
  frozen: boolean;
  hidden: boolean;
};

const NUMERIC = new Set(["number", "money"]);

function formatCell(
  value: unknown,
  column: SheetColumn,
  currency: string | null,
): string {
  if (value === null || value === undefined || value === "") return "—";
  if (column.type === "money") {
    const amount = Number(value).toLocaleString(undefined, {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    });
    return currency ? `${currency} ${amount}` : amount;
  }
  if (column.type === "number") return String(value);
  if (column.type === "timestamp") return String(value).slice(0, 10);
  return String(value);
}

export function Grid({
  columns,
  layout,
  rows,
  sort,
  order,
  selected,
  flagLabels,
  onSort,
  onResize,
  onReorder,
  onSelect,
  onOpen,
}: {
  columns: SheetColumn[];
  layout: ColumnLayout[];
  rows: SheetRow[];
  sort: string;
  order: "asc" | "desc";
  selected: Set<string>;
  flagLabels: Record<string, string>;
  onSort: (key: string) => void;
  onResize: (key: string, width: number) => void;
  onReorder: (from: string, to: string) => void;
  onSelect: (caseId: string, shiftKey: boolean) => void;
  onOpen: (caseId: string) => void;
}) {
  const byKey = new Map(columns.map((column) => [column.key, column]));
  const visible = layout.filter((entry) => !entry.hidden && byKey.has(entry.key));
  const dragged = useRef<string | null>(null);

  // Resizing tracks the pointer on the document rather than the header, so the
  // drag survives the cursor leaving the 8px grip.
  const startResize = useCallback(
    (key: string, startX: number, startWidth: number) => {
      const move = (event: PointerEvent) => {
        onResize(key, Math.max(80, startWidth + event.clientX - startX));
      };
      const stop = () => {
        document.removeEventListener("pointermove", move);
        document.removeEventListener("pointerup", stop);
      };
      document.addEventListener("pointermove", move);
      document.addEventListener("pointerup", stop);
    },
    [onResize],
  );

  // Frozen columns stick at their accumulated offset, so two frozen columns do
  // not stack on top of each other.
  let offset = 40;
  const offsets = new Map<string, number>();
  visible.forEach((entry) => {
    if (entry.frozen) {
      offsets.set(entry.key, offset);
      offset += entry.width;
    }
  });

  return (
    <div className="sheet-wrap">
      <table className="sheet">
        <thead>
          <tr>
            <th className="frozen" style={{ left: 0, width: 40 }} scope="col">
              <span className="sr-only">Select</span>
            </th>
            {visible.map((entry) => {
              const column = byKey.get(entry.key)!;
              const isSorted = sort === entry.key;
              return (
                <th
                  key={entry.key}
                  scope="col"
                  className={entry.frozen ? "frozen" : undefined}
                  style={{
                    width: entry.width,
                    minWidth: entry.width,
                    left: entry.frozen ? offsets.get(entry.key) : undefined,
                  }}
                  draggable
                  onDragStart={() => {
                    dragged.current = entry.key;
                  }}
                  onDragOver={(event) => event.preventDefault()}
                  onDrop={() => {
                    if (dragged.current && dragged.current !== entry.key) {
                      onReorder(dragged.current, entry.key);
                    }
                    dragged.current = null;
                  }}
                >
                  {column.sortable ? (
                    <button
                      type="button"
                      className="sheet-sort"
                      onClick={() => onSort(entry.key)}
                      aria-label={`Sort by ${column.label}`}
                    >
                      {column.label}
                      {/* The arrow is the state; the label says which way, so
                          direction is never carried by shape alone. */}
                      <span aria-hidden="true">
                        {isSorted ? (order === "asc" ? "↑" : "↓") : ""}
                      </span>
                      {isSorted ? (
                        <span className="sr-only">
                          {order === "asc" ? "ascending" : "descending"}
                        </span>
                      ) : null}
                    </button>
                  ) : (
                    column.label
                  )}
                  <button
                    type="button"
                    className="sheet-grip"
                    aria-label={`Resize ${column.label}`}
                    onPointerDown={(event) => {
                      event.preventDefault();
                      startResize(entry.key, event.clientX, entry.width);
                    }}
                  />
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.case_id} aria-selected={selected.has(row.case_id)}>
              <td className="frozen" style={{ left: 0, width: 40 }}>
                <input
                  type="checkbox"
                  checked={selected.has(row.case_id)}
                  aria-label={`Select ${row.invoice_number ?? row.case_id}`}
                  onChange={(event) =>
                    onSelect(
                      row.case_id,
                      (event.nativeEvent as MouseEvent).shiftKey,
                    )
                  }
                />
              </td>
              {visible.map((entry) => {
                const column = byKey.get(entry.key)!;
                const value = row[entry.key];
                return (
                  <td
                    key={entry.key}
                    className={[
                      entry.frozen ? "frozen" : "",
                      NUMERIC.has(column.type) ? "num" : "",
                    ]
                      .filter(Boolean)
                      .join(" ")}
                    style={{
                      width: entry.width,
                      left: entry.frozen ? offsets.get(entry.key) : undefined,
                    }}
                    onClick={() => onOpen(row.case_id)}
                  >
                    {entry.key === "quality_flags" ? (
                      <span className="chip-row">
                        {(row.quality_flags ?? []).map((flag) => (
                          <span
                            className="flag"
                            key={flag}
                            title={flagLabels[flag] ?? flag}
                          >
                            {flag.replace(/_/g, " ")}
                          </span>
                        ))}
                        {(row.quality_flags ?? []).length === 0 ? "—" : null}
                      </span>
                    ) : entry.key === "corrected_fields" ? (
                      row.corrected_fields ? (
                        <span className="badge badge-neutral">
                          {row.corrected_fields} edited
                        </span>
                      ) : (
                        "—"
                      )
                    ) : (
                      formatCell(value, column, row.currency)
                    )}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
