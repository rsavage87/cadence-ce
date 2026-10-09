"""
Slice 29 review fixes: what the review of temporary equipment (rentals, vendor loaners, demo units) found, each proved fixed.

- The work order import (legacy._validate, and Settings > Import data's work orders) refuses a PM on a temporary device, done or not
  (OWNER_PM; an open one is refused for any device first, OPEN_PM), so the PM figures never count one; a device of ours still imports
  its PMs, and a temporary device its repairs.
- Recording an incident with a hold, and holding a device, on a returned rental say "returned to owner", never "retired", and so do the
  incident modals' device hints; a device of ours retired still says "retired".
- A rental lost for good leaves through Return to owner as not in hand (ReturnCleaning.NOT_IN_HAND), the one way a missing unit goes
  and never one in hand, and then leaves the Equipment summary's count, the Overview's past due back lines, and the binder; the Return
  to owner modal offers that choice only for a missing unit.
- A returned rental's History reads "Returned to owner"; a device of ours retired still reads "Retired".
- The risk-scoring bands (facility.services.risk_summary, the binder's program section) count our devices only, as the inventory's
  class figures do.
- A vendor loaner is not due back while the vendor repair its device of ours is out for is still open (loaner_device_back,
  loaner_back_q): neither drawer offers Return the loaner and the Overview lists nothing until that repair is done.
- The binder's inventory calls a rental held as evidence with its owner's PM past a FINDING that explains the hold, never a GAP that
  says to return it.
"""
import csv
import io
from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone
from survey_helpers import period, rows_of

from apps.core import history
from apps.equipment import services as eq
from apps.equipment.models import AddedAs, Asset, AssetStatus, Ownership, ReturnCleaning, ReturnData
from apps.facility.services import risk_summary
from apps.imports import services as imports
from apps.imports.models import ImportRun
from apps.incidents import services as inc
from apps.incidents.models import Affected, Outcome
from apps.pm.services import missed_pms, pm_on_time_rate
from apps.reports.services import attention_items, temporary_attention
from apps.reports.survey import FINDING, GAP, inventory, program
from apps.web.forms import temporary_on_site
from apps.web.forms_temporary import ReturnForm
from apps.workorders import legacy
from apps.workorders.models import WorkOrder, WoStatus, WoType
from apps.workorders.services import assign, change_status, create_work_order

HX = {"HTTP_HX_REQUEST": "true"}
S = AssetStatus


@pytest.fixture
def today(ctx):
    return timezone.localdate()


def rental(model, dept, tag="T-0001", *, today, **kw):
    """A rental added new, waiting for its incoming inspection."""
    values = {"tag": tag, "device_model": model, "department": dept, "kind": Ownership.RENTAL, "owner": "Acme Rentals", "serial": f"SN-{tag}",
              "owner_reference": "RA-2026-0042", "due_back_on": today + timedelta(days=14), "owner_pm_due_on": today + timedelta(days=60),
              "today": today}
    return eq.add_temporary_device(**{**values, **kw})


def on_site(model, dept, tag="T-0002", *, today, arrived_days=10, **kw):
    """A temporary device already on site when entered: in service, no inspection."""
    return rental(model, dept, tag, today=today, added_as=AddedAs.EXISTING, arrived_on=today - timedelta(days=arrived_days), **kw)


def returned(model, dept, tag="T-0002", *, today):
    return eq.return_to_owner(on_site(model, dept, tag, today=today), cleaning=ReturnCleaning.DECONTAMINATED, data=ReturnData.CLEARED,
                              today=today)


def record(asset, **kwargs):
    kwargs.setdefault("outcome", Outcome.UNKNOWN)
    kwargs.setdefault("affected", Affected.PATIENT)
    return inc.record_incident(asset=asset, **kwargs)


def drawer(client, tag, tab="overview") -> str:
    r = client.get(f"/equipment/{tag}/?tab={tab}", **HX)
    assert r.status_code == 200
    return r.content.decode()


def status_changes(asset) -> list[tuple[str, str]]:
    entries, _ = history.record_history(Asset.objects.get(pk=asset.pk))
    return [(c.before, c.after) for e in entries if e.action == "changed" for c in e.changes if c.field == "Status"]


# --- 1. the work order import: no PM on a temporary device ---------------------------------------------------------------------------

COLS = ["number", "tag", "type", "status", "opened", "due", "completed"]


def import_csv(*rows) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["WO #", "Control No.", "Work Type", "Status", "Date Opened", "Date Due", "Date Completed"])
    writer.writerows([[str(r.get(c, "")) for c in COLS] for r in rows])
    return buf.getvalue().encode()


def run_through(run, user):
    run = imports.process(run, user)
    while run.status in (ImportRun.Status.CHECKING, ImportRun.Status.IMPORTING):
        run = imports.process(run, user)
    return run


def run_import(user, *rows):
    """Settings > Import data, work orders: upload, columns as matched, the check, then the import."""
    run = imports.upload(user, "work_orders", "work-orders.csv", import_csv(*rows))
    run = run_through(imports.confirm_columns(run, user, imports.mapping_of(run)), user)
    assert run.status == "checked"
    run = run_through(imports.start_import(run, user), user)
    assert run.status == "imported"
    return run


def test_the_work_order_import_refuses_a_pm_on_a_temporary_device_done_or_not(ctx, today, pump_model, dept, pump):
    r = on_site(pump_model, dept, today=today, arrived_days=60)
    demo = on_site(pump_model, dept, "D-1", today=today, arrived_days=60, kind=Ownership.DEMO, owner="BD")
    loaner = on_site(pump_model, dept, "L-1", today=today, arrived_days=60, kind=Ownership.LOANER, owner="BD", stands_in_for=pump)
    pm = {"type": WoType.PM, "opened_on": today - timedelta(days=25), "due_on": today - timedelta(days=20), "today": today}
    for n, (device, status, completed) in enumerate([(r, WoStatus.COMPLETED, today), (r, WoStatus.CLOSED, today - timedelta(days=21)),
                                                     (demo, WoStatus.CLOSED, today), (loaner, WoStatus.COMPLETED, today)]):
        with pytest.raises(ValidationError) as e:
            legacy.import_work_order(asset=device, legacy_number=f"pm-{n}", status=status, completed_on=completed, **pm)
        assert e.value.messages == [legacy.OWNER_PM], device.tag
    with pytest.raises(ValidationError) as e:  # an open PM is refused on any device, before whose it is
        legacy.import_work_order(asset=r, legacy_number="pm-open", status=WoStatus.OPEN, **pm)
    assert e.value.messages == [legacy.OPEN_PM]
    assert not WorkOrder.objects.exists()
    # its repairs still come over (CE's work counts every device), and a device of ours imports its PMs as before
    fix = legacy.import_work_order(asset=r, legacy_number="fix", type=WoType.REPAIR, status=WoStatus.CLOSED, opened_on=today - timedelta(days=5),
                                   completed_on=today - timedelta(days=4), today=today).work_order
    ours = legacy.import_work_order(asset=pump, legacy_number="ours", status=WoStatus.COMPLETED, completed_on=today - timedelta(days=21), **pm).work_order
    assert (fix.asset, fix.type, ours.asset, ours.type, ours.status) == (r, WoType.REPAIR, pump, WoType.PM, WoStatus.COMPLETED)


def test_import_data_skips_a_temporary_devices_pm_row_and_the_pm_figures_never_count_it(ctx, today, make_user, pump_model, dept, pump):
    kim = make_user("director")
    r = on_site(pump_model, dept, today=today, arrived_days=60)

    def ago(n):
        return str(today - timedelta(days=n))

    run = run_import(kim, {"number": "r-pm", "tag": "T-0002", "type": "PM", "status": "Completed", "opened": ago(25), "due": ago(20), "completed": ago(0)},
                     {"number": "r-fix", "tag": "t-0002", "type": "Repair", "status": "Closed", "opened": ago(25), "completed": ago(24)},
                     {"number": "ours", "tag": "CE-10002", "type": "PM", "status": "Closed", "opened": ago(25), "due": ago(20), "completed": ago(21)})
    notes = {key: n for _line, key, _outcome, n in run.results}
    assert notes["r-pm"] == [legacy.OWNER_PM]
    assert run.counts == {"skip": 1, "create": 2}
    assert sorted(WorkOrder.objects.values_list("legacy_number", flat=True)) == ["ours", "r-fix"]
    assert not WorkOrder.objects.filter(asset=r, type=WoType.PM).exists()
    # the rental's late PM would have been 1 due, 0 on time: only ours counts, on time
    assert pm_on_time_rate(today - timedelta(days=30), today) == {"due": 1, "on_time": 1, "rate": 100.0}
    assert list(missed_pms(today)) == []


# --- 2. an incident on a returned rental -----------------------------------------------------------------------------------------

def test_holding_a_returned_rental_says_returned_to_owner_never_retired(ctx, today, pump_model, dept, vent):
    gone = returned(pump_model, dept, today=today)
    with pytest.raises(ValidationError) as e:
        record(gone, hold=True, today=today)
    assert e.value.message_dict == {"hold": ["T-0002 was returned to its owner: record the incident without holding it."]}
    assert "retired" not in str(e.value).lower()
    incident = record(vent, hold=True, today=today)
    with pytest.raises(ValidationError) as e:
        inc.hold_device(incident, gone, today=today)
    assert e.value.message_dict == {"asset": ["T-0002 was returned to its owner: it cannot be held."]}
    # a device of ours retired still says so
    ours = Asset.objects.create(tag="CE-10009", device_model=pump_model, department=dept)
    eq.set_status(ours, S.RETIRED, today=today)
    with pytest.raises(ValidationError) as e:
        inc.hold_device(incident, ours, today=today)
    assert e.value.message_dict == {"asset": ["CE-10009 is retired: it cannot be held."]}
    with pytest.raises(ValidationError) as e:
        record(ours, hold=True, today=today)
    assert e.value.message_dict == {"hold": ["CE-10009 is retired: record the incident without holding it."]}
    # recorded without the hold, the returned rental's incident is kept
    assert record(gone, hold=False, today=today).asset == gone


def test_the_incident_modals_read_returned_to_owner_for_a_returned_rental(client, make_user, today, pump_model, dept, vent):
    gone = returned(pump_model, dept, today=today)
    incident = record(vent, hold=True, today=today)
    client.force_login(make_user("director"))
    body = client.get(f"/incidents/new/?asset={gone.tag}", **HX).content.decode()
    hint = body.split('id="inc-asset-hint">', 1)[1].split("</span>", 1)[0]
    assert hint == f"{gone.device_model} · ICU · SN SN-T-0002 · Returned to owner"
    body = client.get(f"/incidents/{incident.number}/hold/?asset={gone.tag}", **HX).content.decode()
    assert f"{gone.device_model} · ICU · Returned to owner</span>" in body and "Retired" not in body


# --- 3. a rental lost for good ---------------------------------------------------------------------------------------------------

def test_a_missing_rental_leaves_only_as_not_in_hand_and_then_leaves_the_figures(client, make_user, today, pump_model, dept):
    lost = on_site(pump_model, dept, today=today, due_back_on=today - timedelta(days=2))
    here = on_site(pump_model, dept, "T-0003", today=today)
    with pytest.raises(ValidationError) as e:  # a unit in hand never leaves as not in hand
        eq.return_to_owner(here, cleaning=ReturnCleaning.NOT_IN_HAND, data=ReturnData.NOT_APPLICABLE, today=today)
    assert e.value.message_dict == {"cleaning": ["T-0003 is here: say how it was cleaned before it left."]}
    eq.set_status(lost, S.MISSING, today=today)
    assert temporary_on_site() == 2 and [i["right"] for i in temporary_attention(today)] == ["Past due back 2 d"]
    p = period(today - timedelta(days=90), today)
    f = {x.label: x.value for x in inventory.build(p, None).figures}
    assert (f["Temporary devices on site"], f["Devices marked missing"]) == (2, 1)
    for cleaning in (ReturnCleaning.DECONTAMINATED, ReturnCleaning.LABELED):
        with pytest.raises(ValidationError) as e:
            eq.return_to_owner(lost, cleaning=cleaning, data=ReturnData.CLEARED, today=today)
        assert e.value.message_dict == {"cleaning": ["T-0002 is missing: mark it found before it goes back to its owner, or return it as not in "
                                                     "hand (lost, settled with its owner)."]}
    lost = eq.return_to_owner(lost, cleaning=ReturnCleaning.NOT_IN_HAND, data=ReturnData.NOT_APPLICABLE, today=today)
    assert (lost.status, lost.returned_on, lost.return_cleaning) == (S.RETIRED, today, ReturnCleaning.NOT_IN_HAND)
    assert eq.status_label(lost) == eq.RETURNED_LABEL == "Returned to owner"
    assert status_changes(lost)[0] == ("Missing", "Returned to owner")
    assert temporary_on_site() == 1 and temporary_attention(today) == []
    assert not [i for i in attention_items(today) if i.get("asset") == lost.tag]
    f = {x.label: x.value for x in inventory.build(p, None).figures}
    assert (f["Temporary devices on site"], f["Devices marked missing"]) == (1, 0)
    client.force_login(make_user("analyst"))
    assert ">1 temporary on site</a>" in client.get("/equipment/").content.decode()


def test_the_return_modal_offers_not_in_hand_only_for_a_missing_unit(client, make_user, today, pump_model, dept):
    a = on_site(pump_model, dept, today=today)
    assert [v for v, _ in ReturnForm(asset=a).fields["cleaning"].choices] == [ReturnCleaning.DECONTAMINATED, ReturnCleaning.LABELED]
    client.force_login(make_user("technician"))
    body = client.get(f"/equipment/{a.tag}/return/", **HX).content.decode()
    assert 'value="decontaminated"' in body and 'value="labeled"' in body and 'value="not_in_hand"' not in body
    body = client.post(f"/equipment/{a.tag}/return/", {"on": today.isoformat(), "cleaning": "not_in_hand", "data": "cleared"}, **HX).content.decode()
    assert "Choose one of the ways listed." in body
    a.refresh_from_db()
    assert a.status == S.IN_SERVICE
    eq.set_status(a, S.MISSING, today=today)
    a.refresh_from_db()
    form = ReturnForm(asset=a)
    assert [v for v, _ in form.fields["cleaning"].choices] == [ReturnCleaning.NOT_IN_HAND] and form.fields["cleaning"].initial == ReturnCleaning.NOT_IN_HAND
    body = client.get(f"/equipment/{a.tag}/return/", **HX).content.decode()
    assert 'value="not_in_hand"' in body and 'value="decontaminated"' not in body and "<form" in body
    assert "mark it found before it goes back" not in body  # no longer refused up front
    body = client.post(f"/equipment/{a.tag}/return/", {"on": today.isoformat(), "cleaning": "labeled", "data": "cleared"}, **HX).content.decode()
    assert "Choose one of the ways listed." in body
    client.post(f"/equipment/{a.tag}/return/", {"on": today.isoformat(), "cleaning": "not_in_hand", "data": "not_applicable"}, **HX)
    a.refresh_from_db()
    assert (a.status, a.return_cleaning, a.return_data) == (S.RETIRED, ReturnCleaning.NOT_IN_HAND, ReturnData.NOT_APPLICABLE)


# --- 4. History -------------------------------------------------------------------------------------------------------------------

def test_a_returned_rentals_history_reads_returned_to_owner_and_ours_still_reads_retired(client, make_user, today, pump_model, dept, vent):
    gone = returned(pump_model, dept, today=today)
    assert status_changes(gone) == [("In service", "Returned to owner")]
    entries, _ = history.record_history(Asset.objects.get(pk=gone.pk))
    assert "Retired" not in repr(entries)
    eq.set_status(vent, S.RETIRED, today=today)
    assert status_changes(vent) == [("In service", "Retired")]
    kim = make_user("director")
    log, _ = history.change_log(kim, areas=["devices"])
    words = {(e.record.split(" ")[0], c.after) for e in log for c in e.changes if c.field == "Status"}
    assert ("T-0002", "Returned to owner") in words and ("CE-10001", "Retired") in words and ("T-0002", "Retired") not in words
    client.force_login(kim)
    body = drawer(client, gone.tag, "history")
    assert "Returned to owner" in body and "Retired" not in body


# --- 5. the risk-scoring bands ----------------------------------------------------------------------------------------------------

def test_the_risk_bands_count_ours_and_agree_with_the_inventorys_class_figures(ctx, today, dept, vent, pump, vent_model, pump_model):
    on_site(vent_model, dept, "R-1", today=today)
    on_site(vent_model, dept, "R-2", today=today, kind=Ownership.DEMO, owner="Hamilton")
    on_site(pump_model, dept, "R-3", today=today, kind=Ownership.LOANER, owner="BD", stands_in_for=pump)
    assert {b["label"]: b["devices"] for b in risk_summary()} == {"Life support": 1, "High": 1, "Medium": 0, "Low": 0}
    p = period(today - timedelta(days=90), today)
    bands = {r[1]: r[2] for r in rows_of(program.build(p, None), "risk_bands")}
    f = {x.label: x.value for x in inventory.build(p, None).figures}
    assert bands["Life support"] == f["Life-support devices"] == 1 and bands["High"] == f["High-risk devices"] == 1
    assert f["Temporary devices on site"] == 3


# --- 6. a vendor loaner while its device of ours is at the vendor ------------------------------------------------------------------

def test_a_loaner_is_not_due_back_while_the_vendor_repair_is_open(client, make_user, today, pump_model, dept, pump):
    repair = create_work_order(asset=pump, type=WoType.REPAIR, priority="normal", problem="Keypad")  # not tagged out: still in service
    assign(repair, vendor_name="BD field service")
    repair.refresh_from_db()
    loaner = on_site(pump_model, dept, "L-1", today=today, kind=Ownership.LOANER, owner="BD field service", stands_in_for=pump)
    pump.refresh_from_db()
    assert pump.status == S.IN_SERVICE and repair.vendor_service
    assert not eq.loaner_device_back(pump) and not Asset.objects.filter(eq.loaner_back_q(), pk=loaner.pk).exists()
    assert temporary_attention(today) == [] and not [i for i in attention_items(today) if i.get("right") == "Return the loaner"]
    client.force_login(make_user("technician"))
    ours = drawer(client, pump.tag)
    assert "Vendor loaner <a" in ours and "Return the loaner" not in ours and "back in service" not in ours
    theirs = drawer(client, loaner.tag)
    assert "Return the loaner" not in theirs and "back in service" not in theirs
    change_status(repair, WoStatus.IN_PROGRESS)
    assert not eq.loaner_device_back(pump)  # still at the vendor
    change_status(repair, WoStatus.COMPLETED)
    pump.refresh_from_db()
    assert eq.loaner_device_back(pump) and Asset.objects.filter(eq.loaner_back_q(), pk=loaner.pk).exists()
    [line] = temporary_attention(today)
    assert (line["asset"], line["right"]) == ("L-1", "Return the loaner")
    assert f"stands in for {pump.tag}, which is back in service: return the loaner." in drawer(client, pump.tag)
    assert f"{pump.tag}, the device this loaner stands in for, is back in service: return the loaner to BD field service." in drawer(client, loaner.tag)


# --- 7. the binder: a held rental past its owner's PM ----------------------------------------------------------------------------

def test_a_held_rental_past_its_owners_pm_is_a_finding_never_a_gap_to_return_it(ctx, today, dept, vent_model):
    due = today - timedelta(days=5)
    r = on_site(vent_model, dept, "R-9", today=today, owner_pm_due_on=due)
    p = period(today - timedelta(days=200), today)
    what = "R-9 (Hamilton Medical Hamilton-G5, life support, rental from Acme Rentals)"
    [gap] = [g for g in inventory.build(p, None).gaps if g.record == "R-9"]
    assert gap.kind == GAP and gap.text.endswith("or return it to its owner.")  # not held: the gap as before
    record(r, hold=True, today=today)
    r.refresh_from_db()
    assert r.incident_hold
    [gap] = [g for g in inventory.build(p, None).gaps if g.record == "R-9"]
    assert (gap.kind, gap.url) == (FINDING, "/equipment/R-9/")
    assert gap.text == (f"{what} is held as evidence for an incident investigation, not in use, with its owner's PM past due since "
                        f"{inventory._day(due)}: its incident releases it.")
    assert "return it" not in gap.text
