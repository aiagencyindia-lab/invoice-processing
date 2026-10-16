import { expect, test, type Page } from "@playwright/test";

/** The Wave 2 trust surfaces over the scaffold's routes: the queue, a finding's
 *  evidence and lineage, a triage, and the Trust Audit form. The Why engine and
 *  the audit routes land on sibling branches, so their panels are asserted to
 *  degrade, not to work. */

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
  await page.getByLabel("Payment Trust queue").check();
  await expect(page).toHaveURL(/hold=true/);
  await expect(page.locator(".empty-state")).toBeVisible();
});

test("a finding can be read, cited, and triaged", async ({ page }) => {
  await as(page, "alice@e2e.test:clerk"); // triage is a clerk's move
  await goto(page, "/findings");
  await page.getByRole("row", { name: /LEAK-RATE/ }).getByRole("link", { name: "LEAK-RATE" }).click();
  await expect(page.getByRole("heading", { name: /LEAK-RATE/ })).toBeVisible();
  await expect(page.getByText("Line 1 billed above the contract rate").first()).toBeVisible();

  // The Why engine is not on this branch: its panel says so instead of breaking.
  await page.getByRole("tab", { name: "Why" }).click();
  await expect(page.locator(".empty-state")).toContainText("Not Found");

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

test("the trust audit page offers the engagement form", async ({ page }) => {
  await goto(page, "/trust-audit");
  await expect(page.getByRole("heading", { name: "Trust audit" })).toBeVisible();
  // No audit routes on this branch: the list degrades, the form still renders.
  await expect(page.locator(".empty-state")).toContainText("Not Found");
  await expect(page.getByLabel("Name")).toBeVisible();
  await expect(page.getByLabel("Recovered %")).toHaveValue("25");
  await expect(page.getByLabel("Prevented %")).toHaveValue("10");
  await expect(page.getByLabel("Period from")).toHaveAttribute("type", "date");
});
