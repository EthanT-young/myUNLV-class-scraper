"""Offline tests for the seat server (no requests to UNLV)."""

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from scraper.peoplesoft import SearchError
from server.app import RateLimiter, make_server
from server.seats import Catalog, SeatTracker
from tests.test_scraper import header, results_page, row


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def write_site(root: Path):
    data = root / "data"
    data.mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html><title>t</title>")
    (data / "terms.json").write_text(json.dumps({"terms": [{"term": "2272", "name": "2027 Spring"}]}))
    courses = [
        {"subject": "CS", "catalog": "135", "sections": [{"classNbr": "100"}, {"classNbr": "101"}]},
        {"subject": "MATH", "catalog": "181", "sections": [{"classNbr": "200"}]},
    ]
    (data / "2272.json").write_text(json.dumps({"term": "2272", "courses": courses}))
    return data


DETAIL = ("<span id='SSR_CLS_DTL_WRK_ENRL_CAP'>30</span><span id='SSR_CLS_DTL_WRK_ENRL_TOT'>18</span>"
          "<span id='SSR_CLS_DTL_WRK_AVAILABLE_SEATS'>12</span><span id='SSR_CLS_DTL_WRK_WAIT_CAP'>10</span>"
          "<span id='SSR_CLS_DTL_WRK_WAIT_TOT'>0</span>")


class FakeSession:
    def __init__(self, fail_first=False):
        self.term = None
        self.searches = []
        self.details = []
        self.fail_first = fail_first
        self.reopens = 0

    def reopen(self):
        self.reopens += 1

    def search(self, subject, **extra):
        if self.fail_first:
            self.fail_first = False
            raise SearchError("session expired")
        self.searches.append(subject)
        if subject == "CS":
            return results_page(header(0, "CS 135 - Intro") + row(0, "100") + row(1, "101", "Closed"), 2)
        return results_page(header(0, "MATH 181 - Calc") + row(0, "200"), 1)

    def detail(self, r):
        self.details.append(r)
        return DETAIL

    def back_to_criteria(self):
        pass


class TrackerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.catalog = Catalog(write_site(Path(self.tmp.name)))
        self.assertTrue(self.catalog.refresh())
        self.clock = Clock()
        self.tracker = SeatTracker(self.catalog, ttl=120, want_window=180, clock=self.clock, wall=lambda: 5.0)

    def tearDown(self):
        self.tmp.cleanup()

    def test_unknown_class_numbers_are_ignored(self):
        self.assertEqual(self.tracker.want("2272", ["100", "999", "200"]), ["100", "200"])
        self.assertEqual(self.tracker.want("2268", ["100"]), [])

    def test_nothing_fetched_without_viewers(self):
        self.assertIsNone(self.tracker.next_job())

    def test_most_viewed_course_goes_first(self):
        self.tracker.want("2272", ["200"])
        for _ in range(3):
            self.tracker.want("2272", ["100"])
        course, nbrs = self.tracker.next_job()
        self.assertEqual((course.subject, nbrs), ("CS", ["100"]))

    def test_refresh_fills_status_and_seats_then_waits_for_ttl(self):
        self.tracker.want("2272", ["100", "101"])
        course, nbrs = self.tracker.next_job()
        fake = FakeSession()
        self.tracker.refresh_course(fake, course, nbrs)
        got = self.tracker.get("2272", ["100", "101"])
        self.assertEqual(got["100"]["available"], 12)
        self.assertEqual(got["101"]["status"], "Closed")
        self.assertEqual(sorted(fake.details), [0, 1])
        self.assertIsNone(self.tracker.next_job())          # fresh
        self.clock.t += 121
        self.tracker.want("2272", ["100"])
        self.assertIsNotNone(self.tracker.next_job())       # stale again

    def test_demand_expires(self):
        self.tracker.want("2272", ["100"])
        self.clock.t += 181
        self.assertIsNone(self.tracker.next_job())

    def test_worker_recovers_from_expired_session(self):
        fake = FakeSession(fail_first=True)
        tracker = SeatTracker(self.catalog, workers=1, session_factory=lambda: fake)
        tracker.want("2272", ["200"])
        tracker.start()
        try:
            for _ in range(50):
                if tracker.get("2272", ["200"]).get("200", {}).get("available") is not None:
                    break
                threading.Event().wait(0.05)
            self.assertEqual(tracker.get("2272", ["200"])["200"]["available"], 12)
            self.assertGreaterEqual(fake.reopens, 2)
        finally:
            tracker.stop()


class RateLimiterTests(unittest.TestCase):
    def test_burst_then_refill(self):
        clock = Clock()
        rl = RateLimiter(rate_per_min=60, burst=3, clock=clock)
        self.assertEqual([rl.allow("a") for _ in range(4)], [True, True, True, False])
        self.assertTrue(rl.allow("b"))
        clock.t += 1
        self.assertTrue(rl.allow("a"))


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        site = Path(cls.tmp.name)
        catalog = Catalog(write_site(site))
        catalog.refresh()
        tracker = SeatTracker(catalog)  # workers not started: API only
        cls.server = make_server(site, "127.0.0.1", 0, tracker, catalog, RateLimiter(rate_per_min=600, burst=50))
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.tmp.cleanup()

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base + path) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    def test_seats(self):
        status, headers, body = self.get("/api/seats?term=2272&classes=100,abc,999")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["seats"], {})
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_bad_input(self):
        self.assertEqual(self.get("/api/seats?term=../x&classes=100")[0], 400)
        self.assertEqual(self.get("/api/seats?term=2272&classes=")[0], 400)
        many = ",".join(str(i) for i in range(81))
        self.assertEqual(self.get(f"/api/seats?term=2272&classes={many}")[0], 400)
        self.assertEqual(self.get("/api/nope")[0], 404)

    def test_static_files_and_hardening(self):
        status, headers, _ = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(self.get("/data/")[0], 404)            # no directory listing
        self.assertEqual(self.get("/.git/config")[0], 404)       # no dotfiles
        self.assertEqual(self.get("/../../etc/passwd")[0], 404)  # no traversal

    def test_health(self):
        status, _, body = self.get("/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["terms"], ["2272"])


if __name__ == "__main__":
    unittest.main()


class UpdaterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "data"

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def tarball(files: dict, extra=None):
        import io, tarfile
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for name, body in files.items():
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
            for info in extra or []:
                tar.addfile(info)
        return buf.getvalue()

    def good(self):
        return {"terms.json": {"terms": [{"term": "2272"}]}, "2272.json": {"term": "2272", "courses": []}}

    def test_installs_valid_data(self):
        from server.updater import DataUpdater
        called = []
        DataUpdater(self.dir, "o/r", on_update=lambda: called.append(1)).install(self.tarball(self.good()))
        self.assertEqual(json.loads((self.dir / "terms.json").read_text())["terms"][0]["term"], "2272")
        self.assertEqual(called, [1])

    def test_rejects_incomplete_data_and_keeps_old(self):
        from server.updater import DataUpdater
        up = DataUpdater(self.dir, "o/r")
        up.install(self.tarball(self.good()))
        with self.assertRaises(RuntimeError):
            up.install(self.tarball({"terms.json": {"terms": [{"term": "2268"}]}}))
        with self.assertRaises(Exception):
            up.install(self.tarball({"terms.json": b"{not json", "2272.json": b"{}"}))
        self.assertEqual(json.loads((self.dir / "terms.json").read_text())["terms"][0]["term"], "2272")

    def test_ignores_unsafe_entries(self):
        import tarfile
        from server.updater import DataUpdater
        link = tarfile.TarInfo("evil.json")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        files = {**self.good(), "../escape.json": {"x": 1}, "sub/dir.json": {"x": 1}}
        DataUpdater(self.dir, "o/r").install(self.tarball(files, extra=[link]))
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["2272.json", "terms.json"])
        self.assertFalse((Path(self.tmp.name) / "escape.json").exists())

    def test_check_skips_unchanged_release(self):
        import io
        from unittest import mock
        from server.updater import DataUpdater
        up = DataUpdater(self.dir, "o/r")
        blob = self.tarball(self.good())
        release = {"assets": [{"name": "data.tar.gz", "id": 7, "size": len(blob), "updated_at": "x"}]}
        downloads = []

        class Resp(io.BytesIO):
            headers = {"ETag": '"abc"'}
            def __enter__(self): return self
            def __exit__(self, *a): pass

        def fake(url, accept, etag=None):
            if "/assets/" in url:
                downloads.append(url)
                return Resp(blob)
            return Resp(json.dumps(release).encode())

        with mock.patch.object(up, "_request", side_effect=fake):
            self.assertTrue(up.check())
            self.assertFalse(up.check())   # same asset id: no second download
        self.assertEqual(len(downloads), 1)
        self.assertEqual(DataUpdater(self.dir, "o/r").installed_id, 7)  # remembered across restarts

    def test_bad_repo_name(self):
        from server.updater import DataUpdater
        with self.assertRaises(ValueError):
            DataUpdater(self.dir, "../../x")
