#!/usr/bin/env python3
"""
Scrape the Ridgewood YMCA "Lap + Family Swim" schedule from
https://www.ridgewoodymca.org/classes/40

The public page is rendered client-side by a Vue.js app (ReClique platform),
but the same platform serves a printer-friendly view that is rendered on the
server and needs no authentication, so a plain HTTP GET is enough:

    /classes/printer_friendly/?ClassCategoryID=40&date=YYYY-MM-DD

That view returns the whole Sun-Sat week containing `date`, as a table with one
column per day and the recurrence already expanded -- which is the part worth
having, since the app's own JSON endpoint returns raw repeat rules and leaves
the expansion to the client.  One request therefore answers seven days.

Every fetched week is cached as JSON under schedule_cache/, one file per
schedule day.  A day is served from that cache when its file was written today,
so the first request of the day costs one fetch and the rest are free.  Cache
files for days before today are pruned on every run.

Requirements:
    none for --json; only the default text grid needs `pip install tabulate`.
    Keeping the fetch and parse dependency-free lets this run in a bare sandbox
    after nothing more than a git clone.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Final
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

# The server-rendered view of the same schedule, which needs no browser.
PRINTER_URL: Final[str] = "https://www.ridgewoodymca.org/classes/printer_friendly/"

# "LAP + FAMILY SWIM"; the same category id the Vue page uses.
CLASS_CATEGORY_ID: Final[str] = "40"

# The site sits behind Cloudflare, which is happier with a browser-ish agent
# than with the urllib default.
USER_AGENT: Final[str] = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

REQUEST_TIMEOUT: Final[int] = 30

# One JSON file per schedule day; a fetched week writes seven of them, and any
# day is answered from here while its file is still from today.
CACHE_DIR: Final[Path] = Path(__file__).resolve().parent / "schedule_cache"

# date.weekday() numbering: Monday is 0, Sunday is 6.
WEEKDAY_NUMBERS: Final[dict[str, int]] = {
    name.lower(): number
    for number, full in enumerate(
        ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    )
    for name in (full, full[:3])
}

RELATIVE_DAY_OFFSETS: Final[dict[str, int]] = {
    "yesterday": -1,
    "today": 0,
    "tomorrow": 1,
}

START_TIME_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(\d{1,2}):(\d{2})\s*([ap]m)", re.IGNORECASE
)


def start_time_minutes(time_range: str) -> int:
    """Return the start time of a range like '05:30 am - 11:30 am' as minutes since midnight."""
    m = START_TIME_PATTERN.search(time_range)
    if not m:
        return 0
    hour, minute, period = int(m.group(1)), int(m.group(2)), m.group(3).lower()
    if period == "am":
        hour = hour % 12  # 12:xx am → 0:xx
    else:
        hour = hour % 12 + 12  # 12:xx pm → 12:xx, 1:xx pm → 13:xx
    return hour * 60 + minute

HEADERS: Final[list[str]] = ["Name", "Time", "Pool"]


@dataclass
class SwimSession:
    """A single Lap Swim or Family Swim session."""

    name: str
    time: str
    pool: str

    def to_row(self) -> list[str]:
        """Return field values in display order matching HEADERS."""
        return [self.name, self.time, self.pool]


def cache_path(day: date) -> Path:
    """Return the cache file for a given schedule day."""
    return CACHE_DIR / f"schedule-{day.isoformat()}.json"


def load_cached_schedule(day: date) -> list[SwimSession] | None:
    """
    Return the cached sessions for a day, or None if absent, stale or unreadable.

    A file counts as stale once the calendar day turns over, so the schedule is
    re-fetched at most once a day and edits made at the Y are picked up then.
    """
    path: Path = cache_path(day)
    if not path.exists():
        return None
    if date.fromtimestamp(path.stat().st_mtime) != date.today():
        return None
    try:
        data = json.loads(path.read_text())
        return [SwimSession(**item) for item in data]
    except (json.JSONDecodeError, TypeError, OSError):
        return None


def save_cached_schedule(day: date, entries: list[SwimSession]) -> None:
    """Write the scraped sessions for a day to the cache."""
    CACHE_DIR.mkdir(exist_ok=True)
    cache_path(day).write_text(
        json.dumps([asdict(entry) for entry in entries], indent=2)
    )


def save_cached_week(week: dict[date, list[SwimSession]]) -> None:
    """
    Cache the still-useful days of a fetched week, empty days included.

    Days already past are skipped rather than written for prune_cache to delete
    on the next run.
    """
    today: date = date.today()
    for day, entries in week.items():
        if day >= today:
            save_cached_schedule(day, entries)


def prune_cache(today: date) -> None:
    """Delete cache files for days before today; yesterday's schedule is dead weight."""
    for path in CACHE_DIR.glob("schedule-*.json"):
        try:
            file_day: date = date.fromisoformat(path.stem.removeprefix("schedule-"))
        except ValueError:
            continue
        if file_day < today:
            path.unlink(missing_ok=True)


class ScheduleUnavailable(RuntimeError):
    """The schedule could not be fetched or made sense of."""


# Elements that never open a scope; skipping them keeps the parser's depth
# bookkeeping honest (the day headers contain a bare <br>).
VOID_TAGS: Final[frozenset[str]] = frozenset(
    {
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    }
)

# Class name inside a session item -> the SwimSession field it carries.
ITEM_FIELDS: Final[dict[str, str]] = {
    "label": "name",
    "space": "pool",
    "c-time": "start",
    "end_time": "end",
}


class WeekGridParser(HTMLParser):
    """
    Pull the session items out of the printer-friendly week grid.

    The grid is a plain table: a "Start Time" column followed by one column per
    weekday, with each session a `div.item` carrying its fields in known class
    names.  Cells line up one-to-one with the header columns -- the table uses
    no colspan or rowspan -- so a running cell index identifies the day.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.header_cells: list[str] = []
        self.items: list[dict[str, str]] = []
        self._in_table: bool = False
        self._in_head: bool = False
        self._in_header_cell: bool = False
        self._header_text: str = ""
        self._column: int = -1
        self._item: dict[str, str] | None = None
        self._depth: int = 0
        # One entry per open element inside an item: the field its text feeds,
        # or None.  Nesting is why this is a stack -- end_time sits inside a
        # wrapper span that is itself inside the div holding the start time.
        self._fields: list[str | None] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in VOID_TAGS:
            return
        attributes: dict[str, str | None] = dict(attrs)
        classes: list[str] = (attributes.get("class") or "").split()

        if tag == "table":
            if attributes.get("id") == "week_classes":
                self._in_table = True
            return
        if not self._in_table:
            return

        if tag == "thead":
            self._in_head = True
        elif tag == "tr":
            self._column = -1
        elif tag in ("td", "th"):
            if self._in_head:
                self._in_header_cell = True
                self._header_text = ""
            else:
                self._column += 1
        elif self._item is not None:
            self._depth += 1
            self._fields.append(
                next((ITEM_FIELDS[name] for name in classes if name in ITEM_FIELDS), None)
            )
        elif "item" in classes:
            self._item = {"column": str(self._column), "name": "", "pool": "", "start": "", "end": ""}
            self._depth = 1
            self._fields = [None]

    def handle_endtag(self, tag: str) -> None:
        if tag in VOID_TAGS or not self._in_table:
            return
        if tag == "table":
            self._in_table = False
        elif tag == "thead":
            self._in_head = False
        elif tag in ("td", "th"):
            if self._in_header_cell:
                self._in_header_cell = False
                self.header_cells.append(" ".join(self._header_text.split()))
        elif self._item is not None:
            self._fields.pop()
            self._depth -= 1
            if self._depth == 0:
                self.items.append(self._item)
                self._item = None

    def handle_data(self, data: str) -> None:
        if self._in_header_cell:
            self._header_text += " " + data
        elif self._item is not None:
            field: str | None = next((f for f in reversed(self._fields) if f), None)
            if field:
                self._item[field] += data


def week_start(day: date) -> date:
    """Return the Sunday of the week containing a day, matching the grid."""
    return day - timedelta(days=(day.weekday() + 1) % 7)


def printer_url(day: date) -> str:
    """Return the printer-friendly URL for the week containing a day."""
    query: str = urlencode(
        {
            "BranchID": "",
            "ClassCategoryID": CLASS_CATEGORY_ID,
            "SpaceID": "0",
            "InstructorID": "0",
            "monthly": "",
            "date": day.isoformat(),
        }
    )
    return f"{PRINTER_URL}?{query}"


def normalize_time(raw: str) -> str:
    """
    Render a grid time as the '05:30 am' form used everywhere else.

    End times arrive as 'ends @ 9:30 AM', and neither form is zero-padded the
    way the display and the cache expect.
    """
    text: str = raw.strip().removeprefix("ends @").strip()
    try:
        return datetime.strptime(text.upper(), "%I:%M %p").strftime("%I:%M %p").lower()
    except ValueError:
        return text.lower()


def week_columns(day: date, header_cells: list[str]) -> dict[int, date]:
    """
    Map each day column of the grid to its date.

    The dates come from the requested day rather than the headers, which carry
    no year and so would be ambiguous across a New Year.  The headers are still
    checked against them: a mismatch means the grid is not the week we asked
    for, and returning some other week's schedule would be worse than failing.
    """
    sunday: date = week_start(day)
    columns: dict[int, date] = {}
    for offset in range(7):
        column: int = offset + 1  # column 0 is the "Start Time" gutter
        column_day: date = sunday + timedelta(days=offset)
        stamp: str = f"{column_day.month:02d}/{column_day.day:02d}"
        if column >= len(header_cells) or stamp not in header_cells[column]:
            raise ScheduleUnavailable(
                f"schedule grid does not cover {column_day.isoformat()}; "
                f"its columns are {header_cells[1:] or header_cells}"
            )
        columns[column] = column_day
    return columns


def parse_week(html_text: str, day: date) -> dict[date, list[SwimSession]]:
    """Parse a printer-friendly page into sessions keyed by day."""
    parser: WeekGridParser = WeekGridParser()
    parser.feed(html_text)

    if not parser.header_cells:
        raise ScheduleUnavailable("no schedule grid found on the printer-friendly page")

    columns: dict[int, date] = week_columns(day, parser.header_cells)
    week: dict[date, list[SwimSession]] = {value: [] for value in columns.values()}

    for item in parser.items:
        column_day: date | None = columns.get(int(item["column"]))
        if column_day is None:
            continue
        start: str = normalize_time(item["start"])
        end: str = normalize_time(item["end"])
        week[column_day].append(
            SwimSession(
                name=" ".join(item["name"].split()),
                time=f"{start} - {end}" if end else start,
                pool=" ".join(item["pool"].split()),
            )
        )
    return week


def fetch_week(day: date) -> dict[date, list[SwimSession]]:
    """Fetch and parse the week containing a day from the printer-friendly view."""
    request: Request = Request(printer_url(day), headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            html_text: str = response.read().decode("utf-8", errors="replace")
    except (URLError, TimeoutError, OSError) as exc:
        raise ScheduleUnavailable(f"could not fetch the schedule: {exc}") from exc

    return parse_week(html_text, day)


def resolve_day(spec: str) -> date:
    """
    Resolve a --day argument to a calendar date.

    Accepts 'today' / 'tomorrow' / 'yesterday', a weekday name or 3-letter
    abbreviation, or an ISO date (YYYY-MM-DD).  A weekday names the next such
    day, counting today as itself -- asking for 'friday' on a Friday means today.
    """
    token: Final[str] = spec.strip().lower()
    today: Final[date] = date.today()

    if token in RELATIVE_DAY_OFFSETS:
        return today + timedelta(days=RELATIVE_DAY_OFFSETS[token])

    if token in WEEKDAY_NUMBERS:
        ahead: int = (WEEKDAY_NUMBERS[token] - today.weekday()) % 7
        return today + timedelta(days=ahead)

    try:
        return date.fromisoformat(token)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"{spec!r} is not a day: use today/tomorrow/yesterday, "
            f"a weekday such as 'saturday' or 'sat', or an ISO date such as 2026-08-15"
        ) from None


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="Fetch the Ridgewood YMCA Lap + Family Swim schedule.",
    )
    parser.add_argument(
        "--day",
        type=resolve_day,
        default=date.today(),
        metavar="DAY",
        help=(
            "Day to fetch: today (default), tomorrow, yesterday, a weekday "
            "such as saturday or sat, or an ISO date such as 2026-08-15."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help=(
            "Output the sessions as JSON on stdout, for another program to "
            "consume.  Needs no third-party packages."
        ),
    )
    return parser.parse_args()


def main() -> None:
    args: argparse.Namespace = parse_args()
    day: date = args.day
    day_label: str = day.strftime("%A, %B %-d %Y")

    prune_cache(date.today())

    entries: list[SwimSession]
    source: str

    cached: list[SwimSession] | None = load_cached_schedule(day)
    if cached is not None:
        entries, source = cached, f"cache ({cache_path(day)})"
    else:
        try:
            week: dict[date, list[SwimSession]] = fetch_week(day)
        except ScheduleUnavailable as exc:
            print(f"Could not get the schedule for {day_label}: {exc}", file=sys.stderr)
            raise SystemExit(1) from exc
        # The whole week is written, so the next six days cost no request.
        save_cached_week(week)
        entries, source = week[day], printer_url(day)

    # Sort by start time
    entries.sort(key=lambda e: start_time_minutes(e.time))

    if args.json:
        # The day is echoed back so a consumer can check it got the day it
        # asked for rather than trusting the ordering of a pipeline.
        print(
            json.dumps(
                {
                    "day": day.isoformat(),
                    "sessions": [asdict(entry) for entry in entries],
                },
                indent=2,
            )
        )
        return

    if not entries:
        print(f"No Lap + Family Swim classes found for {day_label}.")
        return

    try:
        from tabulate import tabulate
    except ImportError as exc:
        pip: Path = Path(sys.executable).parent / "pip"
        print(
            f"the text table needs tabulate: {pip} install tabulate\n"
            f"(--json needs no packages)",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc

    print(f"Lap + Family Swim schedule for {day_label}")
    print(f"Source: {source}\n")
    table_data: list[list[str]] = [entry.to_row() for entry in entries]
    print(tabulate(table_data, headers=HEADERS, tablefmt="grid"))
    print(f"\n{len(entries)} sessions")


if __name__ == "__main__":
    main()
