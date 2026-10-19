"""
Slice 35, the CE department's test equipment and its calibration, through the rules and the services (wave 1): each standing and its
boundary days, records judged by their own dates with "on file since", the look-back window with and without an earlier passed
certificate, a verification program's words, which work orders may record test equipment and which uses count, the kept-use rule,
usable_today; every write of apps.testequipment.services with its levels and refusals (the tag shared with the devices in any letter
case, the duplicate serial, the tag corrected only before the first calibration or use, the interval at Decide, the status table, the due
day's default and cap, the refused combinations, one live record per day, entered in error, the review's version and frozen list,
recheck progress); the work order's writers (complete_work_order's KEEP / [] / diff, set_test_equipment, assign's clearing, a scoped
user, another facility's id, the work that is not offered); the batches' fixed queries; tenant isolation for the four models. On
PostgreSQL: the services under the policies, two items added with one tag at once, a device and an item with one tag at once, and a
completion naming an item while another request retires it.
"""
import threading
import time
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError, connection, transaction
from django.db.models import ProtectedError
from django.utils import timezone
from pg_helpers import as_app_role, needs_postgres
from repair_fixtures import CHECKED
from testequipment_fixtures import (
    bench_tech,
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
    te_world,  # noqa: F401 - a pytest fixture
    vendor_repair,
)
from vendor_fixtures import vendor_job

from apps.accounts.models import User
from apps.core.history import _rows
from apps.equipment.models import Asset
from apps.equipment.services import create_asset
from apps.pm.dates import add_months
from apps.tenants.context import tenant_context
from apps.testequipment import rules, services
from apps.testequipment.models import (
    AsFound,
    Basis,
    Calibration,
    Kind,
    Mark,
    Program,
    Reason,
    Result,
    Status,
    TestEquipment,
    TestEquipmentUse,
    ToleranceReview,
)
from apps.workorders import permissions as wo_perms
from apps.workorders.completion import complete_work_order, keep_open
from apps.workorders.models import PmResult, Priority, RepairCheck, Source, WorkOrder, WoStatus, WoType
from apps.workorders.services import assign, change_status, create_work_order, set_test_equipment

DAY = timedelta(days=1)


@pytest.fixture
def today(ctx):
    return timezone.localdate()


@pytest.fixture
def users(make_user):
    return SimpleNamespace(**{slug: make_user(slug) for slug in ("director", "manager", "technician", "analyst")})


@pytest.fixture
def vendor_user(make_user):
    user = make_user("vendor")
    user.company = "Philips"
    user.save()
    return user


def errors_of(excinfo) -> dict:
    e = excinfo.value
    return {k: " ".join(v) for k, v in e.message_dict.items()} if hasattr(e, "error_dict") else {"": " ".join(e.messages)}


def refused(call, *args, **kwargs) -> dict:
    with pytest.raises(ValidationError) as e:
        call(*args, **kwargs)
    return errors_of(e)


def denied(call, *args, **kwargs) -> str:
    with pytest.raises(PermissionDenied) as e:
        call(*args, **kwargs)
    return str(e.value)


def tags_on(wo) -> list:
    return sorted(TestEquipmentUse.objects.filter(work_order=wo).values_list("test_equipment__tag", flat=True))


def open_work(asset, type_=WoType.PM, *, day=None, tech=None, vendor=""):
    day = day or timezone.localdate()
    wo = create_work_order(asset=asset, type=type_, priority=Priority.NORMAL, problem="Scheduled work", opened_on=day - 2 * DAY,
                           assigned_to=None if vendor else (tech or bench_tech()))
    if vendor:
        assign(wo, vendor_name=vendor)
    change_status(wo, WoStatus.IN_PROGRESS, as_of=day)
    return wo


def finish(wo, items=services.KEEP, *, day=None, **kwargs):
    """Complete `wo` (a PM passed, a repair checked) on `day` recording `items` (KEEP: nothing sent)."""
    extra = dict(CHECKED) if wo.type == WoType.REPAIR else {"pm_result": PmResult.PASS} if wo.type == WoType.PM else {}
    ids = items if items is services.KEEP else [str(getattr(i, "pk", i)) for i in items]
    return complete_work_order(wo, test_equipment=ids, today=day, **{"resolution": "Done per the procedure", **extra, **kwargs})


def reopen(wo, day=None):
    change_status(wo, WoStatus.IN_PROGRESS, as_of=day)
    wo.refresh_from_db()
    return wo


# --- the rules: standing ------------------------------------------------------------------------------------------------------

def test_each_standing_and_its_boundary_days(today):
    te = item("TE-01")
    assert rules.standing_on(te, today, []).words == "No calibration on file"
    cal = certificate(te, date(2025, 3, 31), due_on=date(2026, 3, 31), today=today)
    on = lambda day: rules.standing_on(te, day, [cal])  # noqa: E731
    assert (on(date(2025, 3, 30)).state, on(date(2025, 3, 30)).calibration) == (rules.NO_RECORD, None)
    assert (on(date(2025, 3, 31)).state, on(date(2025, 3, 31)).words) == (rules.IN_DATE, "Calibration due Mar 31, 2026")
    due = on(date(2026, 3, 31))
    assert (due.state, due.due_on, due.since, due.flagged) == (rules.IN_DATE, date(2026, 3, 31), None, False)  # usable through its due day
    late = on(date(2026, 4, 1))
    assert (late.state, late.since, late.words, late.flagged) == (rules.PAST_DUE, date(2026, 4, 1), "Calibration was due Mar 31, 2026", True)
    bad = certificate(te, date(2025, 9, 1), result=Result.FAILED, as_found=AsFound.OUT_OF_TOLERANCE, today=today)
    st = rules.standing_on(te, date(2025, 9, 2), [cal, bad])
    assert (st.state, st.since, st.due_on, st.words) == (rules.FAILED, date(2025, 9, 1), None, "Failed calibration Sep 1, 2025")
    assert rules.standing_on(te, date(2025, 8, 31), [bad, cal]).state == rules.IN_DATE  # by calibrated_on, any order given


def test_the_latest_by_calibration_day_governs_never_by_entry_and_entered_in_error_is_left_out(today):
    te = item("TE-01")
    newer = certificate(te, today - 30 * DAY, today=today)
    older = certificate(te, today - 400 * DAY, due_on=today - 40 * DAY, today=today)  # typed in later, dated earlier
    cals = [newer, older]
    assert rules.standing_on(te, today, cals).calibration == newer
    assert rules.standing_on(te, today - 35 * DAY, cals).state == rules.PAST_DUE  # between them: the older one governs, past due
    services.mark_entered_in_error(newer, by=None)
    newer.refresh_from_db()
    assert rules.standing_on(te, today, [newer, older]).state == rules.PAST_DUE  # the standing follows from the rest


def test_a_verification_programs_words(today):
    te = item("TE-SP", kind=Kind.SPO2, model="ProSim SPOT", program=Program.VERIFICATION)
    assert rules.standing_on(te, today, []).words == "No verification on file"
    cal = certificate(te, today - 10 * DAY, today=today)
    due = add_months(today - 10 * DAY, 12)
    assert rules.standing_on(te, today, [cal]).words == f"Verification due {rules.fmt(due)}"
    assert rules.standing_on(te, due + DAY, [cal]).words == f"Verification was due {rules.fmt(due)}"


def test_a_record_entered_after_the_day_judged_says_on_file_since(today):
    te = item("TE-01")
    cal = certificate(te, today - 60 * DAY, today=today)
    entered = rules.entered_days([cal])
    assert entered == {cal.pk: today}  # its history's first row, the facility's day
    st = rules.standing_on(te, today - 30 * DAY, [cal], entered=entered)
    assert st.entered_after == today and st.words.endswith(f"(on file since {rules.fmt(today)})")
    assert rules.standing_on(te, today, [cal], entered=entered).entered_after is None  # entered that day: nothing to disclose
    early = rules.standing_on(te, today - 90 * DAY, [cal], entered=entered)
    assert early.words == f"No calibration on file (first certificate on file since {rules.fmt(today)})"
    assert rules.standing_on(te, today - 30 * DAY, [cal]).entered_after is None  # no `entered`: left out
    words = rules.use_words(te, st)
    assert words == (f"TE-01 (Fluke Biomedical ESA612): calibration due {rules.fmt(cal.due_on)} on the day of this work "
                     f"(on file since {rules.fmt(today)})")


def test_the_look_back_window_with_and_without_an_earlier_passed_certificate(today):
    te = item("TE-OT")
    first = certificate(te, date(2025, 1, 10), today=today)
    bad = certificate(te, date(2025, 6, 1), result=Result.FAILED, as_found=AsFound.NOT_REPORTED, today=today)
    found = certificate(te, date(2025, 9, 1), as_found=AsFound.OUT_OF_TOLERANCE, adjusted=True, today=today)
    cals = [first, bad, found]
    assert rules.window(found, cals) == (date(2025, 1, 10), date(2025, 9, 1))  # a failed certificate never narrows it
    assert rules.window(bad, cals) == (date(2025, 1, 10), date(2025, 6, 1))
    bounds = rules.window(found, cals)
    assert [rules.in_window(d, bounds) for d in (date(2025, 1, 10), date(2025, 1, 11), date(2025, 9, 1), date(2025, 9, 2))] == [
        False, True, True, False]  # after the passed one's day, through its own
    alone = item("TE-NEW")
    only = certificate(alone, date(2025, 9, 1), as_found=AsFound.OUT_OF_TOLERANCE, adjusted=True, today=today)
    assert rules.window(only, [only]) == (None, date(2025, 9, 1)) and rules.in_window(date(2001, 1, 1), (None, date(2025, 9, 1)))
    services.mark_entered_in_error(first, by=None)
    first.refresh_from_db()
    assert rules.window(found, [first, bad, found]) == (None, date(2025, 9, 1))  # widened: the passed one was entered in error


def test_matches_the_lists_filters(today):
    st = {name: rules.standing_on(te, today, Calibration.objects.filter(test_equipment=te))
          for name, te in (("in", in_date()), ("pd", past_due()), ("nr", no_record()), ("fl", failed()))}
    soon = item("TE-SOON")
    certificate(soon, today - 300 * DAY, due_on=today + 50 * DAY, today=today)
    st["soon"] = rules.standing_on(soon, today, Calibration.objects.filter(test_equipment=soon))
    assert {k: [n for n, s in st.items() if rules.matches(s, k, today)] for k in rules.FILTERS} == {
        "past_due": ["pd"], "due_soon": ["soon"], "no_record": ["nr"], "failed": ["fl"]}


# --- the rules: which work, which uses ----------------------------------------------------------------------------------------

def test_offered_for_each_type_and_outcome(ctx, pump, vent, vendor_user):
    for type_ in (WoType.PM, WoType.REPAIR, WoType.INSPECTION, WoType.RECALL, WoType.SAFETY):
        assert rules.offered(WorkOrder(type=type_, source=Source.MANUAL, vendor_service=False, status=WoStatus.OPEN)), type_
    vendor_pm = WorkOrder(type=WoType.PM, source=Source.MANUAL, vendor_service=True, status=WoStatus.OPEN)
    assert not rules.offered(vendor_pm)
    repair = WorkOrder(type=WoType.REPAIR, source=Source.MANUAL, vendor_service=True, status=WoStatus.IN_PROGRESS)
    assert rules.offered(repair)  # its outcome is chosen when it is completed
    assert not rules.offered(repair, repair_check=RepairCheck.VENDOR_REPORT)
    assert rules.offered(repair, repair_check=RepairCheck.PASSED) and rules.offered(repair, repair_check=RepairCheck.NOT_RETURNED)
    assert not rules.offered(vendor_repair(pump))  # completed with the vendor's report
    assert rules.offered(vendor_job(pump))  # a vendor repair CE checked after it
    assert not rules.offered(imported_work(vent))
    assert not rules.offered(WorkOrder(type=WoType.PM, source=Source.MANUAL, vendor_service=False, status=WoStatus.OPEN), vendor_user)


def test_counted_uses_are_completed_or_closed_not_imported_and_not_the_vendors(today, pump, vent):
    te = in_date()
    pm = pm_with(pump, te)
    checked = vendor_job(pump, test_equipment=[str(te.pk)])  # a vendor repair CE checked: ours
    assert set(TestEquipmentUse.objects.filter(rules.counted_uses_q()).values_list("work_order", flat=True)) == {pm.pk, checked.pk}
    change_status(pm, WoStatus.CLOSED)
    assert TestEquipmentUse.objects.filter(rules.counted_uses_q(), work_order=pm).exists()
    reopen(pm)
    assert not TestEquipmentUse.objects.filter(rules.counted_uses_q(), work_order=pm).exists()  # no day while reopened
    WorkOrder.objects.filter(pk=checked.pk).update(repair_check=RepairCheck.VENDOR_REPORT)  # what an API PATCH could leave
    assert not TestEquipmentUse.objects.filter(rules.counted_uses_q()).exists()
    WorkOrder.objects.filter(pk=checked.pk).update(repair_check=RepairCheck.PASSED, source=Source.IMPORTED)
    assert not TestEquipmentUse.objects.filter(rules.counted_uses_q()).exists()
    assert not rules.counted(WorkOrder.objects.get(pk=checked.pk)) and not rules.counted(WorkOrder.objects.get(pk=pm.pk))
    assert WorkOrder.objects.filter(rules.counted_work_q()).count() == 0


def test_usable_today_and_the_kept_use_rule(today):
    te = in_date()
    assert rules.usable_today(te, today)
    services.set_status(te, to=Status.OUT_FOR_CALIBRATION, by=None, today=today - DAY)
    assert not rules.usable_today(te, today) and rules.usable_today(te, today - DAY)  # its status changed that day
    cals = list(Calibration.objects.filter(test_equipment=te))
    assert rules.may_remove(te, today - 99 * DAY, cals) and rules.may_remove(te, today, cals)  # each use on its own day (used_on)
    assert not rules.may_remove(te, today - 100 * DAY, cals)  # calibrated on the work's day: judged since
    assert not rules.may_remove(te, today - 300 * DAY, cals)


# --- add, change, status -----------------------------------------------------------------------------------------------------

def test_adding_needs_equipment_edit_and_never_a_scoped_user(ctx, users, vendor_user):
    for user in (users.analyst, vendor_user):
        assert denied(item, "TE-01", by=user) == services.EDIT_PERMISSION
    te = item("TE-01", by=users.technician)
    assert (te.status, te.status_reason, te.status_since, te.tag_key, te.created_by) == (Status.IN_USE, "", timezone.localdate(), "te-01",
                                                                                         users.technician)
    assert te.history.first().history_user == users.technician


def test_a_tag_is_the_facilitys_once_in_any_letter_case(ctx, dept, vent_model):
    create_asset(tag="CE-1", device_model=vent_model, department=dept)
    assert refused(item, "ce-1") == {"tag": "ce-1 is a device's tag."}
    item("TE-01")
    assert refused(item, "te-01", serial="OTHER") == {"tag": "te-01 is already another test equipment item's tag."}
    assert refused(create_asset, tag="Te-01", device_model=vent_model, department=dept) == {"tag": "Te-01 is test equipment's tag."}
    assert refused(item, "new") == {"tag": '"new" cannot be used as a tag.'}
    assert refused(item, "TE 01") == {"tag": "Tags cannot contain spaces or slashes."}
    assert refused(item, "") == {"tag": "Enter the tag on its CE sticker."}


def test_one_item_per_make_model_and_serial(ctx):
    te = item("TE-01", serial="1234567")
    words = "FLUKE biomedical esa612 serial 1234567 is already TE-01: one item, one tag."
    assert refused(item, "TE-02", manufacturer="FLUKE biomedical", model="esa612", serial="1234567") == {"serial": words}
    other = item("TE-02", serial="7654321")
    assert refused(services.update_test_equipment, other, serial="1234567", by=None) == {
        "serial": "Fluke Biomedical ESA612 serial 1234567 is already TE-01: one item, one tag."}
    services.set_status(te, to=Status.RETIRED, reason=Reason.WORN_OUT, by=None)
    item("TE-03", serial="1234567")  # a retired item is not a live one


def test_adding_with_its_latest_certificate_and_every_problem_at_once(today):
    assert refused(item, "", kind="none", interval_months=0, calibration={"calibrated_on": today + DAY, "result": "maybe"}) == {
        "tag": "Enter the tag on its CE sticker.", "kind": "Choose the kind of test equipment.",
        "interval_months": "Enter the interval in months, from 1 to 60.", "calibrated_on": "The calibration day cannot be in the future.",
        "provider": "Enter the lab or manufacturer that issued the certificate.", "certificate": "Enter the certificate's number.",
        "accreditation": "Say whether the certificate carries an ISO/IEC 17025 accreditation mark.",
        "as_found": "Choose what the certificate says as found: in tolerance, out of tolerance, or not reported.",
        "result": "Choose the result: passed or failed."}
    assert not TestEquipment.objects.exists()
    te = item("TE-01", interval_months=6, calibration={"calibrated_on": today - 10 * DAY, "provider": "Fluke Biomedical",
                                                        "certificate": "C-77", "accreditation": Mark.SHOWN, "as_found": AsFound.IN_TOLERANCE,
                                                        "adjusted": False, "result": Result.PASSED})
    cal = Calibration.objects.get(test_equipment=te)
    assert (cal.due_on, cal.recorded_by, te.status) == (add_months(today - 10 * DAY, 6), None, Status.IN_USE)
    assert rules.standing_on(te, today, [cal]).state == rules.IN_DATE
    bad = item("TE-02", calibration={"calibrated_on": today, "provider": "Lab", "certificate": "C-78", "accreditation": Mark.NOT_SHOWN,
                                      "as_found": AsFound.OUT_OF_TOLERANCE, "adjusted": False, "result": Result.FAILED})
    assert (bad.status, bad.status_reason) == (Status.OUT_OF_SERVICE, Reason.FAILED_CALIBRATION)


def test_the_tag_is_corrected_only_before_the_first_calibration_or_use(today, pump):
    te = item("TE-1")
    services.update_test_equipment(te, tag="TE-01", by=None)
    assert (te.tag, te.tag_key) == ("TE-01", "te-01")
    assert _rows(TestEquipment.history.model).filter(id=te.pk).first().history_change_reason == "Tag corrected: TE-1 to TE-01"
    used = item("TE-02")
    pm_with(pump, used)
    assert refused(services.update_test_equipment, used, tag="TE-002", by=None) == {"tag": "TE-02 has calibrations or work on record, so its tag stays."}
    certificate(te, today, today=today)
    assert refused(services.update_test_equipment, te, tag="TE-001", by=None) == {"tag": "TE-01 has calibrations or work on record, so its tag stays."}
    assert refused(services.update_test_equipment, te, status=Status.MISSING, by=None) == {"status": "Change the status with its own action."}


def test_the_interval_once_a_certificate_is_on_file_is_the_managers(today, users):
    te = item("TE-01")
    services.update_test_equipment(te, interval_months=6, by=users.technician)  # nothing on file yet: Edit
    certificate(te, today, today=today)
    assert denied(services.update_test_equipment, te, interval_months=24, by=users.technician) == services.INTERVAL_PERMISSION
    services.update_test_equipment(te, interval_months=24, kind=Kind.ELECTRICAL_SAFETY, by=users.manager)
    assert TestEquipment.objects.get(pk=te.pk).interval_months == 24
    services.update_test_equipment(te, interval_months=24, model="ESA615", by=users.technician)  # unchanged interval: no Decide


def test_held_by_is_an_active_technician_of_this_facility(ctx, other_tenant):
    dana, gone = bench_tech("Dana Whitfield"), bench_tech("Gone Tech")
    gone.is_active = False
    gone.save()
    te = item("TE-01", held_by=dana)
    assert refused(services.update_test_equipment, te, held_by=gone, by=None) == {"held_by": "Gone Tech is no longer active: choose an active technician."}
    with tenant_context(other_tenant):
        theirs = bench_tech("Theirs")
    assert refused(services.update_test_equipment, te, held_by=str(theirs.pk), by=None) == {"held_by": "Choose a technician from this facility."}
    held = item("TE-02", held_by=dana)
    dana.is_active = False
    dana.save()
    services.update_test_equipment(held, held_by=str(dana.pk), model="ESA615", by=None)  # the one it has stays
    services.update_test_equipment(held, held_by=None, by=None)
    assert TestEquipment.objects.get(pk=held.pk).held_by is None


def test_the_status_table_by_level(today, users):
    te = item("TE-01")
    tech, director = users.technician, users.director
    steps = [(Status.OUT_FOR_CALIBRATION, ""), (Status.IN_USE, ""), (Status.MISSING, ""), (Status.IN_USE, ""),
             (Status.OUT_OF_SERVICE, Reason.DAMAGED)]
    for to, reason in steps:
        services.set_status(te, to=to, reason=reason, by=tech, today=today)
        assert (te.status, te.status_reason, te.status_since) == (to, reason, today)
    assert refused(services.set_status, te, to=Status.IN_USE, by=director) == {"status": (
        f"TE-01 is out of service (damaged): it goes back in use when a passed calibration dated on or after {rules.fmt(today)} is "
        "recorded.")}
    assert denied(services.set_status, te, to=Status.RETIRED, reason=Reason.WORN_OUT, by=tech) == services.RETIRE_PERMISSION
    services.set_status(te, to=Status.RETIRED, reason=Reason.WORN_OUT, by=director)
    assert denied(services.set_status, te, to=Status.IN_USE, by=users.manager) == services.RETIRE_PERMISSION  # reinstating: Approve
    # review fix: retired while out of service, Reinstate lands out of service with its reason (no passed certificate since its day out)
    services.set_status(te, to=Status.IN_USE, by=director)
    assert (te.status, te.status_reason, te.status_since) == (Status.OUT_OF_SERVICE, Reason.DAMAGED, today)
    assert refused(services.set_status, te, to=Status.MISSING, by=tech) == {"status": "TE-01 is out of service: from there it goes retired."}
    services.set_status(te, to=Status.RETIRED, reason=Reason.WORN_OUT, by=director)
    certificate(te, today, today=today)  # passed, dated on or after its day out, recorded while retired: it moves nothing
    te.refresh_from_db()
    assert te.status == Status.RETIRED
    services.set_status(te, to=Status.IN_USE, by=director)  # with that certificate on file, Reinstate puts it back in use
    assert (te.status, te.status_reason, te.status_since) == (Status.IN_USE, "", today)
    services.set_status(te, to=Status.MISSING, by=tech)
    assert refused(services.set_status, te, to=Status.RETIRED, reason=Reason.WORN_OUT, by=director) == {"status_reason": "Choose why: lost."}
    services.set_status(te, to=Status.RETIRED, reason=Reason.LOST, by=director)
    services.set_status(te, to=Status.IN_USE, by=director)
    services.set_status(te, to=Status.RETIRED, reason=Reason.RETURNED_TO_OWNER, by=tech)  # returned to its owner: Edit
    reasons = list(_rows(TestEquipment.history.model).filter(id=te.pk).order_by("history_date", "history_id")
                   .values_list("history_change_reason", flat=True))
    assert reasons[1:] == ["Out for calibration", "Back from calibration", "Missing", "Found", "Out of service: damaged",
                           "Retired: worn out or obsolete", "Reinstated: out of service (damaged)", "Retired: worn out or obsolete",
                           "Reinstated", "Missing", "Retired: lost", "Reinstated", "Retired: returned to its owner"]


def test_status_moves_that_are_never_made_by_hand(today):
    te = item("TE-01")
    assert refused(services.set_status, te, to=Status.OUT_OF_SERVICE, reason=Reason.FAILED_CALIBRATION, by=None) == {"status_reason": (
        "A failed calibration is recorded with the calibration itself (Record calibration, result Failed).")}
    assert refused(services.set_status, te, to=Status.OUT_OF_SERVICE, by=None) == {"status_reason": "Choose why: damaged or failed a check."}
    assert refused(services.set_status, te, to=Status.IN_USE, by=None) == {"status": "TE-01 is already in use."}
    assert refused(services.set_status, te, to=Status.MISSING, reason=Reason.DAMAGED, by=None) == {
        "status_reason": "Only out of service and retired take a reason."}
    services.set_status(te, to=Status.OUT_FOR_CALIBRATION, by=None)
    assert refused(services.set_status, te, to=Status.MISSING, by=None) == {"status": "TE-01 is out for calibration: from there it goes in use."}
    assert refused(services.set_status, te, to="lost", by=None) == {"status": "Choose a status listed."}
    services.set_status(te, to=Status.IN_USE, by=None)
    services.set_status(te, to=Status.OUT_OF_SERVICE, reason=Reason.FAILED_CHECK, by=None)
    services.set_status(te, to=Status.RETIRED, reason=Reason.WORN_OUT, by=None)  # beyond build35: out of service leaves too
    assert [m.label for m in services.moves_for(te, User(is_superuser=True))] == ["Reinstate"]


# --- calibrations ------------------------------------------------------------------------------------------------------------

def test_the_due_day_default_and_its_cap(today):
    te = item("TE-01", interval_months=12)
    day = date(2025, 1, 31)
    assert certificate(te, day, today=today).due_on == date(2026, 1, 31)
    assert certificate(te, date(2025, 2, 10), due_on=date(2026, 2, 28), today=today).due_on == date(2026, 2, 28)  # the month's end
    assert refused(certificate, te, date(2025, 3, 10), due_on=date(2026, 4, 1), today=today) == {"due_on": (
        "The due day is at most 12 months after the calibration day (by Mar 31, 2026): check the certificate, or the item's interval.")}
    assert refused(certificate, te, date(2025, 3, 10), due_on=date(2025, 3, 10), today=today) == {"due_on": "The due day comes after the calibration day."}
    assert certificate(te, date(2025, 3, 11), due_on=date(2025, 9, 30), today=today).due_on == date(2025, 9, 30)  # earlier: as printed
    assert refused(certificate, te, date(2025, 4, 1), due_on=date(2026, 4, 1), result=Result.FAILED, as_found=AsFound.OUT_OF_TOLERANCE,
                   today=today) == {"due_on": "A failed calibration has no due day: leave it blank."}
    assert refused(certificate, te, today + DAY, today=today) == {"calibrated_on": "The calibration day cannot be in the future."}


def test_the_refused_combinations(today):
    te = item("TE-01")
    assert refused(certificate, te, today, result=Result.FAILED, as_found=AsFound.IN_TOLERANCE, today=today) == {"as_found": (
        "A failed calibration could not be brought into tolerance, so it was not in tolerance as found: check as found or the result.")}
    assert refused(certificate, te, today, as_found=AsFound.OUT_OF_TOLERANCE, adjusted=False, today=today) == {"adjusted": (
        "Out of tolerance as found and passed means it was adjusted: tick adjusted, or check the result.")}
    certificate(te, today, as_found=AsFound.OUT_OF_TOLERANCE, adjusted=True, today=today)
    certificate(te, today - DAY, as_found=AsFound.NOT_REPORTED, adjusted=True, today=today)
    certificate(te, today - 2 * DAY, result=Result.FAILED, as_found=AsFound.NOT_REPORTED, today=today)
    assert refused(certificate, te, today - 3 * DAY, number="C 1", today=today) == {
        "certificate": "Enter the certificate's number as it is printed, without spaces."}


def test_one_live_record_per_day_then_the_other_entered_in_error(today, users):
    te = item("TE-01")
    first = certificate(te, today - 5 * DAY, number="C-1", today=today)
    assert refused(certificate, te, today - 5 * DAY, number="C-2", today=today) == {"calibrated_on": (
        f"TE-01 already has a calibration on {rules.fmt(today - 5 * DAY)} (certificate C-1). Mark the other entered in error first.")}
    assert denied(services.mark_entered_in_error, first, by=users.technician) == services.DECIDE_PERMISSION
    services.mark_entered_in_error(first, by=users.manager, today=today)
    first.refresh_from_db()
    assert (first.entered_in_error, first.entered_in_error_by, first.entered_in_error_on) == (True, users.manager, today)
    assert refused(services.mark_entered_in_error, first, by=None) == {"": "Certificate C-1 is already marked entered in error."}
    certificate(te, today - 5 * DAY, number="C-2", today=today)
    assert services.entered_in_error_note(first) == ("Certificate C-1 marked entered in error. TE-01's standing follows its other "
                                                     "calibrations; its status (in use) stays as it is.")


def test_recording_moves_the_status_only_when_newest(today):
    te = item("TE-01")
    services.set_status(te, to=Status.OUT_FOR_CALIBRATION, by=None, today=today - 3 * DAY)
    certificate(te, today - 400 * DAY, due_on=today - 40 * DAY, today=today)  # a late certificate dated before it went out
    te.refresh_from_db()
    assert te.status == Status.OUT_FOR_CALIBRATION
    certificate(te, today - 2 * DAY, today=today)
    te.refresh_from_db()
    assert (te.status, te.status_since) == (Status.IN_USE, today)
    certificate(te, today - 200 * DAY, result=Result.FAILED, as_found=AsFound.OUT_OF_TOLERANCE, today=today)  # older: no move
    te.refresh_from_db()
    assert te.status == Status.IN_USE
    certificate(te, today, result=Result.FAILED, as_found=AsFound.OUT_OF_TOLERANCE, today=today)
    te.refresh_from_db()
    assert (te.status, te.status_reason) == (Status.OUT_OF_SERVICE, Reason.FAILED_CALIBRATION)
    reason = _rows(TestEquipment.history.model).filter(id=te.pk).first().history_change_reason
    assert reason == f"Out of service: failed calibration {rules.fmt(today)}"
    for status, reason_ in ((Status.MISSING, ""), (Status.RETIRED, Reason.LOST)):
        lost = item(f"TE-{status}")
        services.set_status(lost, to=Status.MISSING, by=None) if status == Status.MISSING else (
            services.set_status(lost, to=Status.MISSING, by=None), services.set_status(lost, to=status, reason=reason_, by=None))
        certificate(lost, today, today=today)
        certificate(lost, today - DAY, result=Result.FAILED, as_found=AsFound.NOT_REPORTED, today=today)
        lost.refresh_from_db()
        assert lost.status == status  # a certificate typed late never moves a missing or retired item


def test_out_of_service_back_in_use_only_by_a_passed_certificate(today):
    te = item("TE-01")
    services.set_status(te, to=Status.OUT_OF_SERVICE, reason=Reason.DAMAGED, by=None, today=today - 5 * DAY)
    certificate(te, today - 6 * DAY, today=today)  # dated before it was damaged
    te.refresh_from_db()
    assert te.status == Status.OUT_OF_SERVICE
    certificate(te, today - 5 * DAY, today=today)  # on the day it went out of service
    te.refresh_from_db()
    assert (te.status, te.status_reason) == (Status.IN_USE, "")
    bad = item("TE-02")
    certificate(bad, today - 3 * DAY, result=Result.FAILED, as_found=AsFound.OUT_OF_TOLERANCE, today=today)
    certificate(bad, today - 2 * DAY, today=today)  # repaired and calibrated after the failure, typed the same day
    bad.refresh_from_db()
    assert bad.status == Status.IN_USE


# --- the tolerance review ----------------------------------------------------------------------------------------------------

def test_found_out_of_tolerance_later_and_the_reviews_frozen_list(today, vent, pump, users):
    case = out_of_tolerance(vent, "TE-OT")
    found = case.found
    assert services.affected_work(found) == case.work
    summary = services.review_summary(found)
    assert (summary.window, summary.review, summary.words) == ((today - 200 * DAY, today - 10 * DAY), None, "Not reviewed")
    judged = services.judged_for(case.work[0])
    assert [f.state for f in judged[0].later] == [rules.NOT_REVIEWED] and judged[0].flagged
    assert judged[0].words.endswith(f"; found out of tolerance on {rules.fmt(found.calibrated_on)}; not reviewed")
    version = summary.version
    assert denied(services.decide_review, found, basis=Basis.RECHECKED, decided_on=today, version=version,
                  by=users.technician) == services.DECIDE_PERMISSION
    late = pm_with(pump, case.item, completed_on=today - 20 * DAY)  # recorded after the list was shown
    assert refused(services.decide_review, found, basis=Basis.RECHECKED, decided_on=today, version=version, by=users.manager) == {
        "": services.REVIEW_CHANGED}
    version = services.review_summary(found).version
    assert refused(services.decide_review, found, basis="", decided_on=today - 11 * DAY, quality_reference="Q 1", version=version,
                   by=users.manager) == {"basis": "Choose the decision.", "quality_reference": "Enter the quality record's number, without spaces.",
                                         "decided_on": f"The decision comes on or after the calibration day ({rules.fmt(found.calibrated_on)})."}
    review = services.decide_review(found, basis=Basis.RECHECKED, quality_reference="QR-12", decided_on=today, version=version, by=users.manager)
    frozen = sorted([[str(w.pk), w.number] for w in [*case.work, late]])
    assert (review.window_start, review.window_end, review.work_orders, review.work_count, review.decided_by) == (
        today - 200 * DAY, today - 10 * DAY, frozen, 3, users.manager)
    assert refused(services.decide_review, found, basis=Basis.RECHECKED, decided_on=today, version=None, by=None) == {
        "": f"The review of certificate {found.certificate} is already recorded."}
    added = pm_with(pump, case.item, completed_on=today - 15 * DAY)  # recorded since: a GAP
    summary = services.review_summary(found)
    assert [w.number for w in summary.added] == [added.number] and summary.added_words == "1 work order added since the review"
    assert summary.words == "Reviewed " + rules.fmt(today) + ": the affected devices were rechecked"
    states = {j.use.work_order_id: j.later[0].state for j in rules.judge_uses(TestEquipmentUse.objects.filter(test_equipment=case.item)
                                                                               .select_related("work_order", "test_equipment"))}
    assert states == {case.work[0].pk: rules.REVIEWED, case.work[1].pk: rules.REVIEWED, late.pk: rules.REVIEWED, added.pk: rules.ADDED}
    assert services.judged_for(added)[0].words.endswith(f"; added since the review of {rules.fmt(today)}")
    services.mark_entered_in_error(found, by=None)
    found.refresh_from_db()
    assert services.review_summary(found).review is None and not rules.judge_uses(
        TestEquipmentUse.objects.filter(work_order=added).select_related("work_order", "test_equipment"))[0].later  # stops counting
    assert ToleranceReview.objects.filter(pk=review.pk).exists()  # never deleted


def test_a_review_only_where_one_is_needed(today):
    te = item("TE-01")
    fine = certificate(te, today - 10 * DAY, today=today)
    assert refused(services.decide_review, fine, basis=Basis.WITHIN_MARGIN, decided_on=today, version=None, by=None) == {
        "": f"Certificate {fine.certificate} found it in tolerance and it passed: there is nothing to review."}
    bad = certificate(te, today - 5 * DAY, result=Result.FAILED, as_found=AsFound.NOT_REPORTED, today=today)
    assert refused(services.decide_review, bad, basis=Basis.WITHIN_MARGIN, decided_on=today + DAY, version=None, by=None) == {
        "decided_on": "The day it was decided cannot be in the future."}
    services.mark_entered_in_error(bad, by=None)
    assert refused(services.decide_review, bad, basis=Basis.WITHIN_MARGIN, decided_on=today, version=None, by=None) == {
        "": f"Certificate {bad.certificate} was marked entered in error: there is nothing to review."}


def test_recheck_progress(today, ctx, dept, pump_model, users):
    devices = [Asset.objects.create(tag=f"CE-5000{n}", device_model=pump_model, department=dept) for n in range(3)]
    case = out_of_tolerance(devices[0], "TE-OT")
    pm_with(devices[1], case.item, completed_on=today - 60 * DAY)
    services.decide_review(case.found, basis=Basis.NEXT_WORK, decided_on=today, version=None, by=users.manager)
    good, stale = in_date("TE-GOOD"), past_due("TE-OLD")
    progress = services.recheck_progress(case.found)
    assert (progress.words, [d.words for d in progress.devices]) == ("0 of 2 rechecked", ["not yet", "not yet"])
    pm_with(devices[0], stale, completed_on=today - 2 * DAY)  # past due on its day: not a recheck
    safety_with(devices[2], good, completed_on=today - 2 * DAY)  # not affected
    later = repair_with(devices[1], good, completed_on=today - 3 * DAY)
    progress = services.recheck_progress(case.found)
    assert progress.words == "1 of 2 rechecked"
    assert [d.words for d in progress.devices] == ["not yet", f"rechecked on {rules.fmt(today - 3 * DAY)} ({later.number})"]


# --- the work order's side ---------------------------------------------------------------------------------------------------

def test_completing_keeps_records_replaces_and_clears(today, pump):
    a, b, c = in_date("TE-A"), in_date("TE-B"), in_date("TE-C")
    wo = open_work(pump)
    finish(wo)
    assert tags_on(wo) == []  # nothing sent: nothing recorded
    reopen(wo)
    finish(wo, [a, b, str(b.pk)])
    assert tags_on(wo) == ["TE-A", "TE-B"]
    reopen(wo)
    finish(wo)  # KEEP
    assert tags_on(wo) == ["TE-A", "TE-B"]
    reopen(wo)
    finish(wo, [b, c])
    assert tags_on(wo) == ["TE-B", "TE-C"]
    rows = _rows(TestEquipmentUse.history.model).filter(work_order_id=wo.pk).order_by("history_date", "history_id")
    assert [(r.history_type, r.test_equipment_id, r.history_change_reason) for r in rows] == [
        ("+", a.pk, "Recorded with the completion"), ("+", b.pk, "Recorded with the completion"),
        ("-", a.pk, "Taken off with the completion"), ("+", c.pk, "Recorded with the completion")]
    reopen(wo)
    finish(wo, [])
    assert tags_on(wo) == []


def test_new_items_must_be_usable_today_and_those_on_it_pass_whatever_their_status(today, pump):
    a, out = in_date("TE-A"), in_date("TE-OUT")
    services.set_status(out, to=Status.OUT_FOR_CALIBRATION, by=None, today=today - DAY)
    wo = open_work(pump)
    errors = refused(finish, wo, [a, out], pm_result="")
    assert errors == {"pm_result": errors["pm_result"],
                      "test_equipment": "TE-OUT is out for calibration: only test equipment in use is recorded on work completed today."}
    assert WorkOrder.objects.get(pk=wo.pk).status == WoStatus.IN_PROGRESS and tags_on(wo) == []  # nothing saved
    finish(wo, [a])
    services.set_status(a, to=Status.OUT_FOR_CALIBRATION, by=None, today=today - DAY)
    reopen(wo)
    finish(wo, [a])  # already on it: passes whatever its status
    assert tags_on(wo) == ["TE-A"]
    broke = in_date("TE-BROKE")
    other = open_work(pump, WoType.SAFETY)
    services.set_status(broke, to=Status.OUT_OF_SERVICE, reason=Reason.FAILED_CHECK, by=None, today=today)
    complete_work_order(other, resolution="Leakage within limits", test_equipment=[str(broke.pk)], today=today)  # its status changed today
    assert tags_on(other) == ["TE-BROKE"]


def test_the_kept_use_rule_at_completion_and_after(today, pump):
    a = in_date("TE-A")
    wo = pm_with(pump, a, completed_on=today - 20 * DAY)
    reopen(wo, today - 20 * DAY)
    assert services.kept_ids(wo) == set()
    finish(wo, [a], day=today - 20 * DAY)
    certificate(a, today - 5 * DAY, as_found=AsFound.OUT_OF_TOLERANCE, adjusted=True, today=today)  # judged since
    assert services.kept_ids(wo) == {a.pk}
    words = (f"TE-A stays on {wo.number}: a calibration dated on or after the day of this work ({rules.fmt(today - 20 * DAY)}) is on "
             "file, so the work has been judged with it.")
    assert refused(set_test_equipment, wo, [], by=None) == {"test_equipment": words}
    reopen(wo)
    assert refused(finish, wo, []) == {"test_equipment": words}  # reopened: judged by the day it was last completed
    b = in_date("TE-B")
    finish(wo, [a, b])
    assert tags_on(wo) == ["TE-A", "TE-B"]


def test_not_offered_work_and_scoped_users_record_none(today, pump, vent, vendor_user):
    a = in_date("TE-A")
    vendor_pm = open_work(pump, WoType.PM, vendor="Philips")
    assert refused(finish, vendor_pm, [a]) == {"test_equipment": services.VENDOR_WORK.format(number=vendor_pm.number)}
    finish(vendor_pm, [])  # none: fine
    repair = open_work(pump, WoType.REPAIR, vendor="Philips")
    assert refused(finish, repair, [a], repair_check=RepairCheck.VENDOR_REPORT, results=None, checks_reference="FSO-1") == {
        "test_equipment": services.VENDOR_CHECKED}
    finish(repair, [a])  # checked and ready for use by CE, after the vendor
    assert tags_on(repair) == ["TE-A"]
    old = imported_work(vent)
    assert refused(set_test_equipment, old, [a], by=None) == {"test_equipment": services.IMPORTED.format(number=old.number)}
    wo = open_work(vent, WoType.SAFETY)
    assert refused(finish, wo, [a], by=vendor_user) == {"test_equipment": services.SCOPED}
    finish(wo, [a])
    reopen(wo)
    finish(wo, [], by=vendor_user)  # an empty list from a scoped user changes nothing
    assert tags_on(wo) == ["TE-A"]
    assert refused(set_test_equipment, wo, [], by=vendor_user) == {"test_equipment": services.SCOPED}


def test_unknown_or_another_facilitys_ids_and_what_a_list_is(today, pump, other_tenant):
    with tenant_context(other_tenant):
        theirs = item("TE-THEIRS")
    wo = open_work(pump)
    for sent in ([str(theirs.pk)], ["not-an-id"], [42]):
        assert refused(finish, wo, sent) == {"test_equipment": services.CHOOSE}
    assert refused(complete_work_order, wo, resolution="Done", pm_result=PmResult.PASS, test_equipment="TE-A") == {
        "test_equipment": services.LIST_ONLY}
    assert refused(complete_work_order, wo, resolution="Done", pm_result=PmResult.PASS, test_equipment=None) == {
        "test_equipment": services.LIST_ONLY}


def test_set_test_equipment_after_completion(today, pump, users):
    a, retired = in_date("TE-A"), in_date("TE-RET")
    wo = open_work(pump, day=today - 10 * DAY)
    assert refused(set_test_equipment, wo, [str(a.pk)], by=None) == {"test_equipment": services.NOT_DONE.format(number=wo.number)}
    finish(wo, day=today - 10 * DAY)
    services.set_status(retired, to=Status.RETIRED, reason=Reason.WORN_OUT, by=None, today=today - 30 * DAY)
    assert refused(set_test_equipment, wo, [a.pk, retired.pk], by=users.technician) == {
        "test_equipment": f"TE-RET was retired on {rules.fmt(today - 30 * DAY)}, before this work was done."}
    services.set_status(a, to=Status.OUT_FOR_CALIBRATION, by=None, today=today)  # not in use now: judged on the work's day
    set_test_equipment(wo, [a.pk], by=users.technician)
    assert tags_on(wo) == ["TE-A"]
    assert _rows(TestEquipmentUse.history.model).filter(work_order_id=wo.pk).first().history_change_reason == "Recorded after completion"
    gone = item("TE-GONE")
    services.set_status(gone, to=Status.RETIRED, reason=Reason.WORN_OUT, by=None, today=today - 10 * DAY)  # retired that day: fine
    set_test_equipment(wo, [a.pk, gone.pk], by=None)
    change_status(wo, WoStatus.CLOSED)
    assert denied(set_test_equipment, wo, [a.pk], by=users.technician) == services.SET_PERMISSION
    set_test_equipment(wo, [a.pk], by=users.manager)
    assert tags_on(wo) == ["TE-A"]
    assert set_test_equipment(wo, services.KEEP, by=None).pk == wo.pk and tags_on(wo) == ["TE-A"]


def test_can_set_test_equipment(ctx, users, vendor_user):
    done, closed = WorkOrder(status=WoStatus.COMPLETED), WorkOrder(status=WoStatus.CLOSED)
    assert [wo_perms.can_set_test_equipment(u, done) for u in (users.director, users.manager, users.technician, users.analyst, vendor_user)] == [
        True, True, True, False, False]
    assert [wo_perms.can_set_test_equipment(u, closed) for u in (users.manager, users.technician)] == [True, False]


def test_assign_clears_the_uses_when_work_moves_to_a_vendor(today, pump):
    a = in_date("TE-A")
    wo = repair_with(pump, a)
    reopen(wo)
    assign(wo, technician=bench_tech("Tom Okafor"))  # in-house to in-house: kept
    assert tags_on(wo) == ["TE-A"]
    assign(wo, vendor_name="Philips")
    assert tags_on(wo) == []
    row = _rows(TestEquipmentUse.history.model).filter(work_order_id=wo.pk).first()
    assert (row.history_type, row.history_change_reason) == ("-", "Test equipment cleared: assigned to Philips")
    finish(wo, [a])  # CE checks after the vendor's repair
    reopen(wo)
    assign(wo, vendor_name="PHILIPS field service")  # the same vendor: kept
    assert tags_on(wo) == ["TE-A"]
    assign(wo, vendor_name="TechCare")
    assert tags_on(wo) == []


def test_keep_open_records_no_test_equipment_and_a_work_order_with_uses_is_never_deleted(today, pump):
    a = in_date("TE-A")
    wo = open_work(pump, WoType.REPAIR)
    with pytest.raises(TypeError):
        keep_open(wo, results=[], test_equipment=[str(a.pk)])
    done = pm_with(pump, a)
    with pytest.raises(ProtectedError):
        done.delete()
    assert services.has_uses(done) and services.delete_refusal(done) == (
        f"{done.number} has test equipment recorded, so it is not deleted: it is the record of the work done with it.")
    with pytest.raises(ProtectedError):
        a.delete()


# --- the batches and reads ---------------------------------------------------------------------------------------------------

def test_the_batches_read_in_a_fixed_number_of_queries(te_world, django_assert_num_queries):  # noqa: F811
    w = te_world
    for n in range(3):
        pm_with(Asset.objects.get(tag="CE-10002"), w.in_date, w.past_due, w.no_record, completed_on=w.today - (n + 1) * DAY)
    with django_assert_num_queries(2):
        items = services.list_with_standing(TestEquipment.objects.select_related("held_by").order_by("tag"), w.today)
    assert {i.tag: i.standing.state for i in items} == {"TE-FL": rules.FAILED, "TE-IN": rules.IN_DATE, "TE-NR": rules.NO_RECORD,
                                                        "TE-OT1": rules.IN_DATE, "TE-OT2": rules.IN_DATE, "TE-PD": rules.PAST_DUE}
    assert items[1].held_by == w.tech
    uses = list(TestEquipmentUse.objects.select_related("work_order", "test_equipment"))
    with django_assert_num_queries(3):
        judged = rules.judge_uses(uses)
    assert len(judged) == len(uses) == 14
    wos = list(WorkOrder.objects.all())
    with django_assert_num_queries(4):
        by_wo = services.judged_by_work_order(wos)
    assert sum(len(v) for v in by_wo.values()) == 14


def test_the_world_and_its_words(te_world):  # noqa: F811
    w = te_world
    (pm_use,) = services.judged_for(w.pm)
    due = Calibration.objects.get(test_equipment=w.in_date).due_on
    assert pm_use.words == f"TE-IN (Fluke Biomedical ESA612): calibration due {rules.fmt(due)} on the day of this work"
    assert not rules.offered(w.vendor) and not rules.offered(w.imported)
    assert w.failed.status == Status.OUT_OF_SERVICE and w.reviewed.review is not None and w.unreviewed.review is None
    reviewed = services.judged_for(w.reviewed.work[0])[0]
    assert reviewed.later[0].words == (f"found out of tolerance on {rules.fmt(w.reviewed.found.calibrated_on)}; reviewed "
                                       f"{rules.fmt(w.today)}: the error was smaller than the margin to the limits used")
    assert services.work_since_last_calibration(w.unreviewed.item) == 0 and services.work_since_last_calibration(w.in_date) == 1
    assert [j.use.work_order for j in services.work_done(w.unreviewed.item)] == w.unreviewed.work[::-1]


def test_the_picker(today, pump, users):
    dana = bench_tech("Dana Whitfield")
    mine = in_date("TE-MINE", held_by=dana)
    used = in_date("TE-USED")
    pm_with(pump, used, tech=dana, completed_on=today - 5 * DAY)
    away = in_date("TE-AWAY")
    services.set_status(away, to=Status.OUT_FOR_CALIBRATION, by=None, today=today - DAY)
    others = [in_date(f"TE-O{n}", kind=Kind.PATIENT_SIMULATOR, model="ProSim 8") for n in range(2)]
    wo = open_work(pump, tech=dana)
    pick = services.picker(wo, users.technician, today=today)
    assert [o.item.tag for o in pick.yours] == ["TE-MINE", "TE-USED"] and pick.technician == dana
    assert [(label, [o.item.tag for o in options]) for label, options in pick.others] == [("Patient simulator", [o.tag for o in others])]
    assert (pick.others_count, pick.open_others) == (2, False)
    assert not any(o.on_work_order for o in pick.yours)  # never pre-ticked
    finish(wo, [mine])
    certificate(mine, today, today=today)
    services.set_status(away, to=Status.IN_USE, by=None)
    services.set_status(away, to=Status.OUT_FOR_CALIBRATION, by=None, today=today - DAY)
    pick = services.picker(WorkOrder.objects.get(pk=wo.pk), users.technician, today=today)
    first = pick.yours[0]
    assert (first.item.tag, first.on_work_order, first.kept) == ("TE-MINE", True, True)
    assert "TE-AWAY" in [o.item.tag for _label, options in pick.others for o in options]  # after completion: judged on its day


# --- isolation ---------------------------------------------------------------------------------------------------------------

def test_the_four_models_stay_in_their_facility(te_world, other_tenant):  # noqa: F811
    assert TestEquipment.objects.count() == 6 and ToleranceReview.objects.count() == 1
    with tenant_context(other_tenant):
        assert not TestEquipment.objects.exists() and not Calibration.objects.exists()
        assert not ToleranceReview.objects.exists() and not TestEquipmentUse.objects.exists()
        assert not _rows(TestEquipment.history.model).exists()
        theirs = item("TE-IN")  # the same tag in another facility
        certificate(theirs, timezone.localdate(), today=timezone.localdate())
        assert services.list_with_standing(TestEquipment.objects.all(), timezone.localdate())[0].standing.state == rules.IN_DATE
    assert TestEquipment.objects.filter(tag="TE-IN").count() == 1
    assert refused(services.record_calibration, theirs, calibrated_on=timezone.localdate(), provider="x", certificate="C-9",
                   accreditation=Mark.SHOWN, as_found=AsFound.IN_TOLERANCE, adjusted=False, result=Result.PASSED, by=None) == {"": services.CHOOSE}


# --- PostgreSQL ------------------------------------------------------------------------------------------------------------------

@needs_postgres
def test_the_services_under_the_policies(today, pump, vent, other_tenant, make_user):
    with tenant_context(other_tenant):
        theirs = item("TE-THEIRS")
    manager = make_user("manager")
    as_app_role()
    case = out_of_tolerance(vent, "TE-OT")
    services.decide_review(case.found, basis=Basis.FUNCTION_UNUSED, decided_on=today, version=services.review_summary(case.found).version,
                           by=manager)
    te = in_date("TE-A")
    wo = pm_with(pump, te)
    set_test_equipment(wo, [te.pk, case.item.pk], by=manager)
    services.set_status(te, to=Status.MISSING, by=manager)
    assert services.recheck_progress(case.found).words == "0 of 1 rechecked"
    assert sorted(TestEquipment.unscoped.values_list("tag", flat=True)) == ["TE-A", "TE-OT"]
    assert TestEquipmentUse.unscoped.count() == 4 and Calibration.unscoped.count() == 3 and ToleranceReview.unscoped.count() == 1
    assert refused(finish, open_work(pump), [theirs.pk]) == {"test_equipment": services.CHOOSE}
    with pytest.raises(DatabaseError), transaction.atomic():
        TestEquipment.unscoped.create(tenant=other_tenant, tag="TE-X", tag_key="te-x", kind=Kind.OTHER, manufacturer="x", model="x",
                                      serial="x", status_since=today)


def _thread(target, errors, results, key):
    def run():
        try:
            results[key] = target()
        except Exception as e:  # noqa: BLE001 - reported by the test
            errors[key] = e
        finally:
            connection.close()
    return threading.Thread(target=run)


@needs_postgres
@pytest.mark.django_db(transaction=True)
def test_two_items_added_with_one_tag_at_once(tenant, monkeypatch):
    """The first add holds the facility's tag lock while it sleeps between its check and its insert; the second waits for it, then
    finds the tag taken and refuses in words (without the lock it would find nothing and fail on the unique key)."""
    inside = threading.Event()
    real = services._tag_refusal

    def slow(tag, exclude=None):
        found = real(tag, exclude)
        if threading.current_thread().name == "first":
            inside.set()
            time.sleep(1)
        return found

    monkeypatch.setattr(services, "_tag_refusal", slow)
    errors, results = {}, {}

    def add(tag, serial):
        def call():
            with tenant_context(tenant):
                return item(tag, serial=serial).pk
        return call

    first = _thread(add("TE-01", "A1"), errors, results, "first")
    first.name = "first"
    second = _thread(add("te-01", "B2"), errors, results, "second")
    first.start()
    assert inside.wait(20)
    second.start()
    for t in (first, second):
        t.join(30)
    assert "first" in results and isinstance(errors.get("second"), ValidationError), errors
    assert errors["second"].message_dict == {"tag": ["te-01 is already another test equipment item's tag."]}
    with tenant_context(tenant):
        assert list(TestEquipment.objects.values_list("tag", flat=True)) == ["TE-01"]


@needs_postgres
@pytest.mark.django_db(transaction=True)
def test_a_device_and_an_item_with_one_tag_at_once(tenant, monkeypatch):
    """An item being added holds the tag lock; a device with the same tag (another letter case) waits for it, then is refused."""
    from apps.equipment.models import Department, DeviceModel

    with tenant_context(tenant):
        dept = Department.objects.create(name="ICU")
        model = DeviceModel.objects.create(manufacturer="BD", model="Alaris", description="Pump", category="Infusion pumps")
    inside = threading.Event()
    real = services._tag_refusal

    def slow(tag, exclude=None):
        found = real(tag, exclude)
        inside.set()
        time.sleep(1)
        return found

    monkeypatch.setattr(services, "_tag_refusal", slow)
    errors, results = {}, {}

    def add_item():
        with tenant_context(tenant):
            return item("TE-01").pk

    def add_device():
        with tenant_context(tenant):
            return create_asset(tag="te-01", device_model=model, department=dept).pk

    first, second = _thread(add_item, errors, results, "item"), _thread(add_device, errors, results, "device")
    first.start()
    assert inside.wait(20)
    second.start()
    for t in (first, second):
        t.join(30)
    assert "item" in results and errors["device"].message_dict == {"tag": ["te-01 is test equipment's tag."]}, errors
    with tenant_context(tenant):
        assert not Asset.objects.exists() and TestEquipment.objects.count() == 1


@needs_postgres
@pytest.mark.django_db(transaction=True)
def test_a_completion_naming_an_item_while_another_request_retires_it(tenant, monkeypatch):
    """The retirement holds the item's row while it sleeps after reading it; the completion naming the item waits for that row (after
    its work order's), then reads it retired and refuses in words: never a use of an item retired before the day, read stale."""
    from apps.equipment.models import Department, DeviceModel

    today = timezone.localdate()
    with tenant_context(tenant):
        dept = Department.objects.create(name="ICU")
        model = DeviceModel.objects.create(manufacturer="BD", model="Alaris", description="Pump", category="Infusion pumps")
        asset = Asset.objects.create(tag="CE-1", device_model=model, department=dept)
        te = in_date("TE-A")
        wo = open_work(asset)
    inside = threading.Event()
    real = services._move

    def slow(locked, to, reason):
        found = real(locked, to, reason)
        inside.set()
        time.sleep(1)
        return found

    monkeypatch.setattr(services, "_move", slow)
    errors, results = {}, {}

    def retire():
        with tenant_context(tenant):
            return services.set_status(te, to=Status.RETIRED, reason=Reason.WORN_OUT, by=None, today=today - DAY).status

    def complete():
        with tenant_context(tenant):
            return finish(WorkOrder.objects.get(pk=wo.pk), [te], day=today)

    first, second = _thread(retire, errors, results, "retire"), _thread(complete, errors, results, "complete")
    first.start()
    assert inside.wait(20)
    second.start()
    for t in (first, second):
        t.join(30)
    assert results.get("retire") == Status.RETIRED and set(errors) == {"complete"}, errors
    assert errors["complete"].message_dict == {"test_equipment": ["TE-A is retired (worn out or obsolete): only test equipment in use is "
                                                                  "recorded on work completed today."]}
    with tenant_context(tenant):
        assert WorkOrder.objects.get(pk=wo.pk).status == WoStatus.IN_PROGRESS and not TestEquipmentUse.objects.exists()


def test_the_helpers_wave_2_calls():
    """The names the screens, the API, and the binder call (wave 2's contract)."""
    for name in ("add_test_equipment", "update_test_equipment", "set_status", "record_calibration", "mark_entered_in_error", "decide_review",
                 "affected_work", "review_version", "review_summary", "recheck_progress", "list_with_standing", "work_done", "judged_for",
                 "judged_by_work_order", "check_uses", "apply_uses", "clear_for_vendor", "kept_ids", "picker", "moves_for", "delete_refusal",
                 "entered_in_error_note", "work_since_last_calibration", "default_due", "due_cap"):
        assert callable(getattr(services, name)), name
    for name in ("standing_on", "entered_days", "window", "in_window", "window_q", "counted_uses_q", "counted_work_q", "counted", "offered",
                 "usable_today", "may_remove", "kept", "retired_throughout", "judge_uses", "standings", "calibrations_by_item",
                 "reviews_by_calibration", "found_later", "use_words", "item_name", "basis_words", "matches"):
        assert callable(getattr(rules, name)), name
