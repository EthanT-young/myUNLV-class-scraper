"""Live seat counts for the sections people are looking at.

The browser reports which class numbers are on screen. Each report marks
that course as "wanted". Worker threads, each with its own PeopleSoft
guest session, refresh the most-wanted stale course: one search for the
course (status of every section), then a detail page per wanted section
(seat counts). Nothing is fetched for courses nobody is viewing, and the
number of workers times the per-session delay caps the load on UNLV.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from scraper.parse import parse_detail, parse_results
from scraper.peoplesoft import F_CATALOG, F_CATALOG_OP, ClassSearch, SearchError

log = logging.getLogger("seats")


@dataclass
class Course:
    term: str
    subject: str
    catalog: str
    class_nbrs: set[str]
    wanted_until: float = 0.0          # someone viewed it recently
    heat: float = 0.0                  # decayed count of views, for priority
    heat_at: float = 0.0
    wanted_nbrs: dict[str, float] = field(default_factory=dict)  # nbr -> wanted until
    busy: bool = False
    next_try: float = 0.0              # backoff after an error

    @property
    def key(self):
        return (self.term, self.subject, self.catalog)


class Catalog:
    """Which class numbers exist, from the published term files. Reloaded
    when the files change (after the server pulls new data)."""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.by_nbr: dict[tuple[str, str], tuple[str, str, str]] = {}  # (term, nbr) -> course key
        self.courses: dict[tuple[str, str, str], set[str]] = {}
        self.terms: list[str] = []
        self._mtimes: dict[str, float] = {}

    def refresh(self) -> bool:
        try:
            files = {p.name: p.stat().st_mtime for p in self.data_dir.glob("*.json")}
        except OSError:
            return False
        if files == self._mtimes:
            return False
        try:
            index = json.loads((self.data_dir / "terms.json").read_text())
            terms = [t["term"] for t in index.get("terms", []) if str(t.get("term", "")).isdigit()]
            by_nbr, courses = {}, {}
            for term in terms:
                data = json.loads((self.data_dir / f"{term}.json").read_text())
                for c in data.get("courses", []):
                    key = (term, str(c["subject"]), str(c["catalog"]))
                    nbrs = {str(s["classNbr"]) for s in c.get("sections", []) if s.get("classNbr")}
                    courses[key] = nbrs
                    for n in nbrs:
                        by_nbr[(term, n)] = key
        except (OSError, ValueError, KeyError, TypeError) as e:
            log.warning("could not load class data: %s", e)
            return False
        self.terms, self.by_nbr, self.courses, self._mtimes = terms, by_nbr, courses, files
        log.info("loaded %d terms, %d courses", len(terms), len(courses))
        return True


class SeatTracker:
    def __init__(self, catalog: Catalog, *, workers: int = 2, delay: float = 1.0,
                 ttl: float = 120.0, want_window: float = 180.0, session_factory=None,
                 clock=time.monotonic, wall=time.time):
        self.catalog = catalog
        self.workers = workers
        self.delay = delay
        self.ttl = ttl
        self.want_window = want_window
        self.session_factory = session_factory or (lambda: ClassSearch(delay=delay, retries=2))
        self.clock, self.wall = clock, wall
        self.cond = threading.Condition()
        self.courses: dict[tuple[str, str, str], Course] = {}
        self.seats: dict[tuple[str, str], dict] = {}   # (term, nbr) -> latest info
        self.fetched_at: dict[tuple[str, str], float] = {}  # (term, nbr) -> clock() of seat fetch
        self.stats = {"courseRefreshes": 0, "errors": 0, "lastError": None, "lastSuccess": None}
        self._stop = False
        self._threads: list[threading.Thread] = []

    # -- demand ------------------------------------------------------------
    def want(self, term: str, nbrs: list[str]) -> list[str]:
        """Record that these sections are on someone's screen; returns the
        class numbers that exist in the published data."""
        now = self.clock()
        known = []
        with self.cond:
            touched = set()
            for n in nbrs:
                key = self.catalog.by_nbr.get((term, n))
                if not key:
                    continue
                known.append(n)
                c = self.courses.get(key)
                if c is None:
                    c = self.courses[key] = Course(*key, class_nbrs=set(self.catalog.courses.get(key, ())))
                c.wanted_nbrs[n] = now + self.want_window
                if key not in touched:
                    touched.add(key)
                    c.wanted_until = now + self.want_window
                    c.heat = c.heat * 0.5 ** ((now - c.heat_at) / 300) + 1 if c.heat_at else 1
                    c.heat_at = now
            if touched:
                self.cond.notify_all()
        return known

    def get(self, term: str, nbrs: list[str]) -> dict[str, dict]:
        with self.cond:
            return {n: dict(self.seats[(term, n)]) for n in nbrs if (term, n) in self.seats}

    # -- scheduling ----------------------------------------------------------
    def _stale_wanted(self, c: Course, now: float) -> list[str]:
        return [n for n, until in c.wanted_nbrs.items()
                if until > now and now - self.fetched_at.get((c.term, n), -1e9) >= self.ttl]

    def next_job(self) -> tuple[Course, list[str]] | None:
        """Most-viewed course that has a stale, recently viewed section."""
        now = self.clock()
        best, best_nbrs, best_score = None, None, None
        for c in self.courses.values():
            if c.busy or c.wanted_until <= now or c.next_try > now:
                continue
            nbrs = self._stale_wanted(c, now)
            if not nbrs:
                continue
            oldest = min(self.fetched_at.get((c.term, n), -1e9) for n in nbrs)
            score = (c.heat * 0.5 ** ((now - c.heat_at) / 300), -oldest)
            if best_score is None or score > best_score:
                best, best_nbrs, best_score = c, nbrs, score
        return (best, best_nbrs) if best else None

    def prune(self):
        """Forget courses nobody has viewed for a while (bounds memory)."""
        now = self.clock()
        with self.cond:
            for key in [k for k, c in self.courses.items() if not c.busy and c.wanted_until < now - 3600]:
                del self.courses[key]
            live = set(self.catalog.by_nbr)
            for k in [k for k in self.seats if k not in live]:
                self.seats.pop(k, None)
                self.fetched_at.pop(k, None)

    # -- fetching ------------------------------------------------------------
    def refresh_course(self, cs: ClassSearch, c: Course, nbrs: list[str]):
        if cs.term != c.term:
            cs.term = c.term
            cs.reopen()
        page = cs.search(c.subject, **{F_CATALOG_OP: "E", F_CATALOG: c.catalog})
        sections = parse_results(page) if page else []
        now_wall = self.wall()
        found = {}
        for sec in sections:
            if sec["subject"] != c.subject or sec["catalog"] != c.catalog:
                continue
            found[sec["classNbr"]] = sec
            with self.cond:
                info = self.seats.setdefault((c.term, sec["classNbr"]), {})
                info["status"] = sec["status"]
                info["statusChecked"] = now_wall
        for n in nbrs:
            sec = found.get(n)
            if not sec:
                with self.cond:
                    self.seats.setdefault((c.term, n), {})["status"] = "Not listed"
                    self.fetched_at[(c.term, n)] = self.clock()
                continue
            d = parse_detail(cs.detail(sec["row"]))["section"]
            with self.cond:
                info = self.seats.setdefault((c.term, n), {})
                info.update({
                    "capacity": d["capacity"], "enrolled": d["enrolled"],
                    "available": d["seatsAvailable"], "waitCapacity": d["waitCapacity"],
                    "waitTotal": d["waitTotal"], "checked": self.wall(),
                })
                self.fetched_at[(c.term, n)] = self.clock()
        if page:
            cs.back_to_criteria()

    def _worker(self, idx: int):
        cs = None
        while True:
            with self.cond:
                job = None
                while not self._stop:
                    job = self.next_job()
                    if job:
                        break
                    self.cond.wait(timeout=5)
                if self._stop:
                    return
                course, nbrs = job
                course.busy = True
            try:
                if cs is None:
                    cs = self.session_factory()
                    cs.term = course.term
                    cs.reopen()
                try:
                    self.refresh_course(cs, course, nbrs)
                except (SearchError, OSError, ValueError) as first:
                    # Guest sessions expire after inactivity; one fresh session and retry.
                    log.info("worker %d: retrying %s %s after %s", idx, course.subject, course.catalog, first)
                    cs.reopen()
                    self.refresh_course(cs, course, nbrs)
                with self.cond:
                    self.stats["courseRefreshes"] += 1
                    self.stats["lastSuccess"] = self.wall()
            except Exception as e:  # never let one bad course kill the worker
                log.warning("worker %d: %s %s failed: %s", idx, course.subject, course.catalog, e)
                with self.cond:
                    self.stats["errors"] += 1
                    self.stats["lastError"] = f"{type(e).__name__}: {e}"[:300]
                    course.next_try = self.clock() + 60
                cs = None  # start from a clean session next time
            finally:
                with self.cond:
                    course.busy = False
                    self.cond.notify_all()

    def start(self):
        for i in range(self.workers):
            t = threading.Thread(target=self._worker, args=(i,), name=f"seats-{i}", daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self):
        with self.cond:
            self._stop = True
            self.cond.notify_all()
        for t in self._threads:
            t.join(timeout=5)

    def status(self) -> dict:
        now = self.clock()
        with self.cond:
            return {
                "workers": self.workers,
                "watchedCourses": sum(1 for c in self.courses.values() if c.wanted_until > now),
                "pendingCourses": sum(1 for c in self.courses.values()
                                      if c.wanted_until > now and self._stale_wanted(c, now)),
                **self.stats,
            }
