// Vocabulary shared by the findings pages and the Trust Audit workspace.
// Mirrors invoice_processing/trust/findings/model.py; a change there lands
// here in the same change.

export const STATUS_LABELS: Record<string, string> = {
  detected: "Detected",
  triaged: "Triaged",
  accepted: "Accepted",
  rejected: "Rejected",
  needs_vendor_input: "Needs vendor input",
  claimed: "Claimed",
  recovered: "Recovered",
  prevented: "Prevented",
  written_off: "Written off",
};

export const TRANSITIONS: Record<string, string[]> = {
  detected: ["triaged"],
  triaged: ["accepted", "rejected", "needs_vendor_input"],
  needs_vendor_input: ["accepted", "rejected"],
  accepted: ["claimed", "prevented"],
  claimed: ["recovered", "prevented", "written_off"],
  rejected: [],
  recovered: [],
  prevented: [],
  written_off: [],
};

export const REASON_REQUIRED = new Set(["rejected", "written_off"]);

/** Statuses that move money: the API wants an approver for these. */
export const MONEY_STATUSES = new Set([
  "accepted",
  "rejected",
  "claimed",
  "recovered",
  "prevented",
  "written_off",
]);

/** Statuses the ledger prefills with the finding's impact. */
export const LEDGER_STATUSES = new Set(["recovered", "prevented", "written_off"]);

export const ENGINE_LABELS: Record<string, string> = {
  leakage: "Leakage",
  payment: "Payment Trust",
  provenance: "Provenance",
  gst_itc: "GST / ITC",
  reality: "Reality",
  agents: "Agent control",
  trust: "Trust layer",
};

/** The six Transaction Trust Score dimensions (trust/types.py DIMENSIONS). */
export const DIMENSION_LABELS: Record<string, string> = {
  identity: "Identity",
  authorization: "Authorization",
  reality: "Reality",
  price: "Price",
  authenticity: "Authenticity",
  consistency: "Consistency",
};

/** What a reality check found (trust/reality). */
export const REALITY_LABELS: Record<string, string> = {
  CORROBORATED: "Corroborated",
  PARTIAL: "Partly corroborated",
  NO_EVIDENCE: "No evidence",
  CONTRADICTED: "Contradicted",
};

export const STAGE_LABELS: Record<string, string> = {
  contract: "Contract",
  po: "Purchase order",
  grn: "Goods receipt",
  invoice: "Invoice",
  approval: "Approval",
  payment: "Payment",
  gl: "GL posting",
};

const INR = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 2, minimumFractionDigits: 2 });

/** "₹1,23,456.00" for INR, "1,234.50 USD" otherwise, "—" for nothing. */
export function money(amount: string | number | null | undefined, currency?: string | null): string {
  if (amount === null || amount === undefined || amount === "") return "—";
  const value = typeof amount === "number" ? amount : Number(amount);
  if (!Number.isFinite(value)) return String(amount);
  const text = INR.format(value);
  if (!currency || currency === "INR") return `₹${text}`;
  return `${text} ${currency}`;
}

export function statusBadge(status: string): string {
  if (status === "rejected" || status === "written_off") return "badge-neutral";
  if (status === "recovered" || status === "prevented") return "badge-ok";
  if (status === "detected") return "badge-warn";
  return "badge";
}

/** A vendor's name with its tax id: a misread GSTIN is a vendor of its own,
 *  and two of them would otherwise read the same. */
export function vendorLabel(v: { node?: string; name: string | null; tax_id?: string | null } | null | undefined, fallback = "—"): string {
  if (!v) return fallback;
  const name = v.name ?? v.node ?? fallback;
  return v.tax_id ? `${name} · ${v.tax_id}` : name;
}
