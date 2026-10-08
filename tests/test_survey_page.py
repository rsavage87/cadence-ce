"""
The survey binder's screens (slice 25, part E: apps/web/views_survey.py): the page with its period form and presets, the summary, the
gaps, and a panel per section; a CSV per section table and one of every gap; and the printable binder. Built against fake sections
(survey_helpers.fake_sections), so these tests hold whatever the sections' own rules are; access and the requester-text canary over
the real sections are tests/test_survey_access.py.
"""
import html
import re
from datetime import date
from decimal import Decimal

import pytest
from csvutil import csv_rows
from django.db import connection
from django.test.utils import CaptureQueriesContext
from survey_helpers import fake_sections

from apps.reports import survey
from apps.reports.survey import CHECK, DEVICE, FINDING, GAP, GAPS_SHOWN, NOT_COVERED, PREVIEW_ROWS, PRINT_ROWS, WORK_ORDER, Figure, Gap, Section, Table

TODAY = date(2026, 10, 7)
HX = {"HTTP_HX_REQUEST": "true"}
BODY = {**HX, "HTTP_HX_TARGET": "survey-body"}
PERIOD = "from=2026-01-01&to=2026-06-30"


@pytest.fixture(autouse=True)
def _today(monkeypatch, settings):
    monkeypatch.setattr("apps.web.views_survey._today", lambda: TODAY)
    settings.APP_BASE_URL = "https://ce.example.org"


@pytest.fixture
def signed_in(client, make_user):
    def _as(role_slug):
        user = make_user(role_slug)
        client.force_login(user)
        return user

    return _as


def maintenance(rows: int = 12, gaps: int = 2, key: str = "maintenance") -> Section:
    """A section shaped like the real ones: figures, notes, gaps of each kind, a linked table, an empty one, and one too long to print."""
    rows_ = [[f"WO-26-{i:04d}", f"CE-{10000 + i}", "Hamilton G5", date(2026, 3, 1), Decimal("12.50"), 3.14159, None] for i in range(rows)]
    gap_list = [Gap(GAP, f"WO-26-{i:04d} on CE-{10000 + i} was done 12 days late with no reason recorded", f"/work-orders/WO-26-{i:04d}/",
                    f"WO-26-{i:04d}") for i in range(gaps)]
    gap_list += [Gap(CHECK, "Your policy text says a month", "/settings/", "Settings"), Gap(FINDING, "Medium risk: 91.2% on time against 95%")]
    return Section(
        key=key, title="Scheduled maintenance (PM) completion", topic="Whether PMs were done by their due dates",
        covers="Jan 1, 2026 to Jun 30, 2026", figures=[Figure("PMs counted", 40, "in the period"), Figure("Last PM", date(2026, 6, 2))],
        gaps=gap_list,
        tables=[Table("late", "PMs not on time", ["Work order", "Device", "Model", "Due", "Cost", "Rate", "Why late"], lambda: iter(rows_), count=rows,
                      links={0: WORK_ORDER, 1: DEVICE}),
                Table("moved", "Due dates moved after they had passed", ["Work order"], lambda: iter([]), count=0, empty="No due date moved late."),
                Table("every", "Every device", ["Tag"], lambda: iter([[f"T-{i}"] for i in range(7)]), count=7, printed=False)],
        notes=["On time means completed on or before the due date."])


def program() -> Section:
    return Section(key="program", title="Program and policies", topic="The facility's program", covers="as of today, Oct 7, 2026",
                   figures=[Figure("Facility", "Riverside Regional")], tables=[Table("policy", "Maintenance policy settings in Cadence", ["Policy", "Text"],
                                                                                   lambda: iter([["Life support", "OEM interval"]]), count=None)])


def unescaped(r) -> str:
    return html.unescape(r.content.decode())


# --- the page -----------------------------------------------------------------------------------------------------------------

def test_the_page_shows_the_summary_the_gaps_and_each_section(client, signed_in, monkeypatch):
    fake_sections(monkeypatch, {"maintenance": maintenance(), "program": program()})
    signed_in("director")
    r = client.get("/reports/survey/")
    assert r.status_code == 200 and [t.name for t in r.templates[:2]] == ["web/survey.html", "web/base.html"]
    assert r.context["nav_active"] == "reports"
    body = r.content.decode()
    assert "<h1>Survey binder</h1>" in body and "Nov 1, 2025 to Oct 7, 2026" in body  # the default twelve months
    # one card per section, in the binder's order, with its counts or "No gaps"
    cards = re.findall(r'<a class="sv-card" href="#sv-(\w+)">', body)
    assert cards == survey.section_keys()
    card = body[body.index('href="#sv-maintenance"'):]
    card = card[:card.index("</a>")]
    assert '<span class="chip crit">2 gaps</span>' in card and '<span class="chip warn">1 finding</span>' in card and "1 check" in card
    assert 'href="#sv-program"><span class="t">Program and policies</span><span class="sv-chips"><span class="chip ok">No gaps</span>' in body
    assert "2 gaps, 1 finding, 1 check in the 8 sections your role can see" in body and "incomplete" not in body
    # the gaps: kind chip, words, and the record's link (the drawer for a work order, a page for Settings); gaps first, then findings, then checks
    gaps = body[body.index('id="sv-gaps-h"'):body.index('id="sv-program"')]
    assert gaps.index("WO-26-0000 on CE-10000") < gaps.index("Medium risk") < gaps.index("Your policy text")
    assert '<a class="link sv-gap-r" href="/work-orders/WO-26-0001/" hx-get="/work-orders/WO-26-0001/" hx-target="#drawer">WO-26-0001</a>' in gaps
    assert '<a class="link sv-gap-r" href="/settings/">Settings</a>' in gaps
    assert "<span class=\"sv-gap-t\">Medium risk: 91.2% on time against 95%</span></li>" in gaps  # no record, no link
    # each section: topic, covers, figures, notes, the first rows of each table with links, its CSV, and an empty table's words
    sec = body[body.index('id="sv-maintenance"'):body.index('id="sv-aem"')]
    assert "Whether PMs were done by their due dates" in sec and "Jan 1, 2026 to Jun 30, 2026" in sec
    assert '<dt class="l">PMs counted</dt><dd class="v">40</dd><dd class="hint">in the period</dd>' in sec
    assert '<dd class="v">Jun 2, 2026</dd>' in sec and "On time means completed on or before the due date." in sec
    assert sec.count('hx-get="/work-orders/WO-26-') == PREVIEW_ROWS and '<span class="muted">12 rows</span>' in sec
    assert '<a class="link" href="/equipment/CE-10003/" hx-get="/equipment/CE-10003/" hx-target="#drawer">CE-10003</a>' in sec
    assert "WO-26-0010" not in sec.split("The first")[0].split("PMs not on time")[1]  # only the first ten rows
    assert "The first 10 of 12 rows; every row is in the CSV." in sec
    assert '<td class="nw">Mar 1, 2026</td><td class="num">12.50</td><td class="num">3.14</td><td>—</td>' in sec
    assert '<div class="empty sv-empty">No due date moved late.</div>' in sec
    assert 'href="/reports/survey/maintenance/late.csv?from=2025-11-01&amp;to=2026-10-07&amp;facility=riverside" download' in sec
    prog = body[body.index('id="sv-program"'):body.index('id="sv-inventory"')]
    assert "<td>Life support</td><td>OEM interval</td>" in prog and '<span class="muted">' not in prog  # a count the section did not know
    # the head's links carry the period and name the facility
    assert 'href="/print/survey/?from=2025-11-01&amp;to=2026-10-07&amp;facility=riverside" target="_blank"' in body
    assert 'href="/reports/survey/gaps.csv?from=2025-11-01&amp;to=2026-10-07&amp;facility=riverside" download' in body


def test_a_link_opens_a_record_only_for_a_reader_who_can_view_it(client, make_user, ctx, monkeypatch):
    from apps.accounts.models import Level, Module, Role

    fake_sections(monkeypatch, {"program": maintenance(key="program")})  # the program needs Reports View only
    role = Role.objects.create(name="Reports only", slug="reports-only")
    role.set_levels({Module.REPORTS: Level.VIEW})
    user = make_user("director")
    user.role = role
    user.save()
    client.force_login(user)
    body = client.get("/reports/survey/").content.decode()
    sec = body[body.index('id="sv-program"'):]
    assert [c["key"] for c in client.get("/reports/survey/").context["cards"] if not c["left_out"]] == ["program"]
    assert '<td class="nw">WO-26-0003</td><td class="nw">CE-10003</td>' in sec and 'hx-get="/work-orders/' not in sec and 'hx-get="/equipment/' not in sec


def test_the_period_is_kept_by_every_link_and_the_presets(client, signed_in, monkeypatch):
    fake_sections(monkeypatch, {"maintenance": maintenance(rows=3)})
    signed_in("director")
    body = unescaped(client.get(f"/reports/survey/?{PERIOD}"))
    assert "Jan 1, 2026 to Jun 30, 2026" in body
    links = re.findall(r'(?:href|hx-get)="(/(?:reports/survey|print/survey)/[^"#]*)"', body)
    period_links = [u for u in links if not u.startswith("/reports/survey/?") and u != "/reports/survey/"]
    assert len(period_links) >= 5 and all(PERIOD in u and "facility=riverside" in u for u in period_links), period_links
    # the form shows the period, and the presets: the default twelve months, this year to date, last calendar year
    assert 'name="from" value="2026-01-01"' in body and 'name="to" value="2026-06-30"' in body
    assert re.findall(r'class="pill[^"]*" href="/reports/survey/\?([^"]+)"', body) == [
        "from=2025-11-01&to=2026-10-07", "from=2026-01-01&to=2026-10-07", "from=2025-01-01&to=2025-12-31"]
    assert 'class="pill active"' not in body  # Jan to Jun is none of them
    body = client.get("/reports/survey/?from=2025-01-01&to=2025-12-31").content.decode()
    assert body.count('class="pill active"') == 1 and 'aria-current="true">Last calendar year</a>' in body
    form = body[body.index('<form class="toolbar sv-period"'):]
    assert 'hx-get="/reports/survey/" hx-target="#survey-body" hx-swap="outerHTML" hx-push-url="true"' in form[:form.index(">")]


def test_the_period_form_and_presets_swap_the_body_alone(client, signed_in, monkeypatch):
    fake_sections(monkeypatch, {"maintenance": maintenance(rows=3)})
    signed_in("director")
    r = client.get(f"/reports/survey/?{PERIOD}", **BODY)
    body = r.content.decode()
    assert r.status_code == 200 and r.templates[0].name == "web/_survey_body.html"
    title = "<title>Survey binder · Reports · Riverside Regional</title>"
    assert body.lstrip().startswith(title) and body.count("<title>") == 1
    assert "<html" not in body and 'class="nav"' not in body and body.count('id="survey-body"') == 1
    assert body.rstrip().endswith("</div>") and "Jan 1, 2026 to Jun 30, 2026" in body
    assert "<title>" not in client.get("/reports/survey/").content.decode().split("<body")[1]  # never inside the full page's body


@pytest.mark.parametrize("query, words", [
    ("to=2026-10-08", "The binder cannot cover days after today."),
    ("from=2026-05-01&to=2026-04-01", "The period starts after it ends."),
    ("from=2023-01-01", "A binder covers at most three years."),
    ("from=junk", "Enter a date as YYYY-MM-DD."),
])
def test_a_refused_period_shows_the_forms_words(client, signed_in, monkeypatch, query, words):
    fake_sections(monkeypatch, {"maintenance": maintenance(rows=3)})
    signed_in("director")
    for headers in ({}, BODY):
        r = client.get(f"/reports/survey/?{query}", **headers)
        body = r.content.decode()
        assert r.status_code == 200 and words in body and 'role="alert"' in body, headers
        assert 'aria-invalid="true"' in body and 'id="sv-maintenance"' not in body and "Print binder" not in body
    typed = dict(p.split("=") for p in query.split("&"))
    body = client.get(f"/reports/survey/?{query}").content.decode()
    for key, default in (("from", "2025-11-01"), ("to", "2026-10-07")):  # what was typed, to fix; the default for what was not
        assert f'name="{key}" value="{typed.get(key, default)}"' in body
    for url in (f"/reports/survey/gaps.csv?{query}", f"/reports/survey/maintenance/late.csv?{query}", f"/print/survey/?{query}"):
        r = client.get(url)
        assert r.status_code == 400 and words in r.content.decode(), url


def test_a_long_gap_list_is_cut_on_the_screen_and_whole_in_the_csv_and_print(client, signed_in, monkeypatch):
    fake_sections(monkeypatch, {"maintenance": maintenance(rows=1, gaps=GAPS_SHOWN + 4)})
    signed_in("director")
    body = client.get("/reports/survey/").content.decode()
    assert body.count('<li class="sv-gap">') == GAPS_SHOWN and "6 more in the" in body  # 54 gaps + 2 others, 50 shown
    assert f"{GAPS_SHOWN + 4} gaps" in body
    rows = csv_rows(client.get("/reports/survey/gaps.csv"))
    assert len(rows) == GAPS_SHOWN + 6 + 1
    body = client.get("/print/survey/").content.decode()
    assert body.count("was done 12 days late") == GAPS_SHOWN + 4


def test_a_left_out_section_is_named_with_why_and_the_binder_never_reads_as_ready(client, signed_in, monkeypatch):
    fake_sections(monkeypatch, {"maintenance": maintenance(rows=1, gaps=0)})
    signed_in("technician")
    body = client.get("/reports/survey/").content.decode()
    assert ('<div class="sv-card out"><span class="t">Technician qualifications</span><span class="s">Left out: Needs Users and access View, '
            "which your role does not have.</span></div>") in body
    assert "This binder is incomplete for your role: Technician qualifications is left out." in body
    assert 'id="sv-staff"' not in body and 'href="#sv-staff"' not in body
    assert "in the 7 sections your role can see" in body


# --- CSVs ---------------------------------------------------------------------------------------------------------------------

def test_a_section_table_downloads_every_row(client, signed_in, monkeypatch):
    fake_sections(monkeypatch, {"maintenance": maintenance(rows=PREVIEW_ROWS + 15)})
    signed_in("director")
    r = client.get(f"/reports/survey/maintenance/late.csv?{PERIOD}")
    assert r.status_code == 200 and r["Content-Type"] == "text/csv; charset=utf-8"
    assert r["Content-Disposition"] == 'attachment; filename="cadence-survey-maintenance-late-2026-01-01-to-2026-06-30-as-of-2026-10-07.csv"'
    rows = csv_rows(r)
    assert rows[0] == ["Work order", "Device", "Model", "Due", "Cost", "Rate", "Why late"] and len(rows) == PREVIEW_ROWS + 16
    assert rows[1] == ["WO-26-0000", "CE-10000", "Hamilton G5", "2026-03-01", "12.50", "3.14", ""]
    assert len(csv_rows(client.get("/reports/survey/maintenance/every.csv"))) == 8  # a table too long to print still downloads


def test_an_unknown_section_or_table_is_a_404_and_a_section_left_out_a_403_in_words(client, signed_in, monkeypatch):
    fake_sections(monkeypatch, {"maintenance": maintenance(rows=1)})
    signed_in("technician")
    assert client.get("/reports/survey/nope/late.csv").status_code == 404
    assert client.get("/reports/survey/maintenance/nope.csv").status_code == 404
    r = client.get("/reports/survey/staff/main.csv")
    assert r.status_code == 403 and r.content.decode() == "Needs Users and access View, which your role does not have."


def test_the_gaps_csv_lists_every_gap_with_a_link_naming_the_facility(client, signed_in, monkeypatch):
    fake_sections(monkeypatch, {"maintenance": maintenance(rows=1, gaps=2), "program": program()})
    signed_in("director")
    r = client.get(f"/reports/survey/gaps.csv?{PERIOD}")
    assert r["Content-Disposition"] == 'attachment; filename="cadence-survey-gaps-2026-01-01-to-2026-06-30-as-of-2026-10-07.csv"'
    rows = csv_rows(r)
    assert rows[0] == ["Section", "Kind", "Record", "Description", "Link"]
    assert rows[1] == ["Scheduled maintenance (PM) completion", "Gap", "WO-26-0000", "WO-26-0000 on CE-10000 was done 12 days late with no reason recorded",
                       "https://ce.example.org/work-orders/WO-26-0000/?facility=riverside"]
    assert rows[3][1:] == ["Finding", "", "Medium risk: 91.2% on time against 95%", ""]
    assert rows[4][1:] == ["Check", "Settings", "Your policy text says a month", "https://ce.example.org/settings/?facility=riverside"]
    assert len(rows) == 5


# --- the print ------------------------------------------------------------------------------------------------------------------

def test_the_print_has_a_cover_and_a_page_per_section(client, signed_in, monkeypatch):
    fake_sections(monkeypatch, {"maintenance": maintenance(rows=PRINT_ROWS + 3), "program": program()})
    user = signed_in("technician")
    r = client.get(f"/print/survey/?{PERIOD}")
    assert r.status_code == 200 and [t.name for t in r.templates[:2]] == ["web/print_survey.html", "web/print_base.html"]
    body = r.content.decode()
    assert "<title>Survey binder · Riverside Regional</title>" in body and 'class="nav"' not in body
    cover = body[body.index('class="sheet svp-cover"'):body.index('class="sheet svp-sec"')]
    assert "<dd>Riverside Regional</dd>" in cover and "<dd>Jan 1, 2026 to Jun 30, 2026</dd>" in cover and f"<dd>{user}</dd>" in cover
    assert re.search(r"<dt>Generated</dt><dd>\w{3} \d+, \d{4} \d+:\d\d [AP]M \w+</dd>", cover)  # the facility's time, with its zone
    assert "2 gaps, 1 finding, 1 check" in cover and "This binder is incomplete for your role: Technician qualifications is left out." in cover
    assert "<td>Technician qualifications</td><td class=\"out\" colspan=\"2\">Left out: Needs Users and access View" in cover
    assert "<td>Scheduled maintenance (PM) completion</td><td>Jan 1, 2026 to Jun 30, 2026</td><td>2 gaps, 1 finding, 1 check</td>" in cover
    assert "Not covered by this binder" in cover and all(item in html.unescape(cover) for item in NOT_COVERED)
    assert body.count('<section class="sheet svp-sec"') == 7
    sec = body[body.index('id="sv-maintenance"'):body.index('id="sv-aem"')]
    assert body.count('<tr><td class="nw"><a href="/work-orders/WO-26-') == PRINT_ROWS and "3 more rows in the CSV" in sec
    assert '<a href="/work-orders/WO-26-0000/?facility=riverside">WO-26-0000</a>' in sec  # links name the facility
    assert '<a href="/equipment/CE-10000/?facility=riverside">CE-10000</a>' in sec
    assert "7 rows, listed in the CSV rather than printed. See the CSV" in sec and "T-1" not in sec
    assert 'href="/reports/survey/maintenance/every.csv?from=2026-01-01&amp;to=2026-06-30&amp;facility=riverside"' in sec
    assert "No due date moved late." in sec and "On time means completed on or before the due date." in sec
    assert '<span class="chip crit">Gap</span>' in sec and '<span class="chip warn">Finding</span>' in sec
    assert "<td>Life support</td><td>OEM interval</td>" in body  # a table whose count the section did not know


# --- the Reports page, and the size of the page ----------------------------------------------------------------------------------

def test_the_reports_page_and_the_compliance_report_link_to_the_binder(client, signed_in):
    signed_in("technician")
    body = client.get("/reports/").content.decode()
    assert '<a class="btn" href="/reports/survey/" title=' in body and "Survey binder</a>" in body
    body = client.get("/reports/compliance/").content.decode()
    assert 'the <a class="link" href="/reports/survey/">Survey binder</a> lists every PM not done by its due date' in body
    assert "Survey binder</a> lists" not in client.get("/print/reports/compliance/").content.decode()


def test_the_page_makes_a_fixed_number_of_queries(client, signed_in, monkeypatch):
    signed_in("director")

    def count(n):
        fake_sections(monkeypatch, {"maintenance": maintenance(rows=n, gaps=n), "program": program()})
        with CaptureQueriesContext(connection) as q:
            assert client.get("/reports/survey/").status_code == 200
        return len(q)

    count(1)  # warm up: the first request of a test may fill per-process caches
    # 40, not 50: past GAPS_SHOWN the Settings check is not shown, and a gap link reads its area's level once (review fix: links only
    # to what the reader may open), so the pages must show the same kinds of link to compare
    assert count(5) == count(40)
