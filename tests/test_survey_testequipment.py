"""
Slice 35, wave 2D: the survey binder's test equipment section (apps/reports/survey/testequipment.py), its place and access (Reports View,
Equipment View, Work orders View), and the page, CSV, and API serving it.

The section: every item not retired before the period began with its standing today and at the period's end ("on file since" when its
certificate was entered later; not in Cadence yet when added after the period); the period's certificates with their tolerance reviews
(entered in error left out); the work done with test equipment past due, with no calibration on file, failed, found out of tolerance
later, or judged on a certificate entered after it; the work with readings naming no test equipment since the first item was entered.
GAPs: an item in use past due, with no calibration on file, or failed; a certificate that needs a review and has none, while its window
touches the period; a review whose window now holds work outside its frozen list. FINDINGs: one per item and stretch. CHECKs: the work
naming no test equipment (one, with the count), a certificate adjusted with its as found not reported. A fixed number of queries;
nothing a requester typed; another facility's records never read; under row-level security as the runtime role.
"""
from datetime import datetime, time, timedelta

import pytest
from csvutil import csv_rows
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from pg_helpers import as_app_role, needs_postgres
from survey_helpers import gaps_of, period, rows_of
from testequipment_fixtures import (
    certificate,
    failed,
    imported_work,
    in_date,
    item,
    no_record,
    out_of_tolerance,
    past_due,
    pm_with,
    repair_with,
    safety_with,
    vendor_repair,
)

from apps.accounts.models import Level, Module, Role, User
from apps.credentials.models import Technician
from apps.equipment.models import Asset, Department, DeviceModel, RiskClass
from apps.reports import survey
from apps.reports.permissions import survey_refusal
from apps.reports.survey import CHECK, DEVICE, FINDING, GAP, TEST_EQUIPMENT, WORK_ORDER, gap_links
from apps.reports.survey import testequipment as section
from apps.tenants.context import tenant_context
from apps.testequipment import services
from apps.testequipment.models import AsFound, Basis, Calibration, Reason, Status, TestEquipment
from apps.vendors.rules import fmt
from apps.workorders.models import WorkOrder
from apps.workorders.services import set_test_equipment

DAY = timedelta(days=1)
SECRET = "Zq7Canary bed 4"  # what a requester typed: never in the binder
ESA = "(Fluke Biomedical ESA612)"
REMEDY = "If a certificate in force on those days was not recorded, record it: the work is then judged on it, showing the day it was entered."


@pytest.fixture
def today(ctx):
    return timezone.localdate()


def _noon(day):
    return timezone.make_aware(datetime.combine(day, time(12)))


def added_on(te, day):
    """As if the item was entered in Cadence on `day` (its history's first row)."""
    TestEquipment.history.filter(id=te.pk, history_type="+").update(history_date=_noon(day))
    return te


def entered_on(cal, day):
    """As if the certificate was entered in Cadence on `day`."""
    Calibration.history.filter(id=cal.pk, history_type="+").update(history_date=_noon(day))
    return cal


def with_reading(wo, reading="21 µA"):
    """The work order as completed with a measured step's reading (a PM's electrical safety step)."""
    WorkOrder.objects.filter(pk=wo.pk).update(checklist_results=[{"text": "Electrical safety test", "measure": "leakage µA", "result": "pass",
                                                                  "reading": reading}])
    wo.refresh_from_db()
    return wo


def build(today, start=None, end=None):
    return section.build(period(start or today - 200 * DAY, end or today, today), None)


def figures(s) -> dict:
    return {f.label: f.value for f in s.figures}


def listed(s, kind=None) -> list:
    return [(g.kind, g.record) for g in gaps_of(s, kind)]


# --- an empty facility, and the items -------------------------------------------------------------------------------------------


def test_a_facility_with_no_test_equipment_has_no_gaps(today, pump):
    with_reading(pm_with(pump, completed_on=today - 3 * DAY))
    s = build(today)
    assert (s.key, s.title) == ("testequipment", "Test equipment calibration") and s.gaps == []
    assert all(rows_of(s, t.key) == [] for t in s.tables)
    f = figures(s)
    assert f["Test equipment"] == 0 and f["Work done with test equipment"] == 0 and f["Work with readings naming no test equipment"] == 0
    assert f["Work with readings before test equipment was entered"] == 1  # nothing to name yet: counted, never a check


def test_the_items_with_their_standing_today_and_at_the_periods_end(today):
    start, end = today - 200 * DAY, today - 30 * DAY
    good = added_on(in_date("TE-IN"), today - 300 * DAY)  # calibrated 100 days ago, its certificate entered today
    late = added_on(past_due("TE-PD"), today - 300 * DAY)  # due 35 days ago, entered today
    no_record("TE-NEW")  # entered today, after the period's end
    old = added_on(item("TE-OLD"), today - 400 * DAY)
    services.set_status(old, to=Status.RETIRED, reason=Reason.WORN_OUT, by=None, today=start - DAY)  # before the period: left out
    gone = added_on(item("TE-GONE"), today - 400 * DAY)
    services.set_status(gone, to=Status.RETIRED, reason=Reason.WORN_OUT, by=None, today=start + DAY)  # during it: listed
    s = section.build(period(start, end, today), None)
    rows = {r[0]: r for r in rows_of(s, "items")}
    assert list(rows) == ["TE-GONE", "TE-IN", "TE-NEW", "TE-PD"]
    good_due = good.calibrations.get().due_on
    assert rows["TE-IN"] == ["TE-IN", "Electrical safety analyzer", "Fluke Biomedical ESA612", "SN-TE-IN", "Calibration", 12, "In use", today,
                             "", f"Calibration due {fmt(good_due)}", f"Calibration due {fmt(good_due)} (on file since {fmt(today)})"]
    was_due = fmt(late.calibrations.get().due_on)
    assert rows["TE-PD"][9:] == [f"Calibration was due {was_due}", f"Calibration was due {was_due} (on file since {fmt(today)})"]
    assert rows["TE-NEW"][9:] == ["No calibration on file", section.NOT_YET]
    assert rows["TE-GONE"][6:8] == ["Retired (worn out or obsolete)", start + DAY]
    assert s.table("items").links == {0: TEST_EQUIPMENT} and figures(s)["Test equipment"] == 4 and figures(s)["In use today"] == 3


def test_an_item_in_use_past_due_with_no_calibration_on_file_or_failed_is_a_gap(today):
    in_date("TE-IN")
    pd, fl = past_due("TE-PD"), failed("TE-FL")
    no_record("TE-NR")
    s = build(today)
    assert listed(s, GAP) == [(GAP, "TE-NR"), (GAP, "TE-PD"), (GAP, "TE-FL")]  # the items in use, then the review TE-FL's failure needs
    nr, pd_gap = gaps_of(s, GAP)[:2]
    assert nr.text == f"TE-NR {ESA} is in use with no calibration on file. Record its latest certificate, or move it out for calibration."
    assert pd_gap.text == (f"TE-PD {ESA} is in use: calibration was due {fmt(today - 35 * DAY)}. Record its new certificate, or move it out "
                           "for calibration.")
    assert pd_gap.url == "/test-equipment/TE-PD/"
    f = figures(s)
    assert (f["In use today"], f["In use and past due today"], f["In use with no calibration on file today"]) == (3, 1, 1)
    services.set_status(pd, to=Status.OUT_FOR_CALIBRATION, by=None)  # at the lab: not in use
    assert listed(build(today), GAP) == [(GAP, "TE-NR"), (GAP, "TE-FL")]
    # a failed item back in use (retired, then reinstated) while its failed certificate still governs
    services.set_status(fl, to=Status.RETIRED, reason=Reason.WORN_OUT, by=None)
    services.set_status(fl, to=Status.IN_USE, by=None)
    gap = gaps_of(build(today), GAP)[0]
    assert gap.record == "TE-FL" and gap.text == (f"TE-FL {ESA} is in use: failed calibration {fmt(today - 10 * DAY)}. Take it out of use, "
                                                  "or record the certificate that passed it since.")


# --- the tolerance reviews ------------------------------------------------------------------------------------------------------


def test_a_certificate_that_found_it_out_of_tolerance_needs_its_review(today, vent):
    ot = out_of_tolerance(vent, "TE-OT1")
    found = fmt(today - 10 * DAY)
    [gap] = gaps_of(build(today), GAP)
    assert (gap.record, gap.url) == ("TE-OT1", "/test-equipment/TE-OT1/")
    assert gap.text == (f"TE-OT1 (Fluke Biomedical IDA-5): certificate {ot.found.certificate} ({found}) found it out of tolerance, and the "
                        f"2 work orders done with it after its previous passed certificate ({fmt(today - 200 * DAY)}) through {found} are "
                        "not reviewed. Record the tolerance review on its page.")
    # dated after the period's end, its window still holds the period's work; one ending before the period does not
    assert listed(build(today, end=today - 20 * DAY), GAP) == [(GAP, "TE-OT1")]
    assert build(today, start=today - 5 * DAY).gaps == []
    services.decide_review(ot.found, basis=Basis.WITHIN_MARGIN, decided_on=today, version=None, by=None)
    assert build(today).gaps == []
    # work recorded in the window since the review: no decision covers it
    more = pm_with(vent, completed_on=today - 30 * DAY)
    set_test_equipment(more, [ot.item.pk], by=None)
    [gap] = gaps_of(build(today), GAP)
    assert gap.text == (f"TE-OT1 (Fluke Biomedical IDA-5): the tolerance review of certificate {ot.found.certificate} ({fmt(today)}) covered "
                        "2 work orders; 1 more work order done with it in the same window was recorded since, and no decision covers it. "
                        "Decide about it in the facility's quality record (its page lists it).")
    row = next(r for r in rows_of(build(today), "calibrations") if r[4] == ot.found.certificate)
    assert row[-1] == "Reviewed " + fmt(today) + ": the error was smaller than the margin to the limits used; 1 work order added since the review"
    services.mark_entered_in_error(ot.found, by=None)  # the review stops counting with it
    assert build(today).gaps == []


def test_a_failed_certificate_with_no_work_since_the_last_passed_one(today):
    fl = failed("TE-FL")
    services.set_status(fl, to=Status.RETIRED, reason=Reason.RETURNED_TO_OWNER, by=None)  # out of use: only the review is a gap
    [gap] = gaps_of(build(today))
    assert gap.text == (f"TE-FL {ESA}: certificate {fl.calibrations.get().certificate} ({fmt(today - 10 * DAY)}) failed it, and it is not "
                        f"reviewed (no work is recorded with it on or before {fmt(today - 10 * DAY)}). Record the tolerance review on its page.")


# --- findings -------------------------------------------------------------------------------------------------------------------


def test_one_finding_per_item_and_stretch(today, pump, vent):
    pd = past_due("TE-PD")  # due 35 days ago
    pm_with(pump, pd, completed_on=today - 20 * DAY)
    pm_with(vent, pd, completed_on=today - 10 * DAY)
    nr = no_record("TE-NR")
    late = pm_with(pump, nr, completed_on=today - 5 * DAY)
    fl = failed("TE-FL")  # failed 10 days ago; out of service since today, so work completed today may still record it
    pm_with(vent, fl)
    s = build(today)
    assert listed(s, FINDING) == [(FINDING, "TE-FL"), (FINDING, "TE-NR"), (FINDING, "TE-PD")]
    texts = [g.text for g in gaps_of(s, FINDING)]
    assert texts == [
        f"TE-FL {ESA} was used on 1 work order after it failed, {fmt(today)} (failed calibration {fmt(today - 10 * DAY)}). {REMEDY}",
        f"TE-NR {ESA} was used on 1 work order with no calibration on file, {fmt(today - 5 * DAY)}. {REMEDY}",
        f"TE-PD {ESA} was used on 2 work orders while past due, {fmt(today - 20 * DAY)} to {fmt(today - 10 * DAY)} (calibration was due "
        f"{fmt(today - 35 * DAY)}). {REMEDY}",
    ]
    assert all(g.url == f"/test-equipment/{g.record}/" for g in gaps_of(s, FINDING))
    f = figures(s)
    assert f["Work done with test equipment"] == 4 and f["Done with test equipment past due, with no calibration on file, or failed"] == 4
    # a certificate in force that day, entered late, clears the finding, and the work says when it was entered
    cal = certificate(nr, today - 60 * DAY)
    s = build(today)
    assert (FINDING, "TE-NR") not in listed(s, FINDING)
    row = next(r for r in rows_of(s, "work") if r[0] == late.number)
    assert row[4:] == ["TE-NR", f"Calibration due {fmt(cal.due_on)}", today, ""]


def test_two_stretches_of_one_item_are_two_findings(today, pump):
    te = item("TE-2X")
    certificate(te, today - 500 * DAY, due_on=today - 400 * DAY)
    first = pm_with(pump, te, completed_on=today - 390 * DAY)
    certificate(te, today - 380 * DAY, due_on=today - 100 * DAY)
    second = pm_with(pump, te, completed_on=today - 50 * DAY)
    s = build(today, start=today - 400 * DAY)
    texts = [g.text for g in gaps_of(s, FINDING)]
    assert texts == [
        f"TE-2X {ESA} was used on 1 work order while past due, {fmt(first.completed_on)} (calibration was due {fmt(today - 400 * DAY)}). {REMEDY}",
        f"TE-2X {ESA} was used on 1 work order while past due, {fmt(second.completed_on)} (calibration was due {fmt(today - 100 * DAY)}). {REMEDY}",
    ]


# --- the tables -----------------------------------------------------------------------------------------------------------------


def test_the_work_found_out_of_tolerance_later_and_what_is_left_out(today, pump, vent):
    good = in_date("TE-IN")
    entered_on(good.calibrations.get(), today - 100 * DAY)  # entered the day it came back
    pm_with(pump, good, completed_on=today - 3 * DAY)  # nothing to say: not listed
    ot = out_of_tolerance(vent, "TE-OT1")
    for cal in ot.item.calibrations.all():
        entered_on(cal, cal.calibrated_on)
    vendor_repair(pump, completed_on=today - 5 * DAY)
    imported_work(vent)
    s = build(today)
    due = fmt(ot.before.due_on)
    assert rows_of(s, "work") == [[w.number, "Preventive maintenance", w.completed_on, "CE-10001", "TE-OT1", f"Calibration due {due}", None,
                                   f"found out of tolerance on {fmt(today - 10 * DAY)}; not reviewed"] for w in ot.work]
    assert s.table("work").links == {0: WORK_ORDER, 3: DEVICE, 4: TEST_EQUIPMENT}
    f = figures(s)
    assert f["Work done with test equipment"] == 3 and f["Done with test equipment found out of tolerance later"] == 2
    assert f["Done with test equipment past due, with no calibration on file, or failed"] == 0 and gaps_of(s, FINDING) == []


def test_the_calibrations_in_the_period(today, vent):
    ot1 = out_of_tolerance(vent, "TE-OT1")
    ot2 = out_of_tolerance(vent, "TE-OT2", reviewed=True, model="IDA-6")
    te = in_date("TE-IN")
    wrong = certificate(te, today - 50 * DAY, number="WRONG-1")
    services.mark_entered_in_error(wrong, by=None)
    s = build(today, start=today - 150 * DAY)
    rows = rows_of(s, "calibrations")
    assert [(r[0], r[1]) for r in rows] == [("TE-IN", today - 100 * DAY), ("TE-OT1", today - 10 * DAY), ("TE-OT2", today - 10 * DAY)]
    assert rows[0][2:] == [te.calibrations.get(entered_in_error=False).due_on, "Fluke Biomedical", f"C-TE-IN-{today - 100 * DAY:%Y%m%d}",
                           "In tolerance as found", "No", "Passed", "Yes", today, section.NOT_NEEDED]
    assert rows[1][2:] == [ot1.found.due_on, "Fluke Biomedical", ot1.found.certificate, "Out of tolerance as found", "Yes", "Passed", "Yes",
                           today, section.NOT_REVIEWED]
    assert rows[2][-1] == f"Reviewed {fmt(ot2.review.decided_on)}: the error was smaller than the margin to the limits used"
    f = {x.label: x for x in s.figures}
    assert f["Calibrations in the period"].value == 3 and f["Calibrations in the period"].hint == "1 more entered in error, left out"
    assert f["Found out of tolerance or failed"].value == 2 and f["Of those, reviewed"].value == 1
    assert "Certificates entered in error are left out (1 dated in the period)." in s.notes
    assert s.table("calibrations").links == {0: TEST_EQUIPMENT}


# --- checks ---------------------------------------------------------------------------------------------------------------------


def test_work_with_readings_naming_no_test_equipment_since_the_first_was_entered(today, pump, vent):
    te = added_on(in_date("TE-IN"), today - 30 * DAY)
    with_reading(pm_with(pump, completed_on=today - 40 * DAY))  # before test equipment was entered: a figure apart
    after = with_reading(pm_with(vent, completed_on=today - 20 * DAY))
    safety = safety_with(pump, completed_on=today - 10 * DAY)  # a safety check names its analyzer, readings or not
    with_reading(pm_with(pump, te, completed_on=today - 5 * DAY))  # names it
    pm_with(vent, completed_on=today - 4 * DAY)  # no reading
    with_reading(repair_with(vent, te, completed_on=today - 4 * DAY))
    vendor_repair(pump, completed_on=today - 3 * DAY)  # the vendor's report names theirs
    with_reading(imported_work(vent, completed_on=today - 2 * DAY))  # brought over: offers nothing
    s = build(today)
    [check] = gaps_of(s, CHECK)
    assert check.text == (f"2 work orders completed {fmt(today - 20 * DAY)} to {fmt(today - 10 * DAY)}, since test equipment was first "
                          f"entered in Cadence ({fmt(today - 30 * DAY)}), recorded readings or were safety checks but name no test "
                          "equipment: record the test equipment used on each (the table lists them).")
    assert (check.url, check.record) == ("", "")
    assert rows_of(s, "unnamed") == [[after.number, "Preventive maintenance", today - 20 * DAY, "CE-10001"],
                                     [safety.number, "Safety check", today - 10 * DAY, "CE-10002"]]
    assert s.table("unnamed").links == {0: WORK_ORDER, 3: DEVICE}
    f = figures(s)
    assert f["Work with readings naming no test equipment"] == 2 and f["Work with readings before test equipment was entered"] == 1


def test_a_certificate_adjusted_with_its_as_found_not_reported_is_a_check(today):
    te = in_date("TE-IN")
    certificate(te, today - 20 * DAY, as_found=AsFound.NOT_REPORTED, adjusted=True, number="KCL-77")
    [check] = gaps_of(build(today))
    assert (check.kind, check.record, check.url) == (CHECK, "TE-IN", "/test-equipment/TE-IN/")
    assert check.text == (f"TE-IN {ESA}: certificate KCL-77 ({fmt(today - 20 * DAY)}) says it was adjusted but does not report what it "
                          "found before: ask the provider for the as-found data (it shows whether the work done with it before needs a review).")
    assert build(today, start=today - 10 * DAY).gaps == []  # dated before the period
    certificate(te, today - 5 * DAY, as_found=AsFound.NOT_REPORTED, adjusted=False, number="KCL-78")  # not adjusted: nothing to ask
    assert build(today, start=today - 10 * DAY).gaps == []


def test_a_gaps_link_needs_equipment_view(today, ctx, make_user):
    past_due("TE-PD")
    gap = gaps_of(build(today), GAP)[0]
    role = Role.objects.create(name="Work orders only", slug="wo-only")
    role.set_levels({Module.REPORTS: Level.VIEW, Module.WORKORDERS: Level.VIEW})
    user = User.objects.create_user(username="wo@riverside.example", password="Test-Pass-2026-x", tenant=ctx, role=role)
    assert gap_links(make_user("director"))(gap) == "/test-equipment/TE-PD/" and gap_links(user)(gap) == ""


# --- queries, requester text, other facilities ----------------------------------------------------------------------------------


def _world(today, pump, vent, n: int, tag: str):
    """n of each thing the section reads: items in each standing, work with each, reviews, checks."""
    for i in range(n):
        t = f"{tag}{i}"
        pd, good, nr = added_on(past_due(f"PD-{t}"), today - 300 * DAY), in_date(f"IN-{t}"), no_record(f"NR-{t}")
        for wo in (pm_with(pump, pd, completed_on=today - (20 + i) * DAY), pm_with(vent, good, nr, completed_on=today - (5 + i) * DAY),
                   with_reading(pm_with(pump, completed_on=today - (3 + i) * DAY)), safety_with(vent, completed_on=today - (2 + i) * DAY)):
            WorkOrder.objects.filter(pk=wo.pk).update(problem=SECRET, requester=SECRET, resolution=SECRET)
        ot = out_of_tolerance(vent, f"OT-{t}", model=f"IDA-{t}")
        services.decide_review(ot.found, basis=Basis.RECHECKED, decided_on=today, version=None, by=None)
        set_test_equipment(pm_with(vent, completed_on=today - (30 + i) * DAY), [ot.item.pk], by=None)
        out_of_tolerance(vent, f"OU-{t}", model=f"IDB-{t}")
        certificate(good, today - (40 + i) * DAY, as_found=AsFound.NOT_REPORTED, adjusted=True, number=f"NR-{t}")
        services.mark_entered_in_error(certificate(good, today - (60 + i) * DAY, number=f"ERR-{t}"), by=None)
        gone = item(f"GO-{t}")
        services.set_status(gone, to=Status.RETIRED, reason=Reason.WORN_OUT, by=None)


def test_a_fixed_number_of_queries_and_nothing_a_requester_typed(today, pump, vent):
    def read() -> tuple:
        with CaptureQueriesContext(connection) as q:
            s = build(today)
            rows = [list(r) for t in s.tables for r in t.rows()]
        text = " ".join([*(g.text for g in s.gaps), *(f"{f.value}{f.hint}" for f in s.figures), *s.notes, *(str(c) for r in rows for c in r)])
        assert SECRET not in text
        return len(q), s.counts()

    _world(today, pump, vent, 1, "a")
    small, counts = read()
    # past due, no record, a review's added work, a review missing; two stretches; the unnamed work and the as found not reported
    assert counts == {"gap": 4, "finding": 2, "check": 2}
    _world(today, pump, vent, 3, "b")
    big, counts = read()
    assert big == small <= 8 and counts == {"gap": 16, "finding": 8, "check": 5}


def test_another_facilitys_test_equipment_stays_there(today, ctx, other_tenant):
    with tenant_context(other_tenant):
        dept = Department.objects.create(name="OR")
        model = DeviceModel.objects.create(manufacturer="GE", model="Carescape B650", description="Monitor", category="Monitors",
                                           risk_class=RiskClass.HIGH)
        theirs = Asset.objects.create(tag="OT-1", device_model=model, department=dept)
        pd = past_due("TE-PD")
        pm_with(theirs, pd, completed_on=timezone.localdate() - 3 * DAY, tech=Technician.objects.create(name="Pat Lee"))
        out_of_tolerance(theirs, "TE-OT1")
        assert len(gaps_of(build(timezone.localdate()))) == 3
    s = build(today)
    assert s.gaps == [] and all(rows_of(s, t.key) == [] for t in s.tables) and figures(s)["Test equipment"] == 0


# --- its place, access, and the screens -----------------------------------------------------------------------------------------


def test_the_section_follows_repairs_and_needs_equipment_and_work_orders_view(ctx, make_user):
    keys = survey.section_keys()
    assert keys[keys.index("repairs") + 1] == "testequipment" and survey.section_title("testequipment") == section.TITLE
    for slug in ("director", "manager", "technician", "analyst"):
        assert survey_refusal(make_user(slug), "testequipment") == "", slug
    for module, missing in ((Module.WORKORDERS, "Equipment"), (Module.EQUIPMENT, "Work orders")):
        role = Role.objects.create(name=f"Reports and {module}", slug=f"reports-{module}")
        role.set_levels({Module.REPORTS: Level.VIEW, module: Level.VIEW})
        user = User.objects.create_user(username=f"{module}@riverside.example", password="Test-Pass-2026-x", tenant=ctx, role=role)
        assert survey_refusal(user, "testequipment") == f"Needs {missing} View, which your role does not have.", module


def test_the_page_the_csvs_and_the_api_serve_it(client, today, pump, make_user):
    pd = past_due("TE-PD")
    wo = pm_with(pump, pd, completed_on=today - 3 * DAY)
    client.force_login(make_user("director"))
    body = client.get("/reports/survey/").content.decode()
    sec = body[body.index('id="sv-testequipment"'):body.index('id="sv-recalls"')]
    assert "Test equipment calibration" in sec and 'href="/test-equipment/TE-PD/" hx-get="/test-equipment/TE-PD/"' in sec  # the drawer
    assert f'href="/work-orders/{wo.number}/"' in sec
    rows = csv_rows(client.get("/reports/survey/testequipment/items.csv"))
    assert rows[0] == section.ITEM_COLUMNS and rows[1][0] == "TE-PD"
    rows = csv_rows(client.get("/reports/survey/testequipment/work.csv"))
    assert rows[0] == section.WORK_COLUMNS and rows[1][:2] == [wo.number, "Preventive maintenance"]
    data = client.get("/api/v1/survey/testequipment/").json()
    assert data["key"] == "testequipment" and {g["record"] for g in data["gaps"]} == {"TE-PD"}
    print_body = client.get("/print/survey/").content.decode()
    assert "Test equipment calibration" in print_body and "/test-equipment/TE-PD/?facility=riverside" in print_body


@needs_postgres
def test_the_section_under_row_level_security(today, pump, vent):
    pd = past_due("TE-PD")
    pm_with(pump, pd, completed_on=today - 3 * DAY)
    out_of_tolerance(vent, "TE-OT1")
    as_app_role()
    s = build(today)
    assert listed(s) == [(GAP, "TE-PD"), (GAP, "TE-OT1"), (FINDING, "TE-PD")]
    assert [r[0] for r in rows_of(s, "items")] == ["TE-OT1", "TE-PD"] and len(rows_of(s, "work")) == 3
