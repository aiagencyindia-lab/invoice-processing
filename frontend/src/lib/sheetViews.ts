import type { SheetFilters } from "@/lib/api";

/** The views everyone gets, without anyone having to save them.
 *
 *  Definitions rather than rows: a built-in view is the same for every tenant,
 *  and seeding six rows per tenant would mean a migration to change a filter
 *  and no way to fix one that turned out wrong.
 */
export const BUILT_IN_VIEWS: { id: string; name: string; definition: SheetFilters }[] = [
  { id: "all-approved", name: "All approved", definition: {} },
  {
    id: "this-month",
    name: "Approved this month",
    definition: { approved_from: new Date().toISOString().slice(0, 8) + "01" },
  },
  { id: "missing-po", name: "Missing PO", definition: { quality: ["missing_po"] } },
  {
    id: "tax-exceptions",
    name: "Tax exceptions",
    definition: { quality: ["tax_mismatch", "missing_tax"] },
  },
  {
    id: "needs-attention",
    // One flag, not three: naming three would ask for rows that are all of
    // them at once, which is almost nothing. The server composes the "any of"
    // behind this name, and the summary tile counts the same set.
    name: "Needs attention",
    definition: { quality: ["needs_attention"] },
  },
  { id: "edited", name: "Edited after extraction", definition: { quality: ["edited"] } },
];
