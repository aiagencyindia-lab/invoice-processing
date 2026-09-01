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

let token: string | null = null;

/** Set once sign-in lands; until then the API is reached in dev mode,
 *  where the service issues a local principal and ignores the header. */
export function setToken(value: string | null): void {
  token = value;
}

export function hasToken(): boolean {
  return token !== null;
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
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
