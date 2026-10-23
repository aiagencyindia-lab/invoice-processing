import { expect, test, type Page } from "@playwright/test";

/** Wave 5 agents screen: an agent bound to an API key, a two-person mandate,
 *  and a refused action that raises the banner and the blocks tile. Seeded
 *  through the API here; the specs share one database, so counts are read
 *  relative to a baseline. */

const ADMIN = "dev@localhost:admin";
const SECOND = "it@e2e-sso.test:admin";
const AGENT = "E2E bot";

async function goto(page: Page, path: string) {
  await page.goto(path);
  await expect(page.locator("nav[aria-label='Primary']")).toBeVisible();
}

function as(page: Page, who: string) {
  return page.setExtraHTTPHeaders({ "X-Dev-Principal": who });
}

async function call(page: Page, method: "get" | "post", path: string, who: string, data?: unknown) {
  const response = await page.request[method](path, { headers: { "X-Dev-Principal": who }, data });
  return { status: response.status(), body: await response.json() };
}

async function setAgentsMode(page: Page, mode: string) {
  const out = await call(page, "post", "/api/trust/engines/agents/mode", ADMIN, { mode, reason: `E2E: agents ${mode}` });
  expect(out.status).toBe(200);
}

test.describe.serial("agents", () => {
  let key = "";
  let agentId = "";
  let mandateId = "";

  test("an agent is registered and its mandate needs a second admin", async ({ page }) => {
    const created = await call(page, "post", "/api/integrations/api-keys", ADMIN, {
      name: "agents-e2e-bot",
      role: "approver",
    });
    expect(created.status).toBe(201);
    key = created.body.key;
    const agent = await call(page, "post", "/api/trust/agents", ADMIN, {
      name: AGENT,
      owner: "alice@e2e.test",
      principal: { api_key_id: created.body.id },
    });
    expect(agent.status, JSON.stringify(agent.body)).toBe(201);
    agentId = agent.body.id;
    const now = Date.now();
    const mandate = await call(page, "post", `/api/trust/agents/${agentId}/mandates`, ADMIN, {
      scope: { actions: ["vendor.create"], currency: "INR", counterparties: null, caps: {} },
      valid_from: new Date(now - 60_000).toISOString(),
      valid_to: new Date(now + 30 * 86_400_000).toISOString(),
    });
    expect(mandate.status).toBe(201);
    mandateId = mandate.body.id;

    await goto(page, "/agents");
    await expect(page.getByRole("heading", { name: "Agents", level: 1 })).toBeVisible();
    const table = page.getByRole("table", { name: "Agents" });
    await table.getByRole("row", { name: new RegExp(AGENT) }).click();
    const panel = page.getByRole("region", { name: `Agent ${AGENT}` });
    // The proposer cannot be the approver.
    await expect(panel.getByRole("button", { name: "Approve v1" })).toBeDisabled();
    await expect(panel.getByText("A different admin approves")).toBeVisible();
    // And the server says so if asked anyway.
    const self = await call(page, "post", `/api/trust/agents/${agentId}/mandates/${mandateId}/approve`, ADMIN);
    expect(self.status).toBe(403);
    expect(self.body.detail).toBe("A mandate needs a second person to approve it.");

    // The second admin approves from the screen.
    await as(page, SECOND);
    await goto(page, "/agents");
    await table.getByRole("row", { name: new RegExp(AGENT) }).click();
    await panel.getByRole("button", { name: "Approve v1" }).click();
    await page.getByRole("dialog", { name: "Approve mandate" }).getByRole("button", { name: "Approve" }).click();
    await expect(page.getByText("Mandate v1 approved.")).toBeVisible();
    await expect(table.getByRole("row", { name: new RegExp(AGENT) })).toContainText("v1 · until");
    await expect(table.getByRole("row", { name: new RegExp(AGENT) })).toContainText("verified");
    await expect(panel.getByRole("table", { name: "Mandates" })).toContainText("active");
  });

  test("a refused agent action raises the banner and the blocks tile", async ({ page }) => {
    const before = (await call(page, "get", "/api/trust/agents/blocks/count", ADMIN)).body.blocked as number;
    const vendors = (await call(page, "get", "/api/vendors", ADMIN)).body.vendors as { id: string }[];
    expect(vendors.length).toBeGreaterThan(0);
    await setAgentsMode(page, "enforcing");
    try {
      // An API key is at most an approver, so the agent asks for a bank
      // change (clerk+): outside its mandate, refused before anything is written.
      const refused = await page.request.post(`/api/vendors/${vendors[0].id}/bank-changes`, {
        headers: { "X-API-Key": key },
        data: { bank_account_number: "000111222", reason: "E2E: not in the mandate" },
      });
      expect(refused.status()).toBe(403);

      await goto(page, "/agents");
      await expect(page.getByRole("status").filter({ hasText: /agent action\(s\) blocked/ })).toBeVisible();
      await expect(page.locator("[data-tile='blocks'] .tile-value")).toHaveText(String(before + 1));
      await goto(page, "/dashboard");
      await expect(page.locator("[data-tile='agent-blocks'] .tile-value")).toHaveText(String(before + 1));
    } finally {
      await setAgentsMode(page, "off");
    }
  });

  test("the log import explains an engine that is off", async ({ page }) => {
    await goto(page, "/agents");
    await page.getByLabel("Action log (CSV or XLSX)").setInputFiles({
      name: "actions.csv",
      mimeType: "text/csv",
      buffer: Buffer.from(`agent,action,subject_ref,occurred_at\n${AGENT},vendor.create,vendors:x,2026-10-01T00:00:00Z\n`),
    });
    await page.getByRole("button", { name: "Import" }).click();
    const alert = page.getByRole("alert").filter({ hasText: "Agent control is off" });
    await expect(alert).toBeVisible();
    await expect(alert.getByRole("link", { name: "Trust engines" })).toHaveAttribute("href", "/trust-engines");
  });
});
