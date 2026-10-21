"""
Slice 38 review fixes, the services and rules (F1), on SQLite and PostgreSQL:

- C1, C2: Confirm version and Record update judge the outcome against the device's record for the update's day, read from its history
  (updates._release_then), never against a record made since (a bulk set, Record facts) nor, for an install, the install's own result
  (its outcome entered in error and confirmed again, a bulk set dated its day); release_before, and patches' release after, come from the
  same read. The refusal stays where the record that day already named the release. A completion reads the record as it is now. One
  history query per writer call.
- C9: an update imported from another system and reopened is an open update: open_update refuses another (naming it), the batch skips
  the device, and updates.open_number names it.
- C15: where an outcome came from is the record's own (on_vendor_service, vendor_report: the work order when the outcome was recorded),
  never the work order's flags later; Entered in error clears it with the outcome.

C3 and C4 (the openers take the device FOR KEY SHARE after the insert) need two writers at once: tests/test_cyber_review38_pg.py.
"""
from datetime import datetime, time, timedelta

import pytest
from cyber_fixtures import facts, release, update_wo
from django.core.exceptions import ValidationError
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.cybersecurity import rules, updates
from apps.cybersecurity.models import DeviceSecurity, InstalledBy, SoftwareRelease, SoftwareUpdate, UpdateBasis, UpdateOutcome, UpdateTarget
from apps.cybersecurity.services import KEEP, record_facts, record_facts_for
from apps.cybersecurity.updates import UpdateChoice, confirm, open_update, open_updates, record, remove
from apps.equipment.models import Asset
from apps.workorders import legacy
from apps.workorders import services as wo_services
from apps.workorders.completion import complete_work_order
from apps.workorders.models import OPEN_STATUSES, WorkOrder, WoStatus, WoType

DAY = timedelta(days=1)
MR, VL, NS = UpdateBasis.MANUFACTURER_RELEASE, UpdateBasis.VALIDATED_LIST, UpdateBasis.NOT_STATED
INSTALLED, PATCHES, ALREADY = UpdateOutcome.INSTALLED, UpdateOutcome.PATCHES, UpdateOutcome.ALREADY
LATER = "; a later record of the device is kept"


def refused(fn, *args, **kwargs) -> dict:
    with pytest.raises(ValidationError) as e:
        fn(*args, **kwargs)
    return {k: v[0] if len(v) == 1 else v for k, v in e.value.message_dict.items()}


def choice(outcome, release=None, *, reference="", basis=""):
    return UpdateChoice(outcome=outcome, release_id=str(release.pk) if isinstance(release, SoftwareRelease) else release,
                        reference=reference, basis=basis)


def complete(wo, by, software=KEEP, resolution="", *, today=None, **kwargs):
    return complete_work_order(wo, resolution=resolution, software=software, by=by, today=today or timezone.localdate(), **kwargs)


def words(row) -> str:
    fresh = SoftwareUpdate.objects.select_related("work_order", "target_release", "release_before", "release_after").get(pk=row.pk)
    text = rules.update_words(fresh)
    assert not [w for w in rules.NEVER_WORDS if w in text.lower()], text
    return text


def day(d) -> str:
    return rules.day_words(d)


def saved_on(sec, d) -> None:
    """Date every history version of the device's record `sec` to the facility's day `d` (as if it was saved then)."""
    DeviceSecurity.history.filter(id=sec.pk).update(history_date=timezone.make_aware(datetime.combine(d, time(9))))


@pytest.fixture
def today(ctx):
    return timezone.localdate()


@pytest.fixture
def kim(make_user):
    return make_user("director")


@pytest.fixture
def tech(make_user):
    return make_user("technician")


@pytest.fixture
def manager(make_user):
    return make_user("manager")


@pytest.fixture
def vendor(make_user):
    user = make_user("vendor")
    user.company = "BD"
    user.save(update_fields=["company"])
    return user


@pytest.fixture
def r52(pump_model):
    return release(pump_model, "5.2 (demo)")


@pytest.fixture
def r56(pump_model):
    return release(pump_model, "5.6 (demo)")


@pytest.fixture
def r57(pump_model):
    return release(pump_model, "5.7 (demo)")


@pytest.fixture
def on52(pump, r52, today):
    """The pump runs 5.2 (demo) since a month ago (typed in now, dated then)."""
    return facts(pump, r52, release_since=today - 30 * DAY, checked_on=today - 30 * DAY)


@pytest.fixture
def job(pump, r56, techs):
    """An in-house update work order to install 5.6 (demo) on the pump, assigned to Tom."""
    return update_wo(pump, r56, assigned_to=techs["tom"])


@pytest.fixture
def vendor_job(pump, r56, vendor, today):
    """5.6 (demo) installed by BD five days ago, completed by their account with their report's number (nothing recorded)."""
    wo, row = update_wo(pump, r56, vendor_service=True, vendor_name="BD field service", opened_on=today - 10 * DAY)
    complete(wo, vendor, resolution="Updated per our visit", checks_reference="VR-2026-77", today=today - 5 * DAY)
    return wo, row


# --- C1: the real install can be recorded ---------------------------------------------------------------------------------------------

def test_c1_entered_in_error_then_confirm_records_the_install(pump, r52, r56, on52, job, tech, manager, today):
    """Completed as installed with the wrong basis, entered in error (the facts stay at 5.6 since today), then confirmed as installed
    with the right one: what it ran before is the record for the day, never the install's own result."""
    wo, row = job
    complete(wo, tech, choice(INSTALLED, r56, basis=NS))
    remove(row, by=manager)
    confirm(row, choice(INSTALLED, r56, basis=MR), by=tech)
    row.refresh_from_db()
    assert (row.outcome, row.release_before, row.release_after, row.basis, row.applied_on, row.later_kept) == (
        INSTALLED, r52, r56, MR, today, False)
    assert words(row) == f"5.6 (demo) installed {day(today)} (recorded before: 5.2 (demo))"
    sec = DeviceSecurity.objects.get(asset=pump)
    assert (sec.release, sec.release_since) == (r56, today)


def test_c1_the_vendors_job_confirmed_after_a_bulk_set(pump, pump_model, r52, r56, on52, vendor_job, tech, today):
    """The vendor installed 5.6 five days ago; IT's bulk set records the pumps on 5.6 today; CE then confirms: installed, from 5.2.
    "Already on the update" is refused: the record said 5.2 that day."""
    _wo, row = vendor_job
    record_facts_for(pump_model, [pump], by=None, release=r56, today=today)
    assert refused(confirm, row, choice(ALREADY), by=tech) == {
        "software_release": updates.NOT_THE_TARGET.format(version="5.2 (demo)", target="5.6 (demo)")}
    confirm(row, choice(INSTALLED, r56, basis=MR), by=tech)
    row.refresh_from_db()
    assert (row.outcome, row.release_before, row.release_after, row.applied_on, row.later_kept) == (
        INSTALLED, r52, r56, today - 5 * DAY, True)
    assert words(row) == (f"5.6 (demo) installed {day(today - 5 * DAY)} (recorded before: 5.2 (demo)); as the vendor's report "
                          f"VR-2026-77 states{LATER}")


def test_c1_a_bulk_set_dated_on_the_install_day_is_its_result(pump, pump_model, r52, r56, on52, vendor_job, tech, today):
    _wo, row = vendor_job
    record_facts_for(pump_model, [pump], by=None, release=r56, release_since=today - 5 * DAY, today=today)  # since the vendor's day
    confirm(row, choice(INSTALLED, r56, basis=MR), by=tech)
    row.refresh_from_db()
    assert (row.release_before, row.release_after) == (r52, r56)


def test_c1_record_by_hand_after_a_bulk_set_dated_its_day(pump, pump_model, r52, r56, on52, tech, today):
    record_facts_for(pump_model, [pump], by=None, release=r56, release_since=today - 2 * DAY, today=today)
    row = record(pump, choice(INSTALLED, r56, basis=NS), installed_by=InstalledBy.IT, applied_on=today - 2 * DAY, by=tech)
    assert (row.release_before, row.release_after, row.later_kept) == (r52, r56, True)
    assert words(row) == (f"5.6 (demo) installed {day(today - 2 * DAY)} (recorded before: 5.2 (demo)); installed by information "
                          f"technology{LATER}")


def test_c1_the_refusal_stays_where_the_record_that_day_named_the_release(pump, r52, r56, vendor_job, tech, today):
    """The record says 5.6 since before the vendor's day: it already ran it."""
    _wo, row = vendor_job
    facts(pump, r56, release_since=today - 10 * DAY, checked_on=today - 10 * DAY)
    assert refused(confirm, row, choice(INSTALLED, r56, basis=MR), by=tech) == {
        "software_release": updates.ALREADY_RAN.format(version="5.6 (demo)")}
    assert refused(record, pump, choice(INSTALLED, r56, basis=MR), installed_by=InstalledBy.IT, applied_on=today - 5 * DAY, by=tech) == {
        "software_release": updates.ALREADY_RAN_BY_HAND.format(version="5.6 (demo)")}


def test_a_same_day_record_of_another_release_comes_before(pump, r52, r56, on52, job, tech, today):
    """Installed 5.6 today at a completion, then rolled back to 5.2 the same day by hand: it ran 5.6 before the rollback."""
    wo, _row = job
    complete(wo, tech, choice(INSTALLED, r56, basis=MR))
    row = record(pump, choice(INSTALLED, r52, basis=NS), installed_by=InstalledBy.MANUFACTURER, applied_on=today, by=tech)
    assert (row.release_before, row.release_after, row.later_kept) == (r56, r52, False)


def test_a_completion_reads_the_record_as_it_is_now(pump, r52, r56, on52, job, tech, today):
    wo, _row = job
    record_facts(pump, by=None, release=r56)  # recorded today: the record as of today names the target
    assert refused(complete, wo, tech, choice(INSTALLED, r56, basis=MR), "Done") == {
        "software_release": updates.ALREADY_RAN.format(version="5.6 (demo)")}


# --- C2: the versions named are the day's ---------------------------------------------------------------------------------------------

def test_c2_a_later_record_never_names_what_it_ran_before(pump, r52, r56, r57, on52, vendor_job, tech, today):
    _wo, row = vendor_job
    record_facts(pump, by=None, release=r57)  # IT records 5.7 today, five days after the vendor's visit
    confirm(row, choice(INSTALLED, r56, basis=MR), by=tech)
    row.refresh_from_db()
    assert (row.release_before, row.release_after, row.later_kept) == (r52, r56, True)
    assert words(row) == (f"5.6 (demo) installed {day(today - 5 * DAY)} (recorded before: 5.2 (demo)); as the vendor's report "
                          f"VR-2026-77 states{LATER}")
    assert DeviceSecurity.objects.get(asset=pump).release == r57  # the later record is kept


def test_c2_patches_confirmed_after_a_later_record_name_the_version_that_day(pump, r52, r57, on52, vendor, tech, today):
    wo, row = update_wo(pump, reference="ICAR-2026-09", vendor_service=True, vendor_name="BD field service", opened_on=today - 10 * DAY)
    complete(wo, vendor, resolution="Patched per our visit", checks_reference="VR-2026-78", today=today - 5 * DAY)
    record_facts(pump, by=None, release=r57)
    assert refused(confirm, row, choice(PATCHES, r57, reference="ICAR-2026-09", basis=VL), by=tech) == {
        "software_release": updates.PATCHES_UNCHANGED}
    confirm(row, choice(PATCHES, r52, reference="ICAR-2026-09", basis=VL), by=tech)  # the version it ran that day is accepted
    row.refresh_from_db()
    assert (row.release_before, row.release_after, row.later_kept) == (r52, r52, True)
    assert words(row) == (f"Patches on the manufacturer's validated patch list applied {day(today - 5 * DAY)}, version unchanged "
                          f"(5.2 (demo)); ref ICAR-2026-09; as the vendor's report VR-2026-78 states{LATER}")


def test_c2_a_hand_record_in_the_past_names_the_versions_of_its_day(pump, r52, r56, r57, on52, tech, today):
    record_facts(pump, by=None, release=r57, release_since=today - 8 * DAY, checked_on=today - 8 * DAY)
    row = record(pump, choice(INSTALLED, r56, basis=NS), installed_by=InstalledBy.AUTOMATIC, applied_on=today - 10 * DAY, by=tech)
    assert (row.release_before, row.later_kept) == (r52, True)
    assert words(row) == f"5.6 (demo) installed {day(today - 10 * DAY)} (recorded before: 5.2 (demo)); installed automatically{LATER}"
    patched = record(pump, choice(PATCHES, reference="ICAR-2026-07", basis=NS), installed_by=InstalledBy.CE, applied_on=today - 12 * DAY,
                     by=tech)
    assert (patched.release_before, patched.release_after, patched.later_kept) == (r52, r52, True)


def test_c2_a_record_saved_before_the_day_counts_one_saved_after_does_not(dept, pump_model, pump, r52, r56, r57, tech, today):
    """With no "since" on the record, only the day it was saved says when: saved before the update's day it counts; saved after, never."""
    other = Asset.objects.create(tag="CE-10009", device_model=pump_model, department=dept)
    saved_on(facts(pump, r52), today - 20 * DAY)  # 5.2 with no since, saved 20 days ago
    facts(other, r52)  # the same, saved today
    for device in (pump, other):
        record_facts(device, by=None, release=r57)  # 5.7 since today
    row = record(pump, choice(INSTALLED, r56, basis=NS), installed_by=InstalledBy.IT, applied_on=today - 10 * DAY, by=tech)
    assert (row.release_before, row.later_kept) == (r52, True)
    unknown = record(other, choice(INSTALLED, r56, basis=NS), installed_by=InstalledBy.IT, applied_on=today - 10 * DAY, by=tech)
    assert unknown.release_before is None
    assert words(unknown) == (f"5.6 (demo) installed {day(today - 10 * DAY)} (recorded before: not recorded); installed by information "
                              f"technology{LATER}")


def test_one_history_query_per_writer_call(pump, r52, r56, on52, vendor_job, tech, today):
    _wo, row = vendor_job

    def history_reads(fn, *args, **kwargs) -> int:
        with CaptureQueriesContext(connection) as seen:
            fn(*args, **kwargs)
        return sum(1 for q in seen.captured_queries
                   if q["sql"].lstrip().upper().startswith("SELECT") and "historicaldevicesecurity" in q["sql"].lower())

    assert history_reads(confirm, row, choice(INSTALLED, r56, basis=MR), by=tech) == 1
    assert history_reads(record, pump, choice(PATCHES, reference="ICAR-2026-07", basis=NS), installed_by=InstalledBy.CE,
                         applied_on=today, by=tech) == 1


# --- C9: a reopened imported update is an open update ---------------------------------------------------------------------------------

def test_c9_a_reopened_imported_update_is_an_open_update(pump, r52, r56, on52, kim, today):
    imported = legacy.import_work_order(asset=pump, legacy_number="sw-9", type=WoType.SOFTWARE_UPDATE, status=WoStatus.COMPLETED,
                                        opened_on=today - 20 * DAY, completed_on=today - 19 * DAY, today=today).work_order
    assert updates.open_number(pump) == ""  # completed: history only
    wo_services.change_status(imported, WoStatus.IN_PROGRESS)
    assert not SoftwareUpdate.objects.exists() and updates.open_number(pump) == imported.number
    assert refused(open_update, pump, target=UpdateTarget.RELEASE, target_release=r56, due_on=today, priority="normal", by=kim) == {
        "asset": updates.ALREADY_OPEN.format(tag=pump.tag, number=imported.number)}
    batch = open_updates(r56, from_releases=[r52], due_on=today, priority="normal", by=kim)
    assert (batch.created, batch.already_open) == (0, (pump.tag,))
    assert WorkOrder.objects.filter(asset=pump, type=WoType.SOFTWARE_UPDATE, status__in=OPEN_STATUSES).count() == 1


# --- C15: where an outcome came from ---------------------------------------------------------------------------------------------------

def test_c15_an_in_house_outcome_is_never_credited_to_a_later_vendor(pump, r52, r56, on52, job, tech, kim, vendor, today):
    wo, row = job
    complete(wo, tech, choice(INSTALLED, r56, basis=MR))
    row.refresh_from_db()
    assert (row.on_vendor_service, row.vendor_report) == (False, "")
    wo_services.change_status(wo, WoStatus.IN_PROGRESS, by=kim)
    wo_services.assign(wo, vendor_name="BD field service", by=kim)
    complete(wo, vendor, resolution="Checked per our visit", checks_reference="SR-777")  # KEEP: the outcome stays
    wo.refresh_from_db()
    assert wo.vendor_service and wo.checks_reference == "SR-777"
    assert words(row) == f"5.6 (demo) installed {day(today)} (recorded before: 5.2 (demo))"


def test_c15_a_vendors_outcome_stays_the_vendors_after_moving_in_house(pump, r52, r56, on52, vendor_job, tech, kim, techs, manager,
                                                                      today):
    wo, row = vendor_job
    confirm(row, choice(INSTALLED, r56, basis=MR), by=tech)
    row.refresh_from_db()
    assert (row.on_vendor_service, row.vendor_report) == (True, "VR-2026-77")
    wo_services.change_status(wo, WoStatus.IN_PROGRESS, by=kim)
    wo_services.assign(wo, technician=techs["tom"], by=kim)  # in-house now: the work order's report number is cleared
    complete(wo, tech, KEEP, "Checked again")
    wo.refresh_from_db()
    assert not wo.vendor_service and wo.checks_reference == ""
    assert words(row) == f"5.6 (demo) installed {day(today - 5 * DAY)} (recorded before: 5.2 (demo)); as the vendor's report VR-2026-77 states"
    remove(row, by=manager)
    row.refresh_from_db()
    assert (row.outcome, row.on_vendor_service, row.vendor_report) == ("", False, "")  # cleared with the outcome


def test_c15_a_completion_on_vendor_service_records_its_report(pump, r52, r56, on52, kim, today):
    wo, row = update_wo(pump, r56, vendor_service=True, vendor_name="BD field service")
    complete(wo, kim, choice(INSTALLED, r56, basis=MR), checks_reference="VR-2026-80")
    row.refresh_from_db()
    assert (row.on_vendor_service, row.vendor_report) == (True, "VR-2026-80")
    assert words(row).endswith("; as the vendor's report VR-2026-80 states")
