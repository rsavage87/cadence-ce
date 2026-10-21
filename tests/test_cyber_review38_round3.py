"""
Slice 38 review fixes, round 3 (H1: the services, rules, history, and API serializer), on SQLite and PostgreSQL:

1. History never shows before_taken (bookkeeping): the record's History, the change log (screen, CSV, print), and the API change log,
   after a first completion, a Confirm version, and a hand record.
2. What was recorded before is stored as taken, never nulled: an install of the release it names claims no before in the words and the
   API (release_before null there), so a later correction (Entered in error then Confirm version, a re-completion) still knows it.
3. A row that took its before is judged against it: patches and already, on a re-completion or a Confirm version after Entered in error,
   are checked against release_before (the device's record may be the row's own earlier result), and patches naming no release name it.
4. A hand row's correction (a new row) takes the release_before and before_taken of the hand row entered in error whose result the
   device's record still names exactly, and is judged against it.
5. Installed naming the release the record already names keeps the record's earlier since (the day checked moves, never the since).
6. The duplicate rule: patches by day and reference (never the version); installed by release and day while the device's record still
   names it (a reinstall after a rollback that day is accepted); a work order's outcome is never refused for a hand row's.
7. A scoped user who sends an outcome learns only RECORD_PERMISSION, before whether the work order has a record or its device is on
   site (complete_work_order and the API transition); an unscoped user's answers keep their order.
"""
import csv
import io

import pytest
from cyber_fixtures import update_wo
from test_cyber_review38_services import (  # noqa: F401
    ALREADY,
    DAY,
    INSTALLED,
    MR,
    NS,
    PATCHES,
    VL,
    choice,
    complete,
    day,
    job,
    kim,
    manager,
    on52,
    r52,
    r56,
    r57,
    refused,
    tech,
    today,
    vendor,
    vendor_job,
    words,
)
from test_cyber_updates_api import WORK_ORDERS, transition, world  # noqa: F401

from apps.api import serializers_cybersecurity as scs
from apps.core import history
from apps.cybersecurity import updates
from apps.cybersecurity.models import DeviceSecurity, InstalledBy, SoftwareUpdate
from apps.cybersecurity.services import record_facts
from apps.cybersecurity.updates import NOT_CONFIRMED, confirm, record, remove
from apps.equipment.models import Asset, AssetStatus
from apps.equipment.services import set_status
from apps.workorders import services as wo_services
from apps.workorders.models import Priority, Source, WorkOrder, WoStatus, WoType

BEFORE_TAKEN = "Before taken"


def fresh(row):
    return (SoftwareUpdate.objects.select_related("asset", "work_order", "target_release", "release_before", "release_after", "recorded_by")
            .get(pk=row.pk))


def api_before(row):
    """The API's release_before for the row (GET software-updates/'s update_data)."""
    return scs.update_data(fresh(row))["release_before"]


def ref(release) -> dict:
    return {"id": str(release.pk), "version": release.version}


def sec_of(asset) -> tuple:
    sec = DeviceSecurity.objects.get(asset=asset)
    return sec.release, sec.release_since, sec.checked_on


# --- 1. history never shows before_taken -----------------------------------------------------------------------------------------------

def _recordings(pump, r56, job, vendor_job, tech, today) -> list:  # noqa: F811
    """A first completion, a Confirm version (the vendor's job), and a hand record: each a first recording of its row."""
    wo, row = job
    complete(wo, tech, choice(INSTALLED, r56, basis=MR))
    _vwo, vrow = vendor_job
    confirm(vrow, choice(INSTALLED, r56, basis=MR), by=tech)
    hand = record(pump, choice(PATCHES, reference="ICAR-2026-07", basis=NS), installed_by=InstalledBy.IT, applied_on=today, by=tech)
    return [row, vrow, hand]


def test_h1_the_records_history_never_names_before_taken(pump, r52, r56, on52, job, vendor_job, tech, today):  # noqa: F811
    completed, confirmed, hand = _recordings(pump, r56, job, vendor_job, tech, today)
    for row, before in ((completed, True), (confirmed, False), (hand, True)):
        entries, _ = history.record_history(SoftwareUpdate.objects.get(pk=row.pk))
        fields = [c.field for e in entries for c in e.changes]
        assert "What it did" in fields and BEFORE_TAKEN not in fields, fields
        # the fact shown: the record named 5.2, then 5.6; the vendor's job, confirmed under a later record, names none
        assert ("Recorded before" in fields) is before


def test_h1_the_change_log_csv_and_print_never_name_it(client, pump, r52, r56, on52, job, vendor_job, tech, kim, today):  # noqa: F811
    _recordings(pump, r56, job, vendor_job, tech, today)
    entries, _ = history.change_log(kim, areas=["software_updates"], limit=200)
    fields = [c.field for e in entries for c in e.changes]
    assert "What it did" in fields and BEFORE_TAKEN not in fields, fields
    client.force_login(kim)
    screen = client.get("/users/log/?area=software_updates")
    assert screen.status_code == 200 and "What it did" in screen.content.decode() and BEFORE_TAKEN not in screen.content.decode()
    printed = client.get("/users/log/print/?area=software_updates")
    assert printed.status_code == 200 and "What it did" in printed.content.decode() and BEFORE_TAKEN not in printed.content.decode()
    exported = client.get("/users/log/export.csv?area=software_updates")
    text = b"".join(exported.streaming_content).decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text)))
    assert len(rows) > 3 and "What it did" in text and BEFORE_TAKEN not in text


def test_h1_the_api_change_log_never_names_before_taken(client, pump, r52, r56, on52, job, vendor_job, tech, kim, today):  # noqa: F811
    _recordings(pump, r56, job, vendor_job, tech, today)
    client.force_login(kim)
    r = client.get("/api/v1/change-log/?area=software_updates&limit=200")
    assert r.status_code == 200, r.content
    fields = [c["field"] for e in r.json()["results"] for c in e["changes"]]
    assert "What it did" in fields and BEFORE_TAKEN not in fields, fields


# --- 2. the before is stored as taken, never nulled ------------------------------------------------------------------------------------

def test_h2_a_mistaken_pick_of_the_recorded_release_then_a_correction_keeps_the_before(pump, r52, r56, on52, job, tech, manager, today):  # noqa: F811
    """Skeptics' core 5 and 9: the 5.6 update completed as installed 5.2 by mistake (the release the record names): stored before 5.2,
    no claim in the words or the API; Entered in error, then Confirm version installed 5.6: recorded before 5.2."""
    wo, row = job
    complete(wo, tech, choice(INSTALLED, r52, basis=MR))
    row.refresh_from_db()
    assert (row.release_before, row.before_taken, row.release_after) == (r52, True, r52)
    assert words(row) == f"5.2 (demo) installed {day(today)}"
    assert api_before(row) is None
    remove(row, by=manager)
    assert api_before(row) is None  # nothing recorded
    confirm(row, choice(INSTALLED, r56, basis=MR), by=tech)
    row.refresh_from_db()
    assert (row.release_before, row.release_after) == (r52, r56)
    assert words(row) == f"5.6 (demo) installed {day(today)} (recorded before: 5.2 (demo))"
    assert api_before(row) == ref(r52)


def test_h2_complete_remove_confirm_the_before_remove_confirm_the_original(pump, r52, r56, on52, job, tech, manager, today):  # noqa: F811
    """Skeptics' web 3: a correction that names the before itself, then the original install again: the before is never lost."""
    wo, row = job
    complete(wo, tech, choice(INSTALLED, r56, basis=MR))
    assert words(row) == f"5.6 (demo) installed {day(today)} (recorded before: 5.2 (demo))"
    remove(row, by=manager)
    confirm(row, choice(INSTALLED, r52, basis=MR), by=tech)
    row.refresh_from_db()
    assert (row.release_before, row.release_after) == (r52, r52) and words(row) == f"5.2 (demo) installed {day(today)}"
    remove(row, by=manager)
    confirm(row, choice(INSTALLED, r56, basis=MR), by=tech)
    row.refresh_from_db()
    assert words(row) == f"5.6 (demo) installed {day(today)} (recorded before: 5.2 (demo))"
    assert api_before(row) == ref(r52)


def test_h2_a_re_completion_after_a_pick_of_the_recorded_release_keeps_the_before(pump, r52, r56, on52, job, tech, kim, today):  # noqa: F811
    wo, row = job
    complete(wo, tech, choice(INSTALLED, r52, basis=MR))
    wo_services.change_status(wo, WoStatus.IN_PROGRESS, by=kim)
    complete(wo, tech, choice(INSTALLED, r56, basis=MR), "Loaded 5.6")
    row.refresh_from_db()
    assert (row.release_before, row.release_after) == (r52, r56)
    assert words(row) == f"5.6 (demo) installed {day(today)} (recorded before: 5.2 (demo))"


def test_h2_confirm_installed_on_the_recorded_release_then_a_correction(pump, r52, r56, on52, techs, tech, manager, today):  # noqa: F811
    """Skeptics' web 6: a patches update completed with the version not confirmed; Confirm version installed on the recorded release
    (stored before 5.2, no claim); Entered in error; Confirm installed 5.6: recorded before 5.2."""
    wo, row = update_wo(pump, reference="ICAR-2026-09", assigned_to=techs["tom"])
    complete(wo, tech, choice(NOT_CONFIRMED), "Done; version to be checked")
    confirm(row, choice(INSTALLED, r52, basis=NS), by=tech)
    row.refresh_from_db()
    assert (row.release_before, row.before_taken) == (r52, True) and api_before(row) is None
    assert words(row) == f"5.2 (demo) installed {day(today)}"
    remove(row, by=manager)
    confirm(row, choice(INSTALLED, r56, basis=MR), by=tech)
    row.refresh_from_db()
    assert words(row) == f"5.6 (demo) installed {day(today)} (recorded before: 5.2 (demo))"


def test_h2_the_api_names_the_before_of_patches_and_of_an_install_of_another_release(pump, r52, r56, on52, job, techs, tech, today):  # noqa: F811
    wo, row = job
    complete(wo, tech, choice(INSTALLED, r56, basis=MR))
    assert api_before(row) == ref(r52)
    wo2, patched = update_wo(pump, reference="ICAR-2026-09", assigned_to=techs["tom"])
    complete(wo2, tech, choice(PATCHES, reference="ICAR-2026-09", basis=VL))
    patched.refresh_from_db()
    assert (patched.release_before, patched.release_after) == (r56, r56)
    assert api_before(patched) == ref(r56)  # patches: the before is the version they were applied on, shown


# --- 3. a row that took its before is judged against it --------------------------------------------------------------------------------

@pytest.mark.parametrize("named", [True, False], ids=["naming 5.2", "no release"])
def test_h3_entered_in_error_then_confirm_patches_without_record_facts(pump, r52, r56, on52, techs, tech, manager, today, named):  # noqa: F811
    """Skeptics' core 6 and 12 (a): a patches job completed three days ago as installed 5.6 by mistake (the facts: 5.6 since then);
    Entered in error; Confirm version as patches, with no Record facts first: judged against 5.2 (the before taken), never against 5.6
    (the outcome entered in error's own result). The facts follow the outcome as before: the day checked only."""
    done_on = today - 3 * DAY
    wo, row = update_wo(pump, reference="ICAR-2026-09", assigned_to=techs["tom"], opened_on=today - 5 * DAY)
    complete(wo, tech, choice(INSTALLED, r56, basis=MR), today=done_on)
    remove(row, by=manager)
    assert refused(confirm, row, choice(PATCHES, r56, reference="ICAR-2026-09", basis=VL), by=tech) == {
        "software_release": updates.PATCHES_UNCHANGED}
    confirm(row, choice(PATCHES, r52 if named else None, reference="ICAR-2026-09", basis=VL), by=tech)
    row.refresh_from_db()
    assert (row.outcome, row.release_before, row.release_after, row.later_kept) == (PATCHES, r52, r52, False)
    assert words(row) == (f"Patches on the manufacturer's validated patch list applied {day(done_on)}, version unchanged (5.2 (demo)); "
                          "ref ICAR-2026-09")
    assert sec_of(pump) == (r56, done_on, done_on)  # Record facts corrects the release


def test_h3_entered_in_error_then_confirm_already_is_judged_against_the_before(pump, r52, r56, on52, techs, tech, manager, today):  # noqa: F811
    done_on = today - 3 * DAY
    wo, row = update_wo(pump, reference="ICAR-2026-09", assigned_to=techs["tom"], opened_on=today - 5 * DAY)
    complete(wo, tech, choice(INSTALLED, r56, basis=MR), today=done_on)
    remove(row, by=manager)
    assert refused(confirm, row, choice(ALREADY, r56), by=tech) == {"software_release": updates.RECORDED_AS.format(version="5.2 (demo)")}
    confirm(row, choice(ALREADY), by=tech)
    row.refresh_from_db()
    assert (row.release_before, row.release_after) == (r52, r52)
    assert words(row) == f"Checked {day(done_on)}: already on 5.2 (demo); nothing installed"


@pytest.mark.parametrize("named", [True, False], ids=["naming 5.2", "no release"])
def test_h3_a_re_completion_as_patches_is_judged_against_the_before(pump, r52, r56, on52, techs, tech, kim, today, named):  # noqa: F811
    """Skeptics' core 6 and 12 (b): completed today as installed 5.6 by mistake, reopened, completed again as patches: 5.2 (the before
    taken at the first completion) is the version, never 5.6; naming 5.6 is refused."""
    wo, row = update_wo(pump, reference="ICAR-2026-09", assigned_to=techs["tom"])
    complete(wo, tech, choice(INSTALLED, r56, basis=MR))
    wo_services.change_status(wo, WoStatus.IN_PROGRESS, by=kim)
    assert refused(complete, wo, tech, choice(PATCHES, r56, reference="ICAR-2026-09", basis=VL), "Patched") == {
        "software_release": updates.PATCHES_UNCHANGED}
    complete(wo, tech, choice(PATCHES, r52 if named else None, reference="ICAR-2026-09", basis=VL), "Patched")
    row.refresh_from_db()
    assert (row.outcome, row.release_before, row.release_after) == (PATCHES, r52, r52)
    assert words(row) == (f"Patches on the manufacturer's validated patch list applied {day(today)}, version unchanged (5.2 (demo)); "
                          "ref ICAR-2026-09")


def test_h3_a_re_completion_as_already_is_judged_against_the_before(pump, r52, r56, on52, techs, tech, kim, today):  # noqa: F811
    wo, row = update_wo(pump, reference="ICAR-2026-09", assigned_to=techs["tom"])
    complete(wo, tech, choice(INSTALLED, r56, basis=MR))
    wo_services.change_status(wo, WoStatus.IN_PROGRESS, by=kim)
    assert refused(complete, wo, tech, choice(ALREADY, r56), "Checked") == {
        "software_release": updates.RECORDED_AS.format(version="5.2 (demo)")}
    complete(wo, tech, choice(ALREADY, r52), "Checked")
    row.refresh_from_db()
    assert (row.release_before, row.release_after) == (r52, r52)
    assert words(row) == f"Checked {day(today)}: already on 5.2 (demo); nothing installed"


# --- 4. a hand row's correction ---------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("ago", [2, 0], ids=["two days ago", "today"])
def test_h4_a_hand_row_corrected_to_another_release(pump, r52, r56, r57, on52, tech, manager, today, ago):  # noqa: F811
    """Skeptics' core 1, 7, and 13: IT records installed 5.6, but it was 5.7: Entered in error, then installed 5.7 on the same day:
    recorded before 5.2 (the removed row's before), never 5.6 (the release entered in error, which the device never ran)."""
    on = today - ago * DAY
    wrong = record(pump, choice(INSTALLED, r56, basis=NS), installed_by=InstalledBy.IT, applied_on=on, by=tech)
    remove(wrong, by=manager)
    right = record(pump, choice(INSTALLED, r57, basis=NS), installed_by=InstalledBy.IT, applied_on=on, by=tech)
    assert (right.release_before, right.before_taken, right.release_after) == (r52, True, r57)
    assert words(right) == f"5.7 (demo) installed {day(on)} (recorded before: 5.2 (demo)); installed by information technology"
    assert sec_of(pump)[:2] == (r57, on)


@pytest.mark.parametrize("named", [True, False], ids=["naming 5.2", "no release"])
@pytest.mark.parametrize("ago", [2, 0], ids=["two days ago", "today"])
def test_h4_a_hand_row_corrected_to_patches(pump, r52, r56, on52, tech, manager, today, ago, named):  # noqa: F811
    on = today - ago * DAY
    wrong = record(pump, choice(INSTALLED, r56, basis=NS), installed_by=InstalledBy.IT, applied_on=on, by=tech)
    remove(wrong, by=manager)
    assert refused(record, pump, choice(PATCHES, r56, reference="ICAR-2026-09", basis=NS), installed_by=InstalledBy.IT, applied_on=on,
                   by=tech) == {"software_release": updates.PATCHES_UNCHANGED}
    patched = record(pump, choice(PATCHES, r52 if named else None, reference="ICAR-2026-09", basis=NS), installed_by=InstalledBy.IT,
                     applied_on=on, by=tech)
    assert (patched.release_before, patched.before_taken, patched.release_after) == (r52, True, r52)
    assert words(patched) == (f"Patches applied {day(on)}, version unchanged (5.2 (demo)); ref ICAR-2026-09; installed by information "
                              "technology")


def test_h4_only_a_record_that_still_names_the_removed_rows_result(pump, r52, r56, r57, on52, tech, manager, today):  # noqa: F811
    """Record facts corrected the release after the removal (5.7 since that day): the record no longer names what the removed row
    wrote, so the next hand record's before is the record as usual."""
    on = today - 2 * DAY
    wrong = record(pump, choice(INSTALLED, r56, basis=NS), installed_by=InstalledBy.IT, applied_on=on, by=tech)
    remove(wrong, by=manager)
    record_facts(pump, by=None, release=r57, release_since=on, checked_on=on)
    patched = record(pump, choice(PATCHES, r57, reference="ICAR-2026-09", basis=NS), installed_by=InstalledBy.IT, applied_on=today - DAY,
                     by=tech)
    assert (patched.release_before, patched.release_after) == (r57, r57)


# --- 5. installed naming the recorded release keeps the earlier since -------------------------------------------------------------------

def test_h5_record_facts_on_the_install_day_then_completion_the_next(pump, r52, r56, on52, techs, tech, kim, today):  # noqa: F811
    """Skeptics' core 8 and 14 (a): started and recorded (5.6 since the day before) the day before, completed as installed 5.6 today:
    the since stays the install's day; the day checked is the completion's."""
    wo, row = update_wo(pump, r56, assigned_to=techs["tom"], opened_on=today - 3 * DAY)
    wo_services.change_status(wo, WoStatus.IN_PROGRESS, by=kim, as_of=today - DAY)
    record_facts(pump, by=None, release=r56, release_since=today - DAY, checked_on=today - DAY)
    complete(wo, tech, choice(INSTALLED, r56, basis=MR))
    assert sec_of(pump) == (r56, today - DAY, today)
    row.refresh_from_db()
    assert words(row) == f"5.6 (demo) installed {day(today)}"


def test_h5_a_vendors_job_confirmed_after_the_since_from_its_report(pump, r52, r56, on52, vendor_job, tech, today):  # noqa: F811
    """Skeptics' core 8 and 14 (b): BD installed 5.6 the day before it closed its job; IT records 5.6 since then, as the report says;
    Confirm installed 5.6: the since stays the visit's day."""
    _wo, row = vendor_job
    record_facts(pump, by=None, release=r56, release_since=today - 6 * DAY, checked_on=today - 6 * DAY)
    confirm(row, choice(INSTALLED, r56, basis=MR), by=tech)
    assert sec_of(pump) == (r56, today - 6 * DAY, today - 5 * DAY)
    row.refresh_from_db()
    assert (row.release_before, row.later_kept) == (r56, False)
    assert words(row) == f"5.6 (demo) installed {day(today - 5 * DAY)}; as the vendor's report VR-2026-77 states"


def test_h5_a_hand_record_of_the_recorded_release_keeps_the_since(pump, r56, tech, today):  # noqa: F811
    from cyber_fixtures import facts

    facts(pump, r56, release_since=today - 10 * DAY, checked_on=today - 10 * DAY)
    record(pump, choice(INSTALLED, r56, basis=NS), installed_by=InstalledBy.IT, applied_on=today - 4 * DAY, by=tech)
    assert sec_of(pump) == (r56, today - 10 * DAY, today - 4 * DAY)


def test_h5_a_since_is_set_when_none_is_recorded_or_the_release_changes(pump, r52, r56, tech, today):  # noqa: F811
    from cyber_fixtures import facts

    facts(pump, r56)  # no since, no day checked
    record(pump, choice(INSTALLED, r56, basis=NS), installed_by=InstalledBy.IT, applied_on=today - 4 * DAY, by=tech)
    assert sec_of(pump) == (r56, today - 4 * DAY, today - 4 * DAY)
    record(pump, choice(INSTALLED, r52, basis=NS), installed_by=InstalledBy.IT, applied_on=today - 2 * DAY, by=tech)
    assert sec_of(pump) == (r52, today - 2 * DAY, today - 2 * DAY)


# --- 6. the duplicate rule ---------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("sent", ["none", "5.2", "5.7"])
def test_h6_patches_recorded_again_after_a_later_record(pump, r52, r57, on52, tech, today, sent):  # noqa: F811
    """Skeptics' core 3 and 11: patches applied five days ago recorded (version 5.2), then a later record (5.7 today): the same patches
    on the same day recorded again, with no version or any version, is the same install."""
    first = record(pump, choice(PATCHES, reference="ICAR-2026-07", basis=NS), installed_by=InstalledBy.IT, applied_on=today - 5 * DAY,
                   by=tech)
    assert first.release_after == r52
    record_facts(pump, by=None, release=r57)
    release = {"none": None, "5.2": r52, "5.7": r57}[sent]
    assert refused(record, pump, choice(PATCHES, release, reference="icar-2026-07", basis=VL), installed_by=InstalledBy.CE,
                   applied_on=today - 5 * DAY, by=tech) == {"software_outcome": updates.DUPLICATE.format(words=words(first))}
    assert SoftwareUpdate.objects.count() == 1


def test_h6_two_records_of_one_patch_list_under_a_later_record(pump, r52, r57, on52, tech, today):  # noqa: F811
    """Skeptics' core 16: the record names 5.7 since today; patches applied four days ago recorded with no version, then again naming
    5.2: the second is the same install."""
    record_facts(pump, by=None, release=r57)
    first = record(pump, choice(PATCHES, reference="ICAR-2026-09", basis=NS), installed_by=InstalledBy.IT, applied_on=today - 4 * DAY,
                   by=tech)
    assert (first.release_after, first.later_kept) == (None, True)
    assert refused(record, pump, choice(PATCHES, r52, reference="ICAR-2026-09", basis=NS), installed_by=InstalledBy.IT,
                   applied_on=today - 4 * DAY, by=tech) == {"software_outcome": updates.DUPLICATE.format(words=words(first))}


def test_h6_a_reinstall_after_a_rollback_that_day_is_accepted(pump, r52, r56, on52, tech, today):  # noqa: F811
    """Skeptics' core 2 and 15: 5.6 pushed today, rolled back to 5.2 today, then installed again today: accepted, the facts on 5.6;
    the same install recorded once more (the record names 5.6 again) is a duplicate."""
    first = record(pump, choice(INSTALLED, r56, basis=NS), installed_by=InstalledBy.MANUFACTURER, applied_on=today, by=tech)
    rollback = record(pump, choice(INSTALLED, r52, basis=NS), installed_by=InstalledBy.CE, applied_on=today, by=tech)
    assert sec_of(pump)[0] == r52 and words(rollback).startswith(f"5.2 (demo) installed {day(today)} (recorded before: 5.6 (demo))")
    again = record(pump, choice(INSTALLED, r56, basis=NS), installed_by=InstalledBy.CE, applied_on=today, by=tech)
    assert (again.release_before, again.release_after) == (r52, r56)
    assert sec_of(pump)[0] == r56
    assert refused(record, pump, choice(INSTALLED, r56, basis=MR), installed_by=InstalledBy.IT, applied_on=today, by=tech) == {
        "software_outcome": updates.DUPLICATE.format(words=words(first))}
    assert SoftwareUpdate.objects.count() == 3


def test_h6_a_work_orders_patches_are_never_refused_for_a_hand_rows(pump, r52, on52, techs, tech, today):  # noqa: F811
    record(pump, choice(PATCHES, reference="ICAR-2026-09", basis=NS), installed_by=InstalledBy.IT, applied_on=today, by=tech)
    wo, row = update_wo(pump, reference="ICAR-2026-09", assigned_to=techs["tom"])
    complete(wo, tech, choice(PATCHES, reference="ICAR-2026-09", basis=VL))
    row.refresh_from_db()
    assert row.outcome == PATCHES and SoftwareUpdate.objects.filter(outcome=PATCHES).count() == 2


# --- 7. a scoped user learns only RECORD_PERMISSION ---------------------------------------------------------------------------------------

def _imported(asset):
    """An update imported from another system, open, on BD's service (no record)."""
    return wo_services.create_work_order(asset=asset, type=WoType.SOFTWARE_UPDATE, priority=Priority.NORMAL, problem="Firmware 5.6 loaded",
                                         source=Source.IMPORTED, _register_opener=True, vendor_service=True, vendor_name="BD")


def _retired_and_reopened(asset, vendor, kim, target, today):  # noqa: F811
    """BD's register-opened update on `asset`, completed by its account (nothing recorded), the device retired, the work order reopened."""
    wo, _row = update_wo(asset, target, vendor_service=True, vendor_name="BD field service", opened_on=today - 3 * DAY)
    complete(wo, vendor, resolution="Updated per our visit")
    set_status(Asset.objects.get(pk=asset.pk), AssetStatus.RETIRED, by=kim)
    wo_services.change_status(WorkOrder.objects.get(pk=wo.pk), WoStatus.IN_PROGRESS, by=kim)
    return WorkOrder.objects.get(pk=wo.pk)


SENT = [choice(INSTALLED, None, basis=MR), choice(NOT_CONFIRMED), choice(PATCHES, reference="ICAR-2026-09", basis=VL)]


def test_h7_a_scoped_vendor_on_an_imported_update_is_told_only_the_permission(pump, r56, on52, vendor, kim):  # noqa: F811
    wo = _imported(pump)
    for sent in [*SENT, choice(INSTALLED, r56, basis=MR)]:
        assert refused(complete, WorkOrder.objects.get(pk=wo.pk), vendor, sent, "Done") == {"software_outcome": updates.RECORD_PERMISSION}
    # an unscoped user's answers keep their order
    assert refused(complete, WorkOrder.objects.get(pk=wo.pk), kim, choice(NOT_CONFIRMED), "Done") == {
        "software_outcome": updates.NOT_FROM_REGISTER}
    assert WorkOrder.objects.get(pk=wo.pk).status == WoStatus.OPEN


def test_h7_a_scoped_vendor_on_a_retired_device_is_told_only_the_permission(pump, r56, on52, vendor, kim, today):  # noqa: F811
    wo = _retired_and_reopened(pump, vendor, kim, r56, today)
    for sent in SENT:
        assert refused(complete, WorkOrder.objects.get(pk=wo.pk), vendor, sent, "Done") == {"software_outcome": updates.RECORD_PERMISSION}
    assert refused(complete, WorkOrder.objects.get(pk=wo.pk), kim, choice(NOT_CONFIRMED), "Done") == {
        "software_outcome": f"{pump.tag} is retired: its facts stay as they were."}
    assert WorkOrder.objects.get(pk=wo.pk).status == WoStatus.IN_PROGRESS


API_SENT = [{"software_reference": "X-1"}, {"software_outcome": "bogus"}, {"software_outcome": "installed"},
            {"software_outcome": "not_confirmed"}]


def _vendor_account(make_user):
    user = make_user("vendor")
    user.company = "BD"
    user.save(update_fields=["company"])
    return user


def test_h7_the_api_tells_a_scoped_vendor_on_an_imported_update_only_the_permission(client, make_user, world):  # noqa: F811
    wo = _imported(world["devices"]["CE-10006"])
    client.force_login(_vendor_account(make_user))
    for sent in API_SENT:
        r = transition(client, wo, resolution="Done", **sent)
        assert r.status_code == 400 and r.json() == {"software_outcome": [updates.RECORD_PERMISSION]}, (sent, r.content)
    assert WorkOrder.objects.get(pk=wo.pk).status == WoStatus.OPEN


def test_h7_the_api_tells_a_scoped_vendor_on_a_retired_device_only_the_permission(client, make_user, world):  # noqa: F811
    vendor_user = _vendor_account(make_user)
    director = make_user("director")
    wo = _retired_and_reopened(world["devices"]["CE-10005"], vendor_user, director, world["r56"], world["today"])
    client.force_login(vendor_user)
    for sent in API_SENT:
        r = transition(client, wo, resolution="Done", **sent)
        assert r.status_code == 400 and r.json() == {"software_outcome": [updates.RECORD_PERMISSION]}, (sent, r.content)
        assert "retired" not in r.content.decode()
    client.force_login(director)
    r = transition(client, wo, resolution="Done", software_outcome="not_confirmed")
    assert r.json() == {"software_outcome": ["CE-10005 is retired: its facts stay as they were."]}
    assert WorkOrder.objects.get(pk=wo.pk).status == WoStatus.IN_PROGRESS
