"""
Slice 38 review fixes, the services and rules (F1), on SQLite and PostgreSQL:

- C1, C2, as round 2 settled them (no history read: Cadence never rebuilds a past record):
  - what was recorded before is taken once, at the row's first recording, from the device's record then, and only when its facts are not
    dated after the update's day; else it is not known. Entered in error and a re-completion keep it; a later Confirm version reuses it.
    An install of the release the record already names claims no before;
  - "installed" is never refused for naming the release the record names, at any door; "patches" and "already" are checked against the
    record only when the facts will be written; with a later record kept, the release sent is the version that day;
  - the words: "(recorded before: X)" only when X is known; patches with no version and a later record kept name none;
  - one install is recorded once by hand (updates.DUPLICATE); a work order's outcome is never refused for a hand row's;
  - Confirm version never tells the user to keep a completed work order open (NOT_THE_TARGET_DONE).
- C9: an update imported from another system and reopened is an open update: open_update refuses another (naming it), the batch skips
  the device, and updates.open_number names it.
- C15: where an outcome came from is the record's own (on_vendor_service, vendor_report: the work order when the outcome was recorded),
  never the work order's flags later; Entered in error clears it with the outcome.

C3 and C4 (the openers take the device FOR KEY SHARE after the insert) need two writers at once: tests/test_cyber_review38_pg.py.
"""
from datetime import timedelta

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


# --- C1, C2 (round 2): what was recorded before is taken once --------------------------------------------------------------------------

def test_c1_entered_in_error_then_confirm_records_the_install(pump, r52, r56, on52, job, tech, manager, today):
    """Completed as installed with the wrong basis, entered in error (the facts stay at 5.6 since today), then confirmed as installed
    with the right one: what it ran before is the one taken at the completion, never the record now (5.6, the install's own result)."""
    wo, row = job
    complete(wo, tech, choice(INSTALLED, r56, basis=NS))
    remove(row, by=manager)
    row.refresh_from_db()
    assert (row.outcome, row.release_before, row.before_taken) == ("", r52, True)  # kept through Entered in error
    confirm(row, choice(INSTALLED, r56, basis=MR), by=tech)
    row.refresh_from_db()
    assert (row.outcome, row.release_before, row.release_after, row.basis, row.applied_on, row.later_kept) == (
        INSTALLED, r52, r56, MR, today, False)
    assert words(row) == f"5.6 (demo) installed {day(today)} (recorded before: 5.2 (demo))"
    sec = DeviceSecurity.objects.get(asset=pump)
    assert (sec.release, sec.release_since) == (r56, today)


def test_c1_remove_then_confirm_another_release_keeps_the_true_before(pump, r52, r56, r57, on52, techs, tech, manager, today):
    """Completed three days ago as installed 5.6, the wrong release picked (the device runs 5.7): entered in error, then confirmed as
    installed 5.7: recorded before 5.2, never 5.6 (the outcome entered in error)."""
    wo, row = update_wo(pump, r57, assigned_to=techs["tom"], opened_on=today - 5 * DAY)
    complete(wo, tech, choice(INSTALLED, r56, basis=MR), today=today - 3 * DAY)
    remove(row, by=manager)
    confirm(row, choice(INSTALLED, r57, basis=MR), by=tech)
    row.refresh_from_db()
    assert (row.release_before, row.release_after, row.applied_on, row.later_kept) == (r52, r57, today - 3 * DAY, False)
    assert words(row) == f"5.7 (demo) installed {day(today - 3 * DAY)} (recorded before: 5.2 (demo))"
    assert DeviceSecurity.objects.get(asset=pump).release == r57  # the facts follow the corrected install


def test_c1_remove_then_confirm_patches_keeps_the_true_before(pump, r52, r56, on52, techs, tech, manager, today):
    """A patches job completed three days ago as installed 5.6 by mistake; entered in error; the release corrected with Record facts as
    the toast says (5.2, since today); then confirmed as patches on 5.2: accepted (a later record is kept, so the release sent is the
    version that day), recorded before 5.2 still."""
    wo, row = update_wo(pump, reference="ICAR-2026-09", assigned_to=techs["tom"], opened_on=today - 5 * DAY)
    complete(wo, tech, choice(INSTALLED, r56, basis=MR), today=today - 3 * DAY)
    remove(row, by=manager)
    record_facts(pump, by=None, release=r52)  # since today
    confirm(row, choice(PATCHES, r52, reference="ICAR-2026-09", basis=VL), by=tech)
    row.refresh_from_db()
    assert (row.outcome, row.release_before, row.release_after, row.later_kept) == (PATCHES, r52, r52, True)
    assert words(row) == (f"Patches on the manufacturer's validated patch list applied {day(today - 3 * DAY)}, version unchanged "
                          f"(5.2 (demo)); ref ICAR-2026-09{LATER}")


def test_c1_a_re_completion_with_a_corrected_basis_keeps_the_before(pump, r52, r56, on52, job, tech, kim, today):
    """Completed as installed 5.6 (basis not stated), reopened, and completed again as installed 5.6 on the manufacturer's release: the
    same row, accepted (never "It already ran 5.6 before"), recorded before 5.2 as taken at the first completion."""
    wo, row = job
    complete(wo, tech, choice(INSTALLED, r56, basis=NS))
    wo_services.change_status(wo, WoStatus.IN_PROGRESS, by=kim)
    complete(wo, tech, choice(INSTALLED, r56, basis=MR), "Done again")
    row.refresh_from_db()
    assert (row.outcome, row.release_before, row.release_after, row.basis) == (INSTALLED, r52, r56, MR)
    assert words(row) == f"5.6 (demo) installed {day(today)} (recorded before: 5.2 (demo))"
    assert SoftwareUpdate.objects.count() == 1


@pytest.mark.parametrize("door", ["bulk set", "record facts"])
def test_c1_a_vendor_closing_its_job_the_day_after_the_visit(pump, pump_model, r52, r56, on52, vendor_job, tech, today, door):
    """BD installed 5.6 the day before it closed its job (five days ago); IT records the pump on 5.6 since the visit, as the vendor's
    report says. Confirm installed 5.6 is accepted: the facts are newer than the job's day, so what it ran before is not known (never the
    false "already on 5.6; nothing installed")."""
    _wo, row = vendor_job
    if door == "bulk set":
        record_facts_for(pump_model, [pump], by=None, release=r56, release_since=today - 6 * DAY, today=today)
    else:
        record_facts(pump, by=None, release=r56, release_since=today - 6 * DAY)
    confirm(row, choice(INSTALLED, r56, basis=MR), by=tech)
    row.refresh_from_db()
    assert (row.outcome, row.release_before, row.release_after, row.applied_on, row.later_kept) == (
        INSTALLED, None, r56, today - 5 * DAY, True)
    assert words(row) == f"5.6 (demo) installed {day(today - 5 * DAY)}; as the vendor's report VR-2026-77 states{LATER}"


def test_c1_the_vendors_job_confirmed_after_a_bulk_set(pump, pump_model, r52, r56, on52, vendor_job, tech, today):
    _wo, row = vendor_job
    record_facts_for(pump_model, [pump], by=None, release=r56, today=today)  # 5.6 since today
    confirm(row, choice(INSTALLED, r56, basis=MR), by=tech)
    row.refresh_from_db()
    assert (row.release_before, row.release_after, row.later_kept) == (None, r56, True)
    assert words(row) == f"5.6 (demo) installed {day(today - 5 * DAY)}; as the vendor's report VR-2026-77 states{LATER}"


def test_c1_the_record_facts_first_then_completion_as_installed(pump, r52, r56, on52, job, tech, techs, kim, today):
    """The technician records 5.6 with Record facts, then completes as installed 5.6: accepted, with no false before (the record named
    5.6 already). The same over two days: started and recorded the day before, completed the next."""
    wo, row = job
    record_facts(pump, by=None, release=r56)
    complete(wo, tech, choice(INSTALLED, r56, basis=MR))
    row.refresh_from_db()
    assert (row.outcome, row.release_before, row.release_after, row.later_kept) == (INSTALLED, None, r56, False)
    assert words(row) == f"5.6 (demo) installed {day(today)}"

    other = Asset.objects.create(tag="CE-10009", device_model=pump.device_model, department=pump.department)
    facts(other, r52, release_since=today - 30 * DAY, checked_on=today - 30 * DAY)
    wo2, row2 = update_wo(other, r56, assigned_to=techs["tom"], opened_on=today - 3 * DAY)
    wo_services.change_status(wo2, WoStatus.IN_PROGRESS, by=kim, as_of=today - 2 * DAY)
    record_facts(other, by=None, release=r56, release_since=today - 2 * DAY, checked_on=today - 2 * DAY)
    complete(wo2, tech, choice(INSTALLED, r56, basis=MR), today=today - DAY)
    row2.refresh_from_db()
    assert (row2.release_before, row2.release_after, row2.applied_on, row2.later_kept) == (None, r56, today - DAY, False)


def test_c1_installed_is_never_refused_for_the_release_the_record_names(pump, r56, vendor_job, tech, today):
    """The record says 5.6 since before the vendor's day: installed 5.6 is what the person states, accepted at Confirm version and by hand
    (another day), naming no before."""
    _wo, row = vendor_job
    facts(pump, r56, release_since=today - 10 * DAY, checked_on=today - 10 * DAY)
    confirm(row, choice(INSTALLED, r56, basis=MR), by=tech)
    row.refresh_from_db()
    assert (row.release_before, row.later_kept) == (None, False)
    hand = record(pump, choice(INSTALLED, r56, basis=MR), installed_by=InstalledBy.IT, applied_on=today - 4 * DAY, by=tech)
    assert (hand.release_before, hand.release_after) == (None, r56)
    assert words(hand) == f"5.6 (demo) installed {day(today - 4 * DAY)}; installed by information technology"


def test_a_same_day_record_of_another_release_comes_before(pump, r52, r56, on52, job, tech, today):
    """Installed 5.6 today at a completion, then rolled back to 5.2 the same day by hand: it ran 5.6 before the rollback."""
    wo, _row = job
    complete(wo, tech, choice(INSTALLED, r56, basis=MR))
    row = record(pump, choice(INSTALLED, r52, basis=NS), installed_by=InstalledBy.MANUFACTURER, applied_on=today, by=tech)
    assert (row.release_before, row.release_after, row.later_kept) == (r56, r52, False)


def test_c1_the_present_tense_refusals_are_gone(pump, pump_model, r52, r56, on52, vendor, tech, today):
    """The vendor's job closed five days ago; IT records the pump on 5.6 today. Already on the update is accepted (a later record is
    kept: the release sent, or the target, is the version that day), never refused with "Its record says it runs 5.2". Where the facts
    will be written and the record says 5.2, Confirm's refusal never tells the user to keep the completed work order open."""
    wo, row = update_wo(pump, r56, vendor_service=True, vendor_name="BD field service", opened_on=today - 10 * DAY)
    complete(wo, vendor, resolution="Updated per our visit", checks_reference="VR-2026-77", today=today - 5 * DAY)
    other = Asset.objects.create(tag="CE-10009", device_model=pump_model, department=pump.department)
    facts(other, r52, release_since=today - 30 * DAY, checked_on=today - 30 * DAY)
    wo2, row2 = update_wo(other, r56, vendor_service=True, vendor_name="BD field service", opened_on=today - 10 * DAY)
    complete(wo2, vendor, resolution="Updated per our visit", today=today - 5 * DAY)

    record_facts_for(pump_model, [pump], by=None, release=r56, today=today)
    confirm(row, choice(ALREADY), by=tech)
    row.refresh_from_db()
    assert (row.outcome, row.release_after, row.later_kept) == (ALREADY, r56, True)
    assert words(row) == (f"Checked {day(today - 5 * DAY)}: already on 5.6 (demo); nothing installed; as the vendor's report VR-2026-77 "
                          f"states{LATER}")

    found = refused(confirm, row2, choice(ALREADY), by=tech)
    assert found == {"software_release": updates.NOT_THE_TARGET_DONE.format(version="5.2 (demo)", target="5.6 (demo)")}
    assert "keep the work order open" not in found["software_release"]


# --- C2 (round 2): a later record never names the versions --------------------------------------------------------------------------

def test_c2_a_later_record_never_names_what_it_ran_before(pump, r52, r56, r57, on52, vendor_job, tech, today):
    _wo, row = vendor_job
    record_facts(pump, by=None, release=r57)  # IT records 5.7 today, five days after the vendor's visit
    confirm(row, choice(INSTALLED, r56, basis=MR), by=tech)
    row.refresh_from_db()
    assert (row.release_before, row.release_after, row.later_kept) == (None, r56, True)  # never 5.7, recorded after
    assert words(row) == f"5.6 (demo) installed {day(today - 5 * DAY)}; as the vendor's report VR-2026-77 states{LATER}"
    assert DeviceSecurity.objects.get(asset=pump).release == r57  # the later record is kept


def test_c2_a_since_corrected_later(pump, r52, r56, r57, on52, vendor_job, vendor, tech, today):
    """Record facts set 5.7 since a day before the vendor's visit (a typo), then corrected the since to today: the corrected record is
    newer than the vendor's day, so Confirm names no before, and patches confirmed the same way take the version sent (5.2)."""
    _wo, row = vendor_job
    record_facts(pump, by=None, release=r57, release_since=today - 19 * DAY)
    record_facts(pump, by=None, release=r57, release_since=today)
    confirm(row, choice(INSTALLED, r56, basis=MR), by=tech)
    row.refresh_from_db()
    assert (row.release_before, row.later_kept) == (None, True)
    assert words(row) == f"5.6 (demo) installed {day(today - 5 * DAY)}; as the vendor's report VR-2026-77 states{LATER}"
    wo2, patches = update_wo(pump, reference="ICAR-2026-09", vendor_service=True, vendor_name="BD field service", opened_on=today - 10 * DAY)
    complete(wo2, vendor, resolution="Patched per our visit", checks_reference="VR-2026-78", today=today - 5 * DAY)
    confirm(patches, choice(PATCHES, r52, reference="ICAR-2026-09", basis=VL), by=tech)
    patches.refresh_from_db()
    assert (patches.release_before, patches.release_after, patches.later_kept) == (None, r52, True)


def test_c2_patches_confirmed_after_a_later_record_take_the_version_sent(pump, r52, r57, on52, vendor, tech, today):
    wo, row = update_wo(pump, reference="ICAR-2026-09", vendor_service=True, vendor_name="BD field service", opened_on=today - 10 * DAY)
    complete(wo, vendor, resolution="Patched per our visit", checks_reference="VR-2026-78", today=today - 5 * DAY)
    record_facts(pump, by=None, release=r57)
    confirm(row, choice(PATCHES, r52, reference="ICAR-2026-09", basis=VL), by=tech)  # the version it ran that day, as the person states
    row.refresh_from_db()
    assert (row.release_before, row.release_after, row.later_kept) == (None, r52, True)
    assert words(row) == (f"Patches on the manufacturer's validated patch list applied {day(today - 5 * DAY)}, version unchanged "
                          f"(5.2 (demo)); ref ICAR-2026-09; as the vendor's report VR-2026-78 states{LATER}")


def test_c2_a_hand_record_in_the_past_names_no_versions_of_a_later_record(pump, r52, r56, r57, on52, tech, today):
    record_facts(pump, by=None, release=r57, release_since=today - 8 * DAY, checked_on=today - 8 * DAY)
    row = record(pump, choice(INSTALLED, r56, basis=NS), installed_by=InstalledBy.AUTOMATIC, applied_on=today - 10 * DAY, by=tech)
    assert (row.release_before, row.later_kept) == (None, True)
    assert words(row) == f"5.6 (demo) installed {day(today - 10 * DAY)}; installed automatically{LATER}"
    patched = record(pump, choice(PATCHES, reference="ICAR-2026-07", basis=NS), installed_by=InstalledBy.CE, applied_on=today - 12 * DAY,
                     by=tech)
    assert (patched.release_before, patched.release_after, patched.later_kept) == (None, None, True)
    # no version sent and a later record kept: the version part is left out, never "version not recorded"
    assert words(patched) == (f"Patches applied {day(today - 12 * DAY)}, version unchanged; ref ICAR-2026-07; installed by clinical "
                              f"engineering{LATER}")


def test_c2_a_record_with_no_day_names_what_it_ran_before(pump, r52, r56, tech, today):
    """A record with no since and no day checked is not newer than any day: it is what the device ran before."""
    facts(pump, r52)
    row = record(pump, choice(INSTALLED, r56, basis=NS), installed_by=InstalledBy.IT, applied_on=today - 10 * DAY, by=tech)
    assert (row.release_before, row.later_kept) == (r52, False)
    assert words(row) == f"5.6 (demo) installed {day(today - 10 * DAY)} (recorded before: 5.2 (demo)); installed by information technology"


# --- round 2: one install is recorded once by hand ------------------------------------------------------------------------------------

def test_a_duplicate_hand_record_is_refused(pump, r52, r56, on52, tech, manager, today):
    first = record(pump, choice(INSTALLED, r56, basis=MR), installed_by=InstalledBy.IT, applied_on=today, by=tech)
    duplicate = {"software_outcome": updates.DUPLICATE.format(words=words(first))}
    versions = DeviceSecurity.history.filter(id=on52.pk).count()
    assert refused(record, pump, choice(INSTALLED, r56, basis=NS), installed_by=InstalledBy.CE, applied_on=today, by=tech) == duplicate
    assert SoftwareUpdate.objects.count() == 1 and DeviceSecurity.history.filter(id=on52.pk).count() == versions  # nothing written
    patched = record(pump, choice(PATCHES, reference="ICAR-2026-07", basis=NS), installed_by=InstalledBy.IT, applied_on=today, by=tech)
    assert refused(record, pump, choice(PATCHES, reference="icar-2026-07", basis=VL), installed_by=InstalledBy.CE, applied_on=today,
                   by=tech) == {"software_outcome": updates.DUPLICATE.format(words=words(patched))}
    record(pump, choice(PATCHES, reference="ICAR-2026-08", basis=NS), installed_by=InstalledBy.IT, applied_on=today, by=tech)  # another
    remove(first, by=manager)
    again = record(pump, choice(INSTALLED, r56, basis=MR), installed_by=InstalledBy.IT, applied_on=today, by=tech)  # entered in error
    assert again.pk != first.pk


def test_a_completion_then_a_hand_record_of_the_same_install_is_refused(pump, r52, r56, on52, job, tech, today):
    wo, row = job
    complete(wo, tech, choice(INSTALLED, r56, basis=MR))
    assert refused(record, pump, choice(INSTALLED, r56, basis=MR), installed_by=InstalledBy.IT, applied_on=today, by=tech) == {
        "software_outcome": updates.DUPLICATE.format(words=f"5.6 (demo) installed {day(today)} (recorded before: 5.2 (demo))")}
    assert SoftwareUpdate.objects.count() == 1


def test_a_work_orders_outcome_is_never_refused_for_a_hand_rows(pump, r52, r56, on52, job, vendor_job, tech, today):
    """A hand record first, then the work order's completion (and a vendor's job confirmed) naming the same install: accepted."""
    wo, row = job
    record(pump, choice(INSTALLED, r56, basis=MR), installed_by=InstalledBy.IT, applied_on=today, by=tech)
    complete(wo, tech, choice(INSTALLED, r56, basis=MR))
    row.refresh_from_db()
    assert (row.outcome, row.release_before) == (INSTALLED, None)  # the record named 5.6 already
    _vwo, vrow = vendor_job
    record(pump, choice(INSTALLED, r56, basis=MR), installed_by=InstalledBy.IT, applied_on=today - 5 * DAY, by=tech)
    confirm(vrow, choice(INSTALLED, r56, basis=MR), by=tech)
    assert SoftwareUpdate.objects.filter(outcome=INSTALLED).count() == 4


def test_no_history_read_and_fixed_queries(pump, r52, r56, on52, vendor_job, tech, today):
    """Nothing reads the device's history any more; Record update's queries do not grow with the device's rows."""
    _wo, row = vendor_job

    def reads(fn, *args, **kwargs) -> tuple:
        with CaptureQueriesContext(connection) as seen:
            fn(*args, **kwargs)
        history = sum(1 for q in seen.captured_queries
                      if q["sql"].lstrip().upper().startswith("SELECT") and "historicaldevicesecurity" in q["sql"].lower())
        return history, len(seen.captured_queries)

    assert reads(confirm, row, choice(INSTALLED, r56, basis=MR), by=tech)[0] == 0
    one = reads(record, pump, choice(PATCHES, reference="ICAR-2026-01", basis=NS), installed_by=InstalledBy.CE, applied_on=today, by=tech)
    for n in range(2, 5):
        record(pump, choice(PATCHES, reference=f"ICAR-2026-0{n}", basis=NS), installed_by=InstalledBy.CE, applied_on=today, by=tech)
    more = reads(record, pump, choice(PATCHES, reference="ICAR-2026-09", basis=NS), installed_by=InstalledBy.CE, applied_on=today, by=tech)
    assert one[0] == more[0] == 0 and one[1] == more[1]


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
