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

  await page.getByRole("button", { name: /^Approve/ }).click();
  await expect(page.getByText(/^Approved /)).toBeVisible();

  // Filtered: headless Chromium treats the document pane's PDF as a download
  // too, so an unqualified wait catches whichever fires first.
  const download = page.waitForEvent("download", {
    predicate: (d) => d.suggestedFilename().endsWith(".csv"),
  });
  await page.getByRole("button", { name: /^Export/ }).click();
  expect((await download).suggestedFilename()).toMatch(/\.csv$/);
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
    "Documents",
    "Activity",
    "Purchase orders",
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
