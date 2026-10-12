/** One fetch wrapper for the whole app.
 *
 *  Every request goes to a same-origin /api path, which next.config.ts
 *  rewrites to the FastAPI service. The browser therefore never sees a
 *  cross-origin request and the bearer token never crosses an origin.
 */

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
    /** The raw `detail`, for the refusals that carry structure rather than a
     *  sentence -- a 423 names the lock holder and when it lapses. */
    readonly detail?: unknown,
  ) {
    super(message);
  }

  /** 401/403 read differently to a user: one means sign in, the other
   *  means your role is too low. Both are "not your fault" -- neither
   *  should surface as a raw status code. */
  get friendly(): string {
    if (this.status === 401) return "Your session has expired. Sign in again.";
    // A 403 usually carries the reason ("Step 2 is assigned to user:x."),
    // which is more useful than a blanket role message.
    if (this.status === 403) {
      return typeof this.detail === "string" && this.detail
        ? this.detail
        : "Your role does not allow this.";
    }
    if (this.status === 423) {
      const lock = this.detail as RecordLock | undefined;
      return lock?.holder
        ? `${lock.holder} is editing this invoice until ${clockTime(lock.expires_at)}.`
        : "Someone else is editing this invoice.";
    }
    return this.message;
  }
}

export type RecordLock = {
  case_id?: string;
  holder: string;
  expires_at: string | null;
};

/** An expiry as a wall clock, because "until 14:35" is actionable and an ISO
 *  timestamp is something the reader has to decode. */
export function clockTime(iso: string | null | undefined): string {
  if (!iso) return "shortly";
  const when = new Date(iso);
  return Number.isNaN(when.getTime())
    ? "shortly"
    : when.toLocaleTimeString(undefined, {
        hour: "2-digit",
        minute: "2-digit",
      });
}

/** Where the bearer token comes from.
 *
 *  A provider function rather than a stored string: Identity Platform tokens
 *  expire after an hour, so a token captured at sign-in would turn every
 *  request after that into a 401. The provider is asked on each request and
 *  hands back a refreshed token when the old one is close to expiring.
 */
type TokenProvider = () => Promise<string | null>;

let provider: TokenProvider | null = null;

export function setTokenProvider(value: TokenProvider | null): void {
  provider = value;
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  const token = provider ? await provider() : null;
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (init.body && !(init.body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
  }

  const response = await fetch(path, { ...init, headers });
  if (!response.ok) {
    // FastAPI puts the useful sentence in `detail`; falling back to the
    // status text alone would throw away the explanation the backend
    // went to the trouble of writing.
    const detail = await response
      .json()
      .then((body) => body?.detail)
      .catch(() => null);
    // A structured detail is kept whole and never squeezed into the message:
    // `String({holder})` is "[object Object]", which tells the reader nothing.
    // A pydantic 422 is a list of {loc, msg}; show it as "field: message".
    const message =
      typeof detail === "string" && detail
        ? detail
        : Array.isArray(detail) && detail.length
          ? detail
              .map(
                (item: { loc?: unknown[]; msg?: string }) =>
                  `${(item.loc ?? []).filter((p) => p !== "body").join(".")}: ${item.msg ?? ""}`,
              )
              .join("\n")
          : response.statusText;
    throw new ApiError(response.status, message, detail);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export type SystemStatus = {
  worker: {
    alive: boolean;
    last_seen_seconds: number | null;
    stale_after_seconds: number;
    ever_run: boolean;
  };
  queue: { depth: number | null; dead_letters: number | null };
  storage: { documents: string; database: string; queue: string };
};

export type Me = { email: string; role: string; tenant_id: string };

/** Ascending privilege, mirroring ROLE_RANK in api/auth.py. An unknown role
 *  ranks below viewer, so a typo in a claim hides everything rather than
 *  revealing it -- the same direction the server fails in. */
export const ROLE_RANK: Record<string, number> = {
  viewer: 0,
  clerk: 1,
  approver: 2,
  admin: 3,
};

export function can(me: Me | null, required: string): boolean {
  if (!me) return false;
  return (ROLE_RANK[me.role] ?? -1) >= (ROLE_RANK[required] ?? 99);
}

export type Stats = {
  total: number;
  needs_review: number;
  auto: number;
  touchless_rate: number | null;
  corrections: number;
  corrected_fields: string[];
};

export type KpiReason =
  | "rejected_by_rules"
  | "three_way_hold"
  | "low_confidence"
  | "approval_required"
  | "manual_decision"
  | "other";

export const REASON_LABELS: Record<KpiReason, string> = {
  rejected_by_rules: "Rejected by rules",
  three_way_hold: "Held for goods receipt",
  low_confidence: "Sent for review",
  approval_required: "Approval chain",
  manual_decision: "Decided by hand",
  other: "Other",
};

export type Kpis = {
  window: { days: number; since: string | null };
  received: number;
  decided: number;
  touchless: number;
  touchless_rate: number | null;
  exception_rate: number | null;
  exceptions: Record<KpiReason, number>;
  cycle_hours: { median: number | null; p90: number | null; skipped: number };
  open: { count: number; oldest_hours: number | null };
  three_way: { po_invoices: number; held: number; hold_rate: number | null };
  ai_cost: { per_invoice_usd: number | null; total_usd: number };
};

export type KpiCases = { case_ids: string[]; truncated: boolean };

export type DocumentRow = {
  document_id: string;
  filename: string | null;
  source: string | null;
  byte_size: number | null;
  sha256: string | null;
  received_at: string | null;
  stage:
    | "queued"
    | "processing"
    | "done"
    | "failed"
    | "unknown"
    | "awaiting_confirmation"
    | "blocked"
    | "accepted_unstored";
  /** invoice | purchase_order | goods_receipt; null until classified. */
  kind: string | null;
  explanation: string;
  /** How far through the nine pipeline steps the worker is. Null until it
   *  reports one -- an older document processed before this existed has none. */
  progress: {
    step: number;
    total: number;
    key: string;
    label: string;
  } | null;
  case_id: string;
};

export type DocumentList = {
  documents: DocumentRow[];
  counts: Record<string, number>;
  total: number;
};

export type AuditEvent = {
  id: string | null;
  at: string | null;
  actor: string | null;
  actor_type: string | null;
  action: string | null;
  entity_type: string | null;
  entity_id: string | null;
  /** What the event recorded. `before` is the prior state where there was
   *  one -- null on an event that created something rather than changing it. */
  payload: Record<string, unknown> | null;
  before: Record<string, unknown> | null;
  request_id: string | null;
  ip: string | null;
};

export type SettingField = {
  key: string;
  type: string;
  description: string;
  value: unknown;
};

export type SettingsResponse = {
  settings: Record<string, unknown>;
  fields: SettingField[];
};

export type InvoiceSummary = {
  case_id: string;
  vendor: string | null;
  invoice_number: string | null;
  invoice_date: string | null;
  currency: string | null;
  total: number | null;
  decision: string | null;
  rejection_reason: string | null;
  routing: string;
  min_confidence: number | null;
  fields_needing_review: string[];
  has_text_layer: boolean | null;
};

export type InvoiceList = {
  invoices: InvoiceSummary[];
  counts: { total: number; needs_review: number; auto: number };
};

export type InvoiceField = {
  name: string;
  value: unknown;
  confidence: number | null;
  grounded: boolean | null;
  page: number | null;
  checked: boolean;
};

export type Finding = {
  phase: string;
  step: string | null;
  rule: string | null;
  passed: boolean | null;
  informational: boolean;
  implemented: boolean;
  evidence: string | null;
  rejection_template: string | null;
  signals?: string[] | null;
  lines?: ThreeWayLine[] | null;
};

export type ThreeWayLine = {
  line_number: number;
  description: string;
  po_line_number: number | null;
  paired_by: "item_code" | "line_number" | "description" | null;
  quantity: number;
  unit_price: number;
  billed: number;
  received: number;
  signals: string[];
  evidence: string;
};

export type Correction = {
  case_id: string;
  field_path: string;
  old_value: unknown;
  new_value: unknown;
  actor: string;
  at: string;
};

/** Why a reviewer decided what they did. Mirrors REASON_CATEGORIES in
 *  `api/service.py`; the labels are what a person reads. */
export const REASON_CATEGORIES: { value: string; label: string }[] = [
  { value: "policy_exception", label: "Policy exception — the rulebook doesn't cover this" },
  { value: "extraction_wrong", label: "Read the invoice wrong" },
  { value: "duplicate", label: "Duplicate — already paid or in flight" },
  { value: "vendor_issue", label: "Something the supplier must fix" },
  { value: "other", label: "Other" },
];

export type RuleProposal = {
  rule_id: string;
  name: string;
  description: string | null;
  status: "pending_draft" | "drafting" | "draft" | "approved" | "rejected" | "failed";
  enabled: boolean;
  conditions: { field: string; operator: string; value: unknown }[];
  actions: { type: string; target?: string; value?: unknown }[];
  author: string | null;
  approver: string | null;
  source_case_id: string | null;
  reason_category: string | null;
  note: string | null;
  impact: {
    target_matched?: boolean;
    collateral_matches?: { case_id?: string; decision?: string }[];
    total_cases?: number;
    sampled?: boolean;
    sample_size?: number;
    summary?: string;
  } | null;
  /** The rule still matches cases nobody meant it to. Approving it needs a
   *  deliberate acknowledgement, because that is how an exemption for one
   *  supplier quietly re-decides everyone else's invoices. */
  has_collateral: boolean;
  /** Action types that call a model. A rule carrying one runs Gemini on every
   *  invoice it matches, for as long as it is enabled. */
  llm_actions: string[];
  collateral_warning: string | null;
  error: string | null;
  created_at: string | null;
};

export type Workflow = {
  decision?: string;
  decided_by?: string;
  decided_at?: string;
  reason?: string | null;
  reason_category?: string;
};

export type InvoiceDetail = InvoiceSummary & {
  workflow: Workflow;
  /** null until someone decides; then agree | exemption | too_permissive. */
  agreement: string | null;
  fields: InvoiceField[];
  line_items: Record<string, unknown>[];
  /** The projected lines: the numbering the GL endpoint addresses, and each
   *  line's coded account. Absent from an older API. */
  lines?: Record<string, unknown>[];
  findings: Finding[];
  corrections: Correction[];
  /** Who is editing this record, if anyone. Null also means the deployment has
   *  no lock table, so a caller must not read absence as "free to edit
   *  exclusively" -- only as "nobody is stopping you". */
  lock: RecordLock | null;
  /** Per-line GL coding; absent on a case coded before GL coding existed. */
  gl_coding?: GlCoding | null;
};

/** A tenant rule that codes invoice lines to a GL account. At least one of
 *  vendor_name/keyword is set. */
export type GlRule = {
  id: string;
  vendor_name: string | null;
  keyword: string | null;
  gl_account: string;
  enabled: boolean;
  created_by: string;
  created_at: string;
};

export type GlCodingLine = {
  line_number: number;
  gl_account: string | null;
  source: "rule" | "manual" | "po" | "none";
  rule_id: string | null;
  /** Set when source is "po": the PO line the account came from. */
  po_line_number?: number | null;
  coded_by: string | null;
  coded_at: string | null;
};

export type GlCoding = { lines: GlCodingLine[] };

export type PurchaseOrder = {
  po_number: string | null;
  vendor_name: string | null;
  total_amount: number | null;
  amount_invoiced: number | null;
  remaining: number;
  currency: string | null;
  status: string | null;
  open: boolean;
  line_count?: number;
  received_total?: number;
  billed_total?: number;
};

export type PoLine = {
  line_number: number;
  item_code: string | null;
  description: string | null;
  qty_ordered: number;
  unit_price: number | null;
  uom: string | null;
  receipt_required: boolean;
  gl_account: string | null;
  received: number;
  billed: number;
  remaining: number;
};

export type Grn = {
  grn_number: string;
  received_on: string;
  reason: string | null;
  recorded_by: string | null;
  /** "document" when a clerk confirmed it from a scan. */
  source?: string | null;
  document_id?: string | null;
  lines: { line_number: number; qty_received: number }[];
};

export type Evidence = {
  value: unknown;
  grounded: boolean;
  page: number | null;
  confidence: number | null;
};

export type SupportingDocument = {
  id: string;
  document_id: string;
  kind: "purchase_order" | "goods_receipt";
  status: "pending" | "blocked" | "accepted" | "rejected";
  po_number: string | null;
  blocked_reason: string | null;
  extraction_error: string | null;
  document_url: string;
  /** Something (a PO or receipts) was stored from it; false on an accepted
   *  row means the acceptance stored nothing and an admin can reopen it. */
  stored: boolean;
  extracted: {
    kind?: string;
    fields: Record<string, string | number | null>;
    lines: Record<string, string | number | null>[];
    _evidence?: Record<string, Evidence | Record<string, unknown>>;
  } | null;
};

export type SupportingList = {
  documents: SupportingDocument[];
  /** The server's cap (200) cut the list. */
  truncated: boolean;
};

export type SupportingDetail = SupportingDocument & {
  /** Per extracted receipt line, the PO line number it pairs to (null: none). */
  paired: (number | null)[] | null;
  po_on_file: boolean;
};

// The order's receipts come back under `receipts` (po_routes.read_purchase_order).
export type PurchaseOrderDetail = PurchaseOrder & {
  lines: PoLine[];
  receipts: Grn[];
};

export type ImportResult = {
  created?: number;
  updated?: number;
  lines?: number;
  recorded?: number;
  skipped_duplicates?: number;
  rematch_queued?: number;
};

export type PurchaseOrderList = {
  purchase_orders: PurchaseOrder[];
  count: number;
  source: string;
};

export type Vendor = {
  id: string;
  name: string;
  tax_id: string;
  kind: string;
  email_domain: string | null;
  bank_status: "verified" | "unverified" | "pending";
  bank_account_masked: string | null;
  bank_account_name: string | null;
  bank_code: string | null;
  bank_verified_at: string | null;
  bank_verified_by: string | null;
  pending_change_id: string | null;
};
export type VendorList = { vendors: Vendor[]; count: number; source: string };
export type BankChange = {
  id: string;
  vendor_id: string;
  vendor_name: string;
  status: "pending" | "approved" | "rejected";
  reason: string;
  source_case_id: string | null;
  proposed_by: string;
  decided_by: string | null;
  decided_at: string | null;
  decision_note: string | null;
  created_at: string | null;
  proposed_account_name: string | null;
  proposed_account_number: string | null;
  proposed_code: string | null;
  proposed_iban: string | null;
  previous_account_name: string | null;
  previous_account_number: string | null;
  previous_code: string | null;
  previous_iban: string | null;
};
export type BankChangeList = { changes: BankChange[]; count: number };

export type Tenant = {
  id: string;
  slug: string;
  name: string;
  region?: string | null;
  inbox: string | null;
  purchase_order_file: string;
};

export type TenantResponse = {
  tenant: Tenant;
  can_create_tenants: boolean;
};

export type User = {
  id?: string;
  manager_email?: string | null;
  email: string;
  role: string;
  uid?: string | null;
  is_active?: boolean;
  last_login_at?: string | null;
  display_name?: string | null;
  claims_written?: boolean;
};

export type SsoDomain = {
  id: string;
  domain: string;
  provider: "google" | "microsoft";
  default_role: "viewer" | "clerk";
  created_by: string;
  created_at: string | null;
};
export type SsoDomainList = { domains: SsoDomain[] };

export type UserList = { users: User[]; count: number; roles: string[] };

export type Integration = { configured: boolean; sets: string[] };

export type Integrations = Record<string, Integration>;

/** Fetch a binary response as an object URL, carrying the bearer token.
 *
 *  An `<iframe src="/api/...">` is a plain browser navigation: it sends no
 *  Authorization header and cannot be made to. Against a service that requires
 *  one, the document pane rendered the API's 401 body instead of the invoice --
 *  which local development never showed, because authentication is disabled
 *  there and the unauthenticated request succeeded.
 *
 *  The caller must revoke the URL when it is done with it, or every invoice a
 *  reviewer opens leaks its PDF for the life of the tab.
 */
export async function objectUrl(path: string): Promise<string> {
  const headers = new Headers();
  const token = provider ? await provider() : null;
  if (token) headers.set("Authorization", `Bearer ${token}`);

  const response = await fetch(path, { headers });
  if (!response.ok) {
    const detail = await response
      .json()
      .then((body) => body?.detail)
      .catch(() => null);
    throw new ApiError(response.status, detail || response.statusText);
  }
  return URL.createObjectURL(await response.blob());
}

/** Fetch a file with the bearer token and hand it to the browser as a
 *  download. A plain <a href> cannot carry the Authorization header. */
export async function downloadFile(path: string, filename: string): Promise<void> {
  const url = await objectUrl(path);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}

export type Check = {
  id: string;
  name: string;
  phase: string;
  rejection: string | null;
  informational: boolean;
  mandatory: boolean;
  enabled: boolean;
};

/* -------------------------------------------------------------------------
 * Approved-invoice sheet
 *
 * The server sends the column catalogue with every page, so the grid renders
 * what the query actually supports rather than a list kept in step by hand.
 * ---------------------------------------------------------------------- */

export type SheetColumn = {
  key: string;
  label: string;
  type: "text" | "number" | "money" | "date" | "timestamp" | "list" | "boolean";
  source: "column" | "workflow" | "derived";
  sortable: boolean;
  filterable: boolean;
  default: boolean;
};

export type SheetRow = Record<string, unknown> & {
  case_id: string;
  vendor_name: string | null;
  invoice_number: string | null;
  total_inc_tax: number | null;
  currency: string | null;
  approved_at: string | null;
  approved_by: string | null;
  quality_flags: string[];
  corrected_fields: number;
};

export type SheetPage = {
  rows: SheetRow[];
  total: number;
  offset: number;
  limit: number;
  columns: SheetColumn[];
  quality_flags: Record<string, string>;
};

export type SheetSummary = {
  total: number;
  by_currency: {
    currency: string;
    count: number;
    total: number;
    tax: number;
  }[];
  vendors: number;
  approved_today: number;
  approved_this_month: number;
  duplicates: number;
  needs_attention: number;
};

export type SheetFacets = { vendors: string[]; currencies: string[] };

export type SheetFilters = {
  status?: string;
  q?: string;
  vendor?: string;
  currency?: string;
  date_from?: string;
  date_to?: string;
  approved_from?: string;
  approved_to?: string;
  min_total?: string;
  max_total?: string;
  po?: string;
  tax?: string;
  quality?: string[];
};

export type SheetViewDefinition = {
  filters?: SheetFilters;
  sort?: { key: string; order: "asc" | "desc" };
  columns?: { key: string; width?: number; frozen?: boolean }[];
};

export type SheetView = {
  id: string;
  name: string;
  owner: string;
  shared: boolean;
  definition: SheetViewDefinition;
  created_at: string | null;
  updated_at: string | null;
};

export type ExportJob = {
  id: string;
  requested_by: string | null;
  format: "csv" | "xlsx" | "json";
  status: "queued" | "running" | "done" | "failed";
  row_count: number | null;
  storage_key: string | null;
  error: string | null;
  created_at: string | null;
  finished_at: string | null;
  /** Unfinished for so long that its runner is gone. Offers a retry. */
  stale: boolean;
};

/** Filters as a query string, omitting anything unset.
 *
 *  One place builds it, so the grid, the summary and an export always describe
 *  the same rows -- a total that counts something the sheet is not showing is
 *  a number someone will try to reconcile and lose an afternoon to.
 */
export function sheetQuery(filters: SheetFilters, extra: Record<string, string | number | undefined> = {}): string {
  const params = new URLSearchParams();
  Object.entries(filters).forEach(([key, value]) => {
    if (value === undefined || value === null || value === "") return;
    if (Array.isArray(value)) value.forEach((v) => params.append(key, v));
    else params.set(key, String(value));
  });
  Object.entries(extra).forEach(([key, value]) => {
    if (value !== undefined && value !== "") params.set(key, String(value));
  });
  return params.toString();
}

export type ApprovalStep = {
  id: string;
  position: number;
  assignee_kind: "user" | "group" | "role";
  assignee_value: string;
  status: "pending" | "approved" | "rejected" | "cancelled";
  due_at: string | null;
  decided_by: string | null;
  decided_at: string | null;
  acting_for: string | null;
  override_reason: string | null;
  escalated_to: string | null;
  escalated_at: string | null;
  note: string | null;
  source_rule_id: string | null;
};
/** `can_decide`/`reason` answer for the caller, by the same test the
 *  actions endpoint applies. */
export type ApprovalChain = {
  steps: ApprovalStep[];
  current: number | null;
  can_decide: boolean;
  reason: string | null;
};
export type MyApproval = {
  case_id: string;
  vendor: string | null;
  invoice_number: string | null;
  total: number | null;
  currency: string | null;
  position: number;
  due_at: string | null;
  overdue: boolean;
  why: string;
  assignee: string;
};
export type ApproverGroup = { id: string; name: string; members: string[] };
export type ApprovalRule = {
  id: string;
  name: string;
  kind: "vendor" | "entity" | "gl" | "amount";
  match: Record<string, unknown>;
  steps: { kind: "user" | "group" | "role"; value: string }[];
  enabled: boolean;
};
export type Delegation = {
  id: string;
  delegator_email: string;
  delegate_email: string;
  starts_on: string;
  ends_on: string;
  created_by: string;
};
/** The reply to an approve/reject: a chain with steps left answers with the
 *  step it recorded and the one now waiting; the last step finishes the case. */
export type ActionResult = {
  case_id: string;
  action: string;
  completed: boolean;
  step?: number;
  next?: ApprovalStep | null;
};
