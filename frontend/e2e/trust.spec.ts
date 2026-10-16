import { expect, test, type Page } from "@playwright/test";

/** The Wave 2 trust surfaces: the queue, a finding's explanation, evidence,
 *  lineage and triage, and a Trust Audit engagement from creation to ledger.
 *  No worker runs in this stack, so a batch run is asserted queued, not done. */

async function goto(page: Page, path: string) {
  await page.goto(path);
  // Every view waits on /api/me before it renders anything.
  await expect(page.locator("nav[aria-label='Primary']")).toBeVisible();
}

// Who the local dev principal is, per request: the e2e API runs with auth
// disabled, and this header is how one browser plays two people.
function as(page: Page, who: string) {
  return page.setExtraHTTPHeaders({ "X-Dev-Principal": who });
}

test("the findings queue lists the seeded finding", async ({ page }) => {
  await goto(page, "/findings");
  await expect(page.getByRole("heading", { name: "Findings" })).toBeVisible();
  const row = page.getByRole("row", { name: /LEAK-RATE/ });
  await expect(row).toContainText("QuickFix HVAC Repairs");
  await expect(row).toContainText("₹1,250.00");
  await expect(row.getByText("Detected")).toBeVisible();

  // The hold-only view is the Payment Trust queue, and the seed is not a hold.
  // click, not check(): the box reads its state from the URL, which router.replace
  // updates a tick later, and check() verifies the state synchronously.
  await page.getByLabel("Payment Trust queue").click();
  await expect(page).toHaveURL(/hold=true/);
  await expect(page.locator(".empty-state")).toBeVisible();
});

test("a finding can be read, cited, and triaged", async ({ page }) => {
  await as(page, "alice@e2e.test:clerk"); // triage is a clerk's move
  await goto(page, "/findings");
  await page.getByRole("row", { name: /LEAK-RATE/ }).getByRole("link", { name: "LEAK-RATE" }).click();
  await expect(page.getByRole("heading", { name: /LEAK-RATE/ })).toBeVisible();
  await expect(page.getByText("Line 1 billed above the contract rate").first()).toBeVisible();

  // Why: what happened, the cited evidence, and what would be payable.
  await page.getByRole("tab", { name: "Why" }).click();
  await expect(page.getByText("What happened")).toBeVisible();
  await expect(page.getByText(/^Line 1 billed above the contract rate/).last()).toBeVisible();
  await expect(page.getByRole("list", { name: "Explanation evidence" }).getByRole("listitem")).not.toHaveCount(0);
  await expect(page.getByText("Invoice INV-00005")).toBeVisible();
  await expect(page.getByText("Payable if correct")).toBeVisible();
  await expect(page.locator("dt", { hasText: "Delta" }).locator("+ dd")).toHaveText("₹1,250.00");

  // A clerk rates the explanation once.
  await page.getByRole("group", { name: "Correct?" }).getByRole("button", { name: "✓ Yes" }).click();
  await page.getByRole("group", { name: "Useful?" }).getByRole("button", { name: "✓ Yes" }).click();
  await page.getByRole("button", { name: "Rate this explanation" }).click();
  await expect(page.getByText("your rating was recorded")).toBeVisible();

  await page.getByRole("tab", { name: "Evidence" }).click();
  await expect(page.locator(".citation-list li")).toHaveCount(1);
  await expect(page.locator("iframe.doc-frame")).toBeVisible();

  await page.getByRole("tab", { name: "Lineage" }).click();
  await expect(page.locator(".chain li")).toHaveCount(7);
  await expect(page.locator(".chain li.found")).toHaveCount(1);

  await page.getByRole("button", { name: "Triaged" }).first().click();
  await page.getByRole("dialog").getByRole("button", { name: "Triaged" }).click();
  await expect(page.locator("header .badge").filter({ hasText: "Triaged" })).toBeVisible();
  await page.getByRole("tab", { name: "History" }).click();
  await expect(page.getByRole("row", { name: /alice@e2e.test/ })).toContainText("Triaged");
});

test("an engagement runs from creation to sample, report and ledger", async ({ page }) => {
  const name = "E2E Trust Audit";
  await goto(page, "/trust-audit");
  await expect(page.getByRole("heading", { name: "Trust audit" })).toBeVisible();
  await expect(page.getByText("No engagements yet.")).toBeVisible();

  // Vendors and categories left empty: the engagement covers everything in
  // the period, which includes the seeded invoice (dated 2026-02-11).
  await page.getByLabel("Name").fill(name);
  await page.getByLabel("Period from").fill("2026-01-01");
  await page.getByLabel("Period to").fill("2026-12-31");
  await expect(page.getByLabel("Recovered %")).toHaveValue("25");
  await expect(page.getByLabel("Prevented %")).toHaveValue("10");
  await page.getByRole("button", { name: "Create engagement" }).click();
  await expect(page.getByRole("heading", { name })).toBeVisible();

  // It is listed, and opens from the list.
  await goto(page, "/trust-audit");
  await page.getByRole("link", { name }).click();
  await expect(page.getByRole("heading", { name })).toBeVisible();

  await page.getByRole("button", { name: "Run batch" }).click();
  await expect(page.locator(".badge", { hasText: /^queued$/ })).toBeVisible();
  await expect(page.getByRole("button", { name: "Run batch" })).toBeDisabled();

  await page.getByRole("tab", { name: "Sampling" }).click();
  await page.getByLabel("Sample size").fill("1");
  await page.getByRole("button", { name: "Draw sample" }).click();
  await page.getByRole("button", { name: /^Mark .* true$/ }).click();
  await expect(page.locator(".tiles .panel", { hasText: "Precision" })).toContainText("100%");

  await page.getByRole("tab", { name: "Report" }).click();
  await expect(page.getByRole("heading", { name: "Executive summary" })).toBeVisible();
  await expect(page.getByRole("cell", { name: "LEAK-RATE · Leakage" })).toBeVisible();

  await page.getByRole("tab", { name: "Ledger" }).click();
  await expect(page.getByRole("button", { name: "Download CSV" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Download JSON" })).toBeVisible();
});
