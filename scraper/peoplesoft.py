"""Client for UNLV's public (guest) PeopleSoft class search.

The page is a stateful PeopleSoft form: every request posts back the hidden
fields of the previous response (ICSID, ICStateNum, ...) plus an ICAction
naming the button that was "clicked". No login is involved.
"""

from __future__ import annotations

import html
import re
import time

import requests

SEARCH_URL = (
    "https://my.unlv.nevada.edu/psc/lvporprd/EMPLOYEE/SA/c/"
    "COMMUNITY_ACCESS.CLASS_SEARCH.GBL"
)
INSTITUTION = "UNLV1"
USER_AGENT = "unlv-class-browser/0.1 (student project; python-requests)"

# Search-form field names
F_INST = "CLASS_SRCH_WRK2_INSTITUTION$31$"
F_TERM = "CLASS_SRCH_WRK2_STRM$35$"
F_SUBJECT = "SSR_CLSRCH_WRK_SUBJECT$0"
F_CATALOG_OP = "SSR_CLSRCH_WRK_SSR_EXACT_MATCH1$1"
F_CATALOG = "SSR_CLSRCH_WRK_CATALOG_NBR$1"
F_CAREER = "SSR_CLSRCH_WRK_ACAD_CAREER$2"
F_OPEN_ONLY = "SSR_CLSRCH_WRK_SSR_OPEN_ONLY$chk$3"
F_COMPONENT = "SSR_CLSRCH_WRK_SSR_COMPONENT$11"
F_MODE = "SSR_CLSRCH_WRK_INSTRUCTION_MODE$13"
MSG_ID = "DERIVED_CLSMSG_ERROR_TEXT"

# Dropdowns used to split a search that exceeds the 300-section limit.
# Their values are read from the live form, so new careers/components/modes
# are never skipped.
SPLIT_FIELDS = [F_CAREER, F_COMPONENT, F_MODE]

RETRY_STATUS = {429, 500, 502, 503, 504}
CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


class SearchError(RuntimeError):
    pass


class TooManyResults(SearchError):
    pass


def text(fragment: str) -> str:
    """HTML fragment -> plain text, keeping <br> as newlines."""
    fragment = re.sub(r"<br\s*/?>", "\n", fragment, flags=re.I)
    fragment = re.sub(r"<[^>]+>", "", fragment)
    fragment = CONTROL_CHARS.sub("", html.unescape(fragment))
    lines = [re.sub(r"[ \t\xa0]+", " ", ln).strip() for ln in fragment.split("\n")]
    return "\n".join(ln for ln in lines if ln)


def span(page: str, el_id: str) -> str | None:
    """Text of the <span id=el_id> element (nested spans included), or None."""
    m = re.search(r"<span\b[^>]*\sid=(['\"])%s\1[^>]*>" % re.escape(el_id), page, flags=re.I)
    if not m:
        return None
    depth, pos = 1, m.end()
    tag = re.compile(r"<(/?)span\b[^>]*>", re.I)
    while depth:
        t = tag.search(page, pos)
        if not t:
            return text(page[m.end():])
        depth += -1 if t.group(1) else 1
        pos = t.end()
    return text(page[m.end():t.start()])


def hidden_fields(page: str) -> dict[str, str]:
    fields = {}
    for tag in re.findall(r"<input\b[^>]*type=['\"]hidden['\"][^>]*>", page, flags=re.I):
        name = re.search(r"name=(['\"])(.*?)\1", tag)
        value = re.search(r"value=(['\"])(.*?)\1", tag, flags=re.S)
        if name:
            fields[name.group(2)] = html.unescape(value.group(2)) if value else ""
    return fields


def select_options(page: str, el_id: str) -> list[tuple[str, str]]:
    m = re.search(r"<select[^>]*id=(['\"])%s\1[^>]*>(.*?)</select>" % re.escape(el_id),
                  page, flags=re.S | re.I)
    if not m:
        return []
    return [(v, text(label))
            for _, v, label in re.findall(r"<option[^>]*value=(['\"])(.*?)\1[^>]*>([^<]*)", m.group(2))
            if v]


class ClassSearch:
    def __init__(self, delay: float = 1.0, timeout: float = 60, retries: int = 4):
        self.delay = delay
        self.timeout = timeout
        self.retries = retries
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.page = ""
        self.term = None
        self.split_values: dict[str, list[str]] = {}
        self._last = 0.0

    # -- low level -------------------------------------------------------
    def _wait(self):
        # The gap is measured from the end of the previous response, so slow
        # responses never shrink the pause below `delay`.
        pause = self.delay - (time.monotonic() - self._last)
        if pause > 0:
            time.sleep(pause)

    def _request(self, method: str, data=None) -> str:
        for attempt in range(self.retries + 1):
            self._wait()
            try:
                r = self.session.request(method, SEARCH_URL, data=data, timeout=self.timeout)
            except (requests.ConnectionError, requests.Timeout):
                if attempt == self.retries:
                    raise
                wait = 2 ** attempt * 10
            else:
                self._last = time.monotonic()
                if r.status_code not in RETRY_STATUS or attempt == self.retries:
                    r.raise_for_status()
                    self.page = r.text
                    return self.page
                retry_after = r.headers.get("Retry-After", "")
                wait = int(retry_after) if retry_after.isdigit() else 2 ** attempt * 10
            time.sleep(min(wait, 600))
            self._last = time.monotonic()
        raise SearchError("unreachable")

    def _get(self):
        return self._request("GET")

    def _post(self, action: str, **fields: str) -> str:
        data = hidden_fields(self.page)
        data["ICAction"] = action
        data.update(fields)
        return self._request("POST", data)

    # -- navigation ------------------------------------------------------
    def open(self, term: str | None = None):
        """Start a fresh guest session; optionally pick a term."""
        self.session.cookies.clear()
        self._get()
        if F_TERM not in self.page:
            self._get()  # first hit only sets cookies
        if F_TERM not in self.page:
            raise SearchError("Class search form did not load")
        if term:
            self._post(F_TERM, **{F_INST: INSTITUTION, F_TERM: term})
            self.term = term
            self._learn_split_values()

    def reopen(self, attempts: int = 4):
        """open() that survives a temporary outage (backs off up to ~20 min)."""
        for i in range(attempts):
            try:
                return self.open(self.term)
            except (requests.RequestException, SearchError):
                if i == attempts - 1:
                    raise
                time.sleep(30 * 3 ** i)

    def _learn_split_values(self):
        for field in SPLIT_FIELDS:
            values = [v for v, _ in select_options(self.page, field)]
            if values:
                self.split_values[field] = values

    def terms(self) -> list[tuple[str, str]]:
        if F_TERM not in self.page:
            self.open()
        return select_options(self.page, F_TERM)

    def subjects(self) -> dict[str, str]:
        """All subject codes -> names, via the 'select subject' lookup."""
        self.open(self.term)
        self._post("CLASS_SRCH_WRK2_SSR_PB_SUBJ_SRCH$0")
        found: dict[str, str] = {}
        for ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789":
            action = f"SSR_CLSRCH_WRK2_SSR_ALPHANUM_{ch}"
            if action not in self.page:
                continue
            self._post(action)
            i = 0
            while True:
                code = span(self.page, f"SSR_CLSRCH_SUBJ_SUBJECT${i}")
                if code is None:
                    break
                found[code] = span(self.page, f"SUBJECT_TBL_DESCRFORMAL${i}") or code
                i += 1
        self.open(self.term)  # leave the lookup page
        return found

    def search(self, subject: str, **extra: str) -> str:
        """Run one search from the criteria page; returns the results page.

        Returns "" only when PeopleSoft says there are no matches. Raises
        TooManyResults (>300 sections) or SearchError for anything else, so a
        failure is never mistaken for an empty subject. Leaves the session on
        the results page so details can be opened.
        """
        if "CLASS_SRCH_WRK2_SSR_PB_CLASS_SRCH" not in self.page:
            self.open(self.term)
        fields = {
            F_INST: INSTITUTION, F_TERM: self.term, F_SUBJECT: subject,
            F_CATALOG_OP: "G", F_CATALOG: "0", F_OPEN_ONLY: "N",
            F_CAREER: "", F_COMPONENT: "", F_MODE: "",
        }
        fields.update(extra)
        page = self._post("CLASS_SRCH_WRK2_SSR_PB_CLASS_SRCH", **fields)
        if "#ICSave" in page and "class section(s) found" not in page and MSG_ID not in page:
            page = self._post("#ICSave")  # "more than 50 sections, continue?" confirmation
        if re.search(r"\d+ class section\(s\) found", page):
            return page
        msg = span(page, MSG_ID) or ""
        if "no results" in msg.lower():
            return ""
        if "exceed the maximum" in msg.lower():
            raise TooManyResults(subject)
        raise SearchError(f"Unexpected response searching {subject} {extra}: {msg or 'no message'}")

    def back_to_criteria(self):
        if "CLASS_SRCH_WRK2_SSR_PB_MODIFY" in self.page:
            self._post("CLASS_SRCH_WRK2_SSR_PB_MODIFY")
        else:
            self.open(self.term)

    def detail(self, row: int) -> str:
        """Open section `row` of the current results; returns detail page and
        navigates back to the results."""
        page = self._post(f"MTG_CLASS_NBR${row}")
        if "SSR_CLS_DTL_WRK_CLASS_NBR" not in page:
            raise SearchError(f"Detail page for row {row} did not load")
        self._post("CLASS_SRCH_WRK2_SSR_PB_BACK")
        if "MTG_CLASS_NBR$" not in self.page:
            raise SearchError("Did not return to results after detail")
        return page
