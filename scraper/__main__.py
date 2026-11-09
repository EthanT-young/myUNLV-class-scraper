"""Scrape UNLV's public class search into JSON for the website.

    python3 -m scraper                      # every term offered, course details
    python3 -m scraper --term 2272 --subjects CS,MATH
    python3 -m scraper --details section    # also seat counts (slow: ~2 requests per section)
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from queue import Empty, Queue

import requests

from .parse import parse_detail, parse_results
from .peoplesoft import F_CAREER, SPLIT_FIELDS, ClassSearch, SearchError, TooManyResults

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "site" / "data"
CACHE_DIR = ROOT / "data" / "cache"
CAREER_NAMES = {"UGRD": "Undergraduate", "GRAD": "Graduate", "LAWS": "Law",
                "DENT": "Dental School", "MEDI": "Medical School"}
TERM_RE = re.compile(r"\d{4}")
SUBJECT_RE = re.compile(r"[A-Z0-9]{1,8}")
SUBJECT_ATTEMPTS = 3
DETAIL_ATTEMPTS = 2
MAX_WORKERS = 8
MIN_KEEP_RATIO = 0.8  # refuse to replace data with a file this much smaller (unless --force)


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def dump_term(data: dict) -> str:
    """Term data as JSON with one course per line, so git stores small diffs
    between scrapes instead of a whole new multi-megabyte line."""
    compact = {"ensure_ascii": False, "separators": (",", ":")}
    head = json.dumps({k: v for k, v in data.items() if k != "courses"}, **compact)
    lines = ",\n".join(json.dumps(c, **compact) for c in data.get("courses", []))
    return head[:-1] + (',' if len(head) > 2 else '') + '"courses":[\n' + lines + "\n]}\n"


def write_json(path: Path, data, compact=False, term_file=False):
    """Atomic write: unique temp file in the same directory, fsync, rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            if term_file:
                f.write(dump_term(data))
            else:
                json.dump(data, f, ensure_ascii=False,
                          separators=(",", ":") if compact else None, indent=None if compact else 1)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def course_sort_key(c):
    m = re.match(r"(\d+)(.*)", c["catalog"])
    return (c["subject"], int(m.group(1)) if m else 0, m.group(2) if m else c["catalog"])


class TermScraper:
    """Scrapes one term. Each worker thread drives its own PeopleSoft session
    (sessions are stateful, so one session can only do one thing at a time);
    shared results are guarded by a lock."""

    def __init__(self, new_session, term: str, details: str, max_age_days: int, workers: int = 1):
        self.new_session, self.term, self.details = new_session, term, details
        self.workers = workers
        self.lock = threading.RLock()
        self.done = 0
        self.cache_path = CACHE_DIR / f"courses-{term}.json"
        self.course_cache = load_json(self.cache_path, {})
        self.stale_before = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
        self.sections: dict[str, dict] = {}
        self.detail_done: set[str] = set()       # course keys (course mode) or class numbers (section mode)
        self.detail_failures: dict[str, int] = {}
        self.errors: list[str] = []
        self.failed_subjects: set[str] = set()

    def _detail_key(self, sec: dict) -> str:
        return sec["classNbr"] if self.details == "section" else f"{sec['subject']} {sec['catalog']}"

    def _needs_detail(self, sec: dict) -> bool:
        if self.details == "none":
            return False
        key = self._detail_key(sec)
        with self.lock:
            if key in self.detail_done or self.detail_failures.get(key, 0) >= DETAIL_ATTEMPTS:
                return False
            if self.details == "section":
                return True
            cached = self.course_cache.get(key)
            return not cached or cached.get("fetched", "") < self.stale_before

    def collect(self, cs: ClassSearch, subject: str, extra: dict | None = None, depth: int = 0) -> dict[str, dict]:
        """Sections for one search, splitting when PeopleSoft refuses >300.
        Raises on failure; the caller retries the whole subject."""
        extra = extra or {}
        try:
            page = cs.search(subject, **extra)
        except TooManyResults:
            if depth >= len(SPLIT_FIELDS):
                raise SearchError(f"{subject} {extra}: still over 300 sections after every split")
            field = SPLIT_FIELDS[depth]
            values = cs.split_values.get(field)
            if not values:
                raise SearchError(f"No values known for split field {field}")
            out: dict[str, dict] = {}
            for v in values:
                out.update(self.collect(cs, subject, {**extra, field: v}, depth + 1))
            return out
        if not page:
            return {}
        found: dict[str, dict] = {}
        for sec in parse_results(page):
            if F_CAREER in extra:
                sec["career"] = CAREER_NAMES.get(extra[F_CAREER], extra[F_CAREER])
            if self._needs_detail(sec):
                key = self._detail_key(sec)
                try:
                    d = parse_detail(cs.detail(sec["row"]))
                except (SearchError, requests.RequestException) as e:
                    # Count the failure so this detail is skipped after a retry or two,
                    # then let the subject-level retry rebuild the session.
                    with self.lock:
                        self.detail_failures[key] = self.detail_failures.get(key, 0) + 1
                    raise SearchError(f"detail {sec['classNbr']}: {e}") from e
                if self.details == "section":
                    sec.update({k: v for k, v in d["section"].items() if v is not None})
                with self.lock:
                    self.detail_done.add(key)
                    self.course_cache[f"{sec['subject']} {sec['catalog']}"] = {**d["course"], "fetched": now_iso()}
            sec.pop("row", None)
            found[sec["classNbr"]] = sec
        cs.back_to_criteria()
        return found

    def scrape_subject(self, cs: ClassSearch, code: str) -> int:
        for attempt in range(1, SUBJECT_ATTEMPTS + 1):
            try:
                found = self.collect(cs, code)
            except (SearchError, requests.RequestException, ValueError) as e:
                with self.lock:
                    self.errors.append(f"{code} (attempt {attempt}): {type(e).__name__}: {e}")
                if attempt == SUBJECT_ATTEMPTS:
                    with self.lock:
                        self.failed_subjects.add(code)
                    cs.reopen()
                    return 0
                time.sleep(15 * attempt)
                cs.reopen()  # raises only after a long outage, which should stop the run
                continue
            with self.lock:
                # Keep sections from other subjects' headers (cross-listed courses) too.
                self.sections.update(found)
            return len(found)
        return 0

    def _worker(self, queue: Queue, total: int, fatal: list):
        try:
            cs = self.new_session()
            cs.term = self.term
            cs.reopen()
            while not fatal:
                try:
                    code = queue.get_nowait()
                except Empty:
                    return
                t0 = time.monotonic()
                n = self.scrape_subject(cs, code)
                with self.lock:
                    self.done += 1
                    flag = "  FAILED" if code in self.failed_subjects else ""
                    log(f"  [{self.done}/{total}] {code}: {n} sections ({time.monotonic() - t0:.0f}s){flag}")
                    write_json(self.cache_path, self.course_cache)
        except BaseException as e:  # e.g. the site stayed down through every reopen
            fatal.append(e)

    def run(self, subjects: dict[str, str]):
        queue: Queue = Queue()
        for code in sorted(subjects):
            queue.put(code)
        fatal: list[BaseException] = []
        n = max(1, min(self.workers, len(subjects)))
        threads = [threading.Thread(target=self._worker, args=(queue, len(subjects), fatal),
                                    name=f"scrape-{i}", daemon=True) for i in range(n)]
        for i, t in enumerate(threads):
            t.start()
            if i < n - 1:
                time.sleep(1)  # stagger session start-up
        for t in threads:
            t.join()
        with self.lock:
            write_json(self.cache_path, self.course_cache)
        if fatal:
            raise fatal[0]

    def build(self, subjects: dict[str, str], term_name: str) -> dict:
        courses: dict[str, dict] = {}
        for sec in self.sections.values():
            key = f"{sec['subject']} {sec['catalog']}"
            c = courses.get(key)
            if not c:
                info = {k: v for k, v in self.course_cache.get(key, {}).items() if k != "fetched"}
                c = courses[key] = {
                    "id": key, "subject": sec["subject"], "catalog": sec["catalog"],
                    "title": sec["title"], "subjectName": subjects.get(sec["subject"], sec["subject"]),
                    **info, "sections": [],
                }
            if not c.get("career") and sec.get("career"):
                c["career"] = sec["career"]
            c["sections"].append({k: v for k, v in sec.items()
                                  if k not in ("subject", "catalog", "title", "career")})
        for c in courses.values():
            c["sections"].sort(key=lambda s: s["section"])
        return {
            "term": self.term, "name": term_name, "updated": now_iso(),
            "detailLevel": self.details,
            "subjects": {k: subjects.get(k, k) for k in sorted({c["subject"] for c in courses.values()})},
            "courses": sorted(courses.values(), key=course_sort_key),
        }


def merge_previous(data: dict, old: dict | None, replaced: set[str]) -> dict:
    """Keep courses from the previous file for every subject not freshly scraped."""
    if not old:
        return data
    keep = [c for c in old.get("courses", []) if c["subject"] not in replaced]
    data["courses"] = sorted(keep + data["courses"], key=course_sort_key)
    data["subjects"] = {**{k: v for k, v in old.get("subjects", {}).items() if k not in replaced},
                        **data["subjects"]}
    data["subjects"] = dict(sorted(data["subjects"].items()))
    return data


def count_sections(d: dict | None) -> int:
    return sum(len(c["sections"]) for c in (d or {}).get("courses", []))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python3 -m scraper", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--term", action="append", help="term code, e.g. 2272 (repeatable; default: all offered)")
    ap.add_argument("--subjects", help="comma-separated subject codes (default: all)")
    ap.add_argument("--details", choices=["none", "course", "section"], default="course",
                    help="course: units/description/prereqs once per course (cached). "
                         "section: also seats for every section (slow). Default: course")
    ap.add_argument("--max-age", type=int, default=14, help="days before cached course details are refetched")
    ap.add_argument("--delay", type=float, default=1.0, help="seconds between requests (be polite; default 1.0)")
    ap.add_argument("--workers", type=int, default=4,
                    help=f"parallel sessions, each with its own --delay (1-{MAX_WORKERS}; default 4)")
    ap.add_argument("--force", action="store_true",
                    help="write output even if it is much smaller than the existing data")
    args = ap.parse_args(argv)

    if not 1 <= args.workers <= MAX_WORKERS:
        ap.error(f"--workers must be between 1 and {MAX_WORKERS}")
    if args.delay < 0.5:
        ap.error("--delay below 0.5s is not allowed; please be polite to UNLV's servers")
    for t in args.term or []:
        if not TERM_RE.fullmatch(t):
            ap.error(f"invalid term code {t!r} (expected 4 digits, e.g. 2272)")
    wanted = None
    if args.subjects:
        wanted = [s.strip().upper() for s in args.subjects.split(",") if s.strip()]
        bad = [s for s in wanted if not SUBJECT_RE.fullmatch(s)]
        if bad:
            ap.error(f"invalid subject code(s): {', '.join(bad)}")

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    lock = open(CACHE_DIR / ".scrape.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit("Another scrape is already running (data/cache/.scrape.lock).")

    cs = ClassSearch(delay=args.delay)
    cs.open()
    offered = {code: name for code, name in cs.terms() if TERM_RE.fullmatch(code)}
    terms = args.term or list(offered)
    log(f"Terms offered: {offered}")

    index = load_json(OUT_DIR / "terms.json", {"terms": []})
    known = {t["term"]: t for t in index.get("terms", []) if TERM_RE.fullmatch(str(t.get("term", "")))}
    exit_code = 0

    for term in terms:
        name = offered.get(term, term)
        log(f"== {name} ({term})")
        cs.open(term)
        subj_cache = CACHE_DIR / f"subjects-{term}.json"
        subjects = load_json(subj_cache, {})
        if not subjects or time.time() - subj_cache.stat().st_mtime > 7 * 86400:
            log("  fetching subject list...")
            fetched = {k: v for k, v in cs.subjects().items() if SUBJECT_RE.fullmatch(k)}
            if fetched:
                subjects = fetched
                write_json(subj_cache, subjects)
            elif subjects:
                log("  ! subject lookup returned nothing; using the cached list")
        if not subjects:
            log("  ! no subjects found; skipping this term (existing data left untouched)")
            exit_code = 1
            continue
        if wanted:
            subjects = {s: subjects.get(s, s) for s in wanted}
        log(f"  {len(subjects)} subjects")

        ts = TermScraper(lambda: ClassSearch(delay=args.delay), term, args.details, args.max_age, args.workers)
        ts.run(subjects)

        out_path = OUT_DIR / f"{term}.json"
        old = load_json(out_path, None)
        data = ts.build(subjects, name)
        if wanted or ts.failed_subjects:
            # Replace only subjects that were scraped successfully; failed ones keep their old data.
            data = merge_previous(data, old, set(subjects) - ts.failed_subjects)

        for e in ts.errors:
            log(f"  ! {e}")
        new_n, old_n = count_sections(data), count_sections(old)
        if new_n == 0:
            log("  ! scrape produced 0 sections; NOT writing (existing data left untouched)")
            exit_code = 1
            continue
        if old_n and new_n < old_n * MIN_KEEP_RATIO and not args.force:
            log(f"  ! new data has {new_n} sections vs {old_n} before; NOT writing. "
                "Re-run with --force if the drop is expected.")
            exit_code = 1
            continue

        write_json(out_path, data, term_file=True)
        known[term] = {"term": term, "name": name, "updated": data["updated"],
                       "courses": len(data["courses"]), "sections": new_n}
        log(f"  wrote {out_path.relative_to(ROOT)}: {len(data['courses'])} courses, {new_n} sections"
            + (f" ({len(ts.failed_subjects)} subject(s) kept from previous data)" if ts.failed_subjects else ""))
        if ts.failed_subjects:
            exit_code = 1

    index["terms"] = sorted(known.values(), key=lambda t: t["term"])
    write_json(OUT_DIR / "terms.json", index)
    lock.close()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
