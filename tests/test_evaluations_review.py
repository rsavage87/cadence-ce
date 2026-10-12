"""
Slice 32's adversarial review, each finding pinned:

#1 A counted previous column from only part of the 12 months before (Cadence's records begin inside them) carries the day the records
   begin (indicators._previous), and the page's heading, its "Beside each" line, and the print's heading say the column covers only
   the days from then (views_evaluations._previous); a column from the whole 12 months, an empty one, and an approved one say nothing.
#2 A superseded version's approvals stay as recorded: correcting or removing one is refused in words by the service (so its approval
   day can never move after the amendment that replaced it), the page offers neither, and a POST is refused with nothing changed.
#3 The API is read-only for every holder of Program evaluation Edit: a DELETE by Edit, Approve (the default manager), or Full is a 405
   like POST, PUT, and PATCH, never a 403 naming a level they hold; a View-only reader keeps the 403 in words.
#4/#6 The survey binder's table links open the drawer only for records that open in one: the program section's evaluation number is a
   plain link to its page (never hx-target="#drawer"), while device and work order links keep the drawer; the print's links never.
#5 The next evaluation's due day, pinned on a year where the old formula (12 months from the last period's end) lands a day early.
"""
import html
import re
from datetime import date, timedelta

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone
from evaluation_fixtures import approved, draft, last_year, sent, year_before
from survey_helpers import gaps_of, period

from apps.accounts.models import Level, Module, Role, User
from apps.api import views_evaluations as api_views
from apps.equipment.models import Asset
from apps.evaluations import indicators, services
from apps.evaluations.models import Body, EvaluationApproval, ProgramEvaluation, Status
from apps.pm.dates import add_months
from apps.reports.survey import CHECK, DEVICE, EVALUATION, INCIDENT, WORK_ORDER, program
from apps.web import views_evaluations, views_survey
from apps.workorders.models import Priority, WorkOrder, WoStatus, WoType
from apps.workorders.services import create_work_order

DAY = timedelta(days=1)
HX = {"HTTP_HX_REQUEST": "true"}
API = "/api/v1/program-evaluations/"
EVERY_VIEW = {m: Level.VIEW for m in Module.values}


@pytest.fixture
def today(ctx):
    return timezone.localdate()


@pytest.fixture
def span(today):
    return last_year(today)


def day(d: date) -> str:
    return f"{d:%b} {d.day}, {d.year}"


def text(r) -> str:
    return html.unescape(r.content.decode())


def custom_user(tenant, name, levels) -> User:
    role = Role.objects.create(name=name.title(), slug=name)
    role.set_levels({m: levels.get(m, Level.NONE) for m in Module.values})
    return User.objects.create_user(username=f"{name}@riverside.example", password="Test-Pass-2026-x", tenant=tenant,
                                    role=Role.objects.get(pk=role.pk))


def records_from(on: date, dept, model) -> WorkOrder:
    """Cadence's records begin on `on`: the facility's first work order was opened that day (indicators.records_begin)."""
    device = Asset.objects.create(tag=f"CE-R{on:%y%m%d}", device_model=model, department=dept)
    wo = create_work_order(asset=device, type=WoType.REPAIR, priority=Priority.NORMAL, problem="Broken", opened_on=on)
    WorkOrder.objects.filter(pk=wo.pk).update(status=WoStatus.COMPLETED, completed_on=on + DAY)
    return wo


def partial_words(today: date, begin: date) -> str:
    return f"counted {day(today)}, not approved; only from {day(begin)}, when Cadence's records begin"


# --- #1: a counted previous column from part of the 12 months before ------------------------------------------------------------

def test_a_previous_column_counted_from_part_of_its_months_carries_the_day_records_begin(ctx, today, span, dept, vent_model):
    S, E = span
    begin = S - 18 * DAY  # 18 days before the period: inside the 12 months before, after they start
    records_from(begin, dept, vent_model)
    prev = indicators.snapshot(S, E, today=today)["previous"]
    assert prev["source"] == "counted" and prev["period"]["start"] == year_before(S).isoformat()
    assert prev["records_begin"] == begin.isoformat()
    assert prev["values"]["repairs_completed"]["value"] == 1  # counted, from those 18 days only


@pytest.mark.parametrize("offset, flagged", [(0, False), (1, True), (-1, False)])
def test_the_flag_starts_the_day_after_the_months_start(ctx, today, span, dept, vent_model, offset, flagged):
    S, E = span
    records_from(year_before(S) + offset * DAY, dept, vent_model)
    prev = indicators.snapshot(S, E, today=today)["previous"]
    assert prev["values"] and ("records_begin" in prev) is flagged


def test_no_flag_when_nothing_was_counted_or_the_column_is_approved(ctx, today, span, dept, vent_model):
    S, E = span
    records_from(S + 10 * DAY, dept, vent_model)  # after the 12 months before end: nothing counted, the empty note says why
    prev = indicators.snapshot(S, E, today=today)["previous"]
    assert prev["values"] == {} and "records_begin" not in prev
    earlier = approved(start=year_before(S), today=today)
    prev = indicators.snapshot(S, E, today=today, previous=earlier)["previous"]
    assert prev["source"] == "approved" and "records_begin" not in prev


def test_the_page_and_the_print_say_the_column_covers_only_the_days_from_then(client, make_user, today, span, dept, vent_model):
    S, _E = span
    begin = S - 18 * DAY
    records_from(begin, dept, vent_model)
    ev = draft(start=S, today=today)
    client.force_login(make_user("director"))
    words = partial_words(today, begin)
    body = text(client.get(f"/reports/evaluations/{ev.number}/v1/"))
    heading = body[body.index('<th class="num ev-prev-h">'):]
    heading = heading[:heading.index("</th>")]
    assert f"<small>{words}</small>" in heading
    assert f"Beside each: The 12 months before, {services.period_label(year_before(S), S - DAY)}, {words}." in body
    assert "Nothing was counted for" not in body
    assert "work before then is not in Cadence" not in body  # the period's own note: its records begin before this period starts
    printed = text(client.get(f"/print/evaluations/{ev.number}/v1/"))
    assert f"<small>{words}</small>" in printed
    part = text(client.get(f"/reports/evaluations/{ev.number}/v1/", **HX, HTTP_HX_TARGET="ev-figures"))
    assert f"<small>{words}</small>" in part


def test_a_column_from_the_whole_twelve_months_says_nothing_more(client, make_user, today, span, dept, vent_model):
    S, _E = span
    records_from(year_before(S), dept, vent_model)
    ev = draft(start=S, today=today)
    client.force_login(make_user("director"))
    for body in (text(client.get(f"/reports/evaluations/{ev.number}/v1/")), text(client.get(f"/print/evaluations/{ev.number}/v1/"))):
        assert f"counted {day(today)}, not approved" in body and "; only from" not in body


def test_a_reader_without_reports_view_reads_no_column_at_all(client, tenant, today, span, dept, vent_model):
    S, _E = span
    records_from(S - 18 * DAY, dept, vent_model)
    ev = draft(start=S, today=today)
    client.force_login(custom_user(tenant, "committee", {Module.EVALUATIONS: Level.VIEW}))
    body = text(client.get(f"/reports/evaluations/{ev.number}/v1/"))
    assert "only from" not in body and "The 12 months before" not in body


# --- #2: a superseded version's approvals stay as recorded --------------------------------------------------------------------

@pytest.fixture
def superseded(ctx, today):
    """v1 approved 20 days ago by the EOC committee, then by QAPI the next day; v2 amends it and is approved yesterday: v1 superseded."""
    v1 = approved(today=today, meeting_on=today - 20 * DAY)
    later = services.record_approval(v1, body=Body.QUALITY, meeting_on=today - 19 * DAY, by=None, today=today)
    v2 = services.amend(v1, by=None, today=today)
    sent(ev=v2, today=today - 2 * DAY)
    services.record_approval(v2, body=Body.EOC, meeting_on=today - DAY, by=None, today=today)
    v1.refresh_from_db()
    v2.refresh_from_db()
    assert (v1.status, v2.status, v1.approved_on, v2.approved_on) == (Status.SUPERSEDED, Status.APPROVED, today - 20 * DAY, today - DAY)
    return {"v1": v1, "v2": v2, "first": v1.approvals.order_by("created_at").first(), "later": later}


def test_correcting_a_superseded_versions_approval_is_refused(superseded, today, make_user):
    v1, first, later = superseded["v1"], superseded["first"], superseded["later"]
    manager = make_user("manager")
    for approval, fields in ((first, {"meeting_on": today - 2 * DAY}), (first, {"minutes_reference": "EOC-9"}),
                             (later, {"meeting_on": today - 2 * DAY})):
        with pytest.raises(ValidationError) as e:
            services.correct_approval(approval, by=manager, today=today, **fields)
        assert f"{v1.label} was superseded by its amendment: its approvals stay as recorded" in e.value.messages[0]
    v1.refresh_from_db()
    first.refresh_from_db()
    later.refresh_from_db()
    assert (v1.approved_on, first.meeting_on, first.minutes_reference, later.meeting_on) == (today - 20 * DAY, today - 20 * DAY, "EOC-1",
                                                                                              today - 19 * DAY)
    v2 = superseded["v2"]  # the version of record's approval is still corrected
    services.correct_approval(v2.approvals.get(), minutes_reference="EOC-10", by=manager, today=today)
    assert v2.approvals.get().minutes_reference == "EOC-10"


def test_removing_a_superseded_versions_later_approval_is_refused(superseded, make_user):
    v1, later = superseded["v1"], superseded["later"]
    with pytest.raises(ValidationError) as e:
        services.remove_approval(later, by=make_user("manager"))
    assert f"{v1.label} was superseded by its amendment" in e.value.messages[0]
    assert EvaluationApproval.objects.filter(pk=later.pk).exists() and v1.approvals.count() == 2


def test_the_page_offers_no_correct_or_remove_on_a_superseded_version(client, superseded, make_user):
    manager = make_user("manager")
    v1, v2 = superseded["v1"], superseded["v2"]
    assert views_evaluations.actions_for(manager, v1, None)["correct"] is False
    assert views_evaluations.actions_for(manager, v2, None)["correct"] is True
    client.force_login(manager)
    old = text(client.get(f"/reports/evaluations/{v1.number}/v1/"))
    approvals = old[old.index('id="ev-approvals"'):]
    assert "Environment of care or safety committee" in approvals and "Quality oversight (QAPI)" in approvals
    assert ">Correct</button>" not in approvals and ">Remove</button>" not in approvals
    assert "/approvals/" not in approvals
    current = text(client.get(f"/reports/evaluations/{v2.number}/v2/"))
    assert ">Correct</button>" in current[current.index('id="ev-approvals"'):]


def test_a_post_to_a_superseded_versions_approval_is_refused_in_words(client, superseded, make_user, today):
    v1, first, later = superseded["v1"], superseded["first"], superseded["later"]
    client.force_login(make_user("manager"))
    base = f"/reports/evaluations/{v1.number}/v1/approvals/"
    r = client.post(f"{base}{first.pk}/", {"body": "eoc", "meeting_on": (today - 2 * DAY).isoformat(), "minutes_reference": "EOC-1"}, **HX)
    assert r.status_code == 200 and "was superseded by its amendment: its approvals stay as recorded" in text(r)
    r = client.post(f"{base}{later.pk}/remove/", **HX)
    assert r["HX-Retarget"] == "#ev-approvals" and "was superseded by its amendment" in text(r)
    v1.refresh_from_db()
    assert v1.approved_on == today - 20 * DAY and v1.approvals.count() == 2
    assert EvaluationApproval.objects.get(pk=first.pk).meeting_on == today - 20 * DAY
    assert f"Approved {day(today - 20 * DAY)} by Environment of care or safety committee" in text(client.get(f"/print/evaluations/{v1.number}/v1/"))


def test_the_correct_form_of_a_superseded_versions_approval_is_never_served(client, superseded, make_user):
    v1, first = superseded["v1"], superseded["first"]
    client.force_login(make_user("manager"))
    r = client.get(f"/reports/evaluations/{v1.number}/v1/approvals/{first.pk}/", **HX)
    assert r["HX-Retarget"] == "#ev-approvals" and "was superseded by its amendment" in text(r) and 'name="meeting_on"' not in text(r)
    r = client.get(f"/reports/evaluations/{v1.number}/v1/approvals/{first.pk}/")  # opened directly: the page with the refusal
    assert r.status_code == 400 and "was superseded by its amendment" in text(r)


# --- #3: every write is a 405 for a holder of Edit -------------------------------------------------------------------------------

@pytest.fixture
def one(ctx, today):
    ev = draft(today=today)
    return {"ev": ev, "url": f"{API}{ev.id}/"}


@pytest.mark.parametrize("who", ["director", "manager", "editor"])
def test_every_write_by_a_holder_of_edit_is_a_405(client, make_user, tenant, one, who):
    user = custom_user(tenant, "editor", {**EVERY_VIEW, Module.EVALUATIONS: Level.EDIT}) if who == "editor" else make_user(who)
    client.force_login(user)
    assert client.post(API, {}, content_type="application/json").status_code == 405
    for r in (client.put(one["url"], {}, content_type="application/json"), client.patch(one["url"], {}, content_type="application/json"),
              client.delete(one["url"]), client.delete(f"{API}{one['ev'].number}/")):
        assert r.status_code == 405, (who, r.status_code, r.content)
        assert "needs Program evaluation Edit" not in r.content.decode()
    assert ProgramEvaluation.objects.filter(pk=one["ev"].pk, status=Status.DRAFT).exists()


def test_a_view_only_readers_delete_is_still_refused_in_words(client, make_user, one):
    client.force_login(make_user("analyst"))
    r = client.delete(one["url"])
    assert r.status_code == 403 and r.json()["detail"] == api_views.WRITE_REFUSAL
    assert client.get(one["url"]).status_code == 200


# --- #4 / #6: a binder table's link opens the drawer only for a drawer --------------------------------------------------------

def test_cells_open_the_drawer_only_for_a_record_that_opens_in_one(tenant):
    row = ["EV-26-0001", "WO-26-0001", "CE-10001", "IN-26-0001", "text"]
    links = {0: EVALUATION, 1: WORK_ORDER, 2: DEVICE, 3: INCIDENT}
    can = {EVALUATION: True, WORK_ORDER: True, DEVICE: True, INCIDENT: True}
    screen = views_survey.cells(row, links, can)
    assert [c["url"] for c in screen] == ["/reports/evaluations/EV-26-0001/", "/work-orders/WO-26-0001/", "/equipment/CE-10001/",
                                          "/incidents/IN-26-0001/", ""]
    assert [c["drawer"] for c in screen] == [False, True, True, True, False]
    printed = views_survey.cells(row, links, can, tenant)  # the print's links name the facility and never open a drawer
    assert all(c["url"].endswith("?facility=riverside") for c in printed[:4]) and not any(c["drawer"] for c in printed)


def test_the_binders_evaluation_link_is_a_page_and_device_links_keep_the_drawer(client, make_user, today, dept, vent_model):
    ev = approved(start=add_months(today.replace(day=1), -12), today=today)
    Asset.objects.create(tag="CE-PAST", device_model=vent_model, department=dept, next_pm_on=today - 5 * DAY)  # past its PM date today
    client.force_login(make_user("director"))
    body = client.get("/reports/survey/").content.decode()
    prog = body[body.index('id="sv-program"'):body.index('id="sv-inventory"')]
    link = f'<a class="link" href="/reports/evaluations/{ev.number}/">{ev.number}</a>'
    assert link in prog
    assert not re.search(r'hx-get="/reports/evaluations/[^"]*"', body) and prog.count('hx-target="#drawer"') == 0
    maint = body[body.index('id="sv-maintenance"'):]
    assert '<a class="link" href="/equipment/CE-PAST/" hx-get="/equipment/CE-PAST/" hx-target="#drawer">CE-PAST</a>' in maint
    printed = client.get("/print/survey/").content.decode()
    assert f'<a href="/reports/evaluations/{ev.number}/?facility=riverside">{ev.number}</a>' in printed and 'hx-target' not in printed


# --- #5: the next evaluation's due day, pinned --------------------------------------------------------------------------------

def test_the_due_day_on_a_year_where_twelve_months_from_the_end_is_a_day_early(ctx):
    """The binder test's assertion on the day it once failed: approved Mar 2022 to Feb 28, 2023 (as test_the_check_starts_the_day_
    after_it_is_due does when run on Mar 15, 2025); the next 12 months end Feb 29, 2024, so the 90th day after is May 29."""
    on = date(2025, 3, 15)
    last = approved(start=add_months(on.replace(day=1), -36), today=on)
    assert (last.period_start, last.period_end) == (date(2022, 3, 1), date(2023, 2, 28))
    due = services.next_due(on).due_on
    assert due == services.period_end_of(last.period_end + DAY) + 90 * DAY == date(2024, 5, 29)
    assert add_months(last.period_end, 12) + 90 * DAY == date(2024, 5, 28)  # the old assertion's day: one early
    for d, checks in ((due, 0), (due + DAY, 1)):
        assert len(gaps_of(program.build(period(d - 30 * DAY, d, d), None), CHECK)) == checks, d
