"""Turn PeopleSoft class-search HTML into plain dicts."""

from __future__ import annotations

import re
from datetime import datetime

from .peoplesoft import SearchError, span, text

DAY_CODES = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]


def _iso_date(s: str) -> str | None:
    try:
        return datetime.strptime(s.strip(), "%m/%d/%Y").date().isoformat()
    except ValueError:
        return None


def _to_24h(s: str) -> str:
    return datetime.strptime(s.strip().upper(), "%I:%M%p").strftime("%H:%M")


def _unparsed(s: str) -> dict:
    return {"days": [], "start": None, "end": None, "minutes": None, "raw": s.strip()}


def parse_daytime(s: str) -> dict:
    """'MoWe 11:30AM - 12:45PM' -> days, start, end, minutes."""
    m = re.match(r"^((?:Mo|Tu|We|Th|Fr|Sa|Su)+)\s+(\d{1,2}:\d{2}[AP]M)\s*-\s*(\d{1,2}:\d{2}[AP]M)$",
                 s.strip())
    if not m:
        return _unparsed(s)
    try:
        start, end = _to_24h(m.group(2)), _to_24h(m.group(3))
    except ValueError:  # e.g. "13:00PM"
        return _unparsed(s)
    sh, sm = map(int, start.split(":"))
    eh, em = map(int, end.split(":"))
    return {
        "days": re.findall(r"Mo|Tu|We|Th|Fr|Sa|Su", m.group(1)),
        "start": start, "end": end,
        "minutes": ((eh * 60 + em) - (sh * 60 + sm)) % 1440,  # overnight classes wrap
        "raw": s.strip(),
    }


def parse_dates(s: str) -> tuple[str | None, str | None]:
    parts = s.split("-")
    if len(parts) != 2:
        return None, None
    return _iso_date(parts[0]), _iso_date(parts[1])


def parse_results(page: str) -> list[dict]:
    """All sections on a results page, each tagged with its course.

    Raises SearchError if the number parsed differs from PeopleSoft's own
    "N class section(s) found" count, so markup changes fail loudly instead
    of silently dropping or misfiling sections.
    """
    sections = []
    course = None
    token = re.compile(
        r"id=['\"]win0divSSR_CLSRSLT_WRK_GROUPBOX2GP\$\d+['\"][^>]*>(?P<head>.*?)</div>"
        r"|<a\b[^>]*\bname=['\"]MTG_CLASS_NBR\$(?P<row>\d+)['\"]",
        re.S | re.I,
    )
    for m in token.finditer(page):
        if m.group("head") is not None:
            head = text(m.group("head"))
            code, _, title = head.partition(" - ")
            subject, _, catalog = code.strip().partition(" ")
            course = {"subject": subject, "catalog": catalog.strip(), "title": title.strip()}
            continue
        row = int(m.group("row"))
        if course is None:
            raise SearchError(f"Section row {row} appears before any course header")
        sec = parse_row(page, row)
        if not sec["classNbr"]:
            raise SearchError(f"Could not read class number for row {row}")
        sections.append({**course, **sec, "row": row})
    found = re.search(r"(\d+) class section\(s\) found", page)
    if found and int(found.group(1)) != len(sections):
        raise SearchError(f"Parsed {len(sections)} sections but page reports {found.group(1)}")
    return sections


def parse_row(page: str, i: int) -> dict:
    nbr = re.search(r"id='MTG_CLASS_NBR\$%d'[^>]*>(\d+)<" % i, page)
    name = re.search(r"id='MTG_CLASSNAME\$%d'[^>]*>(.*?)</a>" % i, page, re.S)
    name_lines = text(name.group(1)).split("\n") if name else [""]
    sec, _, component = name_lines[0].partition("-")
    # Bounded to this row's status cell so a missing icon can't borrow the next row's.
    status = re.search(
        r"id=['\"]win0divDERIVED_CLSRCH_SSR_STATUS_LONG\$%d['\"](?:(?!</td>).)*?alt=\"([^\"]+)\"" % i,
        page, re.S | re.I)

    daytimes = (span(page, f"MTG_DAYTIME${i}") or "").split("\n")
    rooms = (span(page, f"MTG_ROOM${i}") or "").split("\n")
    dates = (span(page, f"MTG_TOPIC${i}") or "").split("\n")
    instr_raw = span(page, f"MTG_INSTR${i}") or ""
    instr_lines = [ln.strip().rstrip(",") for ln in instr_raw.split("\n")]
    # Line-by-line pairing is only trustworthy when every column has one line per meeting.
    aligned = len(daytimes) == len(rooms) == len(dates) == len(instr_lines)

    meetings = []
    for j, dt in enumerate(daytimes):
        start, end = parse_dates(dates[j] if j < len(dates) else dates[-1])
        meetings.append({
            **parse_daytime(dt),
            "room": rooms[j] if len(rooms) == len(daytimes) else " ".join(rooms),
            "instructor": instr_lines[j] if aligned else None,
            "startDate": start, "endDate": end,
        })

    instructors = []
    for ln in instr_lines:
        for name_ in ln.split(","):
            name_ = name_.strip()
            if name_ and name_ not in instructors:
                instructors.append(name_)

    return {
        "classNbr": nbr.group(1) if nbr else None,
        "section": sec.strip(),
        "component": component.strip(),
        "session": name_lines[1] if len(name_lines) > 1 else "",
        "status": status.group(1) if status else "Unknown",
        "instructors": instructors,
        "meetings": meetings,
    }


def _int(s):
    try:
        return int(s)
    except (TypeError, ValueError):
        return None


def parse_detail(page: str) -> dict:
    """Fields from a class-detail page. Course-level and section-level are
    kept apart so course info can be cached once per course."""
    units = span(page, "SSR_CLS_DTL_WRK_UNITS_RANGE") or ""
    u = re.match(r"([\d.]+)(?:\s*-\s*([\d.]+))?", units)
    reqs = span(page, "SSR_CLS_DTL_WRK_SSR_REQUISITE_LONG")
    attrs = span(page, "SSR_CLS_DTL_WRK_SSR_CRSE_ATTR_LONG")
    course = {
        "unitsMin": float(u.group(1)) if u else None,
        "unitsMax": float(u.group(2) or u.group(1)) if u else None,
        "career": span(page, "PSXLATITEM_XLATLONGNAME"),
        "description": span(page, "DERIVED_CLSRCH_DESCRLONG"),
        "requirements": reqs,
        "attributes": [a for a in (attrs or "").split("\n") if a],
        "grading": span(page, "GRADE_BASIS_TBL_DESCRFORMAL"),
    }
    section = {
        "instructionMode": span(page, "INSTRUCT_MODE_DESCR"),
        "location": span(page, "CAMPUS_LOC_VW_DESCR"),
        "capacity": _int(span(page, "SSR_CLS_DTL_WRK_ENRL_CAP")),
        "enrolled": _int(span(page, "SSR_CLS_DTL_WRK_ENRL_TOT")),
        "waitCapacity": _int(span(page, "SSR_CLS_DTL_WRK_WAIT_CAP")),
        "waitTotal": _int(span(page, "SSR_CLS_DTL_WRK_WAIT_TOT")),
        "seatsAvailable": _int(span(page, "SSR_CLS_DTL_WRK_AVAILABLE_SEATS")),
        "notes": span(page, "DERIVED_CLSRCH_SSR_CLASSNOTE_LONG"),
    }
    return {"course": course, "section": section}
