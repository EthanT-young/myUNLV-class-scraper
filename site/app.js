"use strict";

const PAGE_SIZE = 25;
const DAY_NAMES = { Mo: "Mon", Tu: "Tue", We: "Wed", Th: "Thu", Fr: "Fri", Sa: "Sat", Su: "Sun" };
const DAY_LONG = { Mo: "Monday", Tu: "Tuesday", We: "Wednesday", Th: "Thursday", Fr: "Friday", Sa: "Saturday", Su: "Sunday" };
const COMPONENTS = {
  LEC: "Lecture", LAB: "Lab", DIS: "Discussion", SEM: "Seminar", IND: "Independent study",
  CLN: "Clinical", FLD: "Field studies", INT: "Internship", PRA: "Practicum", RSC: "Research",
  SUP: "Supervision", THE: "Thesis research",
};

const own = (obj, k) => (Object.hasOwn(obj, k) ? obj[k] : undefined);
const $ = (id) => document.getElementById(id);
const form = $("filters");
const els = {
  term: $("term"), q: $("q"), subject: $("subject"), career: $("career"), attr: $("attr"),
  open: $("open"), after: $("after"), before: $("before"), mode: $("mode"), units: $("units"),
  sort: $("sort"), live: $("live"), degree: $("degree"), degreeOnly: $("degreeOnly"), degreeCount: $("degree-count"),
  status: $("status"), results: $("results"), more: $("more"), updated: $("updated"),
};
const dayBoxes = [...form.querySelectorAll('input[name="days"]')];

let data = null;          // current term's JSON
let matches = [];         // [{course, sections}]
let shown = 0;
let degreeSet = new Set();
let loadToken = 0;        // guards against out-of-order term loads

// Live seat counts from the server's /api/seats (absent when the site is served statically).
const live = {
  available: false,       // server API reachable and has this term
  terms: [],              // terms the API knows
  enabled: true,          // user preference (WCAG 2.2.2: auto-updating content can be turned off)
  seats: new Map(),       // classNbr -> info from the API
  visible: new Set(),     // class numbers of rows currently on screen
  offset: 0,              // server clock minus browser clock, seconds
  observer: null,
  timer: null,
  lastPoll: 0,
  pending: null,
  failures: 0,
  quickRetries: 0,
};

/* ---------- small helpers ---------- */

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c == null || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* storage unavailable */ } },
};

function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

function fmtTime(hhmm) {
  if (!hhmm) return "";
  let [h, m] = hhmm.split(":").map(Number);
  const ap = h >= 12 ? "pm" : "am";
  h = h % 12 || 12;
  return m ? `${h}:${String(m).padStart(2, "0")} ${ap}` : `${h} ${ap}`;
}

function fmtDate(iso, withYear) {
  if (!iso) return "";
  const d = new Date(iso + "T12:00:00");
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric", ...(withYear ? { year: "numeric" } : {}) });
}

function weeksBetween(a, b) {
  if (!a || !b) return null;
  const days = (new Date(b) - new Date(a)) / 86400000 + 1;
  return Math.max(1, Math.round(days / 7));
}

function fmtUnits(c) {
  if (c.unitsMin == null) return null;
  const n = (x) => (Number.isInteger(x) ? x : x.toFixed(1));
  const max = c.unitsMax ?? c.unitsMin;
  const r = c.unitsMin === max ? n(c.unitsMin) : `${n(c.unitsMin)}–${n(max)}`;
  return `${r} credit${max === 1 ? "" : "s"}`;
}

// Lower-cased text with a leading space before every word, so a search word
// can be matched against word starts with hay.includes(" " + word).
function wordsHay(parts) {
  return " " + parts.join(" ").toLowerCase().replace(/[^a-z0-9]+/g, " ") + " ";
}

function normalizeCode(s) { return s.toUpperCase().replace(/\s+/g, ""); }

// Finds course codes in free text. A bare number right after a code inherits
// its subject, so "CS 135, 202, 218" and "MATH 181/182" each give every course.
function parseDegreeCodes(text) {
  const set = new Set();
  const re = /\b([A-Za-z]{2,5})\s?-?\s?(\d{2,3}[A-Za-z]{0,2})\b((?:\s*(?:,|\/|&|\band\b|\bor\b)\s*\d{3}[A-Za-z]{0,2}\b)*)/g;
  let m;
  while ((m = re.exec(text))) {
    set.add(normalizeCode(m[1] + m[2]));
    for (const n of (m[3] || "").match(/\d{3}[A-Za-z]{0,2}/g) || []) set.add(normalizeCode(m[1] + n));
  }
  return set;
}

// "online", "inperson", or "unknown". Without the class detail page the
// format is often not published, so it is never guessed as in person.
function sectionFormat(s) {
  const mode = s.instructionMode || "";
  if (mode) {
    if (/in.?person|hybrid|on\/off campus/i.test(mode)) return "inperson";
    if (/web|online/i.test(mode)) return "online";
    return "unknown";
  }
  const rooms = s.meetings.map((m) => m.room || "");
  if (rooms.length && rooms.every((r) => /web|online/i.test(r))) return "online";
  if (rooms.some((r) => r && !/^(tba|web|online)/i.test(r) && !/web|online/i.test(r))) return "inperson";
  return "unknown";
}

const FORMAT_LABELS = { online: "Online", inperson: "In person", unknown: "Format not listed" };

function statusInfo(status) {
  if (/open/i.test(status)) return { cls: "st-open", label: "Open" };
  if (/wait/i.test(status)) return { cls: "st-wait", label: "Wait list" };
  if (/closed/i.test(status)) return { cls: "st-closed", label: "Closed" };
  return { cls: "", label: status || "Unknown" };
}

/* ---------- state <-> URL ---------- */

function readState() {
  return {
    term: els.term.value,
    q: els.q.value.trim(),
    subject: els.subject.value,
    career: els.career.value,
    attr: els.attr.value,
    open: els.open.checked,
    days: dayBoxes.filter((b) => b.checked).map((b) => b.value),
    after: els.after.value,
    before: els.before.value,
    mode: els.mode.value,
    units: els.units.value,
    sort: els.sort.value,
    degreeOnly: els.degreeOnly.checked,
  };
}

function writeUrl(st) {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(st)) {
    if (k === "days") { if (v.length) p.set("days", v.join(",")); continue; }
    if (v === true) p.set(k, "1");
    else if (v && !(k === "sort" && v === "code")) p.set(k, v);
  }
  const qs = p.toString();
  history.replaceState(null, "", qs ? `?${qs}` : location.pathname);
}

function applyUrl() {
  const p = new URLSearchParams(location.search);
  const setSel = (sel, v) => { if (v && [...sel.options].some((o) => o.value === v)) sel.value = v; };
  els.q.value = p.get("q") || "";
  setSel(els.subject, p.get("subject"));
  setSel(els.career, p.get("career"));
  setSel(els.attr, p.get("attr"));
  setSel(els.after, p.get("after"));
  setSel(els.before, p.get("before"));
  setSel(els.mode, p.get("mode"));
  setSel(els.units, p.get("units"));
  setSel(els.sort, p.get("sort"));
  els.open.checked = p.get("open") === "1";
  els.degreeOnly.checked = p.get("degreeOnly") === "1";
  if (els.degreeOnly.checked) $("degree-box").open = true;  // never hide an active filter
  const days = (p.get("days") || "").split(",");
  dayBoxes.forEach((b) => { b.checked = days.includes(b.value); });
}

/* ---------- filtering ---------- */

function sectionMatches(s, st, qTokens, courseHit, courseHay) {
  if (st.open && !/open/i.test(s.status)) return false;
  if (st.mode && sectionFormat(s) !== st.mode) return false;
  const timed = s.meetings.filter((m) => m.start);
  if (st.days.length) {
    if (!timed.length) return false;
    if (!timed.every((m) => m.days.every((d) => st.days.includes(d)))) return false;
  }
  if (st.after && !(timed.length && timed.every((m) => m.start >= st.after))) return false;
  if (st.before && !(timed.length && timed.every((m) => m.end <= st.before))) return false;
  if (qTokens.length && !courseHit) {
    const hay = courseHay + wordsHay([s.classNbr, ...s.instructors]);
    if (!qTokens.every((t) => hay.includes(" " + t))) return false;
  }
  return true;
}

function runFilter() {
  if (!data) return;
  const st = readState();
  writeUrl(st);
  const qTokens = st.q.toLowerCase().replace(/[^a-z0-9]+/g, " ").split(" ").filter(Boolean);
  const qCompact = normalizeCode(st.q);

  matches = [];
  for (const c of data.courses) {
    if (st.subject && c.subject !== st.subject) continue;
    if (st.career && c.career !== st.career) continue;
    if (st.attr && !(c.attributes || []).includes(st.attr)) continue;
    if (st.units) {
      const u = Number(st.units);
      if (c.unitsMin == null) continue;
      if (u === 5 ? c.unitsMax < 5 : !(c.unitsMin <= u && u <= c.unitsMax)) continue;
    }
    if (st.degreeOnly && !degreeSet.has(normalizeCode(c.id))) continue;

    // A query matches the whole course (code/title/subject) or individual sections
    // (class number/instructor). Every word must match somewhere.
    let courseHit = true;
    if (qTokens.length) {
      const hay = c._hay;
      courseHit = qTokens.every((t) => hay.includes(" " + t)) || normalizeCode(c.id).startsWith(qCompact);
    }
    const secs = c.sections.filter((s) => sectionMatches(s, st, qTokens, courseHit, c._hay));
    if (!secs.length) continue;
    matches.push({ course: c, sections: secs });
  }

  const openCount = (m) => m.sections.filter((s) => /open/i.test(s.status)).length;
  const byCode = (a, b) => a.course.id.localeCompare(b.course.id, "en", { numeric: true });
  if (st.sort === "title") matches.sort((a, b) => a.course.title.localeCompare(b.course.title) || byCode(a, b));
  else if (st.sort === "open") matches.sort((a, b) => openCount(b) - openCount(a) || byCode(a, b));
  else matches.sort(byCode);

  liveForgetRows();
  els.results.replaceChildren();
  shown = 0;
  renderMore(false);
  announce(st);
}

function setStatus(msg) {
  // Rewriting identical text can make screen readers repeat it, so only update on change.
  if (els.status.textContent !== msg) els.status.textContent = msg;
}

function announce(st = readState()) {
  const nSec = matches.reduce((n, m) => n + m.sections.length, 0);
  const c = matches.length;
  let msg = c
    ? `${c.toLocaleString()} course${c === 1 ? "" : "s"} (${nSec.toLocaleString()} section${nSec === 1 ? "" : "s"}) match.`
      + (c > shown ? ` Showing the first ${shown}.` : "")
    : "No classes match these filters. Try removing a filter.";
  if (st.degreeOnly && !degreeSet.size) msg += " Your degree plan is empty, so no courses are in it yet.";
  setStatus(msg);
}

/* ---------- rendering ---------- */

function renderMore(moveFocus) {
  const start = shown;
  const slice = matches.slice(start, start + PAGE_SIZE);
  const frag = document.createDocumentFragment();
  slice.forEach((m) => frag.append(renderCourse(m)));
  const newRows = frag.querySelectorAll("tr[data-class]");
  els.results.append(frag);
  if (live.observer) newRows.forEach((r) => live.observer.observe(r));
  shown += slice.length;
  els.more.hidden = shown >= matches.length;
  if (!els.more.hidden) {
    els.more.textContent = `Show ${Math.min(PAGE_SIZE, matches.length - shown)} more courses`;
  }
  if (moveFocus && slice.length) {
    // Move focus to the first newly added course so keyboard and screen reader users keep their place.
    const h = els.results.children[start].querySelector("h3");
    h.focus();
  }
  updateScrollers();
}

// A table wrapper is a focusable, named scroll region only while it actually
// scrolls; otherwise it would add a useless tab stop and landmark per course.
function updateScrollers() {
  for (const box of els.results.querySelectorAll(".table-scroll")) {
    const scrolls = box.scrollWidth > box.clientWidth + 1;
    if (scrolls) {
      box.setAttribute("role", "region");
      box.setAttribute("tabindex", "0");
      box.setAttribute("aria-labelledby", box.dataset.caption);
    } else {
      box.removeAttribute("role");
      box.removeAttribute("tabindex");
      box.removeAttribute("aria-labelledby");
    }
  }
}

function renderCourse({ course: c, sections }) {
  const units = fmtUnits(c);
  const openN = sections.filter((s) => /open/i.test(s.status)).length;
  const inDegree = degreeSet.has(normalizeCode(c.id));
  const headingId = `c-${c.subject}-${c.catalog}`.replace(/[^\w-]/g, "");

  const meta = el("ul", { class: "course-meta" },
    units && el("li", {}, units),
    c.career && el("li", {}, c.career),
    el("li", {}, `${openN} of ${sections.length} section${sections.length === 1 ? "" : "s"} open`),
  );

  const tags = [];
  if (inDegree) tags.push(el("span", { class: "tag degree-tag" }, "In my degree plan"));
  for (const a of c.attributes || []) tags.push(el("span", { class: "tag" }, a));

  let more = null;
  if (c.description || c.requirements) {
    more = el("details", {},
      el("summary", {}, "Description and requirements", el("span", { class: "visually-hidden" }, ` for ${c.id}`)),
      el("dl", {},
        c.description && [el("dt", {}, "Description"), el("dd", {}, c.description)],
        c.requirements && [el("dt", {}, "Enrollment requirements"), el("dd", {}, c.requirements)],
        c.grading && [el("dt", {}, "Grading"), el("dd", {}, c.grading)],
      ));
  }

  const total = c.sections.length;
  const note = sections.length < total
    ? el("p", { class: "match-note" }, `${sections.length} of ${total} sections match your filters.`)
    : null;

  return el("li", { class: "course" },
    el("article", { "aria-labelledby": headingId },
      el("h3", { id: headingId, tabindex: "-1" },
        el("span", { class: "code" }, c.id), " — ", c.title),
      meta,
      tags.length ? el("div", {}, tags) : null,
      more,
      note,
      renderSections(c, sections),
    ));
}

function renderSections(c, sections) {
  const showSeats = data.detailLevel === "section" || live.available;
  const labels = ["Class #", "Section", "Status", "Days and times", "Dates", "Instructor", "Room and format"]
    .concat(showSeats ? ["Seats"] : []);
  const block = (...kids) => el("span", { class: "line" }, ...kids);

  // Explicit ARIA table roles keep the table semantics when CSS stacks the
  // rows into cards on narrow screens (some browsers drop them otherwise).
  const head = el("tr", { role: "row" }, labels.map((h) => el("th", { scope: "col", role: "columnheader" }, h)));

  const rows = sections.map((s) => {
    const st = statusInfo(s.status);
    const times = s.meetings.map((m) => m.start
      ? block(
          el("abbr", { title: m.days.map((d) => own(DAY_LONG, d)).join(", ") }, m.days.map((d) => own(DAY_NAMES, d)).join(" ")),
          " ",
          el("span", { class: "nowrap" }, `${fmtTime(m.start)}–${fmtTime(m.end)}`))
      : block(m.raw === "TBA" ? "No set meeting time" : (m.raw || "—")));
    const ranges = new Map(s.meetings.map((m) => [`${m.startDate || ""}|${m.endDate || ""}`, m]));
    const dates = [...ranges.values()].map(({ startDate: a, endDate: b }) => {
      const w = weeksBetween(a, b);
      const label = a && b
        ? [el("span", { class: "nowrap" }, `${fmtDate(a)} –`), " ", el("span", { class: "nowrap" }, fmtDate(b, true))]
        : a ? `Starts ${fmtDate(a, true)}` : b ? `Ends ${fmtDate(b, true)}` : "Dates to be announced";
      return block(
        el("span", {}, label),
        w ? el("span", { class: "sub" }, `${w} week${w === 1 ? "" : "s"}`) : null);
    });
    const fmt = sectionFormat(s);
    const rooms = [...new Set(s.meetings.map((m) => m.room).filter(Boolean))].join(", ");
    const values = [
      s.classNbr,
      [`${s.section}`, el("span", { class: "sub" }, own(COMPONENTS, s.component) || s.component),
        s.session && s.session !== "Regular" ? el("span", { class: "sub" }, s.session) : null],
      statusContent(s, st),
      times,
      dates,
      s.instructors.length ? s.instructors.join(", ") : "Staff",
      [rooms === "TBA" ? "TBA" : rooms || "—",
        fmt === "unknown" && !s.instructionMode ? null : el("span", { class: "sub" }, s.instructionMode || FORMAT_LABELS[fmt])],
    ];
    if (showSeats) values.push(seatsContent(s));
    const cellClass = (i) => (i === 0 ? "num" : i === 2 ? "status-cell" : i === 7 ? "seats-cell" : null);
    return el("tr", { role: "row", "data-class": s.classNbr }, values.map((v, i) =>
      el("td", { role: "cell", class: cellClass(i) },
        // Visible only in the stacked mobile layout; hidden from screen readers,
        // which already announce the column header.
        el("span", { class: "cell-label", "aria-hidden": "true" }, labels[i]),
        el("span", { class: "cell-value" }, v))));
  });

  const captionId = `cap-${c.subject}-${c.catalog}`.replace(/[^\w-]/g, "");
  return el("div", { class: "table-scroll", "data-caption": captionId },
    el("table", { class: "sections", role: "table", "aria-labelledby": captionId },
      el("caption", { id: captionId }, `Sections of ${c.id}`),
      el("thead", { role: "rowgroup" }, head),
      el("tbody", { role: "rowgroup" }, rows)));
}

/* ---------- live seat counts ---------- */

const sub = (text) => el("span", { class: "sub" }, text);

function ago(t) {
  const secs = Math.max(0, Date.now() / 1000 + live.offset - t);
  if (secs < 60) return "just now";
  if (secs < 3600) return `${Math.round(secs / 60)} min ago`;
  return `${Math.round(secs / 3600)} h ago`;
}

function checkedLabel(t) {
  if (live.enabled) return ago(t);
  const d = new Date((t - live.offset) * 1000);
  return `as of ${d.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" })}`;
}

function statusContent(s, st = statusInfo(s.status)) {
  const info = live.seats.get(s.classNbr);
  if (info && info.status && info.status !== "Not listed") st = statusInfo(info.status);
  return el("span", { class: `st ${st.cls}` }, st.label);
}

function seatsContent(s) {
  const info = live.seats.get(s.classNbr);
  if (info && info.capacity != null) {
    return [el("span", {}, `${info.available} of ${info.capacity} open`),
      info.waitCapacity ? sub(`Wait list: ${info.waitTotal} of ${info.waitCapacity}`) : null,
      sub(checkedLabel(info.checked))];
  }
  if (info && info.status === "Not listed") return "No longer listed";
  if (s.capacity != null) return [`${s.seatsAvailable} of ${s.capacity} open`,
    s.waitTotal ? sub(`${s.waitTotal} on wait list`) : null];
  if (!live.available) return "—";
  if (!live.enabled) return "Live counts are off";
  if (live.failures >= 3) return "Unavailable right now";
  return live.visible.has(s.classNbr) ? "Checking…" : "—";
}

// Update seat and status cells in place (no re-render, no focus changes, no announcements).
function updateLiveCells() {
  if (!data) return;
  const byNbr = new Map();
  for (const m of matches.slice(0, shown)) for (const s of m.sections) byNbr.set(s.classNbr, s);
  for (const row of els.results.querySelectorAll("tr[data-class]")) {
    const s = byNbr.get(row.dataset.class);
    if (!s) continue;
    const seats = row.querySelector(".seats-cell .cell-value");
    if (seats) seats.replaceChildren(...[seatsContent(s)].flat().filter(Boolean).map((x) => (x instanceof Node ? x : document.createTextNode(x))));
    const status = row.querySelector(".status-cell .cell-value");
    if (status) status.replaceChildren(statusContent(s));
  }
}

function liveForgetRows() {
  if (live.observer) live.observer.disconnect();
  live.visible.clear();
}

function schedulePoll(ms) {
  clearTimeout(live.pending);
  live.pending = setTimeout(pollSeats, ms);
}

async function pollSeats() {
  if (!live.available || !live.enabled || !data || document.hidden) return;
  const wait = live.lastPoll + 5000 - Date.now();
  if (wait > 0) return schedulePoll(wait);
  const nbrs = [...live.visible].slice(0, 80);
  if (!nbrs.length) return;
  live.lastPoll = Date.now();
  const term = data.term;
  try {
    const res = await fetch(`api/seats?term=${encodeURIComponent(term)}&classes=${nbrs.join(",")}`, { cache: "no-store" });
    if (res.status === 429) { schedulePoll(30000); return; }
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const body = await res.json();
    if (!data || data.term !== term) return;
    live.offset = Number(body.now) - Date.now() / 1000 || 0;
    for (const [nbr, info] of Object.entries(body.seats || {})) {
      if (/^\d+$/.test(nbr) && info && typeof info === "object") live.seats.set(nbr, info);
    }
    live.failures = 0;
    // The server fetches on demand, so new sections arrive a few seconds later.
    const missing = nbrs.some((n) => !live.seats.get(n)?.checked && live.seats.get(n)?.status !== "Not listed");
    if (missing && live.quickRetries < 6) { live.quickRetries++; schedulePoll(8000); } else live.quickRetries = 0;
  } catch {
    live.failures++;
  } finally {
    updateLiveCells();
  }
}

function setLiveTimer() {
  clearInterval(live.timer);
  live.timer = null;
  if (live.available && live.enabled) {
    live.timer = setInterval(() => { pollSeats(); updateLiveCells(); }, 30000);
  }
}

async function initLive() {
  try {
    const res = await fetch("api/health", { cache: "no-store" });
    if (!res.ok) throw new Error();
    const health = await res.json();
    live.terms = arr(health?.terms).map(str);
  } catch {
    live.terms = [];
  }
  const pref = store.get("liveSeats");
  live.enabled = pref !== "0";
  els.live.checked = live.enabled;
  live.observer = new IntersectionObserver((entries) => {
    for (const e of entries) {
      const nbr = e.target.dataset.class;
      if (e.isIntersecting) live.visible.add(nbr); else live.visible.delete(nbr);
    }
    updateLiveCells();
    schedulePoll(1200);
  }, { rootMargin: "200px 0px" });
}

function liveForTerm(term) {
  live.available = live.terms.includes(term);
  live.seats.clear();
  live.failures = 0;
  $("live-box").hidden = !live.available;
  setLiveTimer();
}

/* ---------- data loading ---------- */

function fillSelect(sel, items, keepFirst = true) {
  const first = keepFirst ? sel.options[0] : null;
  sel.replaceChildren(...(first ? [first] : []), ...items.map(([v, t]) => el("option", { value: v }, t)));
}

const str = (v) => (v == null ? "" : String(v));
const arr = (v) => (Array.isArray(v) ? v : []);
const num = (v) => (typeof v === "number" && Number.isFinite(v) ? v : null);

// Coerce scraped JSON into the shape the renderer expects, so one odd record
// can't crash the page.
function normalizeData(d) {
  if (!d || typeof d !== "object") throw new Error("bad data");
  const sectionDetail = d.detailLevel === "section";
  d.subjects = d.subjects && typeof d.subjects === "object" ? d.subjects : {};
  d.courses = arr(d.courses).filter((c) => c && typeof c === "object").map((c) => {
    const course = {
      subject: str(c.subject), catalog: str(c.catalog), title: str(c.title) || "Untitled course",
      subjectName: str(c.subjectName), career: str(c.career) || null,
      description: str(c.description), requirements: str(c.requirements), grading: str(c.grading),
      attributes: arr(c.attributes).map(str).filter(Boolean),
      unitsMin: num(c.unitsMin), unitsMax: num(c.unitsMax) ?? num(c.unitsMin),
    };
    course.id = str(c.id) || `${course.subject} ${course.catalog}`;
    course.sections = arr(c.sections).filter((s) => s && typeof s === "object").map((s) => ({
      classNbr: str(s.classNbr), section: str(s.section), component: str(s.component),
      session: str(s.session), status: str(s.status),
      instructors: arr(s.instructors).map(str).filter(Boolean),
      meetings: arr(s.meetings).filter((m) => m && typeof m === "object").map((m) => ({
        days: arr(m.days).filter((x) => Object.hasOwn(DAY_NAMES, x)),
        start: /^\d\d:\d\d$/.test(m.start) ? m.start : null,
        end: /^\d\d:\d\d$/.test(m.end) ? m.end : null,
        raw: str(m.raw), room: str(m.room),
        startDate: /^\d{4}-\d\d-\d\d$/.test(m.startDate) ? m.startDate : null,
        endDate: /^\d{4}-\d\d-\d\d$/.test(m.endDate) ? m.endDate : null,
      })),
      // Seat counts and format are only trustworthy when every section was fetched.
      ...(sectionDetail ? {
        instructionMode: str(s.instructionMode), capacity: num(s.capacity),
        seatsAvailable: num(s.seatsAvailable), waitTotal: num(s.waitTotal),
      } : {}),
    }));
    course._hay = wordsHay([course.id, course.subject + course.catalog, course.title, course.subjectName]);
    return course;
  });
  return d;
}

async function loadTerm(code) {
  const token = ++loadToken;
  data = null;                      // never show the previous term's classes under a new label
  matches = [];
  setStatus("Loading class data…");
  els.results.replaceChildren();
  els.more.hidden = true;
  let json;
  try {
    const res = await fetch(`data/${encodeURIComponent(code)}.json`, { cache: "no-cache" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    json = normalizeData(await res.json());
  } catch (e) {
    if (token !== loadToken) return false;
    throw new Error("Class data for this term could not be loaded. Try again later or choose another term.");
  }
  if (token !== loadToken) return false;  // a newer term was chosen while this one loaded
  data = json;
  liveForTerm(str(data.term) || code);

  // Rebuild the term-specific dropdowns, keeping the user's choice when it still exists.
  const refill = (sel, items) => {
    const prev = sel.value;
    fillSelect(sel, items);
    if ([...sel.options].some((o) => o.value === prev)) sel.value = prev;
  };
  refill(els.subject, Object.entries(data.subjects).map(([k, v]) => [str(k), `${k} — ${str(v)}`]));
  refill(els.career, [...new Set(data.courses.map((c) => c.career).filter(Boolean))].sort().map((c) => [c, c]));
  const attrs = [...new Set(data.courses.flatMap((c) => c.attributes))].sort();
  refill(els.attr, attrs.map((a) => [a, a]));
  els.attr.disabled = !attrs.length;

  const updated = new Date(data.updated);
  els.updated.textContent = Number.isNaN(updated.getTime()) ? ""
    : `Data updated ${updated.toLocaleString("en-US", { dateStyle: "medium", timeStyle: "short" })}.`;
  document.title = `${str(data.name) || "Class"} classes — UNLV Class Finder`;
  return true;
}

function updateDegree() {
  degreeSet = parseDegreeCodes(els.degree.value);
  store.set("degreePlan", els.degree.value);
  els.degreeCount.textContent = degreeSet.size
    ? `${degreeSet.size} course code${degreeSet.size === 1 ? "" : "s"} found.`
    : "No course codes found yet.";
}

function fillTimes() {
  const opts = [];
  for (let h = 6; h <= 22; h++) {
    for (const m of [0, 30]) {
      const v = `${String(h).padStart(2, "0")}:${m ? "30" : "00"}`;
      opts.push([v, fmtTime(v)]);
    }
  }
  fillSelect(els.after, opts);
  fillSelect(els.before, opts);
}

function safeRunFilter() {
  try {
    runFilter();
  } catch (e) {
    console.error(e);
    setStatus("Something went wrong showing these classes. Try clearing the filters or reloading.");
  }
}

function attachListeners() {
  const refilter = debounce(safeRunFilter, 250);
  // Typing waits longer so screen readers aren't sent a new count after every letter.
  const refilterTyping = debounce(safeRunFilter, 800);
  form.addEventListener("input", (e) => {
    if (e.target === els.term || e.target === els.degree) return;
    (e.target === els.q ? refilterTyping : refilter)();
  });
  form.addEventListener("submit", (e) => { e.preventDefault(); safeRunFilter(); });
  els.term.addEventListener("change", async () => {
    try {
      if (await loadTerm(els.term.value)) safeRunFilter();
    } catch (e) {
      setStatus(e.message);
    }
  });
  els.degree.addEventListener("input", debounce(() => { updateDegree(); safeRunFilter(); }, 600));
  form.addEventListener("reset", () => {
    // Let the browser reset the fields first, then refilter. The term and degree list are kept.
    const saved = els.degree.value;
    const term = els.term.value;
    setTimeout(() => { els.degree.value = saved; els.term.value = term; safeRunFilter(); els.q.focus(); }, 0);
  });
  els.more.addEventListener("click", () => { renderMore(true); announce(); });
  els.live.addEventListener("change", () => {
    live.enabled = els.live.checked;
    store.set("liveSeats", live.enabled ? "1" : "0");
    setLiveTimer();
    updateLiveCells();
    if (live.enabled) schedulePoll(0);
  });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) schedulePoll(500); });
  window.addEventListener("resize", debounce(updateScrollers, 200));
}

async function init() {
  fillTimes();
  els.degree.value = store.get("degreePlan") || "";
  updateDegree();
  if (degreeSet.size) $("degree-box").open = true;
  attachListeners();
  await initLive();

  let terms;
  try {
    const res = await fetch("data/terms.json", { cache: "no-cache" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    terms = arr((await res.json())?.terms).filter((t) => t && /^\d{4}$/.test(t.term));
  } catch (e) {
    setStatus("Class data is not available yet. Please check back later.");
    return;
  }
  if (!terms.length) {
    setStatus("No terms have been published yet. Please check back later.");
    return;
  }
  fillSelect(els.term, terms.map((t) => [t.term, str(t.name) || t.term]), false);
  const wanted = new URLSearchParams(location.search).get("term");
  els.term.value = terms.some((t) => t.term === wanted) ? wanted : terms[terms.length - 1].term;

  applyUrl();
  try {
    if (await loadTerm(els.term.value)) {
      applyUrl();  // term-specific options (subject, level, attribute) exist now
      safeRunFilter();
    }
  } catch (e) {
    setStatus(e.message);
  }
}

init();
