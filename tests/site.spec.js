// @ts-check
const { test, expect } = require("@playwright/test");
const { default: AxeBuilder } = require("@axe-core/playwright");

const WCAG_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"];

async function ready(page, url = "/") {
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  // In static mode the live-seat API is absent, so its 404 is expected; any other 404 is a bug.
  page.on("console", (m) => { if (m.type() === "error" && !/Failed to load resource.*404/.test(m.text())) errors.push(m.text()); });
  page.on("response", (r) => { if (r.status() === 404 && !new URL(r.url()).pathname.endsWith("/api/health")) errors.push(`404 ${r.url()}`); });
  await page.goto(url);
  await expect(page.locator("#status")).toContainText(/match/);
  return errors;
}

async function axe(page) {
  const { violations } = await new AxeBuilder({ page }).withTags(WCAG_TAGS).analyze();
  const summary = violations.map((v) => `${v.id} (${v.impact}): ${v.help} — ${v.nodes.length} node(s)\n    ${v.nodes[0].target}`);
  expect(summary, summary.join("\n")).toEqual([]);
}

test("loads data without script errors", async ({ page }) => {
  const errors = await ready(page);
  await expect(page.locator(".course").first()).toBeVisible();
  expect(errors).toEqual([]);
});

test("axe: default view (light)", async ({ page }) => {
  await ready(page);
  await page.locator(".course details").first().evaluate((d) => { d.open = true; });
  await page.locator("#degree-box").evaluate((d) => { d.open = true; });
  await axe(page);
});

test("axe: dark mode", async ({ page }) => {
  await page.emulateMedia({ colorScheme: "dark" });
  await ready(page);
  await axe(page);
});

const bg = (page) => page.evaluate(() => getComputedStyle(document.body).backgroundColor);
const LIGHT_BG = "rgb(255, 255, 255)";
const DARK_BG = "rgb(20, 20, 23)";

test("theme: follows the system setting by default", async ({ page }) => {
  await page.emulateMedia({ colorScheme: "dark" });
  await ready(page);
  await expect(page.locator("#theme-picker")).toHaveValue("system");
  expect(await bg(page)).toBe(DARK_BG);
  await page.emulateMedia({ colorScheme: "light" });
  expect(await bg(page)).toBe(LIGHT_BG);
});

test("theme: picker overrides the system setting and is remembered", async ({ page }) => {
  await page.emulateMedia({ colorScheme: "light" });
  await ready(page);
  await page.selectOption("#theme-picker", "dark");
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  expect(await bg(page)).toBe(DARK_BG);
  expect(await page.evaluate(() => getComputedStyle(document.documentElement).colorScheme)).toBe("dark");

  await page.reload();
  await expect(page.locator("#theme-picker")).toHaveValue("dark");
  expect(await bg(page)).toBe(DARK_BG);

  await page.emulateMedia({ colorScheme: "dark" });
  await page.selectOption("#theme-picker", "light");
  expect(await bg(page)).toBe(LIGHT_BG);

  await page.selectOption("#theme-picker", "system");
  await expect(page.locator("html")).not.toHaveAttribute("data-theme", /.*/);
  expect(await page.evaluate(() => localStorage.getItem("unlv:theme"))).toBeNull();
  expect(await bg(page)).toBe(DARK_BG);
});

const scheme = (page) => page.evaluate(() => getComputedStyle(document.documentElement).colorScheme);

test("theme: saved choice applies on the first frame", async ({ page }) => {
  await page.addInitScript(() => {
    localStorage.setItem("unlv:theme", "dark");
    // Snapshot the very first frame; a deferred or late theme.js would miss it.
    requestAnimationFrame(() => {
      const root = document.documentElement;
      window.__first = { theme: root.getAttribute("data-theme"), bg: getComputedStyle(document.body).backgroundColor };
    });
  });
  await page.emulateMedia({ colorScheme: "light" });
  await page.goto("/");
  await page.waitForFunction(() => window.__first);
  expect(await page.evaluate(() => window.__first)).toEqual({ theme: "dark", bg: DARK_BG });
});

test("theme: color-scheme matches the theme, so native controls match", async ({ page }) => {
  await page.emulateMedia({ colorScheme: "dark" });
  await ready(page);
  expect(await scheme(page)).toBe("dark");
  await page.selectOption("#theme-picker", "light");
  expect(await scheme(page)).toBe("light");
  await page.emulateMedia({ colorScheme: "light" });
  await page.selectOption("#theme-picker", "system");
  expect(await scheme(page)).toBe("light");
});

test("theme: survives Clear all filters and stays out of the URL", async ({ page }) => {
  await ready(page);
  await page.selectOption("#theme-picker", "dark");
  await page.click("#reset");
  await expect(page.locator("#theme-picker")).toHaveValue("dark");
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  expect(page.url()).not.toContain("theme");
});

test("theme: works when storage is blocked", async ({ page }) => {
  await page.addInitScript(() => {
    Object.defineProperty(window, "localStorage", { get() { throw new DOMException("blocked", "SecurityError"); } });
  });
  const errors = await ready(page);
  await page.selectOption("#theme-picker", "dark");
  expect(await bg(page)).toBe(DARK_BG);
  expect(errors).toEqual([]);
});

test("theme: ignores junk in storage", async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem("unlv:theme", "neon"));
  await page.emulateMedia({ colorScheme: "light" });
  await ready(page);
  await expect(page.locator("html")).not.toHaveAttribute("data-theme", /.*/);
  await expect(page.locator("#theme-picker")).toHaveValue("system");
});

test("axe: dark picked while the system is light", async ({ page }) => {
  await page.emulateMedia({ colorScheme: "light" });
  await ready(page);
  await page.selectOption("#theme-picker", "dark");
  await page.locator(".course details").first().evaluate((d) => { d.open = true; });
  await axe(page);
});

test("axe: light picked while the system is dark", async ({ page }) => {
  await page.emulateMedia({ colorScheme: "dark" });
  await ready(page);
  await page.selectOption("#theme-picker", "light");
  await page.locator(".course details").first().evaluate((d) => { d.open = true; });
  await axe(page);
});

test("axe: no results state", async ({ page }) => {
  await ready(page);
  await page.fill("#q", "zzzznotacourse");
  await expect(page.locator("#status")).toContainText("No classes match");
  await axe(page);
});

test("reflow at 320px: page itself does not scroll sideways", async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 800 });
  await ready(page);
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBeLessThanOrEqual(0);
});

test("search 'cs 135' matches the course code, not words containing 'cs'", async ({ page }) => {
  await ready(page);
  await page.fill("#q", "cs 135");
  await expect(page.locator("#status")).toContainText(/^1 course/);
  await expect(page.locator(".course h3").first()).toContainText("CS 135");
  await page.fill("#q", "cs135");
  await expect(page.locator(".course h3").first()).toContainText("CS 135");
});

test("search by class number finds the one section", async ({ page }) => {
  await ready(page);
  await page.fill("#q", "22031");
  await expect(page.locator("#status")).toContainText("1 course (1 section)");
});

test("open-only filter hides closed and wait-listed sections", async ({ page }) => {
  await ready(page);
  await page.check("#open");
  await expect(page).toHaveURL(/open=1/);
  await expect(page.locator(".st-closed, .st-wait")).toHaveCount(0);
});

test("day filter only keeps classes meeting on those days", async ({ page }) => {
  await ready(page);
  await page.check("#d-Tu");
  await page.check("#d-Th");
  await expect(page).toHaveURL(/days=Tu%2CTh|days=Tu,Th/);
  const days = await page.locator("tbody abbr").allTextContents();
  expect(days.length).toBeGreaterThan(0);
  for (const d of days) expect(d.split(" ").every((x) => ["Tue", "Thu"].includes(x))).toBe(true);
});

test("filters persist in the URL", async ({ page }) => {
  await ready(page);
  await page.selectOption("#subject", "MATH");
  await page.check("#open");
  await expect(page).toHaveURL(/subject=MATH/);
  await page.reload();
  await expect(page.locator("#status")).toContainText(/match/);
  await expect(page.locator("#subject")).toHaveValue("MATH");
  await expect(page.locator("#open")).toBeChecked();
});

test("degree plan tags and filters courses, and survives reload", async ({ page }) => {
  await ready(page);
  await page.locator("#degree-box summary").click();
  await page.fill("#degree", "Required: CS 135, CS 202 and MATH 181");
  await expect(page.locator("#degree-count")).toHaveText("3 course codes found.");
  await page.check("#degreeOnly");
  await expect(page.locator("#status")).toContainText(/^3 courses/);
  await expect(page.locator(".degree-tag")).toHaveCount(3);
  await page.reload();
  await expect(page.locator("#degree")).toHaveValue(/CS 135/);
});

test("keyboard: skip link goes to results; Show more moves focus", async ({ page }) => {
  await ready(page);
  await page.keyboard.press("Tab");
  await expect(page.locator(".skip-link")).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.locator("#results-heading")).toBeFocused();

  const more = page.locator("#more");
  await expect(more).toBeVisible();
  await more.focus();
  await page.keyboard.press("Enter");
  await expect(page.locator(".course").nth(25).locator("h3")).toBeFocused();
});

test("clear all filters keeps the selected term", async ({ page }) => {
  await ready(page);
  const term = await page.locator("#term").inputValue();
  await page.fill("#q", "calculus");
  await page.click("#reset");
  await expect(page.locator("#q")).toHaveValue("");
  await expect(page.locator("#term")).toHaveValue(term);
});
