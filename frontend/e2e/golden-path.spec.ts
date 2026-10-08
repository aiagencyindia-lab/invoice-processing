import { expect, test, type Page } from "@playwright/test";

/** The flow the product is sold on: a document arrives, a reviewer reads the
 *  decision and the evidence behind it, corrects what is wrong, approves, and
 *  exports. Endpoint tests cover each step; this covers that a person can
 *  actually complete them in order. */

// A minimal but genuinely valid PDF, so the ingestion magic-byte check passes.
const PDF = Buffer.from(
  "%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n" +
    "2 0 obj<</Type/Pages/Kids[]/Count 0>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n",
);

async function goto(page: Page, path: string) {
  await page.goto(path);
  // Every view waits on /api/me before it renders anything.
  await expect(page.locator("nav[aria-label='Primary']")).toBeVisible();
}

test("the dashboard reports the numbers a pilot is judged on", async ({
  page,
}) => {
  await goto(page, "/dashboard");
  await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();
  for (const tile of ["Needs review", "Auto-routed", "Touchless rate"]) {
    await expect(page.getByText(tile, { exact: true })).toBeVisible();
  }
});

test("a reviewer can read an invoice and why it was decided", async ({
  page,
}) => {
  await goto(page, "/invoices");

  // The queue is seeded with the three frozen cases.
  const queue = page.locator(".queue-item");
  await expect(queue.first()).toBeVisible();
  expect(await queue.count()).toBeGreaterThanOrEqual(3);

  // Fields, the document, and the findings that explain the decision.
  await expect(page.locator("#f_vendor_name")).toBeVisible();
  await expect(page.locator("iframe[title='Source document']")).toBeVisible();
  await expect(page.getByText("Why this decision")).toBeVisible();
  await expect(page.locator(".finding").first()).toBeVisible();
});

test("j moves down the queue and the detail follows", async ({ page }) => {
  await goto(page, "/invoices");
  const first = await page.locator("#f_vendor_name").inputValue();

  await page.locator("h1", { hasText: "Invoices" }).click();
  await page.keyboard.press("j");

  await expect
    .poll(() => page.locator("#f_vendor_name").inputValue())
    .not.toBe(first);
});

test("an unapproved export explains itself instead of failing", async ({
  page,
}) => {
  await goto(page, "/invoices");
  await page.getByRole("button", { name: /^Export/ }).click();
  await expect(
    page.getByText("Approve the invoice before exporting"),
  ).toBeVisible();
});

test("approve, then export, produces a CSV", async ({ page }) => {
  await goto(page, "/invoices");

  // The last row deliberately. Approving advances to the next invoice, which
  // is right for a reviewer working a queue and wrong for a test that wants to
  // export the thing it just approved -- from the last row there is nowhere to
  // advance to.
  await page.locator(".queue-item").last().click();
  await expect(page.locator("#f_vendor_name")).toBeVisible();

  // Deciding asks why now: the category is what routes a disagreement into
  // the learning loop, and an approval over a pipeline rejection is the
  // exemption a rule gets written from.
  await page.getByRole("button", { name: /^Approve/ }).click();
  const dialog = page.getByRole("dialog", { name: "Approve invoice" });
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: "Approve" }).click();
  await expect(page.getByText(/^Approved /)).toBeVisible();

  // Filtered: headless Chromium treats the document pane's PDF as a download
  // too, so an unqualified wait catches whichever fires first.
  const download = page.waitForEvent("download", {
    predicate: (d) => d.suggestedFilename().endsWith(".csv"),
  });
  await page.getByRole("button", { name: /^Export/ }).click();
  expect((await download).suggestedFilename()).toMatch(/\.csv$/);
});

test("a rejection has to say why, and in which sense", async ({ page }) => {
  // The reason used to be a `window.prompt`, which Playwright cannot see and
  // a person cannot classify. The category is the half the system acts on:
  // only "policy exception" describes a rule the rulebook is missing.
  await goto(page, "/invoices?case=case_004");
  await expect(page.locator("#f_vendor_name")).toBeVisible();

  await page.getByRole("button", { name: /^Reject/ }).click();
  const dialog = page.getByRole("dialog", { name: "Reject invoice" });
  await expect(dialog).toBeVisible();

  // No note, no rejection: the API refuses one and so does the button.
  await expect(dialog.getByRole("button", { name: "Reject" })).toBeDisabled();

  await dialog.getByRole("textbox").fill("Wrong entity: this is Acme UK");
  await dialog.getByRole("button", { name: "Reject" }).click();
  await expect(page.getByText(/^Rejected /)).toBeVisible();
});

test("an edit is recorded as a correction", async ({ page }) => {
  await goto(page, "/invoices");

  const field = page.locator("#f_vendor_name");
  await field.fill("Corrected Vendor Pty Ltd");
  await page.getByRole("button", { name: /^Save/ }).click();

  await expect(page.getByText(/correction(s)? recorded/)).toBeVisible();
  // The corrections table is the audit half: old value, new value, who.
  await expect(page.getByText("Corrections")).toBeVisible();
  await expect(page.getByText("Corrected Vendor Pty Ltd").first()).toBeVisible();
});

test("an upload is received and queued, and says so", async ({ page }) => {
  await goto(page, "/documents");
  await expect(page.getByText(/No documents yet/)).toBeVisible();

  await page.locator("#upload").setInputFiles({
    name: "smoke-invoice.pdf",
    mimeType: "application/pdf",
    buffer: PDF,
  });

  // Received and queued -- not processed. No worker runs in this harness, and
  // claiming otherwise is the confusion the Documents view exists to end.
  await expect(page.getByText(/received and queued/)).toBeVisible();
  await expect(
    page.getByRole("cell", { name: "smoke-invoice.pdf" }),
  ).toBeVisible();
  await expect(page.locator(".badge", { hasText: "queued" })).toBeVisible();
});

test("processing paused is announced while no worker runs", async ({
  page,
}) => {
  await goto(page, "/dashboard");
  await expect(page.getByText(/No worker has ever run/)).toBeVisible();
});

test("an organisation name saved in settings survives a reload", async ({
  page,
}) => {
  await goto(page, "/settings");

  const names = page.locator("#organization_names");
  await names.fill("Smoke Test Pty Ltd\nSmoke Test Ltd");
  await page.getByRole("button", { name: "Save settings" }).click();
  await expect(page.getByText(/Saved: Organization names/)).toBeVisible();

  await page.reload();
  await expect(page.locator("#organization_names")).toHaveValue(
    /Smoke Test Pty Ltd/,
  );
});

test("every sidebar view opens", async ({ page }) => {
  await goto(page, "/dashboard");
  for (const label of [
    "Invoices",
    "Approvals",
    "Approved invoices",
    "Documents",
    "Activity",
    "Purchase orders",
    "Vendors",
    "Settings",
    "Admin",
  ]) {
    // Scoped to the rail: the brand link at its top is also "Invoices".
    await page
      .locator("nav[aria-label='Primary']")
      .getByRole("link", { name: label, exact: true })
      .click();
    await expect(page.getByRole("heading", { name: label })).toBeVisible();
  }
});

// Who the local dev principal is, per request: the e2e API runs with auth
// disabled, and this header is how one browser plays two people.
function as(page: Page, who: string) {
  return page.setExtraHTTPHeaders({ "X-Dev-Principal": who });
}

test("a bank change needs a second person to approve it", async ({ page }) => {
  const vendor = "E2E Dual Control Supplies";
  await goto(page, "/vendors");

  // As the default dev admin: load the supplier.
  page.once("dialog", (dialog) => dialog.accept());
  await page.locator("#vendor-upload").setInputFiles({
    name: "vendors.json",
    mimeType: "application/json",
    buffer: Buffer.from(
      JSON.stringify([{ name: vendor, tax_id: "29AAFCZ4821L1ZS", kind: "goods" }]),
    ),
  });
  const row = page.getByRole("row", { name: new RegExp(vendor) });
  await expect(row.getByText("unverified")).toBeVisible();

  // Alice, a clerk, proposes new bank details.
  await as(page, "alice@e2e.test:clerk");
  await page.reload();
  await row.getByRole("button", { name: "Request bank change" }).click();
  await page.locator("#bank-account-number").fill("123456789012");
  await page.locator("#bank-reason").fill("Confirmed by phone on the number we hold");
  await page.getByRole("button", { name: "Propose" }).click();
  await expect(page.getByText(/Bank change proposed/)).toBeVisible();
  await expect(row.getByText("pending")).toBeVisible();

  // Alice again, now with approval rights: she still cannot decide her own.
  await as(page, "alice@e2e.test:approver");
  await page.reload();
  await expect(page.getByRole("button", { name: "Approve" })).toBeDisabled();
  await expect(
    page.getByText("You proposed this change; another approver must decide it."),
  ).toBeVisible();

  // Bob approves, and the vendor's bank details become verified.
  await as(page, "bob@e2e.test:approver");
  await page.reload();
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "Approve" }).click();
  await expect(page.getByText(`Bank details for ${vendor} are now verified.`)).toBeVisible();
  await expect(row.getByText(/…9012/)).toBeVisible();
});

test("Xero reports whether it is configured", async ({ page }) => {
  await goto(page, "/settings");
  await expect(page.getByText("Export destinations")).toBeVisible();
  // Nothing sets XERO_ACCESS_TOKEN in this harness, so it must say so rather
  // than leave someone guessing why Export to Xero does nothing.
  await expect(page.getByText("not configured")).toBeVisible();
  await expect(page.getByText(/needs XERO_ACCESS_TOKEN/)).toBeVisible();
});

test("replacing purchase orders asks first, and cancelling changes nothing", async ({
  page,
}) => {
  await goto(page, "/purchase-orders");
  await expect(page.getByText(/No purchase orders loaded/)).toBeVisible();

  const orders = [
    { po_number: "PO-9001", vendor_name: "Smoke Supplies", total_amount: 1000 },
  ];

  // Playwright dismisses dialogs by default, which is the cancel path.
  let asked = "";
  page.once("dialog", (dialog) => {
    asked = dialog.message();
    dialog.dismiss();
  });
  await page.locator("#po-upload").setInputFiles({
    name: "orders.json",
    mimeType: "application/json",
    buffer: Buffer.from(JSON.stringify(orders)),
  });

  await expect.poll(() => asked).toContain("purchase orders");
  // Cancelled means nothing was sent: the empty state is still there.
  await expect(page.getByText(/No purchase orders loaded/)).toBeVisible();
});

test("accepting the replacement loads the orders", async ({ page }) => {
  await goto(page, "/purchase-orders");

  page.once("dialog", (dialog) => dialog.accept());
  await page.locator("#po-upload").setInputFiles({
    name: "orders.json",
    mimeType: "application/json",
    buffer: Buffer.from(
      JSON.stringify([
        {
          po_number: "PO-9001",
          vendor_name: "Smoke Supplies",
          total_amount: 1000,
        },
      ]),
    ),
  });

  await expect(page.getByText(/Loaded 1 purchase orders/)).toBeVisible();
  await expect(page.getByRole("cell", { name: "PO-9001" })).toBeVisible();
});

test("the sign-in page is a real route", async ({ page }) => {
  await page.goto("/login");
  await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
  // Reachable without a session -- the shell must not redirect away from the
  // page whose whole job is to end the lack of one.
  await expect(page.getByRole("link", { name: /create one/i })).toHaveAttribute(
    "href",
    "/signup",
  );
});

test("sign up asks for an account, not a request", async ({ page }) => {
  await page.goto("/signup");
  await expect(
    page.getByRole("heading", { name: "Create your account" }),
  ).toBeVisible();
  await expect(page.getByRole("link", { name: /sign in/i })).toHaveAttribute(
    "href",
    "/login",
  );
});

test("every validation check can be switched off from settings", async ({
  page,
}) => {
  await goto(page, "/settings");
  await expect(page.getByText("Validation checks")).toBeVisible();

  // All fourteen, grouped by phase, read from the rulebook rather than a
  // list kept in the UI.
  await expect(page.getByText("Phase 1 — Initial intake")).toBeVisible();
  await expect(page.getByText("3.1 Duplicate Detection")).toBeVisible();
  await expect(page.getByText("4.4 Tax Rate Verification")).toBeVisible();

  // Extraction cannot be switched off: with it gone the rest have nothing
  // to read. Addressed by its own row, not by position -- the page has other
  // checkboxes above these.
  await expect(
    page
      .locator("label", { hasText: "1.1 Extraction Success" })
      .getByRole("checkbox"),
  ).toBeDisabled();

  const duplicates = page
    .locator("label", { hasText: "3.1 Duplicate Detection" })
    .getByRole("checkbox");
  await expect(duplicates).toBeChecked();
  await duplicates.uncheck();

  await page.getByRole("button", { name: "Save settings" }).click();
  await expect(page.getByText(/Saved:/)).toBeVisible();

  await page.reload();
  await expect(
    page.locator("label", { hasText: "3.1 Duplicate Detection" }).getByRole("checkbox"),
  ).not.toBeChecked();
});

test("an approved invoice appears on the approved sheet", async ({ page }) => {
  // Approves one itself rather than relying on an earlier test: the sheet is
  // a database query, and this is the first e2e run that has a database.
  await goto(page, "/invoices?case=case_002");
  const vendor = await page.locator("#f_vendor_name").inputValue();
  await page.getByRole("button", { name: /^Approve/ }).click();
  const dialog = page.getByRole("dialog", { name: "Approve invoice" });
  await dialog.getByRole("button", { name: "Approve" }).click();
  await expect(page.getByText(/^Approved /)).toBeVisible();

  await goto(page, "/sheet");
  await expect(
    page.getByRole("heading", { name: "Approved invoices" }),
  ).toBeVisible();
  await expect(page.locator(".sheet-toolbar")).toBeVisible();
  await expect(page.getByRole("cell", { name: vendor, exact: true }).first()).toBeVisible();
});

// This test owns case_003 (cloned in scripts/e2e_api.py); nothing else decides it.
test("a two-step rule routes an invoice through alice then bob", async ({
  page,
}) => {
  const rule = "E2E two-step amount rule";
  await goto(page, "/admin/approvals");
  await page.getByLabel("Rule name").fill(rule);
  await page.getByLabel("Minimum amount").fill("1");
  await page.getByLabel("Step 1 value").fill("alice@e2e.test");
  await page.getByRole("button", { name: "Add step" }).click();
  await page.getByLabel("Step 2 value").fill("bob@e2e.test");
  await page.getByRole("button", { name: "Add rule" }).click();
  await expect(page.getByText(`Rule ${rule} created.`)).toBeVisible();

  // Nothing is routed until someone acts on a case, so alice starts the chain
  // from the workbench. The panel then shows the second step waiting on bob.
  await as(page, "alice@e2e.test:approver");
  await goto(page, "/invoices?case=case_003");
  await expect(page.locator("#f_vendor_name")).toBeVisible();
  const vendor = await page.locator("#f_vendor_name").inputValue();
  await page.getByRole("button", { name: /^Approve/ }).click();
  await page
    .getByRole("dialog", { name: "Approve invoice" })
    .getByRole("button", { name: "Approve" })
    .click();
  await expect(page.getByText(/Step 1 approved; waiting on user:bob@e2e.test/)).toBeVisible();

  // Alice cannot also sign step 2: she decided step 1. The workbench says so,
  // in the server's words, and disables the buttons rather than offering a
  // decision the server refuses.
  await expect(
    page.getByText("You decided an earlier step; each step needs a different person."),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: /^Approve/ })).toBeDisabled();
  await expect(page.getByRole("button", { name: /^Reject/ })).toBeDisabled();

  // Alice is away: bob hands his seat to pat, an approver, for today.
  await as(page, "bob@e2e.test:approver");
  await goto(page, "/approvals");
  await page.getByLabel("Delegate email").fill("pat@e2e.test");
  await page.getByRole("button", { name: "Add delegation" }).click();
  await expect(page.getByText(/pat@e2e.test will cover for you/)).toBeVisible();
  await as(page, "pat@e2e.test:approver");
  await goto(page, "/approvals");
  await expect(page.getByText("delegate for bob@e2e.test")).toBeVisible();

  // Bob approves from his phone-sized queue; the chain completes.
  await as(page, "bob@e2e.test:approver");
  await goto(page, "/approvals");
  await page.getByRole("button", { name: "Approve" }).click();
  await page
    .getByRole("dialog", { name: "Approve invoice" })
    .getByRole("button", { name: "Approve" })
    .click();
  await expect(page.getByText("Nothing is waiting for you.")).toBeVisible();

  // The sheet row records who signed last, not only that the invoice is there.
  await goto(page, "/sheet");
  const row = page
    .getByRole("row")
    .filter({ has: page.getByRole("cell", { name: vendor, exact: true }) });
  await expect(
    row.getByRole("cell", { name: "bob@e2e.test", exact: true }),
  ).toBeVisible();

  // The rule is tenant policy that outlives the test; remove it.
  await as(page, "dev@localhost:admin");
  await goto(page, "/admin/approvals");
  await page
    .getByText(rule)
    .locator("xpath=ancestor::div[contains(@class,'row-between')][1]")
    .getByRole("button", { name: "Delete" })
    .click();
  await expect(page.getByText(`Rule ${rule} deleted.`)).toBeVisible();
});

test("an admin can add a GL rule, see it listed, and delete it", async ({
  page,
}) => {
  const vendor = "E2E GL Rule Vendor";
  await goto(page, "/admin/gl-rules");
  await expect(page.getByRole("heading", { name: "GL rules" })).toBeVisible();

  await page.getByLabel("Vendor name").fill(vendor);
  await page.getByLabel("GL account", { exact: true }).fill("6100-E2E");
  await page.getByRole("button", { name: "Add rule" }).click();
  await expect(page.getByText("Rule for 6100-E2E created.")).toBeVisible();

  const row = page
    .locator(".row-between")
    .filter({ hasText: `vendor ${vendor}` });
  await expect(row.getByText("6100-E2E", { exact: true })).toBeVisible();

  // Tenant policy that outlives the test; remove it, which asks first.
  page.once("dialog", (dialog) => dialog.accept());
  await row.getByRole("button", { name: "Delete" }).click();
  await expect(page.getByText("Rule deleted.")).toBeVisible();
  await expect(row).toHaveCount(0);
});

// This test owns case_004_gl (cloned in scripts/e2e_api.py); nothing else
// decides it. No worker runs here, so rules never code it: manual coding only.
test("a clerk codes a line to a GL account, and a decision closes it", async ({
  page,
}) => {
  await as(page, "alice@e2e.test:clerk");
  await goto(page, "/invoices?case=case_004_gl");
  await expect(page.locator("#f_vendor_name")).toHaveValue("Ledger Test Supplies");

  const gl = page.getByLabel("GL account for line 1");
  await gl.fill("6200");
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.getByText("Line 1 coded to 6200")).toBeVisible();

  // Persisted, and attributed to a person rather than a rule.
  await page.reload();
  await expect(gl).toHaveValue("6200");
  await expect(gl.locator("xpath=..").getByText("manual", { exact: true })).toBeVisible();

  // A second person approves; the coding is then closed.
  await as(page, "dev@localhost:admin");
  await goto(page, "/invoices?case=case_004_gl");
  await page.getByRole("button", { name: /^Approve/ }).click();
  await page
    .getByRole("dialog", { name: "Approve invoice" })
    .getByRole("button", { name: "Approve" })
    .click();
  await expect(page.getByText(/^Approved /)).toBeVisible();

  // Approving advances the queue; come back to the decided invoice.
  await goto(page, "/invoices?case=case_004_gl");
  await expect(gl).toBeDisabled();
  await expect(gl).toHaveValue("6200");
});
