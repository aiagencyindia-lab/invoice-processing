import { expect, test, type Page } from "@playwright/test";

/** The Wave 3 surfaces: the workbench hold banner and trust score, evidence
 *  requests, the vendor profile, posture, engine readiness and integrations.
 *
 *  The backends land in parallel branches and answer 503 (or 404) until they
 *  merge. Each test asks the route first: when it answers 200 the full flow
 *  is asserted, otherwise the view must still render and say why, rather than
 *  crash. After the backends merge every gate is open. */

const ADMIN = "dev@localhost:admin";
const CLERK = "alice@e2e.test:clerk";

async function goto(page: Page, path: string) {
  await page.goto(path);
  await expect(page.locator("nav[aria-label='Primary']")).toBeVisible();
}

function as(page: Page, who: string) {
  return page.setExtraHTTPHeaders({ "X-Dev-Principal": who });
}

/** Whether a backend route is built: page.request does not carry the page's
 *  extra headers, so the principal is passed explicitly. */
async function built(page: Page, path: string, who = ADMIN): Promise<boolean> {
  const response = await page.request.get(path, { headers: { "X-Dev-Principal": who } });
  return response.status() === 200;
}

async function case005Node(page: Page): Promise<string> {
  const response = await page.request.get("/api/invoices/case_005", { headers: { "X-Dev-Principal": ADMIN } });
  const detail = await response.json();
  return detail.trust_node as string;
}

test("the workbench shows no hold with every mode off, and the six trust dimensions", async ({ page }) => {
  const node = await case005Node(page);
  expect(node).toBeTruthy();
  const scored = await built(page, `/api/trust/transactions/${node}/trust`);

  await goto(page, "/invoices?case=case_005");
  const panel = page.locator("[aria-label='Trust score']");
  await expect(panel).toBeVisible();
  // Every engine is off by default, so nothing holds this payment.
  await expect(page.getByRole("alert", { name: "Payment hold" })).toHaveCount(0);

  if (scored) {
    await expect(panel.locator("[data-dimension]")).toHaveCount(6);
    for (const label of ["Identity", "Authorization", "Reality", "Price", "Authenticity", "Consistency"]) {
      await expect(panel.getByText(label, { exact: true })).toBeVisible();
    }
    await expect(panel.getByText(/composite · advisory/)).toBeVisible();
  } else {
    await expect(panel).toContainText("Not built yet");
  }
});

test("a clerk requests evidence on a reality finding", async ({ page }) => {
  const live = await built(page, "/api/trust/evidence-requests", CLERK);
  await as(page, CLERK);
  await goto(page, "/findings");
  await page.getByRole("row", { name: /REAL-NO-EVIDENCE/ }).getByRole("link", { name: "REAL-NO-EVIDENCE" }).click();
  await expect(page.getByRole("heading", { name: /REAL-NO-EVIDENCE/ })).toBeVisible();
  // The reality type counts against the Reality dimension.
  await expect(page.locator("header .badge", { hasText: "Reality" })).toBeVisible();

  // Evidence is asked for on a triaged finding (detected → triaged first).
  await expect(page.getByRole("button", { name: "Request evidence" })).toHaveCount(0);
  await page.getByRole("button", { name: "Triaged" }).first().click();
  await page.getByRole("dialog").getByRole("button", { name: "Triaged" }).click();
  await expect(page.locator("header .badge").filter({ hasText: "Triaged" })).toBeVisible();

  await page.getByRole("button", { name: "Request evidence" }).click();
  const dialog = page.getByRole("dialog", { name: "Request evidence" });
  await dialog.getByLabel("Owner").fill("stores@e2e.test");
  await dialog.getByLabel("Due date").fill("2026-11-30");
  await dialog.getByLabel("Note (optional)").fill("Goods receipt for the 250 hex bolts");
  await dialog.getByRole("button", { name: "Send request" }).click();

  if (live) {
    await expect(page.getByText("Evidence requested from stores@e2e.test.")).toBeVisible();
    const requests = page.locator("[aria-label='Evidence requests']");
    await expect(requests.getByRole("row", { name: /stores@e2e.test/ })).toContainText("2026-11-30");
    await expect(page.locator("header .badge").filter({ hasText: "Needs vendor input" })).toBeVisible();
  } else {
    await expect(dialog.getByRole("alert")).toContainText("Not built yet");
  }
});

test("a vendor opens from the vendor list with its profile", async ({ page }) => {
  const list = await (await page.request.get("/api/vendors", { headers: { "X-Dev-Principal": ADMIN } })).json();
  const vendor = (list.vendors as { id: string; name: string; node: string | null }[]).find(
    (row) => row.name === "QuickFix HVAC Repairs" && row.node,
  );
  expect(vendor, "the seed puts case_005's supplier in the vendor master").toBeTruthy();
  const profiled = await built(page, `/api/trust/vendors/${vendor!.node}/trust`);

  await goto(page, "/vendors");
  await page.getByRole("link", { name: "QuickFix HVAC Repairs" }).click();
  await expect(page.getByRole("heading", { name: "QuickFix HVAC Repairs" })).toBeVisible();
  const facts = page.locator("[aria-label='Vendor profile']");
  await expect(facts).toContainText("Tax ID");
  await expect(facts).toContainText("GST standing");
  if (profiled) {
    await expect(page.locator("[aria-label='Trust dimensions'] [data-dimension]")).toHaveCount(6);
    await expect(page.getByText("Findings by type")).toBeVisible();
    await expect(page.getByRole("link", { name: "REAL-NO-EVIDENCE" })).toBeVisible();
  } else {
    await expect(page.getByText("Not built yet")).toBeVisible();
  }
});

test("trust posture shows the money tiles", async ({ page }) => {
  const live = await built(page, "/api/trust/posture");
  await goto(page, "/trust-posture");
  await expect(page.getByRole("heading", { name: "Trust posture" })).toBeVisible();
  if (live) {
    for (const tile of ["found", "prevented", "recovered", "written_off", "open_exposure", "holds"]) {
      await expect(page.locator(`[data-tile='${tile}']`)).toBeVisible();
    }
    await expect(page.getByRole("table", { name: "By type" })).toContainText("REAL-NO-EVIDENCE");
    await page.getByRole("button", { name: "All time" }).click();
    await expect(page).toHaveURL(/days=0/);
    await expect(page.locator("[data-tile='found']")).toBeVisible();
  } else {
    await expect(page.locator(".empty-state[role=alert]")).toContainText("Not built yet");
  }
});

test("an administrator promotes and demotes an engine with a reason, and it is audited", async ({ page }) => {
  const live = await built(page, "/api/trust/readiness");
  await goto(page, "/trust-engines");
  await expect(page.getByRole("heading", { name: "Trust engines" })).toBeVisible();
  if (!live) {
    await expect(page.locator(".empty-state[role=alert]")).toContainText("Not built yet");
    return;
  }
  const table = page.getByRole("table", { name: "Engine readiness" });
  // Every engine starts off, and off → shadow is always recommended.
  await expect(table.locator("[data-recommendation='reality']")).not.toBeEmpty();

  await page.getByRole("button", { name: "Promote Reality to shadow" }).click();
  const dialog = page.getByRole("dialog", { name: "Change engine mode" });
  await expect(dialog.getByRole("button", { name: "Set shadow" })).toBeDisabled();
  await dialog.getByLabel("Reason (required)").fill("E2E: start collecting evidence");
  await dialog.getByRole("button", { name: "Set shadow" }).click();
  await expect(page.getByText("Reality is now shadow.")).toBeVisible();

  // Back to off, so the rest of the suite sees the default modes.
  await page.getByRole("button", { name: "Demote Reality to off" }).click();
  await dialog.getByLabel("Reason (required)").fill("E2E: back to the default");
  await dialog.getByRole("button", { name: "Set off" }).click();
  await expect(page.getByText("Reality is now off.")).toBeVisible();

  const audit = await (await page.request.get("/api/audit?limit=100", { headers: { "X-Dev-Principal": ADMIN } })).json();
  const changes = (audit.events as unknown[]).filter((event) =>
    JSON.stringify(event).includes("trust_engine_mode_changed"),
  );
  expect(changes.length).toBeGreaterThanOrEqual(2);
});

test("an administrator creates an API key, sees it once, and revokes it", async ({ page }) => {
  const live = await built(page, "/api/integrations/api-keys");
  await goto(page, "/integrations");
  await expect(page.getByRole("heading", { name: "Integrations" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "API keys" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Webhooks" })).toBeVisible();
  if (!live) {
    await expect(page.locator("[aria-label='API keys'] .empty-state")).toBeVisible();
    return;
  }
  const name = "E2E ledger sync";
  await page.getByLabel("Key name").fill(name);
  await page.getByLabel("Key role").selectOption("viewer");
  await page.getByRole("button", { name: "Create key" }).click();
  const reveal = page.getByRole("status", { name: "New API key" });
  await expect(reveal.locator("code")).toHaveText(/^ik_/);
  await reveal.getByRole("button", { name: "Done" }).click();
  await expect(reveal).toHaveCount(0);

  const row = page.getByRole("row", { name: new RegExp(name) });
  await expect(row).toContainText("viewer");
  page.once("dialog", (dialog) => dialog.accept());
  await row.getByRole("button", { name: `Revoke ${name}` }).click();
  await expect(row.getByText("revoked")).toBeVisible();
});
