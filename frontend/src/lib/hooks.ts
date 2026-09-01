"use client";

import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "@/lib/api";

/** Fetch a JSON endpoint, optionally on an interval.
 *
 *  Polling rather than websockets: the pilot has a handful of users per
 *  tenant, and a socket would be a second delivery path to keep alive for
 *  no gain. `intervalMs = 0` fetches once.
 */
export function usePoll<T>(path: string | null, intervalMs = 0) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Starts true so a first paint shows "loading", not a misleading "none".
  const [loading, setLoading] = useState(true);
  const [nonce, setNonce] = useState(0);

  const reload = useCallback(() => setNonce((n) => n + 1), []);

  useEffect(() => {
    let cancelled = false;
    // A null path means "nothing selected yet", not an error: the workbench
    // has no detail to fetch until a case is chosen.
    if (path === null) return;

    const load = () =>
      api<T>(path)
        .then((value) => {
          if (cancelled) return;
          setData(value);
          setError(null);
        })
        .catch((caught) => {
          if (cancelled) return;
          setError(
            caught instanceof ApiError ? caught.friendly : String(caught),
          );
        })
        .finally(() => !cancelled && setLoading(false));

    load();
    if (!intervalMs) return () => {
      cancelled = true;
    };

    const timer = setInterval(load, intervalMs);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [path, intervalMs, nonce]);

  // A null path is never loading -- there is nothing on its way.
  return { data, error, loading: path !== null && loading, reload };
}
