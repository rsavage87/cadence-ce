"""
Slice 28, wave 2C: the survey binder's Device incidents section (apps/reports/survey/incidents.py), maintenance's finding for a held
device (apps/reports/survey/maintenance.py), the held label in the binder's status columns (inventory, inspections, maintenance), the
incident link on the screen, the print, and the API, and the demo seed's two incidents.

The section: what is listed (occurred in the period, open at its end; never one recorded in error, which is a check), the figures, the
table, at most one gap or finding per incident (the first that applies) and one check, the clock that ran before a decision stopped it
(from the incident's history), nothing a requester typed, and a fixed number of queries; under row-level security as the runtime role.
"""
from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from incident_fixtures import make_incident
from pg_helpers import as_app_role, needs_postgres
from survey_helpers import gaps_of, period, rows_of

from apps.accounts.models import Level, Module, Role, User
from apps.core.workdays import add_work_days, work_days_between
from apps.demo.management.commands import seed_demo
from apps.equipment.models import AddedAs, Asset, AssetStatus
from apps.equipment.services import HELD_LABEL
from apps.incidents import services as inc
from apps.incidents.models import Accessories, Affected, Basis, DecidedBy, EventLog, Finding, Incident, Outcome, Release, Status
from apps.reports.survey import CHECK, DEVICE, FINDING, GAP, INCIDENT, incidents, inspections, inventory, maintenance
from apps.tenants.context import tenant_context
from apps.tenants.models import Tenant
from apps.web import views_survey
from apps.workorders.models import Priority, Source, WorkOrder, WoStatus, WoType
from apps.workorders.services import change_status, create_work_order

DAY = timedelta(days=1)
SECRET = "Jane Roe bed 4 canary"  # what a requester typed: never in the binder


@pytest.fixture
def today(ctx):
    return timezone.localdate()


def make(asset, **kwargs):
    """An incident written straight to the table (tests/incident_fixtures.py): no hold and no work order unless asked."""
    kwargs.setdefault("hold", False)
    kwargs.setdefault("open_work_order", False)
    return make_incident(asset, **kwargs)


def build(today, start=None, end=None):
    return incidents.build(period(start or today - timedelta(days=365), end or today, today), None)


def by_record(section) -> dict:
    return {g.record: g for g in section.gaps}


def figures(section) -> dict:
    return {f.label: f.value for f in section.figures}


def hints(section) -> dict:
    return {f.label: f.hint for f in section.figures}


def _day(d) -> str:
    return f"{d:%b} {d.day}, {d.year}"


# --- the section ------------------------------------------------------------------------------------------------------------------

def test_the_figures_the_table_and_one_gap_per_incident(today, vent, pump):
    T = today
    then = T - timedelta(days=25)  # every clock that started then is past due (and none is open more than 30 days)
    due = add_work_days(then, 10)
    decided = {"decided_on": then + DAY, "decided_by": DecidedBy.RISK}
    unknown = make(vent, outcome=Outcome.UNKNOWN, occurred_on=then)
    undecided = make(vent, outcome=Outcome.SERIOUS_INJURY, occurred_on=then)
    sent_anyway = make(vent, outcome=Outcome.SERIOUS_INJURY, occurred_on=then, manufacturer_reported_on=then + 3 * DAY)
    missing = make(pump, outcome=Outcome.DEATH, occurred_on=then, reportable=True, basis=Basis.MAY_HAVE, event_reference="SE-1",
                   manufacturer_reported_on=then + 5 * DAY, **decided)
    late_on = add_work_days(due, 2)
    late = make(pump, outcome=Outcome.SERIOUS_INJURY, occurred_on=then, reportable=True, basis=Basis.MAY_HAVE, event_reference="SE-2",
                manufacturer_reported_on=late_on, report_number="0123456789-2026-0001", **decided)
    on_time = make(pump, outcome=Outcome.DEATH, occurred_on=then, reportable=True, basis=Basis.MAY_HAVE, event_reference="SE-3",
                   fda_reported_on=then + DAY, manufacturer_reported_on=then + 2 * DAY, **decided)
    no_reference = make(pump, outcome=Outcome.INJURY, occurred_on=T - 5 * DAY, reportable=False, basis=Basis.NOT_SERIOUS,
                        decided_on=T - 2 * DAY, decided_by=DecidedBy.CE)
    fresh = make(vent, outcome=Outcome.UNKNOWN, occurred_on=T - DAY)  # its clock still running: nothing yet

    s = build(T)
    assert figures(s) == {"Incidents recorded": 8, "With the report clock": 7, "Decided reportable": 3, "Reports on time": 1, "Reports late": 2,
                          "Decisions pending": 4, "Devices held now": 0}
    assert hints(s)["Decided reportable"] == "1 decided not reportable" and hints(s)["Decisions pending"] == "3 past their report due date"
    gaps = by_record(s)
    assert set(gaps) == {unknown.number, undecided.number, missing.number, late.number, no_reference.number}
    url = reverse("web:incident", args=[unknown.number])
    assert (gaps[unknown.number].kind, gaps[unknown.number].url) == (FINDING, url)
    assert gaps[unknown.number].text.startswith(f"{unknown.number} on CE-10001: the outcome is still not known, and its report was due {_day(due)}")
    assert gaps[undecided.number].kind == FINDING and "was not decided by its report due date" in gaps[undecided.number].text
    assert "it should have been reported" in gaps[undecided.number].text
    assert gaps[missing.number].kind == GAP and "its report to the FDA is not recorded" in gaps[missing.number].text
    assert gaps[missing.number].text.endswith("record it if it was sent")
    assert gaps[late.number].kind == FINDING and f"went out {_day(late_on)}, 2 work days after its due date ({_day(due)})" in gaps[late.number].text
    assert gaps[no_reference.number].kind == GAP and "decided with no event report number" in gaps[no_reference.number].text
    assert not gaps_of(s, CHECK)
    assert sent_anyway.number not in gaps and on_time.number not in gaps and fresh.number not in gaps

    table = s.table("incidents")
    assert table.columns == incidents.COLUMNS and table.links == {0: INCIDENT, 1: DEVICE} and table.count == 8
    rows = {r[0]: r for r in rows_of(s, "incidents")}
    assert rows[missing.number] == [missing.number, "CE-10002", "BD Alaris 8015 PCU", then, then, "Death", "Patient", "Reportable",
                                     "May have caused or contributed", then + DAY, "Risk management or patient safety", due, None,
                                     then + 5 * DAY, "", "", "SE-1", "No hold", "Open"]
    assert rows[fresh.number][7] == "Pending" and rows[no_reference.number][7:9] == ["Not reportable", "Not a death or serious injury"]
    assert rows[late.number][14] == "0123456789-2026-0001"


def test_what_is_listed_and_recorded_in_error(today, vent):
    T = today
    start, end = T - timedelta(days=200), T - timedelta(days=100)
    before = T - timedelta(days=300)
    still_open = make(vent, outcome=Outcome.NO_HARM, affected=Affected.NONE, occurred_on=before)
    closed_after = make(vent, outcome=Outcome.NO_HARM, affected=Affected.NONE, occurred_on=before, status=Status.CLOSED, closed_on=T - 50 * DAY)
    make(vent, outcome=Outcome.NO_HARM, affected=Affected.NONE, occurred_on=before, status=Status.CLOSED, closed_on=T - 250 * DAY)
    make(vent, outcome=Outcome.NO_HARM, affected=Affected.NONE, occurred_on=T - 50 * DAY)  # after the period
    in_period = make(vent, outcome=Outcome.NO_HARM, affected=Affected.NONE, occurred_on=T - 150 * DAY, finding=Finding.MET_SPECS)
    error = make(vent, outcome=Outcome.SERIOUS_INJURY, occurred_on=T - 150 * DAY, status=Status.IN_ERROR)
    make(vent, outcome=Outcome.SERIOUS_INJURY, occurred_on=before, status=Status.IN_ERROR)  # in error before the period: nothing

    s = build(T, start, end)
    assert [r[0] for r in rows_of(s, "incidents")] == [in_period.number, closed_after.number, still_open.number]  # newest first
    assert figures(s)["Incidents recorded"] == 1 and hints(s)["Incidents recorded"] == "occurred in the period; 2 more from before it were open at its end"
    assert figures(s)["With the report clock"] == 0 and figures(s)["Decisions pending"] == 0  # the one in error is counted nowhere
    [check] = [g for g in s.gaps if g.record == error.number]
    assert check.kind == CHECK and check.text == (f"{error.number} on CE-10001 (occurred {_day(T - 150 * DAY)}) was marked recorded in error: it is "
                                                  "left out of this section's figures and table")
    # still_open: open 300 days with no finding (a check, as of today); the closed ones never are
    assert {g.record: g.kind for g in s.gaps} == {error.number: CHECK, still_open.number: CHECK}


def test_the_checks_one_per_incident(today, vent, pump, make_user):
    T = today
    old = make(vent, outcome=Outcome.NO_HARM, affected=Affected.NONE, occurred_on=T - 31 * DAY)
    make(vent, outcome=Outcome.NO_HARM, affected=Affected.NONE, occurred_on=T - 30 * DAY)  # 30 days: not yet
    held = make(pump, outcome=Outcome.NO_HARM, affected=Affected.NONE, occurred_on=T - 20 * DAY, finding=Finding.MET_SPECS, hold=True,
                open_work_order=True)
    WorkOrder.objects.filter(pk=held.work_order_id).update(status=WoStatus.COMPLETED, completed_on=T - 15 * DAY)
    both = make(vent, outcome=Outcome.NO_HARM, affected=Affected.NONE, occurred_on=T - 40 * DAY, hold=True, open_work_order=True)
    WorkOrder.objects.filter(pk=both.work_order_id).update(status=WoStatus.CLOSED, completed_on=T - 20 * DAY)
    recent = make(pump, outcome=Outcome.NO_HARM, affected=Affected.NONE, occurred_on=T - 20 * DAY, finding=Finding.MET_SPECS, hold=True,
                  open_work_order=True)
    WorkOrder.objects.filter(pk=recent.work_order_id).update(status=WoStatus.COMPLETED, completed_on=T - 14 * DAY)  # 14 days: not yet

    s = build(T)
    checks = {g.record: g.text for g in gaps_of(s, CHECK)}
    assert set(checks) == {old.number, held.number, both.number} and not gaps_of(s, GAP) and not gaps_of(s, FINDING)
    assert checks[old.number] == (f"{old.number} on CE-10001 has been open 31 days since it occurred ({_day(T - 31 * DAY)}) with no device "
                                  "evaluation finding recorded")
    wo = WorkOrder.objects.get(pk=held.work_order_id)
    assert checks[held.number] == f"{held.number}: its investigation {wo.number} was completed {_day(T - 15 * DAY)}, and CE-10002 is still held 15 days later"
    assert "no device evaluation finding" in checks[both.number]  # the first check that applies, only
    assert figures(s)["Devices held now"] == 2  # CE-10002 by two incidents, CE-10001 by one: each device once
    assert [r[17] for r in rows_of(s, "incidents") if r[0] == held.number] == ["0 of 1"]


def test_the_clock_that_ran_counts_after_a_decision_stops_it(today, vent):
    """Decided not reportable clears the due date; the incident's history still shows the clock ran (one read, this facility's)."""
    i = inc.record_incident(asset=vent, outcome=Outcome.UNKNOWN, affected=Affected.PATIENT, hold=False, event_reference="SE-9", today=today)
    inc.decide(i, outcome=Outcome.INJURY, basis=Basis.NOT_SERIOUS, decided_on=today, decided_by=DecidedBy.CE, today=today)
    i.refresh_from_db()
    assert i.report_due_on is None and i.reportable is False
    make(vent, outcome=Outcome.NO_HARM, affected=Affected.NONE)
    s = build(today)
    assert figures(s)["With the report clock"] == 1 and figures(s)["Decided reportable"] == 0
    assert {r[0]: r[7] for r in rows_of(s, "incidents")}[i.number] == "Not reportable"


def test_a_released_and_closed_incident_through_the_services(today, vent):
    """The services' own rows: recorded with a hold, investigated, decided, released, closed; listed with its holds released and
    no gap."""
    i = inc.record_incident(asset=vent, outcome=Outcome.NO_HARM, affected=Affected.PATIENT, event_reference="SE-10", today=today)
    wo = i.work_order

    change_status(wo, WoStatus.IN_PROGRESS)
    change_status(wo, WoStatus.COMPLETED)
    inc.record_finding(i, Finding.MET_SPECS)
    inc.release(i.holds.get(), Release.RETURN_TO_USE, today=today)
    inc.close(i, today=today)
    s = build(today)
    [row] = rows_of(s, "incidents")
    assert row[0] == i.number and row[7] == "None needed" and row[17:] == ["1 of 1", "Closed"] and not s.gaps
    assert figures(s)["Devices held now"] == 0


def test_nothing_a_requester_typed_and_a_fixed_number_of_queries(today, dept, vent_model, pump_model):
    T = today

    def device(tag, model):
        return Asset.objects.create(tag=tag, device_model=model, department=dept, notes=SECRET, next_pm_on=T + 100 * DAY)

    def some(n, offset):
        for k in range(n):
            a = device(f"Q-{offset}-{k}", vent_model if k % 2 else pump_model)
            wo = create_work_order(asset=a, type=WoType.REPAIR, priority=Priority.HIGH, problem=SECRET, requester=SECRET, callback=SECRET,
                                   reported_location=SECRET, opened_on=T - 40 * DAY)
            make(a, outcome=Outcome.UNKNOWN, occurred_on=T - 40 * DAY, work_order=wo, hold=True)
            make(a, outcome=Outcome.DEATH, occurred_on=T - 40 * DAY, reportable=True, basis=Basis.MAY_HAVE, event_reference="SE-1",
                 decided_on=T - 39 * DAY, decided_by=DecidedBy.RISK, manufacturer_reported_on=T - 30 * DAY)
            make(a, outcome=Outcome.NO_HARM, affected=Affected.NONE, occurred_on=T - 50 * DAY, status=Status.IN_ERROR)

    def queries():
        with CaptureQueriesContext(connection) as q:
            s = build(T)
            rows = [list(r) for t in s.tables for r in t.rows()]
        return len(q.captured_queries), s, rows

    some(2, "a")
    few, s, rows = queries()
    assert SECRET not in repr(rows) + repr(s.gaps) + repr(s.figures)
    some(15, "b")
    many, s, rows = queries()
    assert len(rows) == 2 * 17 and len(s.gaps) == 5 * 17 and SECRET not in repr(rows) + repr(s.gaps)
    assert many == few, (few, many)
    assert few <= 4


# --- the links: the screen, the print, the API ------------------------------------------------------------------------------------

def test_the_screen_links_an_incident_to_its_drawer_for_incidents_view(client, ctx, tenant, today, vent, make_user, settings):
    settings.APP_BASE_URL = "https://ce.example.org"
    i = make(vent, outcome=Outcome.UNKNOWN, occurred_on=today - 40 * DAY)
    url = reverse("web:incident", args=[i.number])
    client.force_login(make_user("analyst"))  # Reports and Incidents View
    body = client.get("/reports/survey/").content.decode()
    assert f'<a class="link" href="{url}" hx-get="{url}" hx-target="#drawer">{i.number}</a>' in body  # the table's cell
    assert f'<a class="link sv-gap-r" href="{url}" hx-get="{url}" hx-target="#drawer">{i.number}</a>' in body  # the finding's
    printed = client.get("/print/survey/").content.decode()
    assert f'href="{url}?facility=riverside"' in printed
    data = client.get("/api/v1/survey/incidents/").json()
    assert data["tables"][0]["rows"][0][0] == i.number and data["gaps"][0]["url"] == f"{url}?facility=riverside"
    # a reader without Incidents View gets the number with no link (the section itself is left out for them: survey_refusal)
    quality = Role.objects.create(name="Quality", slug="quality")
    quality.set_levels({Module.REPORTS: Level.VIEW, Module.EQUIPMENT: Level.VIEW})
    reader = User.objects.create_user(username="q@riverside.example", password="Test-Pass-2026-x", tenant=tenant, role=quality)
    can = views_survey.linkable(reader)
    assert can[INCIDENT] is False and can[DEVICE] is True
    cells = views_survey.cells([i.number, "CE-10001"], {0: INCIDENT, 1: DEVICE}, can)
    assert (cells[0]["url"], cells[1]["url"]) == ("", reverse("web:asset", args=["CE-10001"]))
    assert views_survey.opens_in_drawer(url)


# --- maintenance: a held device past its PM date ---------------------------------------------------------------------------------

def _pm(asset, due):
    return create_work_order(asset=asset, type=WoType.PM, priority=Priority.NORMAL, problem=SECRET, opened_on=due - 20 * DAY, due_on=due,
                             source=Source.PM_PLANNER)


def test_a_held_device_past_its_pm_date_is_a_finding_never_a_gap(today, dept, vent, vent_model):
    T = today
    Asset.objects.filter(pk=vent.pk).update(next_pm_on=T - 5 * DAY)
    before = _pm(vent, T - 20 * DAY)  # already late when the device was held: the hold does not explain it
    during = _pm(vent, T - 5 * DAY)  # came due while held
    make(vent, outcome=Outcome.UNKNOWN, occurred_on=T - 10 * DAY, hold=True)
    other = Asset.objects.create(tag="CE-10009", device_model=vent_model, department=dept, next_pm_on=T - 5 * DAY)
    _pm(other, T - 5 * DAY)
    s = maintenance.build(period(T - 365 * DAY, T, T), None)
    gaps = {(g.kind, g.record) for g in s.gaps}
    assert (FINDING, "CE-10001") in gaps and (GAP, "CE-10001") not in gaps and (GAP, "CE-10009") in gaps
    [device] = [g for g in s.gaps if g.record == "CE-10001"]
    assert device.text == (f"CE-10001 (life support) is 5 days past its PM date ({_day(T - 5 * DAY)}): held as evidence for an incident "
                           f"investigation since {_day(T - 10 * DAY)}; not in use")
    assert device.url == reverse("web:asset", args=["CE-10001"])
    by_number = {g.record: g for g in s.gaps}
    assert by_number[before.number].kind == GAP and "with no reason recorded" in by_number[before.number].text
    assert by_number[during.number].kind == FINDING and by_number[during.number].text == (
        f"{during.number} on CE-10001 is 5 days past its due date: CE-10001 is held as evidence for an incident investigation since "
        f"{_day(T - 10 * DAY)}; not in use; the reason is recorded when the hold is released")
    assert {r[0]: r[3] for r in rows_of(s, "past_pm_date")} == {"CE-10001": HELD_LABEL, "CE-10009": "In service"}
    assert maintenance.HELD_NOTE in s.notes and SECRET not in repr(s.gaps)


def test_maintenance_reads_the_held_day_in_one_query_only_when_a_device_is_held(today, vent):
    T = today
    Asset.objects.filter(pk=vent.pk).update(next_pm_on=T - 5 * DAY)
    _pm(vent, T - 5 * DAY)

    def count():
        with CaptureQueriesContext(connection) as q:
            maintenance.build(period(T - 365 * DAY, T, T), None)
        return len(q.captured_queries)

    plain = count()
    make(vent, outcome=Outcome.UNKNOWN, occurred_on=T - 10 * DAY, hold=True)
    assert count() == plain + 1


# --- the held label in the binder's status columns --------------------------------------------------------------------------------

def test_the_binder_words_a_held_device_as_every_screen_does(today, dept, vent, pump_model):
    make(vent, outcome=Outcome.UNKNOWN, hold=True)
    p = period(today - 365 * DAY, today, today)
    devices = {r[0]: r[8] for r in rows_of(inventory.build(p, None), "devices")}
    assert devices["CE-10001"] == HELD_LABEL
    new = Asset.objects.create(tag="CE-30001", device_model=pump_model, department=dept, added_as=AddedAs.NEW, status=AssetStatus.OUT_OF_SERVICE)
    Asset.history.filter(id=new.pk).update(incident_hold=True)  # its first row as if held: the copy reads the label first
    rows = {r[0]: r[4] for r in rows_of(inspections.build(p, None), "new_devices")}
    assert rows["CE-30001"] == HELD_LABEL


# --- the demo seed ----------------------------------------------------------------------------------------------------------------

def test_the_demo_seeds_an_open_and_a_closed_incident(db):
    call_command("seed_demo", stdout=StringIO())
    with tenant_context(Tenant.objects.get(slug="riverside")):
        today = timezone.localdate()
        closed, open_ = Incident.objects.order_by("number")
        # the closed one: two months back, no harm, met its specifications, decided not serious by risk management, returned to use
        occurred = today - timedelta(days=seed_demo.CLOSED_INCIDENT_DAYS_AGO)
        steps = {k: occurred + timedelta(days=n) for k, n in seed_demo.CLOSED_INCIDENT_STEPS.items()}
        assert (closed.status, closed.outcome, closed.affected, closed.finding, closed.basis, closed.decided_by, closed.reportable) == (
            Status.CLOSED, Outcome.NO_HARM, Affected.PATIENT, Finding.MET_SPECS, Basis.NOT_SERIOUS, DecidedBy.RISK, False)
        assert (closed.occurred_on, closed.decided_on, closed.closed_on, closed.asset.device_model.model) == (
            occurred, steps["decided"], steps["closed"], seed_demo.CLOSED_INCIDENT_MODEL)
        assert closed.recorded_by.username == "rfeldman@riverside.example" and closed.created_by.role.slug == "technician"
        hold = closed.holds.get()
        assert (hold.release, hold.released_on, hold.held_on) == (Release.RETURN_TO_USE, steps["released"], occurred)
        assert closed.asset.status == AssetStatus.IN_SERVICE and not closed.asset.incident_hold
        wo = closed.work_order
        assert closed.opened_work_order and wo.status == WoStatus.CLOSED and wo.completed_on == steps["completed"] and wo.resolution
        history = [(r.history_change_reason, r.history_date.astimezone(timezone.get_current_timezone()).date())
                   for r in closed.history.order_by("history_date")]
        assert history == [("Recorded", occurred), ("Finding recorded", steps["completed"]), ("Decided: not reportable", steps["decided"]),
                           ("Closed", steps["closed"])]
        # the open one: a portal request adopted as the investigation, in progress; the defibrillator held; the decision pending
        assert (open_.status, open_.outcome, open_.affected, open_.accessories, open_.event_log, open_.reportable) == (
            Status.OPEN, Outcome.UNKNOWN, Affected.PATIENT, Accessories.KEPT, EventLog.SAVED, None)
        assert open_.report_due_on == add_work_days(open_.aware_on, 10) and work_days_between(today, open_.report_due_on) == seed_demo.OPEN_INCIDENT_LEFT
        wo = open_.work_order
        assert not open_.opened_work_order and wo.source == Source.PORTAL and wo.status == WoStatus.IN_PROGRESS and wo.tagged_out
        assert wo.opened_on == open_.occurred_on and wo.service_request.created_at.astimezone(timezone.get_current_timezone()).date() == open_.occurred_on
        assert open_.asset.incident_hold and open_.asset.status == AssetStatus.OUT_OF_SERVICE and open_.event_reference
        assert inc.badge(today) == (1, False)  # due in 4 work days: on the badge, not hot yet
        assert open_.holds.get().held_on == open_.occurred_on
        # the binder: both listed, nothing to fix, the device held now
        s = incidents.build(period(today - 365 * DAY, today, today), None)
        assert [r[0] for r in rows_of(s, "incidents")] == [open_.number, closed.number] and not s.gaps
        assert figures(s)["Devices held now"] == 1 and figures(s)["Decisions pending"] == 1
        assert not WorkOrder.objects.filter(asset=open_.asset).exclude(pk=wo.pk).exclude(status__in=(WoStatus.COMPLETED, WoStatus.CLOSED)).exists()


# --- row-level security -----------------------------------------------------------------------------------------------------------

@needs_postgres
def test_the_section_reads_its_history_under_the_policies(tenant, other_tenant, today, vent):
    """As the runtime role: the incident's history (the clock that ran) is this facility's only."""
    i = inc.record_incident(asset=vent, outcome=Outcome.UNKNOWN, affected=Affected.PATIENT, hold=False, event_reference="SE-9", today=today)
    inc.decide(i, outcome=Outcome.INJURY, basis=Basis.NOT_SERIOUS, decided_on=today, decided_by=DecidedBy.CE, today=today)
    held = make(vent, outcome=Outcome.UNKNOWN, occurred_on=today - 40 * DAY, hold=True, open_work_order=True)
    as_app_role()
    with tenant_context(tenant):
        s = build(today)
        assert figures(s)["With the report clock"] == 2 and figures(s)["Devices held now"] == 1
        assert {g.record for g in s.gaps} == {held.number}
    with tenant_context(other_tenant):
        s = build(today)
        assert not rows_of(s, "incidents") and figures(s)["Devices held now"] == 0
