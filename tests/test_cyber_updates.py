"""
Slice 38 wave 1, software updates (apps.cybersecurity.updates and the rules' slice 38 readers), on SQLite and PostgreSQL:

- What a completion records (complete_work_order's `software`, updates.check_choice and record_at_completion): every refusal, keyed and
  with the completion's other problems at once; the "required" rule (a user who may record must choose; the system and a user with no
  offer need not); a scoped vendor's completion records nothing; installed, patches, and already, with the device's facts and the
  default resolution (never a version); not confirmed; a re-completion; history reasons (never a reference).
- Confirm version (the vendor's job; the facts never move backwards: later_kept), Record update (an install with no work order: its
  day's bounds and its installer), Entered in error (both kinds: Confirm offered again), and remove_release's refusal while an open
  update is to install the release.
- Opening one update and a release's batch: every refusal and skip, the cap, due at each device's next PM, the assignment by level and
  by who installs updates here, the fixed problem (no version), source manual, the record's target, a lock conflict answered in words.
- The rules: release_offer, update_state, update_words (each sentence, never a never word), updates_of and campaigns in one query each.
- The lock spy: every writer takes the register lock before anything else, then the contract's order.
"""
import re
from datetime import timedelta
from types import SimpleNamespace

import pytest
from cyber_fixtures import facts, release, update_wo
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import OperationalError
from django.utils import timezone

from apps.accounts.models import Level, Module, Role, User
from apps.cybersecurity import rules, services, updates
from apps.cybersecurity.models import (
    OUTCOME_FIELDS,
    DeviceSecurity,
    InstalledBy,
    SoftwareRelease,
    SoftwareUpdate,
    UpdateBasis,
    UpdateOutcome,
    UpdatesBy,
    UpdateTarget,
)
from apps.cybersecurity.services import KEEP, record_facts, remove_release
from apps.cybersecurity.updates import (
    NOT_CONFIRMED,
    UpdateChoice,
    confirm,
    open_update,
    open_updates,
    record,
    remove,
)
from apps.equipment import services as eq
from apps.equipment.models import Asset, AssetStatus, Department, DeviceModel, Ownership
from apps.tenants.context import tenant_context
from apps.workorders import legacy
from apps.workorders import services as wo_services
from apps.workorders.completion import complete_work_order
from apps.workorders.models import Priority, Source, WorkOrder, WorkOrderStatusHistory, WoStatus, WoType

DAY = timedelta(days=1)
MR, VL, NS = UpdateBasis.MANUFACTURER_RELEASE, UpdateBasis.VALIDATED_LIST, UpdateBasis.NOT_STATED
INSTALLED, PATCHES, ALREADY = UpdateOutcome.INSTALLED, UpdateOutcome.PATCHES, UpdateOutcome.ALREADY
SAY = "Say what was found and done."  # the completion's own refusal of a missing resolution
WORDS_SEEN: list = []  # every sentence the rules gave in these tests, for the never-words scan


def never(text: str) -> list:
    """rules.NEVER_WORDS found in `text`, as whole words in any letter case (none may be)."""
    return [w for w in rules.NEVER_WORDS if re.search(rf"(?<![A-Za-z]){re.escape(w)}(?![A-Za-z])", text, re.IGNORECASE)]


def words(row) -> str:
    """update_words of the row as stored now (its work order and releases read again), kept for the never-words scan."""
    fresh = SoftwareUpdate.objects.select_related("work_order", "target_release", "release_before", "release_after").get(pk=row.pk)
    text = rules.update_words(fresh)
    WORDS_SEEN.append(text)
    return text


def state(row) -> str:
    return rules.update_state(SoftwareUpdate.objects.select_related("work_order").get(pk=row.pk))


def refused(fn, *args, **kwargs) -> dict:
    with pytest.raises(ValidationError) as e:
        fn(*args, **kwargs)
    return {k: v[0] if len(v) == 1 else v for k, v in e.value.message_dict.items()}


def reasons(obj) -> list:
    return list(type(obj).history.filter(id=obj.pk).order_by("history_date", "history_id").values_list("history_change_reason", flat=True))


def outcomes(obj) -> list:
    return list(type(obj).history.filter(id=obj.pk).order_by("history_date", "history_id").values_list("outcome", flat=True))


def choice(outcome, release=None, *, reference="", basis=""):
    return UpdateChoice(outcome=outcome, release_id=str(release.pk) if isinstance(release, SoftwareRelease) else release,
                        reference=reference, basis=basis)


def complete(wo, by, software=KEEP, resolution="", *, today=None, **kwargs):
    return complete_work_order(wo, resolution=resolution, software=software, by=by, today=today or timezone.localdate(), **kwargs)


def custom_user(tenant, slug, levels, **fields):
    role = Role.objects.create(name=slug.title(), slug=slug)
    role.set_levels(levels)
    return User.objects.create_user(username=f"{slug}@riverside.example", password="Test-Pass-2026-x", tenant=tenant, role=role,
                                    first_name=slug.title(), last_name="User", **fields)


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
def on52(pump, r52, today):
    """The pump runs 5.2 (demo), recorded a month ago."""
    return facts(pump, r52, release_since=today - 30 * DAY, checked_on=today - 30 * DAY)


@pytest.fixture
def job(pump, r56, techs):
    """An update work order to install 5.6 (demo) on the pump, assigned to Tom: (work order, its record)."""
    return update_wo(pump, r56, assigned_to=techs["tom"])


# --- the completion's choice: refusals --------------------------------------------------------------------------------------------

def test_a_choice_on_another_type_is_refused_with_the_completions_other_problems(pump, r56, tech, techs):
    wo = wo_services.create_work_order(asset=pump, type=WoType.SAFETY, priority=Priority.NORMAL, problem="Electrical safety check",
                                       assigned_to=techs["tom"])
    wo_services.change_status(wo, WoStatus.IN_PROGRESS)  # a safety check is started on its own
    assert refused(complete, wo, tech, choice(INSTALLED, r56, basis=MR)) == {"software_outcome": updates.ONLY_UPDATES, "resolution": SAY}
    complete(wo, tech, resolution="Checked")  # KEEP on another type: no query of the register, never refused
    assert not SoftwareUpdate.objects.exists()


def test_an_imported_update_records_nothing(pump, r56, tech, techs, today):
    wo = legacy.import_work_order(asset=pump, legacy_number="sw-9", type=WoType.SOFTWARE_UPDATE, status=WoStatus.COMPLETED,
                                  opened_on=today - 20 * DAY, completed_on=today - 19 * DAY, today=today).work_order
    wo_services.change_status(wo, WoStatus.IN_PROGRESS)
    wo_services.assign(wo, technician=techs["tom"])
    assert wo.source == Source.IMPORTED and rules.release_offer(wo, tech) is None
    assert rules.offer_or_why(wo, None) == (None, rules.NO_RECORD)
    assert refused(complete, wo, tech, choice(INSTALLED, r56, basis=MR), "Done") == {"software_outcome": updates.NOT_FROM_REGISTER}
    complete(wo, tech, resolution="Done")  # KEEP: nothing is needed where nothing may be recorded
    assert not SoftwareUpdate.objects.exists() and not DeviceSecurity.objects.exists()


def test_a_device_not_on_site_is_refused_in_the_registers_words(pump, r56, job, tech):
    wo, _row = job
    eq.set_status(pump, AssetStatus.RETIRED)  # cancels the open update
    wo.refresh_from_db()
    assert wo.status == WoStatus.CANCELLED
    wo_services.change_status(wo, WoStatus.OPEN)
    assert rules.offer_or_why(wo, tech) == (None, rules.NOT_ON_SITE)
    assert refused(complete, wo, tech, choice(INSTALLED, r56, basis=MR), "Done") == {
        "software_outcome": "CE-10002 is retired: its facts stay as they were."}
    complete(wo, tech, resolution="Done")
    assert SoftwareUpdate.objects.get().outcome == ""


def test_a_user_who_may_not_record_is_refused_never_dropped(tenant, pump, r56, job):
    wo, row = job
    no_register = custom_user(tenant, "wo-edit", {Module.WORKORDERS: Level.EDIT, Module.EQUIPMENT: Level.VIEW})
    assert rules.offer_or_why(wo, no_register) == (None, rules.NO_PERMISSION)
    assert refused(complete, wo, no_register, choice(INSTALLED, r56, basis=MR), "Done") == {"software_outcome": updates.RECORD_PERMISSION}
    assert refused(complete, wo, no_register, choice(NOT_CONFIRMED), "Done") == {"software_outcome": updates.RECORD_PERMISSION}
    complete(wo, no_register, resolution="Done")  # KEEP from a user with no offer: completed, nothing recorded, never refused
    row.refresh_from_db()
    assert row.outcome == "" and state(row) == rules.NOT_CONFIRMED


def test_a_scoped_vendor_completes_with_keep_and_records_nothing(pump, r56, vendor, today):
    wo, row = update_wo(pump, r56, vendor_service=True, vendor_name="BD field service")
    assert rules.release_offer(wo, vendor) is None
    assert refused(complete, wo, vendor, choice(INSTALLED, r56, basis=MR), "Updated") == {"software_outcome": updates.RECORD_PERMISSION}
    complete(wo, vendor, resolution="Updated per our visit")
    row.refresh_from_db()
    assert row.outcome == "" and not DeviceSecurity.objects.exists()
    assert words(row) == f"Completed by the vendor {rules.day_words(today)}; version not confirmed"


def test_a_user_who_may_record_must_choose_the_system_need_not(pump, r56, job, tech, today):
    wo, row = job
    offer = rules.release_offer(wo, tech)
    assert offer.needed and offer.update == row and offer.target_release == r56 and offer.outcomes == (INSTALLED, PATCHES, ALREADY)
    assert refused(complete, wo, tech, KEEP) == {"software_outcome": updates.CHOOSE_OUTCOME, "resolution": SAY}  # every problem at once
    complete(wo, None, resolution="Done by the job")  # the system (the seed, a job) need not choose
    row.refresh_from_db()
    assert row.outcome == "" and wo.status == WoStatus.COMPLETED


VALIDATOR = "the reference validator's words"  # replaced by models.UPDATE_REFERENCE_VALIDATOR's message in the test

# (case, choice builder, expected refusals) with the pump on 5.2 (demo) recorded, the work order to install 5.6 (demo)
OUTCOME_CASES = [
    ("installed with no release or basis", lambda r: choice(INSTALLED),
     {"software_release": updates.CHOOSE_RUNS_NOW, "software_basis": updates.BASIS_WORDS}),
    ("another model's release", lambda r: choice(INSTALLED, r["vent"], basis=MR), {"software_release": updates.CHOOSE_RUNS_NOW}),
    ("a release entered in error", lambda r: choice(INSTALLED, r["removed"], basis=MR), {"software_release": updates.CHOOSE_RUNS_NOW}),
    ("the profile", lambda r: choice(INSTALLED, r["profile"], basis=MR), {"software_release": updates.CHOOSE_RUNS_NOW}),
    ("not an id", lambda r: choice(INSTALLED, "5.6", basis=MR), {"software_release": updates.CHOOSE_RUNS_NOW}),
    ("another facility's release", lambda r: choice(INSTALLED, r["theirs"], basis=MR), {"software_release": updates.CHOOSE_RUNS_NOW}),
    ("installed on the validated patch list", lambda r: choice(INSTALLED, r["56"], basis=VL),
     {"software_basis": updates.VALIDATED_PATCHES_ONLY}),
    ("a basis not on the list", lambda r: choice(INSTALLED, r["56"], basis="authorized"), {"software_basis": updates.BASIS_WORDS}),
    ("patches with no reference", lambda r: choice(PATCHES, basis=VL), {"software_reference": updates.REFERENCE_NEEDED}),
    ("patches with a spaced reference", lambda r: choice(PATCHES, reference="ICAR 2026", basis=VL), {"software_reference": VALIDATOR}),
    ("patches with a long reference", lambda r: choice(PATCHES, reference="X" * 41, basis=VL),
     {"software_reference": updates.REFERENCE_LENGTH}),
    ("patches that changed the version", lambda r: choice(PATCHES, r["56"], reference="ICAR-2026-09", basis=VL),
     {"software_release": updates.PATCHES_UNCHANGED}),
    ("patches with no basis", lambda r: choice(PATCHES, reference="ICAR-2026-09"), {"software_basis": updates.BASIS_WORDS}),
    ("already on another release than recorded", lambda r: choice(ALREADY, r["56"]),
     {"software_release": updates.RECORDED_AS.format(version="5.2 (demo)")}),
    ("already, but not on the target", lambda r: choice(ALREADY),
     {"software_release": updates.NOT_THE_TARGET.format(version="5.2 (demo)", target="5.6 (demo)")}),
    ("already with a basis", lambda r: choice(ALREADY, basis=MR),
     {"software_basis": updates.NO_BASIS, "software_release": updates.NOT_THE_TARGET.format(version="5.2 (demo)", target="5.6 (demo)")}),
    ("an outcome that is not one", lambda r: UpdateChoice(outcome="updated"), {"software_outcome": updates.OUTCOME_WORDS}),
]


@pytest.fixture
def every_release(pump_model, vent_model, r52, r56, other_tenant, ctx):
    removed = release(pump_model, "5.4 (demo)")
    remove_release(removed, by=None)
    with tenant_context(other_tenant):
        dept = Department.objects.create(name="Their ICU")
        model = DeviceModel.objects.create(manufacturer="BD", model="Alaris 8015 PCU", description="Pump", category="Infusion pumps")
        theirs = release(model, "5.6 (demo)")
        Asset.objects.create(tag="THEIRS-1", device_model=model, department=dept)
    return {"52": r52, "56": r56, "vent": release(vent_model, "3.1"), "removed": removed, "profile": release(pump_model, ""),
            "theirs": theirs}


@pytest.mark.parametrize("case,build,expected", OUTCOME_CASES, ids=[c[0] for c in OUTCOME_CASES])
def test_each_outcome_is_checked_keyed_by_field(on52, job, tech, every_release, case, build, expected):
    wo, row = job
    if expected.get("software_reference") == VALIDATOR:
        expected = {"software_reference": updates.UPDATE_REFERENCE_VALIDATOR.message}
    assert refused(complete, wo, tech, build(every_release), "Done") == expected
    wo.refresh_from_db()
    row.refresh_from_db()
    assert wo.status == WoStatus.OPEN and row.outcome == "" and reasons(DeviceSecurity.objects.get())[-1] is None  # nothing written


def test_with_no_release_recorded_patches_name_none_and_already_needs_one(pump, r52, job, tech):
    wo, _row = job
    assert refused(complete, wo, tech, choice(PATCHES, r52, reference="ICAR-2026-09", basis=VL), "Done") == {
        "software_release": updates.PATCHES_NONE_RECORDED}
    assert refused(complete, wo, tech, choice(ALREADY), "Done") == {"software_release": updates.CHOOSE_RUNS_NOW}
    assert refused(complete, wo, tech, choice(ALREADY, r52), "Done") == {
        "software_release": updates.NOT_THE_TARGET.format(version="5.2 (demo)", target="5.6 (demo)")}


# --- the completion's choice: what it records ---------------------------------------------------------------------------------------

def test_installed_records_the_release_since_today_with_the_default_resolution(pump, r52, r56, on52, job, tech, today):
    wo, row = job
    done = complete(wo, tech, choice(INSTALLED, r56, reference="FCO-2026-11", basis=MR))
    row.refresh_from_db()
    assert done.software == row
    assert (row.outcome, row.release_before, row.release_after, row.reference, row.basis, row.applied_on, row.recorded_by,
            row.later_kept) == (INSTALLED, r52, r56, "FCO-2026-11", MR, today, tech, False)
    sec = DeviceSecurity.objects.get(asset=pump)
    assert (sec.release, sec.release_since, sec.checked_on, sec.checked_by) == (r56, today, today, tech)
    assert wo.status == WoStatus.COMPLETED and wo.resolution == updates.DEFAULT_RESOLUTION[INSTALLED]
    notes = " ".join(WorkOrderStatusHistory.objects.filter(work_order=wo).values_list("note", flat=True))
    assert "5.6" not in wo.resolution + notes and "5.2" not in wo.resolution + notes and "FCO" not in notes  # never a version
    assert reasons(row)[-1] == f"Recorded at completion of {wo.number}"
    assert reasons(sec)[-1] == f"Facts recorded at {wo.number}: release set to 5.6 (demo)"
    assert words(row) == f"5.6 (demo) installed {rules.day_words(today)} (recorded before: 5.2 (demo)); ref FCO-2026-11"
    assert rules.release_offer(wo, tech) is not None and not rules.release_offer(wo, tech).needed


def test_installed_on_a_device_with_none_recorded_names_no_before(pump, r56, job, tech, today):
    wo, row = job
    complete(wo, tech, choice(INSTALLED, r56, basis=NS), "Flashed the main board")
    row.refresh_from_db()
    assert row.release_before is None and row.before_taken and wo.resolution == "Flashed the main board"  # a typed resolution stays
    assert words(row) == f"5.6 (demo) installed {rules.day_words(today)}"  # review round 2: never "(recorded before: not recorded)"
    assert DeviceSecurity.objects.get().release == r56


def test_patches_keep_the_version_and_record_the_day_checked(pump, r52, on52, techs, tech, today):
    wo, row = update_wo(pump, reference="ICAR-2026-09", assigned_to=techs["tom"])
    complete(wo, tech, choice(PATCHES, reference="ICAR-2026-09", basis=VL))
    row.refresh_from_db()
    assert (row.outcome, row.release_before, row.release_after, row.reference) == (PATCHES, r52, r52, "ICAR-2026-09")
    sec = DeviceSecurity.objects.get()
    assert (sec.release, sec.release_since, sec.checked_on, sec.checked_by) == (r52, today - 30 * DAY, today, tech)
    assert wo.resolution == updates.DEFAULT_RESOLUTION[PATCHES]
    assert reasons(sec)[-1] == f"Facts recorded at {wo.number}" and reasons(row)[-1] == f"Recorded at completion of {wo.number}"
    assert not any("ICAR" in (r or "") for r in reasons(sec) + reasons(row))  # never a reference in a history reason
    assert words(row) == (f"Patches on the manufacturer's validated patch list applied {rules.day_words(today)}, version unchanged "
                          "(5.2 (demo)); ref ICAR-2026-09")


def test_patches_on_a_device_with_none_recorded(pump, techs, tech, today):
    wo, row = update_wo(pump, reference="ICAR-2026-09", assigned_to=techs["tom"])
    complete(wo, tech, choice(PATCHES, reference="ICAR-2026-09", basis=MR))
    row.refresh_from_db()
    sec = DeviceSecurity.objects.get()
    assert row.release_after is None and (sec.release, sec.checked_on) == (None, today)
    assert words(row) == f"Patches applied {rules.day_words(today)}, version unchanged (version not recorded); ref ICAR-2026-09"


def test_already_keeps_the_release_and_names_one_when_none_was_recorded(pump, r52, r56, techs, tech, today):
    facts(pump, r56, release_since=today - 9 * DAY, checked_on=today - 9 * DAY)
    wo, row = update_wo(pump, r56, assigned_to=techs["tom"])
    complete(wo, tech, choice(ALREADY))
    row.refresh_from_db()
    sec = DeviceSecurity.objects.get()
    assert (row.outcome, row.release_before, row.release_after, row.basis) == (ALREADY, r56, r56, "")
    assert (sec.release, sec.release_since, sec.checked_on) == (r56, today - 9 * DAY, today)
    assert wo.resolution == updates.DEFAULT_RESOLUTION[ALREADY]
    assert words(row) == f"Checked {rules.day_words(today)}: already on 5.6 (demo); nothing installed"
    DeviceSecurity.objects.all().delete()
    other, _row2 = update_wo(pump, reference="ICAR-2026-10", assigned_to=techs["tom"])
    complete(other, tech, choice(ALREADY, r52))  # patches already applied; no release was recorded: the one chosen is
    sec = DeviceSecurity.objects.get()
    assert (sec.release, sec.release_since) == (r52, today) and reasons(sec)[-1] == f"Facts recorded at {other.number}: release set to 5.2 (demo)"


def test_not_confirmed_records_nothing_and_needs_a_resolution(pump, on52, job, tech, today):
    wo, row = job
    assert refused(complete, wo, tech, choice(NOT_CONFIRMED)) == {"resolution": SAY}
    complete(wo, tech, choice(NOT_CONFIRMED), "Update done; version to be checked")
    row.refresh_from_db()
    assert row.outcome == "" and row.applied_on is None and wo.resolution == "Update done; version to be checked"
    assert DeviceSecurity.objects.get().checked_on == today - 30 * DAY  # facts untouched
    assert state(row) == rules.NOT_CONFIRMED and rules.release_offer(wo, tech).needed
    assert words(row) == f"Completed {rules.day_words(today)}; version not confirmed"


def test_a_re_completion_keeps_or_replaces_the_outcome_with_history(pump, r52, r56, on52, job, tech, kim):
    wo, row = job
    complete(wo, tech, choice(INSTALLED, r56, basis=MR))
    wo_services.change_status(wo, WoStatus.IN_PROGRESS, by=kim)
    assert state(row) == rules.OPEN and not rules.release_offer(wo, tech).needed
    complete(wo, tech, KEEP, "Reseated the cable")  # KEEP keeps what was recorded
    wo_services.change_status(wo, WoStatus.IN_PROGRESS, by=kim)
    complete(wo, tech, choice(NOT_CONFIRMED), "Rechecked")  # so does not confirmed now
    row.refresh_from_db()
    assert row.outcome == INSTALLED
    wo_services.change_status(wo, WoStatus.IN_PROGRESS, by=kim)
    complete(wo, tech, choice(ALREADY), "Checked again")  # now on 5.6 (demo), the target
    row.refresh_from_db()
    assert (row.outcome, row.release_before, row.release_after) == (ALREADY, r52, r56)  # what it ran before: taken once (round 2)
    assert outcomes(row) == ["", INSTALLED, ALREADY]
    assert reasons(row) == [None, f"Recorded at completion of {wo.number}", f"Recorded at completion of {wo.number}"]


# --- Confirm version ------------------------------------------------------------------------------------------------------------------

@pytest.fixture
def vendor_job(pump, r56, vendor, today):
    """5.6 (demo) installed by the vendor three days ago, completed by their account (nothing recorded), with their report's number."""
    wo, row = update_wo(pump, r56, vendor_service=True, vendor_name="BD field service", opened_on=today - 10 * DAY)
    complete(wo, vendor, resolution="Updated per our visit", checks_reference="VR-2026-77", today=today - 3 * DAY)
    return wo, row


def test_confirm_records_the_vendors_job_on_its_completed_day(pump, r52, r56, on52, vendor_job, tech, today):
    wo, row = vendor_job
    assert words(row) == f"Completed by the vendor {rules.day_words(today - 3 * DAY)}; version not confirmed"
    done = confirm(row, choice(INSTALLED, r56, basis=MR), by=tech)
    row.refresh_from_db()
    assert done.pk == row.pk and (row.outcome, row.release_before, row.release_after, row.applied_on, row.recorded_by,
                                  row.later_kept) == (INSTALLED, r52, r56, today - 3 * DAY, tech, False)
    sec = DeviceSecurity.objects.get()
    assert (sec.release, sec.release_since, sec.checked_on, sec.checked_by) == (r56, today - 3 * DAY, today - 3 * DAY, tech)
    assert reasons(row)[-1] == "Confirmed"
    assert reasons(sec)[-1] == f"Facts recorded when {wo.number} was confirmed: release set to 5.6 (demo)"
    assert words(row) == (f"5.6 (demo) installed {rules.day_words(today - 3 * DAY)} (recorded before: 5.2 (demo)); as the vendor's "
                          "report VR-2026-77 states")
    assert refused(confirm, row, choice(INSTALLED, r56, basis=MR), by=tech) == {"software_outcome": updates.RECORDED_MEANWHILE}


def test_confirm_never_moves_the_facts_backwards(pump, r52, r56, vendor_job, tech, today):
    wo, row = vendor_job
    sec = facts(pump, r52, release_since=today - 30 * DAY, checked_on=today - DAY)  # checked after the vendor's visit
    history = len(reasons(sec))
    confirm(row, choice(INSTALLED, r56, basis=MR), by=tech)
    row.refresh_from_db()
    sec.refresh_from_db()
    assert row.later_kept and (row.outcome, row.release_before, row.release_after) == (INSTALLED, None, r56)  # before not known
    assert (sec.release, sec.checked_on) == (r52, today - DAY) and len(reasons(sec)) == history  # kept, not written
    assert words(row).endswith("; a later record of the device is kept")


def test_confirm_refuses_in_words(pump, r56, tenant, vendor_job, tech, kim, today):
    wo, row = vendor_job
    assert refused(confirm, row, KEEP, by=tech) == {"software_outcome": updates.CONFIRM_OUTCOME}
    assert refused(confirm, row, choice(NOT_CONFIRMED), by=tech) == {"software_outcome": updates.CONFIRM_OUTCOME}
    assert refused(confirm, row, choice(PATCHES, basis="authorized"), by=tech) == {
        "software_reference": updates.REFERENCE_NEEDED, "software_basis": updates.BASIS_WORDS}
    no_register = custom_user(tenant, "wo-edit", {Module.WORKORDERS: Level.EDIT, Module.EQUIPMENT: Level.VIEW})
    with pytest.raises(PermissionDenied, match=updates.RECORD_PERMISSION):
        confirm(row, choice(INSTALLED, r56, basis=MR), by=no_register)
    wo_services.change_status(wo, WoStatus.IN_PROGRESS, by=kim)
    assert refused(confirm, row, choice(INSTALLED, r56, basis=MR), by=tech) == {
        "software_outcome": updates.REOPENED.format(number=wo.number)}
    hand = record(pump, choice(PATCHES, reference="ICAR-2026-09", basis=MR), installed_by=InstalledBy.IT, applied_on=today, by=tech)
    assert refused(confirm, hand, choice(INSTALLED, r56, basis=MR), by=tech) == {"software_outcome": updates.CONFIRM_WHAT}
    assert refused(confirm, "not-an-id", choice(INSTALLED, r56, basis=MR), by=tech) == {"update": updates.CHOOSE_UPDATE}


def test_confirm_on_a_retired_device_is_refused(pump, r56, vendor_job, tech):
    _wo, row = vendor_job
    eq.set_status(pump, AssetStatus.RETIRED)
    assert refused(confirm, row, choice(INSTALLED, r56, basis=MR), by=tech) == {
        "software_outcome": "CE-10002 is retired: its facts stay as they were."}


# --- Record update (by hand) ----------------------------------------------------------------------------------------------------------

def test_record_an_install_by_hand(pump, r52, r56, on52, tech, today):
    row = record(pump, choice(INSTALLED, r56, basis=MR), installed_by=InstalledBy.IT, applied_on=str(today - DAY), by=tech)
    assert (row.work_order, row.outcome, row.release_before, row.release_after, row.installed_by, row.applied_on, row.later_kept,
            row.target) == (None, INSTALLED, r52, r56, InstalledBy.IT, today - DAY, False, "")
    sec = DeviceSecurity.objects.get()
    assert (sec.release, sec.release_since, sec.checked_on, sec.checked_by) == (r56, today - DAY, today - DAY, tech)
    assert reasons(row) == ["Recorded"] and reasons(sec)[-1] == "Facts recorded with an update recorded by hand: release set to 5.6 (demo)"
    assert words(row) == (f"5.6 (demo) installed {rules.day_words(today - DAY)} (recorded before: 5.2 (demo)); installed by information "
                          "technology")
    assert refused(record, pump, choice(INSTALLED, r56, basis=MR), installed_by=InstalledBy.CE, applied_on=today - DAY, by=tech) == {
        "software_outcome": updates.DUPLICATE.format(words=words(row))}  # the same install again (review round 2)


def test_record_patches_by_the_manufacturer_remotely(pump, r52, on52, tech, today):
    row = record(pump, choice(PATCHES, reference="DEMO-PATCH-2026-09", basis=VL), installed_by=InstalledBy.MANUFACTURER,
                 applied_on=today, by=tech)
    assert (row.release_before, row.release_after, row.reference) == (r52, r52, "DEMO-PATCH-2026-09")
    assert words(row) == (f"Patches on the manufacturer's validated patch list applied {rules.day_words(today)}, version unchanged "
                          "(5.2 (demo)); ref DEMO-PATCH-2026-09; installed by the manufacturer remotely")
    assert rules.update_state(row) == rules.DONE


@pytest.mark.parametrize("applied_on,installed_by,expected", [
    ("tomorrow", InstalledBy.IT, {"applied_on": "Installed on cannot be after today."}),
    ("before install", InstalledBy.IT, {"applied_on": "before install"}),
    (None, InstalledBy.IT, {"applied_on": updates.APPLIED_NEEDED}),
    ("26-10-2026", InstalledBy.IT, {"applied_on": "Installed on: enter a day."}),
    ("today", "vendor", {"installed_by": updates.INSTALLER_WORDS}),
    ("today", "", {"installed_by": updates.INSTALLER_WORDS}),
])
def test_record_checks_the_day_and_the_installer(pump, r56, tech, today, applied_on, installed_by, expected):
    Asset.objects.filter(pk=pump.pk).update(installed_on=today - 100 * DAY)
    day = {"tomorrow": today + DAY, "before install": today - 101 * DAY, "today": today}.get(applied_on, applied_on)
    if expected.get("applied_on") == "before install":
        expected = {"applied_on": f"Installed on cannot be before the device was installed ({rules.day_words(today - 100 * DAY)})."}
    assert refused(record, pump, choice(INSTALLED, r56, basis=MR), installed_by=installed_by, applied_on=day, by=tech) == expected
    assert not SoftwareUpdate.objects.exists()


def test_record_refuses_already_and_a_retired_device(pump, r56, tech, today):
    assert refused(record, pump, choice(ALREADY, r56), installed_by=InstalledBy.CE, applied_on=today, by=tech) == {
        "software_outcome": updates.HAND_OUTCOME}
    assert refused(record, pump, KEEP, installed_by=InstalledBy.CE, applied_on=today, by=tech) == {
        "software_outcome": updates.HAND_OUTCOME}
    eq.set_status(pump, AssetStatus.RETIRED)
    assert refused(record, pump, choice(INSTALLED, r56, basis=MR), installed_by=InstalledBy.CE, applied_on=today, by=tech) == {
        "asset": "CE-10002 is retired: its facts stay as they were."}


def test_record_in_the_past_keeps_a_later_record(pump, r52, r56, tech, today):
    sec = facts(pump, r52, release_since=today - 30 * DAY, checked_on=today - DAY)
    row = record(pump, choice(INSTALLED, r56, basis=NS), installed_by=InstalledBy.AUTOMATIC, applied_on=today - 5 * DAY, by=tech)
    sec.refresh_from_db()
    assert row.later_kept and row.release_before is None and (sec.release, sec.checked_on) == (r52, today - DAY)
    assert words(row) == (f"5.6 (demo) installed {rules.day_words(today - 5 * DAY)}; installed automatically; a later record of the "
                          "device is kept")  # what it ran before is not known (review round 2): the facts are newer than the day


def test_record_needs_device_security_edit_and_equipment_view(tenant, pump, r56, today):
    viewer = custom_user(tenant, "it-view", {Module.CYBERSECURITY: Level.VIEW, Module.EQUIPMENT: Level.VIEW})
    with pytest.raises(PermissionDenied, match=updates.RECORD_PERMISSION):
        record(pump, choice(INSTALLED, r56, basis=MR), installed_by=InstalledBy.IT, applied_on=today, by=viewer)
    it = custom_user(tenant, "it-edit", {Module.CYBERSECURITY: Level.EDIT, Module.EQUIPMENT: Level.VIEW})
    assert record(pump, choice(INSTALLED, r56, basis=MR), installed_by=InstalledBy.IT, applied_on=today, by=it).recorded_by == it


# --- Entered in error -----------------------------------------------------------------------------------------------------------------

def test_a_hand_row_entered_in_error_is_hidden_and_the_facts_stay(pump, r52, r56, on52, tech, manager, today):
    row = record(pump, choice(INSTALLED, r56, basis=MR), installed_by=InstalledBy.IT, applied_on=today, by=tech)
    with pytest.raises(PermissionDenied, match=updates.CORRECT_PERMISSION):
        remove(row, by=tech)  # Device security Edit only
    removed = remove(row, by=manager)
    row.refresh_from_db()
    assert removed == row and (row.removed_on, row.removed_by, row.outcome) == (today, manager, INSTALLED)
    assert reasons(row)[-1] == "Entered in error" and rules.update_state(row) == rules.REMOVED
    assert rules.updates_of(pump) == [] and DeviceSecurity.objects.get().release == r56  # facts never rolled back
    assert refused(remove, row, by=manager) == {"software_outcome": updates.NOTHING_RECORDED}


def test_a_work_orders_outcome_entered_in_error_is_cleared_and_confirm_is_offered_again(pump, r52, r56, on52, job, tech, manager,
                                                                                          today):
    wo, row = job
    assert refused(remove, row, by=manager) == {"software_outcome": updates.NOTHING_RECORDED}  # nothing recorded yet
    complete(wo, tech, choice(INSTALLED, r56, basis=MR))
    remove(row, by=manager)
    row.refresh_from_db()
    assert all(getattr(row, f) in ("", None, False) for f in OUTCOME_FIELDS) and row.removed_on is None
    assert (row.release_before, row.before_taken) == (r52, True)  # what it ran before stays (review round 2: taken once)
    assert reasons(row)[-1] == "Outcome entered in error" and state(row) == rules.NOT_CONFIRMED
    assert rules.release_offer(wo, tech).needed and DeviceSecurity.objects.get().release == r56
    confirm(row, choice(ALREADY), by=tech)  # recorded again: already on 5.6 (demo), the target
    row.refresh_from_db()
    assert (row.outcome, row.applied_on) == (ALREADY, today)


def test_a_release_with_an_open_update_to_install_it_stays(pump, r56, job, kim):
    wo, row = job
    open_words = f"{wo.number} is open to install this release: complete or cancel it first."
    assert services.removal_refusal(r56) == open_words
    assert refused(remove_release, r56, by=kim) == {"release": open_words}
    wo_services.change_status(wo, WoStatus.IN_PROGRESS)
    wo_services.change_status(wo, WoStatus.AWAITING_PARTS)
    assert refused(remove_release, r56, by=kim) == {"release": open_words}  # open, in progress, or awaiting parts
    wo_services.change_status(wo, WoStatus.IN_PROGRESS)
    wo_services.change_status(wo, WoStatus.OPEN)
    wo_services.change_status(wo, WoStatus.CANCELLED)
    assert services.removal_refusal(r56) == ""
    remove_release(r56, by=kim)  # a cancelled update's target never stops it
    assert words(row) == "Cancelled"


def test_a_release_entered_in_error_since_reads_so(pump, r52, r56, on52, kim, today):
    hand = record(pump, choice(PATCHES, reference="ICAR-2026-10", basis=MR), installed_by=InstalledBy.CE, applied_on=today, by=None)
    record_facts(pump, by=None, release=r56)
    remove_release(r52, by=kim)  # no device runs it now: the record reads it as it was
    assert words(hand) == (f"Patches applied {rules.day_words(today)}, version unchanged (5.2 (demo) (entered in error since)); "
                           "ref ICAR-2026-10; installed by clinical engineering")


# --- opening one update ---------------------------------------------------------------------------------------------------------------

def test_open_update_makes_the_work_order_and_its_record(pump, r52, r56, on52, kim, techs, today):
    batch = open_update(pump, target=UpdateTarget.RELEASE, target_release=r56, due_on=today + 14 * DAY, priority=Priority.HIGH,
                        assign_to=techs["tom"], by=kim)
    (wo,) = batch.work_orders
    row = SoftwareUpdate.objects.get(work_order=wo)
    assert (batch.created, batch.unassigned, batch.unassigned_reason) == (1, 0, "")
    assert (wo.type, wo.problem, wo.source, wo.requester, wo.priority, wo.due_on, wo.opened_on, wo.created_by, wo.assigned_to) == (
        WoType.SOFTWARE_UPDATE, updates.UPDATE_PROBLEM, Source.MANUAL, "Director User", Priority.HIGH, today + 14 * DAY, today, kim,
        techs["tom"])
    assert "5.6" not in wo.problem and "release" not in wo.problem.casefold()
    assert (row.asset, row.target, row.target_release, row.target_reference, row.opened_on, row.opened_by, row.outcome) == (
        pump, UpdateTarget.RELEASE, r56, "", today, kim, "")
    assert reasons(row) == [f"Opened with {wo.number}"] and words(row) == "Open: to install 5.6 (demo)"


def test_open_update_for_patches_and_by_the_system(pump, today):
    batch = open_update(pump, target=UpdateTarget.PATCHES, target_reference="ICAR-2026-09", due_on=str(today), priority="normal", by=None)
    (wo,) = batch.work_orders
    row = SoftwareUpdate.objects.get()
    assert (wo.problem, wo.requester, row.target_reference, row.target_release) == (updates.PATCHES_PROBLEM, "Clinical engineering",
                                                                                    "ICAR-2026-09", None)
    assert words(row) == "Open: to apply the manufacturer's patches (ref ICAR-2026-09)"
    assert (batch.unassigned, batch.unassigned_reason) == (1, updates.NOBODY_CHOSEN)  # the system may assign; nobody was chosen


@pytest.mark.parametrize("fields,expected", [
    ({"priority": Priority.CRITICAL}, {"priority": updates.PRIORITY_WORDS}),
    ({"priority": Priority.LOW}, {"priority": updates.PRIORITY_WORDS}),
    ({"due_on": "yesterday"}, {"due_on": updates.DUE_PAST}),
    ({"due_on": None}, {"due_on": updates.DUE_NEEDED}),
    ({"due_on": "soon"}, {"due_on": updates.DUE_NEEDED}),
    ({"due_on": "far"}, {"due_on": updates.DUE_FAR}),
    ({"target": "firmware"}, {"target": updates.TARGET_WORDS}),
    ({"target_release": None}, {"target_release": updates.CHOOSE_TARGET}),
    ({"target_reference": "ICAR-2026-09"}, {"target_reference": updates.NO_TARGET_REFERENCE}),
    ({"target": UpdateTarget.PATCHES}, {"target_release": updates.NO_TARGET_RELEASE, "target_reference": updates.REFERENCE_NEEDED}),
    ({"target_release": "vent"}, {"target_release": updates.CHOOSE_TARGET}),
    ({"target_release": "removed"}, {"target_release": updates.CHOOSE_TARGET}),
    ({"assign_to": "nobody"}, {"assign_to": updates.CHOOSE_TECHNICIAN}),
])
def test_open_update_refuses_each_field_in_words(pump, r56, vent_model, kim, today, fields, expected):
    removed = release(pump.device_model, "5.4 (demo)")
    remove_release(removed, by=None)
    days = {"yesterday": today - DAY, "far": today.replace(year=today.year + 51), "soon": "soon"}
    given = {"target": UpdateTarget.RELEASE, "target_release": r56, "due_on": today + 7 * DAY, "priority": Priority.NORMAL, **fields}
    given["due_on"] = days.get(given["due_on"], given["due_on"])
    given["target_release"] = {"vent": release(vent_model, "3.1"), "removed": removed}.get(given["target_release"], given["target_release"])
    if given.get("assign_to") == "nobody":
        given["assign_to"] = "00000000-0000-0000-0000-000000000000"
    assert refused(open_update, pump, by=kim, **given) == expected
    assert not WorkOrder.objects.exists()


def test_open_update_refuses_a_device_that_cannot_take_one(pump, r52, r56, on52, kim, today):
    def attempt(release_=r56):
        return refused(open_update, pump, target=UpdateTarget.RELEASE, target_release=release_, due_on=today, priority="normal", by=kim)

    assert attempt(r52) == {"target_release": updates.ALREADY_RUNS.format(tag="CE-10002", version="5.2 (demo)")}
    first = open_update(pump, target=UpdateTarget.RELEASE, target_release=r56, due_on=today, priority="normal", by=kim).work_orders[0]
    assert attempt() == {"asset": updates.ALREADY_OPEN.format(tag="CE-10002", number=first.number)}
    wo_services.change_status(first, WoStatus.CANCELLED)
    Asset.objects.filter(pk=pump.pk).update(incident_hold=True)
    pump.refresh_from_db()
    assert attempt() == {"asset": updates.HELD.format(tag="CE-10002")}
    Asset.objects.filter(pk=pump.pk).update(incident_hold=False, status=AssetStatus.RETIRED)
    assert attempt() == {"asset": "CE-10002 is retired: its facts stay as they were."}
    assert WorkOrder.objects.count() == 1


def test_open_update_allows_a_device_waiting_for_its_incoming_inspection(pump, r56, kim, today):
    Asset.objects.filter(pk=pump.pk).update(awaiting_inspection=True, next_pm_on=None)
    assert open_update(pump, target=UpdateTarget.RELEASE, target_release=r56, due_on=today, priority="normal", by=kim).created == 1


def test_open_update_refuses_a_model_changed_meanwhile_and_rolls_everything_back(pump, r56, vent_model, kim, today, monkeypatch):
    real = wo_services.create_work_order

    def moved(**kwargs):
        wo = real(**kwargs)
        Asset.objects.filter(pk=kwargs["asset"].pk).update(device_model=vent_model)  # what the insert's lock then reads
        return wo

    monkeypatch.setattr(wo_services, "create_work_order", moved)
    assert refused(open_update, pump, target=UpdateTarget.RELEASE, target_release=r56, due_on=today, priority="normal", by=kim) == {
        "target_release": updates.MODEL_CHANGED.format(tag="CE-10002")}
    assert not WorkOrder.objects.exists() and not SoftwareUpdate.objects.exists()
    pump.refresh_from_db()
    assert pump.device_model_id != vent_model.pk  # the whole transaction went back


def test_open_update_assigns_only_at_work_orders_approve(pump, r56, tech, kim, techs, today):
    opened = open_update(pump, target=UpdateTarget.RELEASE, target_release=r56, due_on=today, priority="normal", by=tech)
    assert (opened.unassigned, opened.unassigned_reason) == (1, updates.ASSIGN_NEEDS)  # the technician default role cannot assign
    assert opened.work_orders[0].assigned_to is None and not opened.work_orders[0].vendor_service
    wo_services.change_status(opened.work_orders[0], WoStatus.CANCELLED)
    assert refused(open_update, pump, target=UpdateTarget.RELEASE, target_release=r56, due_on=today, priority="normal",
                   assign_to=techs["tom"], by=tech) == {"assign_to": updates.ASSIGN_PERMISSION}
    with pytest.raises(PermissionDenied, match=updates.OPEN_PERMISSION):
        open_update(pump, target=UpdateTarget.RELEASE, target_release=r56, due_on=today, priority="normal",
                    by=custom_user(pump.tenant, "it-edit", {Module.CYBERSECURITY: Level.EDIT, Module.EQUIPMENT: Level.VIEW}))


@pytest.mark.parametrize("updates_by,expected", [
    (UpdatesBy.MANUFACTURER, ("vendor", "BD field service", "")),
    (UpdatesBy.CE, ("nobody", "", updates.NOBODY_CHOSEN)),
    ("", ("nobody", "", updates.NOBODY_CHOSEN)),
    (UpdatesBy.IT, ("nobody", "", "Left unassigned: updates to this model are installed here by information technology.")),
    (UpdatesBy.DEPARTMENT, ("nobody", "", "Left unassigned: updates to this model are installed here by the department that uses it.")),
])
def test_open_update_routes_by_who_installs_updates_here(pump, pump_model, r56, kim, today, updates_by, expected):
    release(pump_model, "", updates_by=updates_by)
    batch = open_update(pump, target=UpdateTarget.RELEASE, target_release=r56, due_on=today, priority="normal", by=kim)
    wo = batch.work_orders[0]
    who = "vendor" if wo.vendor_service else ("technician" if wo.assigned_to_id else "nobody")
    assert (who, wo.vendor_name, batch.unassigned_reason) == expected


def test_open_update_refuses_assign_to_where_it_would_not_be_used(pump, pump_model, dept, r56, kim, techs, today):
    rental = Asset.objects.create(tag="CE-RENT-1", device_model=pump_model, department=dept, ownership=Ownership.RENTAL, owner="RentCo Medical")
    assert refused(open_update, rental, target=UpdateTarget.RELEASE, target_release=r56, due_on=today, priority="normal",
                   assign_to=techs["tom"], by=kim) == {"assign_to": updates.OWNER_INSTALLS.format(tag="CE-RENT-1", kind="rental")}
    batch = open_update(rental, target=UpdateTarget.RELEASE, target_release=r56, due_on=today, priority="normal", by=kim)
    assert batch.work_orders[0].vendor_name == "RentCo Medical" and batch.unassigned == 0  # its owner installs its updates
    release(pump_model, "", updates_by=UpdatesBy.IT)
    assert refused(open_update, pump, target=UpdateTarget.RELEASE, target_release=r56, due_on=today, priority="normal",
                   assign_to=techs["tom"], by=kim) == {"assign_to": updates.INSTALLER_NOT_CE.format(who="information technology")}


# --- opening a release's updates (the batch) -----------------------------------------------------------------------------------------

@pytest.fixture
def fleet(pump_model, dept, r52, r56, today):
    """Five pumps: P-1 and P-2 on 5.2 (demo), P-3 with none recorded, P-4 on 5.6 (demo), P-5 on 5.2 (demo) in another department; P-1's
    next PM in 40 days, P-2's past, P-3 none."""
    west = Department.objects.create(name="West")
    devices = {}
    for tag, next_pm, department in (("P-1", today + 40 * DAY, dept), ("P-2", today - 5 * DAY, dept), ("P-3", None, dept),
                                     ("P-4", today + 3 * DAY, dept), ("P-5", today + 60 * DAY, west)):
        devices[tag] = Asset.objects.create(tag=tag, device_model=pump_model, department=department, next_pm_on=next_pm)
    for tag in ("P-1", "P-2", "P-5"):
        facts(devices[tag], r52)
    facts(devices["P-4"], r56)
    return SimpleNamespace(devices=devices, dept=dept, west=west)


def test_the_batch_opens_one_per_device_on_a_from_release_in_tag_order(fleet, r52, r56, kim, techs, today):
    batch = open_updates(r56, from_releases=[r52], department=fleet.dept, due_on=today + 30 * DAY, due_at_next_pm=True, priority="normal",
                         assign_to=techs["tom"], by=kim)
    assert [wo.asset.tag for wo in batch.work_orders] == ["P-1", "P-2"] and batch.created == 2
    assert [wo.due_on for wo in batch.work_orders] == [today + 40 * DAY, today]  # each device's next PM, never before today
    assert all(wo.assigned_to == techs["tom"] and wo.problem == updates.UPDATE_PROBLEM for wo in batch.work_orders)
    assert (batch.unassigned, batch.held, batch.already_open, batch.changed) == (0, (), (), ())
    assert set(SoftwareUpdate.objects.values_list("target_release", flat=True)) == {r56.pk}
    more = open_updates(r56, from_releases=[str(r52.pk)], include_not_recorded=True, due_on=today + 30 * DAY, due_at_next_pm=True,
                        priority="high", by=kim)
    assert [wo.asset.tag for wo in more.work_orders] == ["P-3", "P-5"] and more.already_open == ("P-1", "P-2")
    assert [wo.due_on for wo in more.work_orders] == [today + 30 * DAY, today + 60 * DAY]  # P-3 has no next PM: the day given
    assert more.unassigned_reason == updates.NOBODY_CHOSEN and more.unassigned == 2


def test_the_batch_skips_held_and_changed_devices_with_words(fleet, r52, r56, vent_model, kim, today, monkeypatch):
    Asset.objects.filter(pk=fleet.devices["P-1"].pk).update(incident_hold=True)
    real = wo_services.create_work_order

    def moving(**kwargs):
        wo = real(**kwargs)
        if kwargs["asset"].tag == "P-2":
            Asset.objects.filter(pk=kwargs["asset"].pk).update(device_model=vent_model)
        if kwargs["asset"].tag == "P-5":
            Asset.objects.filter(pk=kwargs["asset"].pk).update(incident_hold=True)
        return wo

    monkeypatch.setattr(wo_services, "create_work_order", moving)
    batch = open_updates(r56, from_releases=[r52], include_not_recorded=True, due_on=today, priority="normal", by=kim)
    assert (batch.created, batch.held, batch.changed) == (1, ("P-1", "P-5"), ("P-2",))
    assert [wo.asset.tag for wo in batch.work_orders] == ["P-3"]
    assert sorted(WorkOrder.objects.values_list("asset__tag", flat=True)) == ["P-3"]  # each skipped device went back alone


def test_the_batch_refuses_in_words(fleet, pump_model, vent_model, r52, r56, kim, today, monkeypatch):
    def attempt(release_=r56, **fields):
        given = {"from_releases": [r52], "due_on": today, "priority": "normal", **fields}
        return refused(open_updates, release_, by=kim, **given)

    removed = release(pump_model, "5.4 (demo)")
    remove_release(removed, by=None)
    assert attempt(from_releases=[r56]) == {"from_releases": updates.FROM_TARGET.format(version="5.6 (demo)")}
    assert attempt(from_releases=[release(vent_model, "3.1")]) == {"from_releases": updates.CHOOSE_FROM}
    assert attempt(from_releases=["nope"]) == {"from_releases": updates.CHOOSE_FROM}
    assert attempt(from_releases=[]) == {"from_releases": updates.FROM_NEEDED}
    assert attempt(from_releases=[release(pump_model, "5.0 (demo)")]) == {"from_releases": updates.NONE_SELECTED}
    assert attempt(removed) == {"release": updates.CHOOSE_TARGET}
    assert attempt(release(pump_model, "")) == {"release": updates.CHOOSE_TARGET}
    assert attempt(department="nope", priority="critical", due_on=None) == {
        "department": updates.CHOOSE_DEPARTMENT, "priority": updates.PRIORITY_WORDS, "due_on": updates.DUE_NEEDED}
    monkeypatch.setattr(updates, "UPDATE_BATCH_MAX", 2)
    assert attempt() == {"release": updates.TOO_MANY}  # P-1, P-2, P-5 in two departments
    assert attempt(department=fleet.dept, include_not_recorded=True) == {"release": updates.TOO_MANY_IN_ONE.format(count=3)}
    with pytest.raises(PermissionDenied, match=updates.OPEN_PERMISSION):
        open_updates(r56, from_releases=[r52], due_on=today, priority="normal",
                     by=custom_user(fleet.dept.tenant, "it-edit", {Module.CYBERSECURITY: Level.EDIT, Module.EQUIPMENT: Level.VIEW}))
    assert not WorkOrder.objects.exists()


def test_the_batch_routes_and_assigns_as_one_update_does(fleet, pump_model, dept, r52, r56, tech, kim, techs, today):
    rental = Asset.objects.create(tag="P-0", device_model=pump_model, department=dept, ownership=Ownership.RENTAL, owner="RentCo Medical")
    facts(rental, r52)
    by_tech = open_updates(r56, from_releases=[r52], department=dept, due_on=today, priority="normal", by=tech)
    assert (by_tech.created, by_tech.unassigned, by_tech.unassigned_reason) == (3, 3, updates.ASSIGN_NEEDS)
    assert not WorkOrder.objects.filter(assigned_to__isnull=False).exists() and not WorkOrder.objects.filter(vendor_service=True).exists()
    for wo in by_tech.work_orders:
        wo_services.change_status(wo, WoStatus.CANCELLED)
    by_kim = open_updates(r56, from_releases=[r52], department=dept, due_on=today, priority="normal", assign_to=techs["tom"], by=kim)
    assert [(wo.asset.tag, wo.vendor_name, wo.assigned_to) for wo in by_kim.work_orders] == [
        ("P-0", "RentCo Medical", None), ("P-1", "", techs["tom"]), ("P-2", "", techs["tom"])]
    assert by_kim.unassigned == 0
    for wo in by_kim.work_orders:
        wo_services.change_status(wo, WoStatus.CANCELLED)
    release(pump_model, "", updates_by=UpdatesBy.IT)
    assert refused(open_updates, r56, from_releases=[r52], due_on=today, priority="normal", assign_to=techs["tom"], by=kim) == {
        "assign_to": updates.INSTALLER_NOT_CE.format(who="information technology")}
    by_it = open_updates(r56, from_releases=[r52], department=dept, due_on=today, priority="normal", by=kim)
    assert by_it.unassigned == 2 and by_it.unassigned_reason == updates.INSTALLED_HERE.format(who="information technology")


class _Deadlock(Exception):
    sqlstate = "40P01"  # what psycopg's DeadlockDetected carries (services._lock_conflict reads it)


def test_a_lock_conflict_answers_race_in_words_and_opens_nothing(fleet, pump_model, r52, r56, kim, today, monkeypatch):
    real = wo_services.create_work_order

    def deadlocked(**kwargs):
        if kwargs["asset"].tag == "P-2":
            raise OperationalError("deadlock detected") from _Deadlock()
        return real(**kwargs)

    monkeypatch.setattr(wo_services, "create_work_order", deadlocked)
    assert refused(open_updates, r56, from_releases=[r52], due_on=today, priority="normal", by=kim) == {"release": services.RACE}
    assert refused(open_update, fleet.devices["P-2"], target=UpdateTarget.RELEASE, target_release=r56, due_on=today, priority="normal",
                   by=kim) == {"asset": services.RACE}
    assert not WorkOrder.objects.exists() and not SoftwareUpdate.objects.exists()


def test_another_facilitys_rows_are_never_named(other_tenant, pump, r52, r56, kim, today):
    with tenant_context(other_tenant):
        dept = Department.objects.create(name="Their ICU")
        model = DeviceModel.objects.create(manufacturer="BD", model="Alaris 8015 PCU", description="Pump", category="Infusion pumps")
        their_release = release(model, "5.6 (demo)")
        theirs = Asset.objects.create(tag="THEIRS-1", device_model=model, department=dept)
        _wo, their_row = update_wo(theirs, their_release)
    assert refused(remove, their_row.pk, by=kim) == {"update": updates.CHOOSE_UPDATE}
    assert refused(confirm, str(their_row.pk), choice(INSTALLED, r56, basis=MR), by=kim) == {"update": updates.CHOOSE_UPDATE}
    assert refused(record, theirs.pk, choice(INSTALLED, r56, basis=MR), installed_by=InstalledBy.IT, applied_on=today, by=kim) == {
        "asset": services.CHOOSE_DEVICE}
    assert refused(open_update, theirs.pk, target=UpdateTarget.RELEASE, target_release=r56, due_on=today, priority="normal", by=kim) == {
        "asset": services.CHOOSE_DEVICE}
    assert refused(open_update, pump, target=UpdateTarget.RELEASE, target_release=their_release.pk, due_on=today, priority="normal",
                   by=kim) == {"target_release": updates.CHOOSE_TARGET}
    assert refused(open_updates, their_release.pk, from_releases=[r52], due_on=today, priority="normal", by=kim) == {
        "release": updates.CHOOSE_TARGET}
    assert not WorkOrder.objects.exists()


# --- the rules ------------------------------------------------------------------------------------------------------------------------

def test_updates_of_is_one_query_newest_first_and_words_need_none(pump, r52, r56, on52, techs, tech, manager, today,
                                                                  django_assert_num_queries):
    done, done_row = update_wo(pump, r56, assigned_to=techs["tom"], opened_on=today - 20 * DAY)
    complete(done, tech, choice(INSTALLED, r56, basis=MR), today=today - 10 * DAY)
    cancelled, cancelled_row = update_wo(pump, reference="ICAR-2026-01", opened_on=today - 40 * DAY)
    SoftwareUpdate.objects.filter(pk=cancelled_row.pk).update(opened_on=today - 40 * DAY)  # as its work order (the fixture dates it today)
    wo_services.change_status(cancelled, WoStatus.CANCELLED)
    _open, open_row = update_wo(pump, reference="ICAR-2026-09")
    hand = record(pump, choice(PATCHES, reference="ICAR-2026-08", basis=NS), installed_by=InstalledBy.DEPARTMENT,
                  applied_on=today - 15 * DAY, by=tech)
    gone = record(pump, choice(PATCHES, reference="ICAR-2026-07", basis=NS), installed_by=InstalledBy.CE, applied_on=today - DAY, by=tech)
    remove(gone, by=manager)
    with django_assert_num_queries(1):
        rows = rules.updates_of(pump)
        said = [rules.update_words(r) for r in rows]
        states = [rules.update_state(r) for r in rows]
    WORDS_SEEN.extend(said)
    assert [r.pk for r in rows] == [open_row.pk, done_row.pk, hand.pk, cancelled_row.pk]
    assert states == [rules.OPEN, rules.DONE, rules.DONE, rules.CANCELLED]
    assert said[0] == "Open: to apply the manufacturer's patches (ref ICAR-2026-09)" and said[3] == "Cancelled"
    # The completion's record (5.6 (demo) since 10 days ago) is newer and kept; the patches name no version, none being sent (round 2:
    # never the record now, nor "version not recorded")
    assert said[2] == (f"Patches applied {rules.day_words(today - 15 * DAY)}, version unchanged; ref ICAR-2026-08; "
                       "installed by the department that uses it; a later record of the device is kept")


def test_campaigns_count_each_releases_updates_by_state_in_one_query(fleet, pump_model, r52, r56, kim, tech, techs, today,
                                                                     django_assert_num_queries):
    batch = open_updates(r56, from_releases=[r52], include_not_recorded=True, due_on=today, priority="normal", assign_to=techs["tom"],
                         by=kim)
    p1, p2, p3, p5 = batch.work_orders
    complete(p1, tech, choice(INSTALLED, r56, basis=MR))
    complete(p2, tech, choice(NOT_CONFIRMED), "Done; version to be checked")
    wo_services.change_status(p3, WoStatus.CANCELLED)
    r57 = release(pump_model, "5.7 (demo)")
    open_update(fleet.devices["P-4"], target=UpdateTarget.RELEASE, target_release=r57, due_on=today, priority="normal", by=kim)
    with django_assert_num_queries(1):
        found = rules.campaigns(pump_model)
    assert found == {r56.pk: {rules.OPEN: 1, rules.DONE: 1, rules.NOT_CONFIRMED: 1, rules.CANCELLED: 1},
                     r57.pk: {rules.OPEN: 1, rules.DONE: 0, rules.NOT_CONFIRMED: 0, rules.CANCELLED: 0}}
    said = rules.campaign_words(r56, found[r56.pk])
    WORDS_SEEN.append(said)
    assert said == "Updates to 5.6 (demo): 1 open, 1 done, 1 not confirmed, 1 cancelled"
    assert rules.campaign_words(r52, found.get(r52.pk)) == ""


def test_release_offer_lists_the_models_releases_and_the_recorded_one(pump, pump_model, r52, r56, on52, job, tech):
    wo, row = job
    release(pump_model, "")
    removed = release(pump_model, "5.0 (demo)")
    remove_release(removed, by=None)
    offer = rules.release_offer(wo, tech)
    assert offer.releases == (r52, r56) and offer.recorded == r52 and offer.bases == updates.BASES
    assert (offer.target, offer.target_release, offer.target_reference) == (UpdateTarget.RELEASE, r56, "")
    repair = wo_services.create_work_order(asset=pump, type=WoType.REPAIR, priority=Priority.NORMAL, problem="Alarm")
    assert rules.offer_or_why(repair, tech) == (None, rules.NOT_AN_UPDATE)


def every_sentence(pump_model, today) -> list:
    """update_words for every shape a row can take (unsaved rows: no query): each state; each outcome from each source (in-house, the
    vendor's job with and without its report's number, each installer by hand), with each basis it may name, a reference or none, a
    release after or none, entered in error since or not, a later record kept or not."""
    from itertools import product

    def rel(version, removed=False):
        return SoftwareRelease(device_model=pump_model, version=version, version_key=services.version_key(version),
                               removed_on=today if removed else None)

    def wo(status, vendor=False, reference=""):
        return WorkOrder(number="WO-26-0042", status=status, vendor_service=vendor, checks_reference=reference, completed_on=today)

    out = []
    for status in WoStatus.values:
        for target, ref in ((rel("5.6 (demo)"), ""), (rel("5.6 (demo)", True), ""), (None, "ICAR-2026-09"), (None, "")):
            for vendor in (False, True):
                row = SoftwareUpdate(work_order=wo(status, vendor), target=UpdateTarget.RELEASE if target else UpdateTarget.PATCHES,
                                     target_release=target, target_reference=ref)
                out.append(rules.update_words(row))
    sources = [wo(WoStatus.COMPLETED), wo(WoStatus.CLOSED, True), wo(WoStatus.COMPLETED, True, "VR-2026-77")] + list(InstalledBy.values)
    for outcome, source, reference, after, before, kept in product(RECORDED_OUTCOMES_ALL, sources, ("", "FCO-2026-11"),
                                                                    (None, rel("5.6 (demo)"), rel("5.6 (demo)", True)),
                                                                    (None, rel("5.2 (demo)")), (False, True)):
        for basis in updates.BASES[outcome] or ("",):
            row = SoftwareUpdate(outcome=outcome, reference=reference, basis=basis, applied_on=today, release_after=after,
                                 release_before=before, later_kept=kept)
            if isinstance(source, WorkOrder):
                row.work_order = source
            else:
                row.installed_by = source
            out.append(rules.update_words(row))
    removed = SoftwareUpdate(outcome=PATCHES, installed_by=InstalledBy.IT, applied_on=today, removed_on=today)
    out.append(rules.update_words(removed))
    return out


RECORDED_OUTCOMES_ALL = (INSTALLED, PATCHES, ALREADY)


def test_no_sentence_or_refusal_says_a_never_word(pump_model, today):
    sentences = every_sentence(pump_model, today)
    assert len(set(sentences)) > 200
    texts = [v for k, v in vars(updates).items() if k.isupper() and isinstance(v, str)]
    texts += [v for d in (updates.DEFAULT_RESOLUTION, rules.STATE_LABELS, rules.INSTALLER_WORDS) for v in d.values()]
    texts += sentences + WORDS_SEEN + [rules.LATER_KEPT_WORDS, rules.campaign_words(SoftwareRelease(version="5.6 (demo)"),
                                                                                     dict.fromkeys(rules.STATES, 2))]
    assert [(t, never(t)) for t in texts if never(t)] == []
    assert not any(re.search(r"\brequire", t, re.IGNORECASE) for t in texts)
    assert not any("validated" in s for s in sentences if "manufacturer's validated patch list" not in s)
    assert not any("updated from" in s.casefold() or "critical" in s.casefold() for s in sentences)


# --- the lock spy (both databases): the register first, then the contract's order -----------------------------------------------------

@pytest.fixture
def spy(monkeypatch):
    calls: list = []

    def wrap(module, name, label):
        real = getattr(module, name)

        def spied(*args, **kwargs):
            calls.append(label)
            return real(*args, **kwargs)

        monkeypatch.setattr(module, name, spied)

    wrap(services, "lock_register", "register")
    wrap(wo_services, "lock_work", "work")
    wrap(wo_services, "create_work_order", "numbering")
    wrap(services, "_locked_release", "release")
    wrap(services, "_locked_devices", "devices")

    def firsts() -> list:
        seen: list = []
        for label in calls:
            if label not in seen:
                seen.append(label)
        return seen

    return SimpleNamespace(firsts=firsts, reset=calls.clear)


def test_every_writer_takes_the_register_lock_first(pump, r52, r56, on52, techs, tech, kim, manager, today, spy):
    wo, row = update_wo(pump, r56, assigned_to=techs["tom"])
    spy.reset()
    complete(wo, tech, choice(INSTALLED, r56, basis=MR))
    assert spy.firsts() == ["register", "work", "release"]  # the device FOR UPDATE by the completion itself, then the release

    other, other_row = update_wo(pump, reference="ICAR-2026-09", assigned_to=techs["tom"])
    spy.reset()
    complete(other, tech, choice(NOT_CONFIRMED), "Done")
    assert "register" not in spy.firsts()  # not confirmed now records nothing: no register lock
    spy.reset()
    confirm(other_row, choice(PATCHES, reference="ICAR-2026-09", basis=MR), by=tech)
    assert spy.firsts() == ["register", "work", "devices"]  # patches name no release to lock
    spy.reset()
    remove(other_row, by=manager)
    assert spy.firsts() == ["register", "work"]
    spy.reset()
    confirm(other_row, choice(ALREADY, r56), by=tech)
    assert spy.firsts() == ["register", "work", "release", "devices"]

    spy.reset()
    hand = record(pump, choice(INSTALLED, r52, basis=MR), installed_by=InstalledBy.IT, applied_on=today, by=tech)
    assert spy.firsts() == ["register", "release", "devices"]
    spy.reset()
    remove(hand, by=manager)
    assert spy.firsts() == ["register"]

    spy.reset()
    opened = open_update(pump, target=UpdateTarget.RELEASE, target_release=r56, due_on=today, priority="normal", by=kim)
    assert spy.firsts() == ["register", "release", "numbering", "devices"]  # the device FOR KEY SHARE after its insert (review fix C3)
    wo_services.change_status(opened.work_orders[0], WoStatus.CANCELLED)
    spy.reset()
    open_updates(r56, from_releases=[r52], due_on=today, priority="normal", by=kim)
    assert spy.firsts() == ["register", "release", "numbering", "devices"]

    third, _ = update_wo(pump, r56, assigned_to=techs["tom"])
    spy.reset()
    complete(third, None, KEEP, "Done")
    assert spy.firsts() == ["work"]  # KEEP: no register lock
