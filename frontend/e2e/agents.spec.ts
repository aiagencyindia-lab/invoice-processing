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

test.describe("agents display", () => {
  // IST: a UTC evening is the next local day.
  test.use({ timezoneId: "Asia/Kolkata", locale: "en-US" });

  test("dates are local, a currency-less amount is no rupee, and the proposer is matched as the server folds", async ({ page }) => {
    const mandate = {
      id: "m1",
      agent_id: "a1",
      version: 2,
      scope: { actions: ["vendor.create"], currency: "INR", counterparties: null, caps: {} },
      valid_from: "2026-10-01T00:00:00+00:00",
      valid_to: "2027-01-31T20:00:00+00:00",
      status: "active",
      expired: false,
      verified: true,
      proposed_by: "it@e2e-sso.test",
      approved_by: "dev@localhost",
      approved_at: "2026-10-01T00:00:00+00:00",
      revoked_by: null,
      revoked_at: null,
      reason: null,
    };
    const agent = {
      id: "a1",
      name: "Mock bot",
      owner: "alice@e2e.test",
      description: null,
      kind: "external",
      status: "active",
      principal_id: null,
      principal_email: null,
      registered_by: "dev@localhost",
      revoked_at: null,
      revoked_by: null,
      revoke_reason: null,
      mandate,
    };
    // Stored as typed: the server trims and casefolds before comparing.
    const proposed = { ...mandate, id: "m3", version: 3, status: "proposed", approved_by: null, approved_at: null, proposed_by: " DEV@localhost " };
    const json = (body: unknown) => ({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
    await page.route("**/api/trust/agents", (r) => r.fulfill(json({ agents: [agent] })));
    await page.route("**/api/trust/agents/ledger", (r) =>
      r.fulfill(json({ since: "", until: "", owners: [{ owner: "alice@e2e.test", agents: 1, approved: { "": "1200" }, exported: {}, blocked: 0, reviewed: 0 }] })),
    );
    await page.route("**/api/trust/agents/a1/mandates", (r) => r.fulfill(json({ mandates: [mandate, proposed] })));
    await page.route("**/api/trust/agents/a1/ledger", (r) =>
      r.fulfill(json({ agent, mandate, since: "", until: "", spend: {}, period: null, actions: [], exceptions: [] })),
    );

    await goto(page, "/agents");
    const row = page.getByRole("table", { name: "Agents" }).getByRole("row", { name: /Mock bot/ });
    await expect(row).toContainText("v2 · until Feb 1, 2027 · verified");
    await expect(page.locator("[data-tile='approved'] .tile-value")).toHaveText("1,200.00");

    const before = await row.locator("td").first().evaluate((td) => getComputedStyle(td).backgroundColor);
    await row.click();
    await expect.poll(() => row.locator("td").first().evaluate((td) => getComputedStyle(td).backgroundColor)).not.toBe(before);

    const panel = page.getByRole("region", { name: "Agent Mock bot" });
    await expect(panel.getByRole("button", { name: "Approve v3" })).toBeDisabled();

    await panel.getByRole("button", { name: "Propose mandate" }).click();
    await expect(page.getByRole("dialog", { name: "Propose mandate" }).getByRole("checkbox", { name: "vendor.create" })).toBeFocused();
  });
});
