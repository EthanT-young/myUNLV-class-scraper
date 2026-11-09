# UNLV Class Finder

**Live site: [ethantyoung.com/projects/unlvEnrollmentViewer](https://ethantyoung.com/projects/unlvEnrollmentViewer)**

Looking up one class in MyUNLV takes 20 to 30 seconds of clicking. This project grabs the
whole schedule once and puts it on a fast, searchable page instead.

A scraper reads UNLV's public class search, which is open to guests, so you don't need to
log in or hand over your NSHE password. A small website then lets you search and filter
everything it collected, and shows live seat counts for the classes you're looking at.

The work is split between two machines:

1. A personal computer does the heavy lifting. It scrapes the whole catalog and uploads
   the result to this repo's `data-latest` GitHub release with `./publish.sh`.
2. A small server runs one Docker container that serves the site, installs each new data
   release on its own, and checks seat counts for the sections visitors have on screen.

Setting up the server is covered in [deploy/README.md](deploy/README.md).

## Getting started

You need Python 3.10 or newer and the `requests` package.

```bash
# Grab every class for Spring 2027
python3 -m scraper --term 2272

# Or refresh just a couple of subjects
python3 -m scraper --term 2272 --subjects CS,MATH

# Then run the site with live seat counts and open http://localhost:8080
python3 -m server
```

`python3 -m http.server -d site 8000` also works if you only want the static site. Either
way you need a local server: opening `index.html` straight from your file browser won't
work, because the page loads its data with `fetch()`.

## Publishing data

Scraped data isn't committed to git, so the repo doesn't grow with every scrape. Instead,
`./publish.sh` scrapes, runs the scraper tests, and uploads `site/data` as a compressed
archive (about 550 KB) to the `data-latest` release, replacing the previous one. The
server picks it up within 5 minutes.

```bash
gh auth login              # once; needs the GitHub CLI
./publish.sh --term 2272   # scrape and publish one term
./publish.sh --skip-scrape # publish the data you already have
```

It won't publish if the scraper reports a failed subject, unless you add `--allow-failures`.

## The scraper

| Option | Default | What it does |
| --- | --- | --- |
| `--term CODE` | every term on offer | Which term to grab. 2268 is Fall 2026 and 2272 is Spring 2027. You can repeat it. |
| `--subjects A,B` | all ~300 subjects | Refresh only these subjects. Everything else in the data file is left alone. |
| `--details` | `course` | `none` grabs only the search results. `course` also gets credits, description, prerequisites, attributes and level, fetched once per course and cached. `section` adds seat counts for every section, but costs about 2 requests per section, so it's slow. |
| `--delay SECONDS` | `1.0` | How long each session waits between requests. The minimum is 0.5. |
| `--workers N` | `4` | How many sessions run at once, from 1 to 8. Each one waits its own delay, so 4 workers send up to 4 requests per second. |
| `--max-age DAYS` | `14` | How long cached course details are kept before they're fetched again. |
| `--force` | off | Write the output even if it's much smaller than what's already there. |

### How it works

UNLV's public search is fussy. It wants at least two search criteria and won't return
more than 300 sections at a time, so the scraper searches each subject with "course
number ≥ 0". When a subject is too big, like English, it splits the search by level,
then by component (lecture, lab, and so on), then by instruction mode. Credits,
descriptions and prerequisites come from each course's detail page, which it opens once.

The first full run of a term takes about 30 to 45 minutes with 4 to 6 workers. Later
refreshes take a few minutes, because course details come from the cache.

It writes three things:

- `site/data/terms.json`: which terms have been scraped
- `site/data/<term>.json`: every course and its sections, used by the website
- `data/cache/`: the subject list and course-detail cache (not published)

### When things go wrong

Long scrapes hit hiccups, so the scraper tries hard to keep good data:

- A subject that fails gets retried up to 3 times. If it still fails, its classes from
  the last good run are kept.
- When the server is busy (429 or 5xx errors), it backs off and tries again, following
  `Retry-After` when the server sends one.
- Every page of results is checked against UNLV's own "N class section(s) found" count.
  If they don't match, the scrape stops with an error.
- It won't write an empty file, or one with more than 20% fewer sections than the
  current file, unless you pass `--force`.
- Only one scrape can run at a time, and the exit code is non-zero if anything failed,
  so it's safe to run from cron.

## The website

The site is plain HTML, CSS and JavaScript in `site/`, with no build step and no
dependencies. Any static host works: GitHub Pages, Netlify, or a campus web server.

You can:

- Search by course code, title, instructor or class number
- Filter by subject, level, requirement (like general education), open sections only,
  meeting days, start and end time, online or in person, and credits
- See each section's status, days and times, dates (with the length in weeks),
  instructor and room
- See live seat counts and wait lists when the site runs on the server. Only sections on
  your screen are checked, about once a minute, and you can turn this off
- Bookmark or share a search, since your filters are saved in the URL

The "My degree plan" box takes the course codes from your degree worksheet or the
catalog. Shorthand like "CS 135, 202, 218" works too. The site marks those courses and
can show only them. The list stays in your browser.

### Accessibility

The site aims for WCAG 2.2 AA. In practice that means:

- Everything works with just a keyboard. There's a skip link, and focus is always visible.
- Screen readers get proper headings, labels and data tables, plus a short announcement
  of the result count that isn't repeated on every keystroke.
- Status is shown with a shape as well as a color: ● Open, ■ Closed, ▲ Wait list.
- Text and controls have enough contrast in light and dark mode, and the site works in
  Windows high-contrast mode. The theme follows your system setting, or you can pick
  light or dark from the menu in the header.
- On a phone, each section turns into a stacked card, so there's no sideways scrolling.

## Live seat counts

`python3 -m server` serves the site plus `/api/seats`. The browser sends the class numbers
on screen, and the server returns what it knows and queues a refresh for anything older
than 2 minutes. Its worker sessions then refresh the most-viewed courses first: one search
per course for every section's status, then a detail page per section for seat counts.
Many visitors looking at the same class cost the same as one. The number of sessions times
the delay between requests caps the load on UNLV however busy the site gets.

## Running the tests

```bash
npm install && npx playwright install chromium   # first time only
npm test               # everything
npm run test:site      # browser and accessibility tests
npm run test:scraper   # scraper and server tests (offline, no requests to UNLV)
```

## What's next

- Pulling degree requirements straight from the UNLV catalog, so you don't have to paste
  them in
