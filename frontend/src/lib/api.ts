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
  ) {
    super(message);
  }

  /** 401/403 read differently to a user: one means sign in, the other
   *  means your role is too low. Both are "not your fault" -- neither
   *  should surface as a raw status code. */
  get friendly(): string {
    if (this.status === 401) return "Your session has expired. Sign in again.";
    if (this.status === 403) return "Your role does not allow this.";
    return this.message;
  }
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
    throw new ApiError(response.status, detail || response.statusText);
  }
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

export type DocumentRow = {
  document_id: string;
  filename: string | null;
  source: string | null;
  byte_size: number | null;
  sha256: string | null;
  received_at: string | null;
  stage: "queued" | "processing" | "done" | "failed" | "unknown";
  explanation: string;
  case_id: string;
};

export type DocumentList = {
  documents: DocumentRow[];
  counts: Record<string, number>;
  total: number;
};

export type AuditEvent = {
  at: string | null;
  actor: string | null;
  action: string | null;
  entity_type: string | null;
  entity_id: string | null;
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
};

export type Correction = {
  case_id: string;
  field_path: string;
  old_value: unknown;
  new_value: unknown;
  actor: string;
  at: string;
};

export type InvoiceDetail = InvoiceSummary & {
  fields: InvoiceField[];
  line_items: Record<string, unknown>[];
  findings: Finding[];
  corrections: Correction[];
};

export type PurchaseOrder = {
  po_number: string | null;
  vendor_name: string | null;
  total_amount: number | null;
  amount_invoiced: number | null;
  remaining: number;
  currency: string | null;
  status: string | null;
  open: boolean;
};

export type PurchaseOrderList = {
  purchase_orders: PurchaseOrder[];
  count: number;
  source: string;
};

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
  email: string;
  role: string;
  uid?: string | null;
  display_name?: string | null;
  claims_written?: boolean;
};

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

export type Check = {
  id: string;
  name: string;
  phase: string;
  rejection: string | null;
  informational: boolean;
  mandatory: boolean;
  enabled: boolean;
};
