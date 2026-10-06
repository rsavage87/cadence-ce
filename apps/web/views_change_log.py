"""
Users and access: the Change log tab (slice 20, part B). What changed in the facility, newest first, across the areas the reader's
role can view (apps.core.history.change_log: the records' own histories, and the access events apps.accounts.services writes): when,
who, the area, the record (a link where it opens), each field's before and after, and the reason a service gave.

Users View, and never a scoped user's (web_view refuses them: the log is the whole facility's, whatever their levels).

Filters, all in the address (the form swaps the list and pushes the URL): area (one of those the role can view), from and to (local
days, inclusive), and who (a person in the facility). A filter the reader cannot use (an area their role cannot view, someone outside
the facility, a day that is not one) is dropped, as the list screens drop theirs. The list shows PAGE entries; Show older adds the
next page below it (HTMX), or opens that page on its own without JavaScript. Its ?after= is the last entry shown
(apps.core.history.cursor_text), so changes saved meanwhile never repeat or skip entries on the next page. Export CSV writes what
the filters on screen give, newest first, at most CSV_LIMIT entries (apps.web.exports.csv_response); Print opens up to PRINT_LIMIT
of them on paper (web/print_base.html).
"""
import re
from dataclasses import dataclass
from datetime import date

from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Level, Module, User
from apps.core.history import ACCESS, change_log, parse_cursor, readable_areas

from .decorators import web_view
from .exports import csv_response
from .htmx import is_partial
from .views_users import tabs_context

PAGE = 50
CSV_LIMIT = 10_000
PRINT_LIMIT = 500
# The areas whose records open in the drawer, over the log; the rest (roles, settings, custom reports) open their own page.
DRAWER_AREAS = frozenset({"devices", "device_models", "work_orders", "labor", "parts", "contracts", "aem"})
ISO_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
CSV_COLUMNS = ["When", "Who", "Area", "Record", "Action", "What changed", "Reason"]


@dataclass
class LogFilters:
    area: str = ""
    since: date | None = None
    until: date | None = None
    who: int | None = None
    after: str | None = None  # the page's cursor: the last entry of the page before it

    def query(self) -> dict:
        """change_log's keyword arguments for these filters."""
        return {"areas": [self.area] if self.area else None, "since": self.since, "until": self.until, "who": self.who}

    @property
    def any(self) -> bool:
        return bool(self.area or self.since or self.until or self.who is not None)


def _day(value) -> date | None:
    value = (value or "").strip()
    if not ISO_DAY.match(value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _int(value) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def parse_log_filters(params, area_keys: set[str], people: set[int]) -> LogFilters:
    who = _int(params.get("who"))
    after = params.get("after") or None
    return LogFilters(area=params.get("area", "") if params.get("area", "") in area_keys else "", since=_day(params.get("from")),
                      until=_day(params.get("to")), who=who if who in people else None,
                      after=after if after and parse_cursor(after) is not None else None)


def _people(request) -> list[tuple[int, str]]:
    """Everyone with an account in the facility, deactivated ones too (their changes stay in the log). User is not tenant-scoped."""
    users = (User.objects.filter(tenant=request.tenant).order_by("first_name", "last_name", "email", "pk")
             .only("pk", "first_name", "last_name", "email", "username"))
    return [(u.pk, str(u)) for u in users]


def _filters(request):
    areas = readable_areas(request.user)
    people = _people(request)
    return parse_log_filters(request.GET, {a.key for a in areas}, {pk for pk, _ in people}), areas, people


def change_lines(entry) -> list[str]:
    """What changed in an entry as lines of words: "Room: 12 → 14" for a change, "Room: 12" for a value it was added with, and an
    access change's own words."""
    if entry.area == ACCESS.key:
        return [c.after for c in entry.changes]
    if entry.action == "changed":
        return [f"{c.field}: {c.before} → {c.after}" for c in entry.changes]
    return [f"{c.field}: {c.after}" for c in entry.changes]


def _filter_words(f: LogFilters, areas, people) -> str:
    """The filters in words, for the printed page: "Devices · from Oct 1, 2026 · by Kim Lee"."""
    words = [next((a.label for a in areas if a.key == f.area), "")] if f.area else ["Every area your role can view"]
    if f.since:
        words.append(f"from {f.since:%b} {f.since.day}, {f.since.year}")
    if f.until:
        words.append(f"to {f.until:%b} {f.until.day}, {f.until.year}")
    if f.who is not None:
        words.append("by " + next((name for pk, name in people if pk == f.who), ""))
    return " · ".join(words)


def _list_context(request) -> dict:
    f, areas, people = _filters(request)
    entries, following = change_log(request.user, **f.query(), limit=PAGE, after=f.after)
    return {"f": f, "areas": [(a.key, a.label) for a in areas], "people": people, "entries": entries, "more": following is not None,
            "next_after": following, "page_size": PAGE, "list_url": reverse("web:change_log"), "drawer_areas": DRAWER_AREAS}


@web_view(Module.USERS, Level.VIEW)
def change_log_view(request):
    ctx = _list_context(request)
    if is_partial(request, "log-more"):  # Show older: the next page's rows, under the ones on screen
        return render(request, "web/_change_log_rows.html", ctx)
    if is_partial(request, "log-body"):
        return render(request, "web/_change_log_body.html", ctx)
    return render(request, "web/change_log.html", {**tabs_context(request, "log"), **ctx})


@web_view(Module.USERS, Level.VIEW)
def change_log_csv(request):
    f, _areas, _people = _filters(request)
    entries, _more = change_log(request.user, **f.query(), limit=CSV_LIMIT)
    rows = ([timezone.localtime(e.at), e.who, e.area_label, e.record, e.action_label, "; ".join(change_lines(e)), e.reason] for e in entries)
    return csv_response(f"cadence-change-log-{timezone.localdate():%Y-%m-%d}.csv", CSV_COLUMNS, rows)


@web_view(Module.USERS, Level.VIEW)
def change_log_print(request):
    f, areas, people = _filters(request)
    entries, more = change_log(request.user, **f.query(), limit=PRINT_LIMIT)
    return render(request, "web/print_change_log.html", {"entries": [(e, change_lines(e)) for e in entries], "more": more, "limit": PRINT_LIMIT,
                                                         "filters": _filter_words(f, areas, people)})
