// @ts-check
// Live seat counts, with the server API faked through page.route().
const { test, expect } = require("@playwright/test");
const { default: AxeBuilder } = require("@axe-core/playwright");

async function fakeApi(page, { seats = {}, status = 200 } = {}) {
  const calls = [];
  await page.route("**/api/health", (r) => r.fulfill({ contentType: "application/json",
    body: JSON.stringify({ ok: true, terms: ["2272"] }) }));
  await page.route("**/api/seats*", (r) => {
    const url = new URL(r.request().url());
    const asked = (url.searchParams.get("classes") || "").split(",");
    calls.push(asked);
    if (status !== 200) return r.fulfill({ status, body: "" });
    const now = Date.now() / 1000;
    const body = { term: "2272", now, ttl: 120,
      seats: Object.fromEntries(asked.filter((n) => seats[n]).map((n) => [n, { checked: now - 90, ...seats[n] }])) };
    return r.fulfill({ contentType: "application/json", body: JSON.stringify(body) });
  });
  return calls;
}

const CS135 = {
  22031: { status: "Open", capacity: 25, enrolled: 5, available: 20, waitCapacity: 10, waitTotal: 0 },
  22032: { status: "Closed", capacity: 25, enrolled: 25, available: 0, waitCapacity: 10, waitTotal: 4 },
};

test("shows live seat counts for sections on screen", async ({ page }) => {
  const calls = await fakeApi(page, { seats: CS135 });
  await page.goto("/?q=cs+135");
  const row = page.locator('tr[data-class="22031"]');
  await expect(row.locator(".seats-cell")).toContainText("20 of 25 open");
  await expect(row.locator(".seats-cell")).toContainText("2 min ago");
  const closed = page.locator('tr[data-class="22032"]');
  await expect(closed.locator(".seats-cell")).toContainText("Wait list: 4 of 10");
  // The live status replaces the scraped one.
  await expect(closed.locator(".status-cell")).toContainText("Closed");
  expect(calls[0]).toContain("22031");
  expect(calls[0].length).toBeLessThanOrEqual(80);
});

test("turning live counts off stops requests and is remembered", async ({ page }) => {
  const calls = await fakeApi(page, { seats: CS135 });
  await page.goto("/?q=cs+135");
  await expect(page.locator('tr[data-class="22031"] .seats-cell')).toContainText("20 of 25");
  await page.uncheck("#live");
  await expect(page.locator('tr[data-class="22031"] .seats-cell')).toContainText("as of");
  const before = calls.length;
  await page.fill("#q", "math 181");
  await page.waitForTimeout(3000);
  expect(calls.length).toBe(before);
  await page.reload();
  await expect(page.locator("#live")).not.toBeChecked();
  await expect(page.locator(".seats-cell").first()).toContainText("Live counts are off");
});

test("rate limiting and outages degrade gracefully", async ({ page }) => {
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await fakeApi(page, { status: 429 });
  await page.goto("/?q=cs+135");
  await expect(page.locator(".seats-cell").first()).toContainText("Checking");
  expect(errors).toEqual([]);
});

test("no seat column when the API isn't there", async ({ page }) => {
  await page.goto("/?q=cs+135");
  await expect(page.locator("#status")).toContainText("match");
  await expect(page.locator(".seats-cell")).toHaveCount(0);
  await expect(page.locator("#live-box")).toBeHidden();
});

test("axe: live seat column and toggle", async ({ page }) => {
  await fakeApi(page, { seats: CS135 });
  await page.goto("/?q=cs+135");
  await expect(page.locator('tr[data-class="22031"] .seats-cell')).toContainText("20 of 25");
  const { violations } = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]).analyze();
  expect(violations.map((v) => `${v.id}: ${v.help}`)).toEqual([]);
});
