import { defineConfig } from "@playwright/test";

/** End-to-end smoke over the golden path.
 *
 *  Both servers are real: the FastAPI service and a production build of this
 *  app. The API runs against a scratch data tree (see scripts/e2e_api.py) so a
 *  run that approves invoices and saves settings cannot touch anyone's real
 *  invoices.
 *
 *  Extraction is deliberately out of scope. It needs Vertex, costs a model
 *  call, and has been observed hanging for 15-25 minutes -- exactly the thing
 *  a smoke test must not depend on. The run covers upload-to-queued and
 *  everything downstream of an already-processed case, which is where the UI
 *  actually lives.
 */
const API_PORT = process.env.E2E_API_PORT ?? "8100";
const APP_PORT = process.env.E2E_APP_PORT ?? "3100";

export default defineConfig({
  testDir: "./e2e",
  // A failing smoke test is a signal to look, not to retry until it passes.
  retries: 0,
  // One spec file at a time: every file shares one API server and one
  // scratch database, and two files acting on the same seeded invoice at
  // once made golden-path flaky (an export racing another file's approval).
  // ponytail: serial costs a few minutes; per-file seeded tenants if the
  // suite grows past what one worker runs in the job's 20 minutes.
  workers: 1,
  timeout: 30_000,
  expect: { timeout: 10_000 },
  reporter: process.env.CI ? "list" : "line",
  use: {
    baseURL: `http://127.0.0.1:${APP_PORT}`,
    trace: "retain-on-failure",
  },
  webServer: [
    {
      // uv sync puts the interpreter at the repo root's .venv, on CI as well
      // as locally; E2E_PYTHON is the escape hatch for anything else.
      command: `${process.env.E2E_PYTHON ?? "../.venv/bin/python"} ../scripts/e2e_api.py`,
      url: `http://127.0.0.1:${API_PORT}/api/system/status`,
      reuseExistingServer: false,
      timeout: 120_000,
      env: { E2E_API_PORT: API_PORT },
    },
    {
      command: `npx next start --port ${APP_PORT}`,
      url: `http://127.0.0.1:${APP_PORT}/dashboard`,
      reuseExistingServer: false,
      timeout: 120_000,
      // Baked at build time, so the build must have pointed here too.
      env: { API_ORIGIN: `http://127.0.0.1:${API_PORT}` },
    },
  ],
});
