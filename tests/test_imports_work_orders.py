"""
Work order history imports (slice 23, part B): apps.imports.kinds.work_orders reading the file, apps.workorders.legacy recording each
work order in its final state, and apps.workorders.costs' import-only writer recording its lines. A check that changes nothing and
takes no number, history as it was (status, dates, technician, vendor, tagged out, the timeline in business order), costs as lines
(rates from cost over hours, today's rate for hours alone, outside service, a total alone, the notes), the rows refused and why, a
re-run that never doubles, the previous number in the drawer, the print, both searches, and the API, the KPIs reading imported
history, facility isolation, and the same under PostgreSQL's policies.
"""
import csv
import io
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone
from pg_helpers import as_app_role, needs_postgres

from apps.core.models import Sequence
from apps.credentials.models import Technician
from apps.equipment.models import Asset, AssetStatus, Department, DeviceModel
from apps.facility.services import get_settings
from apps.imports import base, kinds, services
from apps.imports.kinds.work_orders import STATUS_WORDS, TYPE_WORDS, person_key
from apps.imports.models import ImportRun
from apps.pm.services import pm_on_time_rate
from apps.reports.services import cost_of_service, overview_kpis
from apps.tenants.context import tenant_context
from apps.workorders import costs, legacy
from apps.workorders.models import LaborLine, PartLine, Source, WorkOrder, WoStatus, WoType
from apps.workorders.services import WorkOrderFilters, change_status, create_work_order, filter_work_orders, timeline

TODAY = date.today()
COLS = ["number", "tag", "type", "priority", "status", "opened", "due", "completed", "technician", "vendor", "hours", "labor", "parts", "outside",
        "total"]
# Headers as another CMMS exports them: every one maps by alias.
HEADER = ["WO #", "Control No.", "Work Type", "Priority", "Status", "Date Opened", "Date Due", "Date Completed", "Technician", "Vendor",
          "Labor Hours", "Labor Cost", "Parts Cost", "Outside Cost", "Total Cost"]


def line(**values) -> list[str]:
    row = {"tag": "CE-10002", "type": "Repair", "status": "Closed", "opened": "2024-03-04", "completed": "2024-03-06", **values}
    return [str(row.get(c, "")) for c in COLS]


def csv_bytes(*rows, header=HEADER) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    writer.writerows(rows)
    return buf.getvalue().encode()


def run_through(run, user):
    run = services.process(run, user)
    while run.status in (ImportRun.Status.CHECKING, ImportRun.Status.IMPORTING):
        run = services.process(run, user)
    return run


def run_file(user, *rows, import_it=True):
    run = services.upload(user, "work_orders", "work-orders.csv", csv_bytes(*rows))
    run = services.confirm_columns(run, user, services.mapping_of(run))
    run = run_through(run, user)
    assert run.status == "checked"
    if import_it:
        run = run_through(services.start_import(run, user), user)
        assert run.status == "imported"
    return run


def notes_by_key(run) -> dict:
    return {key: notes for _line, key, _outcome, notes in run.results}


def by_legacy(number) -> WorkOrder:
    return WorkOrder.objects.get(legacy_number=number)


@pytest.fixture
def kim(make_user):
    return make_user("director")


@pytest.fixture
def former(ctx):
    """A technician who has left: history names them."""
    return Technician.objects.create(name="Maria Lopez", title="BMET II", is_active=False)


# --- the columns ----------------------------------------------------------------------------------------------------------------

def test_the_columns_map_by_alias_and_read_no_free_text():
    importer = kinds.get("work_orders")
    assert base.auto_map(importer.columns, HEADER) == {key: i for i, key in enumerate(["legacy_number", *COLS[1:]])}
    aliases = [a for c in importer.columns for a in c.aliases]
    assert len(aliases) == len(set(aliases))  # no file column could feed two values
    for word in ("problem", "description", "resolution", "comments", "notes", "requester", "caller", "phone", "contact", "patient", "location"):
        assert not any(word in a.split() for a in aliases), word
    assert "id" not in aliases  # never a bare id (a database's own row number)
    assert [c.key for c in importer.required()] == ["legacy_number", "tag", "type", "status", "opened"]
    assert TYPE_WORDS["corrective"] == WoType.REPAIR and TYPE_WORDS["install"] == WoType.INSPECTION and TYPE_WORDS["ppm"] == WoType.PM
    assert STATUS_WORDS["void"] == WoStatus.CANCELLED and STATUS_WORDS["waiting on parts"] == WoStatus.AWAITING_PARTS
    assert person_key("Whitfield,  Dana") == person_key("dana WHITFIELD") == "dana whitfield"
    assert costs.IMPORT_HOURS_MAX == Decimal("9999.99") and costs.IMPORT_RATE_MAX == Decimal("999999.99")
    assert costs.IMPORT_AMOUNT_MAX == Decimal("99999999.99")


# --- the check, and the import --------------------------------------------------------------------------------------------------

def test_the_check_changes_nothing_and_takes_no_number(ctx, kim, pump, techs):
    rows = [line(number="10423", technician="Dana Whitfield", hours="1.5", labor="123", parts="84"),
            line(number="10424", type="PM", status="Completed", due="Mar 2024", completed="2024-03-20")]
    run = run_file(kim, *rows, import_it=False)
    assert run.counts == {"create": 2} and not WorkOrder.objects.exists() and not LaborLine.objects.exists()
    assert not Sequence.objects.filter(key="wo-2024").exists()  # the stand-in number took no lock on the facility's numbering
    run = run_through(services.start_import(run, kim), kim)
    assert run.status == "imported" and run.counts == {"create": 2}
    assert sorted(WorkOrder.objects.values_list("number", flat=True)) == ["WO-24-0001", "WO-24-0002"]
    assert by_legacy("10424").due_on == date(2024, 3, 31)  # a month alone is its last day


def test_history_arrives_in_its_final_state_and_touches_nothing_else(ctx, kim, pump, former, mailoutbox):
    pump.last_pm_on, pump.next_pm_on = date(2024, 1, 10), date(2025, 1, 10)
    pump.save()
    run_file(kim, line(number="10423", technician="Lopez, Maria", priority="Urgent", due="2024-03-05"),
             line(number="10500", status="In progress", opened="2024-05-01", completed="", vendor="BD field service", technician=""),
             line(number="10501", type="PM", status="Closed", due="2024-04-30", completed="2024-04-20", technician="maria lopez"))
    wo = by_legacy("10423")
    assert (wo.source, wo.status, wo.type, wo.priority) == (Source.IMPORTED, WoStatus.CLOSED, WoType.REPAIR, "critical")
    assert (wo.opened_on, wo.started_on, wo.completed_on, wo.due_on) == (date(2024, 3, 4), date(2024, 3, 4), date(2024, 3, 6), date(2024, 3, 5))
    assert wo.assigned_to == former and not wo.vendor_service and wo.created_by == kim and wo.resolution == "" and not wo.tagged_out
    assert wo.problem == "Imported from the previous system (work order 10423)"
    history = list(wo.status_history.order_by("created_at").values_list("from_status", "to_status", "note", "created_at"))
    zone = timezone.get_current_timezone()
    assert [(f, t, n, at.astimezone(zone).date()) for f, t, n, at in history] == [("", "open", "Imported", date(2024, 3, 4)),
                                                                                  ("open", "closed", "Imported", date(2024, 3, 6))]
    vendor = by_legacy("10500")
    assert vendor.vendor_service and vendor.vendor_name == "BD field service" and vendor.assigned_to is None
    assert (vendor.status, vendor.started_on, vendor.completed_on) == (WoStatus.IN_PROGRESS, date(2024, 5, 1), None)
    assert vendor.due_on == date(2024, 5, 6)  # no due date on a repair: the priority's days, as a new work order's
    pm = by_legacy("10501")
    assert pm.type == WoType.PM and pm.assigned_to == former and pm.pm_result == ""
    pump.refresh_from_db()
    assert (pump.status, pump.last_pm_on, pump.next_pm_on) == (AssetStatus.IN_SERVICE, date(2024, 1, 10), date(2025, 1, 10))  # never moved
    assert mailoutbox == []  # an imported assignment is history: nobody is told


def test_an_open_repair_holds_an_out_of_service_device_until_it_is_done(ctx, kim, pump, techs):
    Asset.objects.filter(pk=pump.pk).update(status=AssetStatus.OUT_OF_SERVICE)
    run_file(kim, line(number="20001", status="In progress", opened=str(TODAY - timedelta(days=3)), completed="", technician="Tom Okafor"),
             line(number="20002", type="Inspection", status="Open", opened=str(TODAY - timedelta(days=3)), completed=""))
    repair, inspection = by_legacy("20001"), by_legacy("20002")
    assert repair.tagged_out and not inspection.tagged_out
    pump.refresh_from_db()
    assert pump.status == AssetStatus.OUT_OF_SERVICE  # the import never changes the device
    change_status(repair, WoStatus.COMPLETED, by=kim)
    pump.refresh_from_db()
    assert pump.status == AssetStatus.IN_SERVICE  # completing the repair that held it out returns it, as a portal tag-out's would


# --- costs ------------------------------------------------------------------------------------------------------------------------

def test_costs_become_lines_by_historys_rules(ctx, kim, pump, techs, former):
    s = get_settings()
    s.labor_rate, s.vendor_labor_rate = Decimal("82.00"), Decimal("215.00")
    s.save()
    run = run_file(
        kim,
        line(number="1", technician="Dana Whitfield", hours="3", labor="100", parts="84.50", total="184.50"),  # 33.33/h: 1 cent short
        line(number="2", technician="Maria Lopez", hours="30"),  # hours alone: today's rate; no 24-hour cap on history
        line(number="3", hours="2", vendor="BD field service"),  # vendor service: the vendor rate, no technician
        line(number="4", labor="250", outside="100"),  # a labor cost without hours is outside service
        line(number="5", total="$1,200.00"),  # only a total
        line(number="6", hours="1", labor="80", parts="20", total="150"),  # a total that is not the sum
        line(number="7", hours="two", parts="N/A"),  # unreadable values are left out, never read as 0
    )
    assert run.counts == {"create": 7}
    one = by_legacy("1")
    (labor,) = one.labor_lines.all()
    assert (labor.hours, labor.rate, labor.technician, labor.worked_on) == (Decimal("3.00"), Decimal("33.33"), techs["dana"], date(2024, 3, 6))
    assert [(p.description, p.quantity, p.unit_cost) for p in one.part_lines.all()] == [("Parts (imported)", 1, Decimal("84.50"))]
    zone = timezone.get_current_timezone()
    assert labor.created_at.astimezone(zone).date() == date(2024, 3, 6)
    assert LaborLine.history.get(id=labor.id).history_date.astimezone(zone).date() == date(2024, 3, 6)
    two = by_legacy("2").labor_lines.get()
    assert (two.hours, two.rate, two.technician) == (Decimal("30.00"), Decimal("82.00"), former)
    three = by_legacy("3").labor_lines.get()
    assert (three.rate, three.technician) == (Decimal("215.00"), None)
    assert [(p.description, p.unit_cost) for p in by_legacy("4").part_lines.all()] == [("Outside service (imported)", Decimal("350.00"))]
    assert not by_legacy("4").labor_lines.exists()
    assert [(p.description, p.unit_cost) for p in by_legacy("5").part_lines.all()] == [("Imported cost", Decimal("1200.00"))]
    assert not by_legacy("7").labor_lines.exists() and not by_legacy("7").part_lines.exists()
    notes = notes_by_key(run)
    assert "1" not in notes  # the cent of rounding is a total, not a note on every row
    assert notes["2"] == ["Labor cost estimated at today's rate"] and notes["3"] == ["Labor cost estimated at today's rate"]
    assert notes["4"] == ["Labor cost without hours: imported as outside service"]
    assert notes["6"] == [legacy.TOTAL_DIFFERS] and "5" not in notes
    assert notes["7"] == ["Labor hours not read: left out", "Parts cost not read: left out"]
    totals = run.summary["totals"]["Costs"]
    assert Decimal(totals["Labor hours"]) == Decimal("36.00")
    assert Decimal(totals["Labor $"]) == Decimal("99.99") + 30 * Decimal("82") + 2 * Decimal("215") + 80
    assert Decimal(totals["Parts $"]) == Decimal("104.50") and Decimal(totals["Outside service $"]) == Decimal("350.00")
    assert Decimal(totals["Total only $"]) == Decimal("1200.00") and Decimal(totals["Labor rate rounding (cents)"]) == -1


def test_costs_no_line_can_hold_skip_the_row_in_words(ctx, kim, pump):
    run = run_file(kim, line(number="1", hours="10000"), line(number="2", hours="0.01", labor="50000"), line(number="3", parts="100000000"),
                   line(number="4", hours="1", labor="80"))
    notes = notes_by_key(run)
    assert notes["1"] == ["Labor hours are more than one line holds (9,999.99)"]
    assert notes["2"] == ["Labor cost over hours is more than a rate holds ($999,999.99 an hour)"]
    assert notes["3"] == ["Parts cost is more than one line holds ($99,999,999.99)"]
    assert run.counts == {"skip": 3, "create": 1} and list(WorkOrder.objects.values_list("legacy_number", flat=True)) == ["4"]


def test_the_import_writer_takes_only_imported_work_orders(ctx, kim, pump, techs, other_tenant):
    live = create_work_order(asset=pump, type="repair", priority="normal", problem="Door latch")
    at = timezone.now()
    with pytest.raises(ValidationError, match="was not imported"):
        costs.add_imported_labor(live, hours="1", rate="80", at=at, by=kim)
    with pytest.raises(ValidationError, match="was not imported"):
        costs.add_imported_part(live, description="Parts (imported)", amount="10", at=at, by=kim)
    wo = legacy.import_work_order(asset=pump, legacy_number="9", type="repair", status="closed", opened_on=date(2024, 3, 4),
                                  completed_on=date(2024, 3, 6), by=kim).work_order
    with tenant_context(other_tenant):
        theirs = Technician.objects.create(name="Their Tech")
    with pytest.raises(ValidationError, match="from this facility"):
        costs.add_imported_labor(wo, hours="1", rate="80", technician=theirs, at=at, by=kim)
    with pytest.raises(ValidationError, match="labor hours must be above 0"):
        costs.add_imported_labor(wo, hours="0", rate="80", at=at, by=kim)
    assert costs.add_imported_labor(wo, hours="25", rate="80", technician=techs["dana"], at=at, by=kim).worked_on == date(2024, 3, 6)


# --- rows that are not imported ---------------------------------------------------------------------------------------------------

def test_rows_that_are_not_work_or_not_history_are_skipped_in_words(ctx, kim, pump, vent):
    Asset.objects.filter(pk=vent.pk).update(status=AssetStatus.RETIRED)
    future = str(TODAY + timedelta(days=2))
    run = run_file(
        kim,
        line(number="c1", status="Void"),
        line(number="c2", type="PPM", status="Open", completed="", due="2024-03-31"),
        line(number="c3", opened="1999-12-31"),
        line(number="c4", opened=future, completed=""),
        line(number="c5", completed=""),
        line(number="c6", opened="2024-03-04", completed="2024-03-01"),
        line(number="c7", status="Completed", opened=str(TODAY), completed=future),
        line(number="c8", tag="CE-10001", status="Open", completed=""),
        line(number="c9", tag="CE-99999"),
        line(number="c10", type="Calibration"),
        line(number="c11", type="PM", due=""),
        line(number="c12", type="PM", due="sometime"),
        line(number="c13", opened="13/45/2024"),
        line(number="c14", status="Parked"),
        line(number="ok1", tag="ce-10001"),  # history on a retired device is kept; any letter case finds the tag
    )
    notes = notes_by_key(run)
    assert notes["c1"] == ["Cancelled in the previous system: not imported"]
    assert notes["c2"] == ["Open PMs come from each device's next PM date: not imported"]
    assert notes["c3"] == ["Opened before 2000: not imported"]
    assert notes["c4"] == ["The opened date is in the future"]
    assert notes["c5"] == ["Completed or closed without a completed date"]
    assert notes["c6"] == ["Completed before it was opened"]
    assert notes["c7"] == ["The completed date is in the future"]
    assert notes["c8"] == ["An open work order on a retired device: not imported"]
    assert notes["c9"] == ["No device with this tag here: import devices first"]
    assert notes["c10"] == ["Type not read: use repair (corrective), PM (preventive), inspection (incoming, install), recall, or safety check"]
    assert notes["c11"] == ["A PM needs its due date (PM on-time is counted by it)"]
    assert notes["c12"] == ["Due date not read: a PM needs it (PM on-time is counted by it)"]
    assert notes["c13"] == ["Opened date not read"]
    assert notes["c14"][0].startswith("Status not read")
    assert run.counts == {"skip": 14, "create": 1} and list(WorkOrder.objects.values_list("legacy_number", flat=True)) == ["ok1"]
    assert all("Calibration" not in n and "Parked" not in n for ns in notes.values() for n in ns)  # notes never echo a cell
    assert "Calibration" not in str(run.summary) and "Parked" not in str(run.summary)  # nor keep one past the run


def test_values_it_reads_around_are_noted_and_the_rest_imports(ctx, kim, pump, techs):
    Technician.objects.create(name="Tom Okafor", is_active=False)  # a second Tom Okafor
    run = run_file(
        kim,
        line(number="n1", priority="P9", due="soon", technician="Whitfield, Dana"),
        line(number="n2", status="Open", completed="2024-03-06", technician="Nobody Here"),
        line(number="n3", technician="Tom Okafor"),
        line(number="n4", technician="Dana Whitfield", vendor="BD field service"),
    )
    notes = notes_by_key(run)
    assert notes["n1"] == ["Priority not read: set to normal", "Due date not read: worked out from the priority"]
    n1 = by_legacy("n1")
    assert (n1.priority, n1.due_on, n1.assigned_to) == ("normal", date(2024, 3, 9), techs["dana"])
    assert notes["n2"] == ["Completed date left out: the work order is still open", "Technician not found here: imported without one"]
    assert by_legacy("n2").completed_on is None and by_legacy("n2").assigned_to is None
    assert notes["n3"] == ["Two technicians have this name: imported without one"]
    assert notes["n4"] == ["A technician and a vendor: imported as vendor service"] and by_legacy("n4").vendor_service
    assert run.counts == {"create": 4}


# --- re-runs and the file as a whole ------------------------------------------------------------------------------------------------

def test_a_rerun_finds_what_it_imported_and_never_doubles(ctx, kim, pump, vent, techs):
    rows = [line(number="10423", hours="1", labor="80"), line(number="10424", status="In progress", completed="", opened=str(TODAY))]
    run_file(kim, *rows)
    assert WorkOrder.objects.count() == 2 and LaborLine.objects.count() == 1
    rows[1] = line(number="10424", status="Completed", opened=str(TODAY), completed=str(TODAY))
    again = run_file(kim, *rows, line(number="10425", tag="CE-10001"))
    notes = notes_by_key(again)
    assert notes["10424"] == ["Already imported", "Still open here; done in the file"]
    assert again.results[0][3] == ["Already imported"]
    assert by_legacy("10424").status == WoStatus.IN_PROGRESS  # a re-run never changes what it finds
    assert WorkOrder.objects.count() == 3 and LaborLine.objects.count() == 1


def test_a_number_on_another_device_is_skipped(ctx, kim, pump, vent):
    run_file(kim, line(number="A-7"))
    run = run_file(kim, line(number="a-7", tag="CE-10001"))
    assert run.counts == {"skip": 1} and notes_by_key(run)["a-7"] == ["This number belongs to another device's work order"]


def test_a_number_twice_in_the_file_skips_every_row_of_it(ctx, kim, pump):
    run = run_file(kim, line(number="5", hours="1"), line(number="6"), line(number="5", hours="2"), line(number="5 ", parts="10"))
    assert run.counts == {"skip": 3, "create": 1}
    assert {g["note"]: g["lines"] for g in services.problem_groups(run)} == {
        "This work order number is on more than one row: one row per work order (add up labor lines first)": [2, 4, 5]}
    assert list(WorkOrder.objects.values_list("legacy_number", flat=True)) == ["6"]


def test_the_summary_adds_up_what_the_person_reconciles(ctx, kim, pump):
    run = run_file(kim, line(number="1", type="Corrective"),
                   line(number="2", type="PM", status="Completed", opened="2023-06-01", due="2023-06-30", completed="2023-06-15"),
                   line(number="3", type="Preventive maintenance", status="Closed", opened="2023-07-01", due="2023-07-31", completed="2023-08-03"),
                   line(number="4", type="Install", status="Void"), line(number="5", type="Install"))
    s = run.summary
    assert s["totals"]["Work orders by type"] == {"Corrective repair": "1", "Preventive maintenance": "2", "Incoming inspection": "1"}
    assert s["totals"]["Work orders by year"] == {"2024": "2", "2023": "2"}
    assert s["totals"]["Work orders by status"] == {"Closed": "3", "Completed": "1"}
    assert s["totals"]["PM history"] == {"On time": "1", "Late": "1"}
    assert s["values"]["Type"]["Corrective"] == ["Corrective repair", 1] and s["values"]["Type"]["Install"] == ["Incoming inspection", 1]  # once: void
    assert s["values"]["Status"]["Void"] == ["Cancelled", 1]


# --- what the facility sees of it -------------------------------------------------------------------------------------------------

def test_kpis_read_imported_history_and_the_timeline_reads_in_business_order(ctx, kim, pump, techs):
    run_file(kim, line(number="r1", opened="2024-03-04", completed="2024-03-08", technician="Dana Whitfield", hours="2", labor="164", parts="40"),
             line(number="p1", type="PM", status="Closed", opened="2024-03-01", due="2024-03-15", completed="2024-03-12"),
             line(number="p2", type="PM", status="Closed", opened="2024-03-01", due="2024-03-20", completed="2024-03-25"))
    pm = pm_on_time_rate(date(2024, 3, 1), date(2024, 3, 31), as_of=date(2024, 4, 15))
    assert (pm["due"], pm["on_time"]) == (2, 1)
    k = overview_kpis(2024, 3, today=date(2024, 4, 15))
    assert k["repairs_closed"] == 1 and k["mttr_days"] == 4 and k["repair_spend"] == 204.0
    cost = cost_of_service(date(2024, 4, 15), acquisition=3200)
    assert round(cost["in_house"], 2) == round(204.0 * 365 / 182, 2)
    texts = [e["text"] for e in timeline(by_legacy("r1"))]
    assert texts == ["Opened: Imported from the previous system (work order r1)", "2 h logged by Dana Whitfield", "Part: Parts (imported) ($40.00)",
                     "Status changed to closed: Imported"]


def test_the_previous_number_is_shown_searched_and_read_only_in_the_api(client, ctx, kim, pump):
    run_file(kim, line(number="10423", status="In progress", opened=str(TODAY), completed=""))
    wo = by_legacy("10423")
    client.force_login(kim)
    body = client.get(f"/work-orders/{wo.number}/", HTTP_HX_REQUEST="true").content.decode()
    assert "Previous number" in body and "10423" in body
    printed = client.get(f"/print/work-orders/{wo.number}/").content.decode()
    assert "Previous number" in printed and "10423" in printed
    assert client.get("/search/?q=10423")["Location"] == f"/work-orders/{wo.number}/"
    WorkOrder.objects.filter(pk=wo.pk).update(problem="Found by its previous number alone")
    assert list(filter_work_orders(WorkOrderFilters(q="1042"))) == [wo]
    data = client.get(f"/api/v1/work-orders/{wo.id}/").json()
    assert data["legacy_number"] == "10423" and data["source"] == "imported"
    r = client.patch(f"/api/v1/work-orders/{wo.id}/", {"legacy_number": "999"}, content_type="application/json")
    assert r.status_code == 200 and r.json()["legacy_number"] == "10423"
    live = create_work_order(asset=pump, type="repair", priority="normal", problem="Door latch")
    assert "Previous number" not in client.get(f"/work-orders/{live.number}/", HTTP_HX_REQUEST="true").content.decode()


def test_a_tag_that_reads_like_a_previous_number_still_opens_the_device(client, ctx, kim, pump):
    run_file(kim, line(number="CE-10002"))
    client.force_login(kim)
    assert client.get("/search/?q=ce-10002")["Location"] == "/equipment/CE-10002/"  # tags first, as before imports


def test_previous_numbers_belong_to_their_facility(ctx, kim, pump, other_tenant):
    run_file(kim, line(number="10423"))
    with tenant_context(other_tenant):
        dept = Department.objects.create(name="ICU")
        dm = DeviceModel.objects.create(manufacturer="BD", model="Alaris", description="Pump", category="Infusion pumps")
        theirs = Asset.objects.create(tag="CE-10002", device_model=dm, department=dept, next_pm_on=TODAY + timedelta(days=30))
        assert legacy.find("10423") is None
        result = legacy.import_work_order(asset=theirs, legacy_number="10423", type="repair", status="closed", opened_on=date(2024, 3, 4),
                                          completed_on=date(2024, 3, 6))
        assert result.work_order.number == "WO-24-0001"  # their own numbering
        with pytest.raises(ValidationError, match="not in this facility"):
            legacy.import_work_order(asset=pump, legacy_number="x", type="repair", status="closed", opened_on=date(2024, 3, 4),
                                     completed_on=date(2024, 3, 6))
    assert legacy.find("10423").asset == pump and WorkOrder.objects.count() == 1
    with pytest.raises(ValidationError, match="already on a work order here"):
        legacy.import_work_order(asset=pump, legacy_number="10423", type="repair", status="closed", opened_on=date(2024, 3, 4),
                                 completed_on=date(2024, 3, 6))
    with pytest.raises(IntegrityError), transaction.atomic():
        WorkOrder(asset=pump, type="repair", problem="x", due_on=TODAY, legacy_number="10423").save()


def test_importing_work_orders_needs_work_order_approve(ctx, make_user, pump):
    from django.core.exceptions import PermissionDenied

    with pytest.raises(PermissionDenied):
        services.upload(make_user("technician"), "work_orders", "w.csv", csv_bytes(line(number="1")))


# --- under PostgreSQL's policies ----------------------------------------------------------------------------------------------------

@needs_postgres
def test_a_check_and_an_import_under_the_policies(ctx, kim, pump, techs):
    as_app_role()
    run = run_file(kim, line(number="10423", technician="Dana Whitfield", hours="1.5", labor="123", parts="84"),
                   line(number="10424", type="PM", status="Closed", due="2024-03-31", completed="2024-03-20"), import_it=False)
    assert run.counts == {"create": 2} and not WorkOrder.objects.exists()
    run = run_through(services.start_import(run, kim), kim)
    assert run.counts == {"create": 2} and WorkOrder.objects.count() == 2 and LaborLine.objects.count() == 1 and PartLine.objects.count() == 1
    assert sorted(WorkOrder.objects.values_list("number", flat=True)) == ["WO-24-0001", "WO-24-0002"]
    assert by_legacy("10423").status_history.count() == 2


@needs_postgres
def test_values_longer_than_their_columns_skip_the_row_under_the_policies(ctx, kim, pump):
    as_app_role()
    run = run_file(kim, line(number="x" * 41), line(number="v1", vendor="V" * 121), line(number="ok"))
    assert run.counts == {"skip": 2, "create": 1}
    assert sorted(n for ns in notes_by_key(run).values() for n in ns) == ["Vendor is longer than 120 characters",
                                                                          "Work order number is longer than 40 characters"]
