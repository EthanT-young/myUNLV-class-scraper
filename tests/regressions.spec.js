// @ts-check
// Regression tests for issues found in the adversarial review.
const { test, expect } = require("@playwright/test");

const meeting = (o = {}) => ({ days: ["Mo", "We"], start: "13:00", end: "14:15", raw: "MoWe 1:00PM - 2:15PM",
  room: "TBA", startDate: "2027-01-19", endDate: "2027-05-08", ...o });
const section = (nbr, o = {}) => ({ classNbr: nbr, section: "1001", component: "LEC", session: "Regular",
  status: "Open", instructors: ["Staff"], meetings: [meeting()], ...o });
const course = (subject, catalog, o = {}) => ({ id: `${subject} ${catalog}`, subject, catalog,
  title: `${subject} ${catalog} title`, subjectName: subject, unitsMin: 3, unitsMax: 3, career: "Undergraduate",
  attributes: [], sections: [section(`${subject}${catalog}`.replace(/\D/g, "") || "1")], ...o });
const termData = (term, name, courses) => ({ term, name, updated: "2026-11-07T00:00:00+00:00",
  detailLevel: "course", subjects: Object.fromEntries(courses.map((c) => [c.subject, c.subject])), courses });

const TERMS = { terms: [{ term: "2268", name: "2026 Fall" }, { term: "2272", name: "2027 Spring" }] };
const FALL = termData("2268", "2026 Fall", [course("MATH", "181", { sections: [section("111")] }), course("ART", "100", { sections: [section("112")] })]);
const SPRING = termData("2272", "2027 Spring", [course("MATH", "182", { sections: [section("221")] }), course("CS", "135", { sections: [section("222")] })]);

/** Serve fake data. `routes` maps file name -> body object | number (HTTP status) | {delay, body}. */
async function serve(page, routes) {
  await page.route("**/data/*.json", async (route) => {
    const name = new URL(route.request().url()).pathname.split("/").pop();
    let r = routes[name];
    if (r && r.delay) { await new Promise((ok) => setTimeout(ok, r.delay)); r = r.body; }
    if (typeof r === "number") return route.fulfill({ status: r, body: "" });
    if (r === undefined) return route.fulfill({ status: 404, body: "" });
    return route.fulfill({ contentType: "application/json", body: JSON.stringify(r) });
  });
}

function trackErrors(page) {
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  page.on("console", (m) => { if (m.type() === "error" && !/Failed to load resource.*404/.test(m.text())) errors.push(m.text()); });
  page.on("response", (r) => { if (r.status() === 404 && !new URL(r.url()).pathname.endsWith("/api/health")) errors.push(`404 ${r.url()}`); });
  return errors;
}

test("recovers when the default term fails to load", async ({ page }) => {
  await serve(page, { "terms.json": TERMS, "2268.json": FALL, "2272.json": 404 });
  await page.goto("/");
  await expect(page.locator("#status")).toContainText("could not be loaded");
  await page.selectOption("#term", "2268");
  await expect(page.locator("#status")).toContainText("2 courses");
  await expect(page.locator(".course h3").first()).toContainText("ART 100");
});

test("a slow earlier term load can't replace a later choice", async ({ page }) => {
  await serve(page, { "terms.json": TERMS, "2268.json": { delay: 1500, body: FALL }, "2272.json": SPRING });
  await page.goto("/");
  await expect(page.locator("#status")).toContainText("match");
  await page.selectOption("#term", "2268");
  await page.waitForTimeout(100);
  await page.selectOption("#term", "2272");
  await page.waitForTimeout(2000);
  await expect(page).toHaveTitle(/2027 Spring/);
  await expect(page.locator(".course h3").first()).toContainText(/CS 135|MATH 182/);
});

test("a failed term switch doesn't show the old term's classes", async ({ page }) => {
  await serve(page, { "terms.json": TERMS, "2268.json": 500, "2272.json": SPRING });
  await page.goto("/");
  await expect(page.locator("#status")).toContainText("match");
  await page.selectOption("#term", "2268");
  await expect(page.locator("#status")).toContainText("could not be loaded");
  await page.check("#open");
  await page.waitForTimeout(500);
  await expect(page.locator(".course")).toHaveCount(0);
  await expect(page.locator("#status")).toContainText("could not be loaded");
});

test("subject filter survives a term switch when the subject exists", async ({ page }) => {
  await serve(page, { "terms.json": TERMS, "2268.json": FALL, "2272.json": SPRING });
  await page.goto("/?subject=MATH");
  await expect(page.locator("#subject")).toHaveValue("MATH");
  await page.selectOption("#term", "2268");
  await expect(page.locator("#status")).toContainText("1 course");
  await expect(page.locator("#subject")).toHaveValue("MATH");
});

test("sections with an unlisted format aren't called in person", async ({ page }) => {
  const data = termData("2272", "2027 Spring", [
    course("CS", "326", { sections: [section("1", { meetings: [meeting({ room: "TBA" })] })] }),
    course("CS", "135", { sections: [section("2", { meetings: [meeting({ room: "Web-based", start: null, end: null, days: [], raw: "TBA" })] })] }),
    course("CS", "489", { sections: [section("3", { meetings: [meeting({ room: "TBA" }), meeting({ room: "and Online" })] })] }),
  ]);
  await serve(page, { "terms.json": { terms: [{ term: "2272", name: "2027 Spring" }] }, "2272.json": data });
  await page.goto("/?q=cs+326");
  await expect(page.locator("tbody")).not.toContainText("In person");
  await page.goto("/?mode=inperson");
  await expect(page.locator("#status")).toContainText("No classes match");
  await page.goto("/?mode=online");
  await expect(page.locator("#status")).toContainText("1 course");
  await expect(page.locator(".course h3")).toContainText("CS 135");
});

test("imperfect records don't crash the page", async ({ page }) => {
  const errors = trackErrors(page);
  const data = termData("2272", "2027 Spring", [
    course("CS", "100", { unitsMax: null }),
    course("CS", "101", { title: null }),
    course("CS", "102", { sections: [section("9", { meetings: [meeting({ endDate: null })], instructors: null, component: "constructor" })] }),
    course("CS", "103", { sections: [section("10", { meetings: [meeting({ days: ["__proto__", "toString", "Tu"] })] })] }),
    { subject: "CS" },
  ]);
  await serve(page, { "terms.json": { terms: [{ term: "2272", name: "2027 Spring" }] }, "2272.json": data });
  await page.goto("/?sort=title");
  await expect(page.locator("#status")).toContainText("match");
  const body = await page.locator("#results").innerText();
  expect(body).not.toContain("Invalid Date");
  expect(body).not.toContain("native code");
  expect(body).not.toContain("[object Object]");
  expect(body).toContain("Starts Jan 19, 2027");
  expect(errors).toEqual([]);
});

test("course number sort is numeric", async ({ page }) => {
  const data = termData("2272", "2027 Spring", ["488", "120E", "20B", "181", "24B"].map((n, i) =>
    course("MATH", n, { sections: [section(String(i + 1))] })));
  await serve(page, { "terms.json": { terms: [{ term: "2272", name: "2027 Spring" }] }, "2272.json": data });
  await page.goto("/");
  await expect(page.locator("#status")).toContainText("5 courses");
  const codes = await page.locator(".course h3 .code").allTextContents();
  expect(codes).toEqual(["MATH 20B", "MATH 24B", "MATH 120E", "MATH 181", "MATH 488"]);
});

test("status text isn't rewritten when the count doesn't change", async ({ page }) => {
  await serve(page, { "terms.json": TERMS, "2268.json": FALL, "2272.json": SPRING });
  await page.goto("/");
  await expect(page.locator("#status")).toContainText("match");
  await page.evaluate(() => {
    window.__mutations = 0;
    new MutationObserver((m) => { window.__mutations += m.length; })
      .observe(document.getElementById("status"), { childList: true, characterData: true, subtree: true });
  });
  await page.selectOption("#sort", "title");
  await page.waitForTimeout(500);
  await page.selectOption("#sort", "code");
  await page.waitForTimeout(500);
  expect(await page.evaluate(() => window.__mutations)).toBe(0);
});

test("shared degreeOnly link shows the filter and explains an empty plan", async ({ page }) => {
  await serve(page, { "terms.json": TERMS, "2268.json": FALL, "2272.json": SPRING });
  await page.goto("/?degreeOnly=1");
  await expect(page.locator("#degreeOnly")).toBeVisible();
  await expect(page.locator("#status")).toContainText("degree plan is empty");
});

test("degree plan understands shorthand lists", async ({ page }) => {
  await serve(page, { "terms.json": TERMS, "2268.json": FALL, "2272.json": SPRING });
  await page.goto("/");
  await page.locator("#degree-box summary").click();
  await page.fill("#degree", "CS 135, 202, 218 and MATH 181/182");
  await expect(page.locator("#degree-count")).toHaveText("5 course codes found.");
});

test("day hint is announced with the day checkboxes", async ({ page }) => {
  await serve(page, { "terms.json": TERMS, "2268.json": FALL, "2272.json": SPRING });
  await page.goto("/");
  const described = await page.locator("fieldset:has(#d-Mo)").getAttribute("aria-describedby");
  expect(described).toBe("days-hint");
});

test("tables that don't scroll aren't extra tab stops or landmarks", async ({ page }) => {
  await serve(page, { "terms.json": TERMS, "2268.json": FALL, "2272.json": SPRING });
  await page.setViewportSize({ width: 1280, height: 800 });
  await page.goto("/");
  await expect(page.locator("#status")).toContainText("match");
  await expect(page.locator(".table-scroll[tabindex]")).toHaveCount(0);
  await expect(page.locator(".table-scroll[role=region]")).toHaveCount(0);
});

test("long unbroken text doesn't cause sideways scrolling at 320px", async ({ page }) => {
  const url = "https://catalog.unlv.edu/" + "x".repeat(90);
  const data = termData("2272", "2027 Spring", [course("CS", "135", { requirements: `See ${url}` })]);
  await serve(page, { "terms.json": { terms: [{ term: "2272", name: "2027 Spring" }] }, "2272.json": data });
  await page.setViewportSize({ width: 320, height: 800 });
  await page.goto("/");
  await page.locator(".course details").evaluate((d) => { d.open = true; });
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBeLessThanOrEqual(0);
});

test("malicious data is rendered as text", async ({ page }) => {
  const xss = '<img src=x onerror="window.__pwned=1"><svg onload="window.__pwned=1">';
  const data = termData("2272", xss, [course("CS", "135", { title: xss, description: xss, attributes: [xss],
    sections: [section("1", { instructors: [xss], status: xss, meetings: [meeting({ room: xss, raw: xss })] })] })]);
  data.subjects = { CS: xss };
  await serve(page, { "terms.json": { terms: [{ term: "2272", name: xss }] }, "2272.json": data });
  await page.goto("/?q=" + encodeURIComponent(xss));
  await page.goto("/");
  await expect(page.locator("#status")).toContainText("match");
  await page.locator(".course details").evaluate((d) => { d.open = true; });
  expect(await page.evaluate(() => window.__pwned)).toBeUndefined();
  await expect(page.locator("#results img, #results svg")).toHaveCount(0);
});
