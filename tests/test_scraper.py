"""Offline tests for the scraper (no network). Run: python3 -m unittest discover -s tests"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scraper import __main__ as cli
from scraper import peoplesoft as ps
from scraper.parse import parse_daytime, parse_results


def header(i, text):
    return f"<DIV    id='win0divSSR_CLSRSLT_WRK_GROUPBOX2GP${i}'><a class='x'></a>&nbsp;{text}</DIV>"


def row(i, nbr, status="Open", img=True):
    alt = f'<img src="x.gif" alt="{status}">' if img else ""
    return (f"<a name='MTG_CLASS_NBR${i}' id='MTG_CLASS_NBR${i}' href='#'>{nbr}</a>"
            f"<a name='MTG_CLASSNAME${i}' id='MTG_CLASSNAME${i}'>100{i}-LEC<br />Regular</a>"
            f"<span  class='PSLONGEDITBOX' id='MTG_DAYTIME${i}' >MoWe 1:00PM - 2:15PM</span>"
            f"<span  class='PSLONGEDITBOX' id='MTG_ROOM${i}' >TBA</span>"
            f"<span  class='PSLONGEDITBOX' id='MTG_INSTR${i}' >Staff</span>"
            f"<span  class='PSLONGEDITBOX' id='MTG_TOPIC${i}' >01/19/2027 - 05/08/2027</span>"
            f"<td><DIV id='win0divDERIVED_CLSRCH_SSR_STATUS_LONG${i}'><div>{alt}</div></DIV></td>")


def results_page(rows_html, count):
    return f"<td>{count} class section(s) found</td>" + rows_html


class ParseTests(unittest.TestCase):
    def test_sections_attach_to_their_course(self):
        page = results_page(header(0, "MATH 181 - Calc I") + row(0, "111")
                            + header(1, "MATH 182 - Calc II") + row(1, "222"), 2)
        secs = parse_results(page)
        self.assertEqual([(s["classNbr"], s["catalog"]) for s in secs], [("111", "181"), ("222", "182")])

    def test_count_mismatch_raises(self):
        page = results_page(header(0, "MATH 181 - Calc I") + row(0, "111"), 2)
        with self.assertRaises(ps.SearchError):
            parse_results(page)

    def test_missing_class_number_raises(self):
        page = results_page(header(0, "MATH 181 - Calc I") + row(0, "abc"), 1)
        with self.assertRaises(ps.SearchError):
            parse_results(page)

    def test_status_does_not_leak_from_next_row(self):
        page = results_page(header(0, "CS 1 - X") + row(0, "1", img=False) + row(1, "2", "Closed"), 2)
        self.assertEqual([s["status"] for s in parse_results(page)], ["Unknown", "Closed"])

    def test_times(self):
        self.assertEqual(parse_daytime("Fr 10:00PM - 12:30AM")["minutes"], 150)
        self.assertIsNone(parse_daytime("Mo 13:00PM - 2:00PM")["start"])
        self.assertEqual(parse_daytime("TuTh 12:00PM - 1:15PM")["start"], "12:00")

    def test_span_handles_nested_spans(self):
        self.assertEqual(ps.span("<span id='X'>Intro <span>bold</span> rest</span>", "X"), "Intro bold rest")

    def test_hidden_field_quotes(self):
        f = ps.hidden_fields("""<input type='hidden' name='A' value="O'Brien">""")
        self.assertEqual(f["A"], "O'Brien")


class SearchTests(unittest.TestCase):
    def make(self, page):
        cs = ps.ClassSearch(delay=0)
        cs.term = "2272"
        cs.page = "CLASS_SRCH_WRK2_SSR_PB_CLASS_SRCH"
        cs._post = mock.Mock(return_value=page)
        return cs

    def msg(self, text):
        return f"CLASS_SRCH_WRK2_SSR_PB_CLASS_SRCH <span class='PSPAGE' id='{ps.MSG_ID}' >{text}</span>"

    def test_no_results(self):
        self.assertEqual(self.make(self.msg("The search returns no results that match the criteria specified.")).search("ACA"), "")

    def test_too_many(self):
        with self.assertRaises(ps.TooManyResults):
            self.make(self.msg("Your search will exceed the maximum limit of 300 sections.")).search("ENG")

    def test_criteria_page_without_message_is_an_error(self):
        with self.assertRaises(ps.SearchError):
            self.make("CLASS_SRCH_WRK2_SSR_PB_CLASS_SRCH Select at least 2 search criteria.").search("CS")


class FakeCS:
    """Stands in for ClassSearch: one subject, three sections, configurable failures."""

    def __init__(self, fail_detail_rows=(), fail_search=()):
        self.term = None
        self.split_values = {}
        self.fail_detail_rows = set(fail_detail_rows)
        self.fail_search = set(fail_search)
        self.calls = {"search": 0, "detail": 0, "reopen": 0}

    def open(self, term=None):
        self.term = term

    def reopen(self):
        self.calls["reopen"] += 1

    def terms(self):
        return [("2272", "2027 Spring"), ("../evil", "Evil")]

    def subjects(self):
        return {"CS": "Computer Science", "MATH": "Mathematics"}

    def search(self, subject, **extra):
        self.calls["search"] += 1
        if subject in self.fail_search:
            raise ps.SearchError("boom")
        h = header(0, f"{subject} 101 - Intro")
        base = 100 if subject == "CS" else 200
        return results_page(h + row(0, str(base)) + row(1, str(base + 1)) + row(2, str(base + 2)), 3)

    def detail(self, r):
        self.calls["detail"] += 1
        if r in self.fail_detail_rows:
            raise ps.SearchError("detail failed")
        return "<span id='SSR_CLS_DTL_WRK_UNITS_RANGE'>3 units</span>"

    def back_to_criteria(self):
        pass


class RunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.out, self.cache = root / "out", root / "cache"
        self.patches = [mock.patch.object(cli, "OUT_DIR", self.out),
                        mock.patch.object(cli, "CACHE_DIR", self.cache),
                        mock.patch.object(cli, "ROOT", root),
                        mock.patch.object(cli.time, "sleep", lambda s: None)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def run_main(self, fake, *args):
        with mock.patch.object(cli, "ClassSearch", lambda delay: fake):
            return cli.main(["--term", "2272", *args])

    def data(self):
        return json.loads((self.out / "2272.json").read_text())

    def test_full_run_writes_all_sections(self):
        self.assertEqual(self.run_main(FakeCS()), 0)
        self.assertEqual(cli.count_sections(self.data()), 6)

    def test_persistent_detail_failure_is_bounded(self):
        fake = FakeCS(fail_detail_rows={0, 1, 2})
        self.run_main(fake)
        self.assertLess(fake.calls["detail"], 20)
        self.assertLess(fake.calls["search"], 10)

    def test_failed_subject_keeps_previous_data(self):
        self.run_main(FakeCS())
        self.assertEqual(self.run_main(FakeCS(fail_search={"MATH"})), 1)
        nbrs = {s["classNbr"] for c in self.data()["courses"] for s in c["sections"]}
        self.assertEqual(nbrs, {"100", "101", "102", "200", "201", "202"})

    def test_failed_subject_in_partial_run_keeps_its_data(self):
        self.run_main(FakeCS())
        self.run_main(FakeCS(fail_search={"MATH"}), "--subjects", "MATH")
        self.assertEqual(cli.count_sections(self.data()), 6)

    def test_empty_subject_list_does_not_overwrite(self):
        self.run_main(FakeCS())
        before = (self.out / "2272.json").read_text()
        fake = FakeCS()
        fake.subjects = lambda: {}
        (self.cache / "subjects-2272.json").unlink()
        self.assertEqual(self.run_main(fake), 1)
        self.assertEqual((self.out / "2272.json").read_text(), before)

    def test_big_drop_is_refused_without_force(self):
        self.run_main(FakeCS())
        fake = FakeCS(fail_search={"CS", "MATH"})
        fake.subjects = lambda: {"CS": "x"}
        (self.cache / "subjects-2272.json").unlink()
        # CS fails -> its old data is kept; nothing is lost, so the write is allowed but flagged.
        self.assertEqual(self.run_main(fake), 1)
        self.assertEqual(cli.count_sections(self.data()), 6)

    def test_bad_term_rejected(self):
        with self.assertRaises(SystemExit):
            cli.main(["--term", "../../x"])

    def test_bad_subject_rejected(self):
        with self.assertRaises(SystemExit):
            cli.main(["--term", "2272", "--subjects", "../etc"])


if __name__ == "__main__":
    unittest.main()
