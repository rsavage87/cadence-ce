"""
Slice 38 review fixes, round 4 (I1: the services, rules, model, history, and API), on SQLite and PostgreSQL. Round 4 simplifies by
deleting rules: an update's record states only what the person recording it says the update did. Cadence never asks what the device ran
before (the device's own History shows every change of its release with its reason), and no outcome is judged against the device's
record.

1. "Recorded before" is gone: no field, no column (the generated migration), no history label, no API key, no words.
2. No outcome is judged against the record: installed names a release of the model; already names the release it ran, the target on a
   release target (with none sent it takes the target; a patches target needs one).
3. Patches never take a release from the person, at every door (a completion, the API's transition, Confirm version, Record update),
   the recorded release included: PATCHES_NO_RELEASE. They run the device's recorded release when the facts are written, else none.
4. The facts written: installed X, X since the day (always); patches, the day checked; already X, the day checked, and X since the day
   when the record does not name X.
5. The words: "X installed <day>"; "Patches applied <day>, version unchanged (X)", "(version not recorded)", or no version with a later
   record kept; "Checked <day>: already on X; nothing installed".
6. The duplicate hand record: installed by release and day, patches by reference and day, nothing about the record (a rollback then a
   reinstall the same day is refused as the duplicate it matches).
7. Each scenario of skeptics38c gives a truthful result or a plain refusal (PA, PA2, PA3, PB, PB2, PC, PD, PE, PF, PF2, N2, N3b, N6, N8,
   D, F, J, the rollback).
"""
import pytest
import test_cyber_review38_services as base
from cyber_fixtures import facts, release, update_wo
from django.db import connection
from test_cyber_review38_services import ALREADY, DAY, INSTALLED, LATER, MR, NS, PATCHES, VL, choice, complete, day, refused, words
from test_cyber_updates_api import transition

from apps.api import serializers_cybersecurity as scs
from apps.core import history
from apps.cybersecurity import updates
from apps.cybersecurity.models import DeviceSecurity, InstalledBy, SoftwareUpdate
from apps.cybersecurity.services import record_facts
from apps.cybersecurity.updates import confirm, record, remove
from apps.equipment import services as eq
from apps.equipment.models import Asset, DeviceModel
from apps.workorders import services as wo_services
from apps.workorders.models import WorkOrder, WoStatus

# The services review's fixtures, as this module's (assigned, never imported by name: a test's argument of the same name is the fixture)
today, kim, tech, manager, vendor = base.today, base.kim, base.tech, base.manager, base.vendor
r52, r56, r57, on52, job, vendor_job = base.r52, base.r56, base.r57, base.on52, base.job, base.vendor_job

NO_RELEASE = {"software_release": updates.PATCHES_NO_RELEASE}
IT, CE, MAKER = InstalledBy.IT, InstalledBy.CE, InstalledBy.MANUFACTURER


def sec_of(asset) -> tuple:
    sec = DeviceSecurity.objects.get(asset=asset)
    return sec.release, sec.release_since, sec.checked_on, sec.checked_by


def last_reason(asset) -> str:
    sec = DeviceSecurity.objects.get(asset=asset)
    return DeviceSecurity.history.filter(id=sec.pk).order_by("history_date", "history_id").last().history_change_reason


def hand(asset, outcome, release=None, *, on, by, installed_by=IT, reference="", basis=NS):
    return record(asset, choice(outcome, release, reference=reference, basis=basis), installed_by=installed_by, applied_on=on, by=by)


def reopen(wo, by):
    wo_services.change_status(WorkOrder.objects.get(pk=wo.pk), WoStatus.IN_PROGRESS, by=by)
    return WorkOrder.objects.get(pk=wo.pk)


# --- 1. "recorded before" is gone ----------------------------------------------------------------------------------------------------

def test_r1_no_release_before_column_field_label_key_or_words(pump, r52, r56, on52, job, tech, today):
    for model in (SoftwareUpdate, SoftwareUpdate.history.model):
        with connection.cursor() as cursor:
            columns = {c.name for c in connection.introspection.get_table_description(cursor, model._meta.db_table)}
        assert "release_after_id" in columns and not {"release_before_id", "before_taken"} & columns, columns
        assert not {"release_before", "before_taken"} & {f.name for f in model._meta.get_fields()}
    assert "release_before" not in history.LABELS["cybersecurity.softwareupdate"]
    assert "release_before" not in history.EMPTY_WORDS["cybersecurity.softwareupdate"]
    assert history.HIDE["cybersecurity.softwareupdate"] == {"asset", "work_order"}
    wo, row = job
    complete(wo, tech, choice(INSTALLED, r56, basis=MR))
    data = scs.update_data(SoftwareUpdate.objects.select_related("asset", "work_order", "target_release", "release_after",
                                                                 "recorded_by").get(pk=row.pk))
    assert "release_before" not in data and data["release_after"] == {"id": str(r56.pk), "version": "5.6 (demo)"}
    assert words(row) == data["words"] == f"5.6 (demo) installed {day(today)}"


# --- 2, 3. no judging against the record; patches never take a release ---------------------------------------------------------------

@pytest.mark.parametrize("named", ["the recorded release", "another release"])
def test_r3_patches_naming_a_release_are_refused_at_every_door(client, pump, r52, r56, on52, techs, tech, vendor_job, today, named):
    sent = r52 if named == "the recorded release" else r56
    wo, row = update_wo(pump, reference="ICAR-2026-09", assigned_to=techs["tom"])
    assert refused(complete, wo, tech, choice(PATCHES, sent, reference="ICAR-2026-09", basis=VL), "Patched") == NO_RELEASE
    client.force_login(tech)
    api = transition(client, wo, software_outcome="patches", software_release=str(sent.pk), software_reference="ICAR-2026-09",
                     software_basis="validated_list", resolution="Patched")
    assert api.status_code == 400 and api.json() == {"software_release": [updates.PATCHES_NO_RELEASE]}, api.content
    _vwo, vrow = vendor_job
    assert refused(confirm, vrow, choice(PATCHES, sent, reference="ICAR-2026-09", basis=VL), by=tech) == NO_RELEASE
    assert refused(hand, pump, PATCHES, sent, reference="ICAR-2026-09", on=today, by=tech) == NO_RELEASE
    assert WorkOrder.objects.get(pk=wo.pk).status == WoStatus.OPEN and SoftwareUpdate.objects.exclude(outcome="").count() == 0
    # with none named, each door records the version the device's record names (the vendor's day first: the facts never go backwards)
    confirm(vrow, choice(PATCHES, reference="ICAR-2026-09", basis=VL), by=tech)
    done = transition(client, wo, software_outcome="patches", software_reference="ICAR-2026-09", software_basis="validated_list")
    assert done.status_code == 200, done.content
    by_hand = hand(pump, PATCHES, reference="ICAR-2026-10", on=today, by=tech)
    rows = SoftwareUpdate.objects.filter(pk__in=[row.pk, vrow.pk, by_hand.pk])
    assert {r.pk: (r.outcome, r.release_after, r.later_kept) for r in rows} == {
        pk: (PATCHES, r52, False) for pk in (row.pk, vrow.pk, by_hand.pk)}


def test_r2_already_takes_the_target_and_needs_a_release_on_a_patches_target(pump, r52, r56, on52, job, techs, tech, today):
    """The record says 5.2; the person checked and found 5.6, the target: Already with none sent is already on 5.6 (never refused for the
    record's 5.2), the record moving to 5.6 since the day (found then). A patches target needs a release; naming one is enough."""
    wo, row = job
    complete(wo, tech, choice(ALREADY))
    row.refresh_from_db()
    assert (row.outcome, row.release_after, row.basis, row.later_kept) == (ALREADY, r56, "", False)
    assert sec_of(pump) == (r56, today, today, tech)
    assert last_reason(pump) == f"Facts recorded at {wo.number}: release set to 5.6 (demo)"
    assert words(row) == f"Checked {day(today)}: already on 5.6 (demo); nothing installed"
    patches, prow = update_wo(pump, reference="ICAR-2026-09", assigned_to=techs["tom"])
    assert refused(complete, patches, tech, choice(ALREADY), "Checked") == {"software_release": updates.CHOOSE_RUNS_NOW}
    complete(patches, tech, choice(ALREADY, r52), "Checked")  # the person found 5.2: the record says so from today
    prow.refresh_from_db()
    assert prow.release_after == r52 and sec_of(pump)[:2] == (r52, today)


def test_r2_installed_is_never_refused_for_the_record(pump, r52, r56, on52, job, tech, today):
    wo, row = job
    complete(wo, tech, choice(INSTALLED, r52, basis=MR))  # what the person states, the record's release too
    row.refresh_from_db()
    assert row.release_after == r52 and words(row) == f"5.2 (demo) installed {day(today)}"


# --- 4, 5. the facts and the words of every outcome ---------------------------------------------------------------------------------

def test_r4_installed_sets_the_since_to_the_install_day_always(pump, r56, techs, tech, kim, today):
    """The record named 5.6 since ten days ago: an install of 5.6 recorded four days ago moves the since to that day (round 3's kept
    since is gone), and a completion today moves it to today."""
    facts(pump, r56, release_since=today - 10 * DAY, checked_on=today - 10 * DAY)
    by_hand = hand(pump, INSTALLED, r56, on=today - 4 * DAY, by=tech)
    assert sec_of(pump) == (r56, today - 4 * DAY, today - 4 * DAY, tech)
    assert last_reason(pump) == updates.FACTS_BY_HAND  # the release did not change
    assert words(by_hand) == f"5.6 (demo) installed {day(today - 4 * DAY)}; installed by information technology"
    wo, row = update_wo(pump, r56, assigned_to=techs["tom"], opened_on=today - 3 * DAY)
    wo_services.change_status(wo, WoStatus.IN_PROGRESS, by=kim, as_of=today - DAY)
    record_facts(pump, by=None, release=r56, release_since=today - DAY, checked_on=today - DAY)
    complete(wo, tech, choice(INSTALLED, r56, basis=MR))
    assert sec_of(pump) == (r56, today, today, tech)


def test_r4_confirm_installed_sets_the_since_to_the_jobs_day(pump, r52, r56, on52, vendor_job, tech, today):
    _wo, row = vendor_job  # completed five days ago
    record_facts(pump, by=None, release=r56, release_since=today - 6 * DAY, checked_on=today - 6 * DAY)
    confirm(row, choice(INSTALLED, r56, basis=MR), by=tech)
    assert sec_of(pump) == (r56, today - 5 * DAY, today - 5 * DAY, tech)


def test_r4_patches_write_the_day_checked_only(pump, r52, on52, techs, tech, today):
    wo, row = update_wo(pump, reference="ICAR-2026-09", assigned_to=techs["tom"])
    complete(wo, tech, choice(PATCHES, reference="ICAR-2026-09", basis=VL))
    row.refresh_from_db()
    assert (row.release_after, row.later_kept) == (r52, False)
    assert sec_of(pump) == (r52, today - 30 * DAY, today, tech) and last_reason(pump) == f"Facts recorded at {wo.number}"
    assert words(row) == (f"Patches on the manufacturer's validated patch list applied {day(today)}, version unchanged (5.2 (demo)); "
                          "ref ICAR-2026-09")


def test_r4_already_on_the_recorded_release_writes_the_day_checked_only(pump, r56, techs, tech, today):
    facts(pump, r56, release_since=today - 9 * DAY, checked_on=today - 9 * DAY)
    wo, row = update_wo(pump, r56, assigned_to=techs["tom"])
    complete(wo, tech, choice(ALREADY, r56))
    assert sec_of(pump) == (r56, today - 9 * DAY, today, tech) and last_reason(pump) == f"Facts recorded at {wo.number}"
    assert words(row) == f"Checked {day(today)}: already on 5.6 (demo); nothing installed"


def test_r5_patches_words_by_what_the_record_says(pump, r52, r57, tech, today):
    """No release recorded: "version not recorded"; one recorded and the facts written: that one; a later record kept: no version."""
    first = hand(pump, PATCHES, reference="ICAR-2026-01", on=today - 5 * DAY, by=tech, installed_by=CE)
    assert (first.release_after, first.later_kept) == (None, False)
    assert words(first) == (f"Patches applied {day(today - 5 * DAY)}, version unchanged (version not recorded); ref ICAR-2026-01; "
                            "installed by clinical engineering")
    record_facts(pump, by=None, release=r52, release_since=today - 4 * DAY, checked_on=today - 4 * DAY)
    second = hand(pump, PATCHES, reference="ICAR-2026-02", on=today - 3 * DAY, by=tech, installed_by=CE)
    assert words(second) == (f"Patches applied {day(today - 3 * DAY)}, version unchanged (5.2 (demo)); ref ICAR-2026-02; "
                             "installed by clinical engineering")
    record_facts(pump, by=None, release=r57)  # since today
    third = hand(pump, PATCHES, reference="ICAR-2026-03", on=today - 2 * DAY, by=tech, installed_by=CE)
    assert (third.release_after, third.later_kept) == (None, True)
    assert words(third) == (f"Patches applied {day(today - 2 * DAY)}, version unchanged; ref ICAR-2026-03; installed by clinical "
                            f"engineering{LATER}")


# --- 6. the duplicate hand record ---------------------------------------------------------------------------------------------------

def test_pb_a_backdated_install_under_a_later_record_recorded_twice(pump, r52, r56, r57, on52, tech, today):
    record_facts(pump, by=None, release=r57)  # since today
    first = hand(pump, INSTALLED, r56, on=today - 5 * DAY, by=tech)
    assert first.later_kept
    assert refused(hand, pump, INSTALLED, r56, on=today - 5 * DAY, by=tech, basis=MR) == {
        "software_outcome": updates.DUPLICATE.format(words=words(first))}
    assert SoftwareUpdate.objects.count() == 1


def test_pb2_the_same_past_install_again_after_the_record_moved_on(pump, r52, r56, r57, on52, tech, today):
    first = hand(pump, INSTALLED, r56, on=today - 5 * DAY, by=tech, installed_by=MAKER)
    hand(pump, INSTALLED, r57, on=today - DAY, by=tech, installed_by=MAKER)
    assert refused(hand, pump, INSTALLED, r56, on=today - 5 * DAY, by=tech, installed_by=CE) == {
        "software_outcome": updates.DUPLICATE.format(words=words(first))}
    assert SoftwareUpdate.objects.count() == 2


def test_n3b_a_hand_repeat_of_a_work_orders_install_after_the_record_moved_on(pump, r52, r56, r57, on52, techs, tech, today):
    wo, row = update_wo(pump, r56, assigned_to=techs["tom"], opened_on=today - 6 * DAY)
    complete(wo, tech, choice(INSTALLED, r56, basis=MR), today=today - 4 * DAY)
    record_facts(pump, by=None, release=r57, release_since=today - DAY, checked_on=today - DAY)
    assert refused(hand, pump, INSTALLED, r56, on=today - 4 * DAY, by=tech, installed_by=CE) == {
        "software_outcome": updates.DUPLICATE.format(words=words(row))}


def test_a_reinstall_after_a_rollback_the_same_day_is_refused_as_its_duplicate(pump, r52, r56, on52, tech, today):
    """Accepted by round 4: the same install on the same day is the same record; Record facts records the device's release then."""
    first = hand(pump, INSTALLED, r56, on=today, by=tech, installed_by=MAKER)
    hand(pump, INSTALLED, r52, on=today, by=tech, installed_by=CE)
    assert sec_of(pump)[0] == r52
    assert refused(hand, pump, INSTALLED, r56, on=today, by=tech, installed_by=CE) == {
        "software_outcome": updates.DUPLICATE.format(words=words(first))}
    record_facts(pump, by=None, release=r56)
    assert sec_of(pump)[0] == r56


def test_a_work_orders_install_is_never_refused_for_a_hand_rows(pump, r52, r56, on52, job, tech, today):
    by_hand = hand(pump, INSTALLED, r56, on=today, by=tech)
    wo, row = job
    complete(wo, tech, choice(INSTALLED, r56, basis=MR))
    row.refresh_from_db()
    assert row.outcome == INSTALLED and by_hand.removed_on is None and SoftwareUpdate.objects.filter(outcome=INSTALLED).count() == 2


# --- 7. skeptics38c's scenarios -------------------------------------------------------------------------------------------------------

def test_pa3_a_hand_row_made_redundant_by_the_job_then_entered_in_error(pump, r52, r56, r57, on52, techs, tech, kim, manager, today):
    """The 5.6 job started yesterday and installed yesterday; IT also recorded it by hand; the job completed today; the hand row marked
    entered in error as the job's duplicate. Later records say what they did: patches naming 5.6 a plain refusal, with none the version
    the record names (5.6); an install of 5.7 never names a before."""
    wo, _row = update_wo(pump, r56, assigned_to=techs["tom"], opened_on=today - 3 * DAY)
    wo_services.change_status(wo, WoStatus.IN_PROGRESS, by=kim, as_of=today - DAY)
    by_hand = hand(pump, INSTALLED, r56, on=today - DAY, by=tech, basis=MR)
    complete(wo, tech, choice(INSTALLED, r56, basis=MR))
    remove(by_hand, by=manager)
    assert sec_of(pump)[:2] == (r56, today)
    assert refused(hand, pump, PATCHES, r56, reference="ICAR-2026-10", on=today, by=tech) == NO_RELEASE
    patched = hand(pump, PATCHES, reference="ICAR-2026-10", on=today, by=tech)
    assert words(patched) == (f"Patches applied {day(today)}, version unchanged (5.6 (demo)); ref ICAR-2026-10; installed by information "
                              "technology")
    newer = hand(pump, INSTALLED, r57, on=today, by=tech, basis=MR)
    assert words(newer) == f"5.7 (demo) installed {day(today)}; installed by information technology"
    assert sec_of(pump)[:2] == (r57, today)


def test_pa_an_installer_correction_then_later_hand_records(pump, r52, r56, r57, on52, tech, manager, today):
    on = today - 5 * DAY
    wrong = hand(pump, INSTALLED, r56, on=on, by=tech)
    remove(wrong, by=manager)
    right = hand(pump, INSTALLED, r56, on=on, by=tech, installed_by=MAKER)
    assert words(right) == f"5.6 (demo) installed {day(on)}; installed by the manufacturer remotely"
    patched = hand(pump, PATCHES, reference="ICAR-2026-09", on=today - DAY, by=tech)
    assert patched.release_after == r56 and words(patched).startswith(f"Patches applied {day(today - DAY)}, version unchanged (5.6 (demo))")
    newer = hand(pump, INSTALLED, r57, on=today, by=tech)
    assert words(newer) == f"5.7 (demo) installed {day(today)}; installed by information technology"


@pytest.mark.parametrize("corrected", [False, True], ids=["record as left", "record corrected"])
def test_pa2_a_hand_row_entered_in_error_and_never_recorded_again(pump, r52, r56, r57, on52, techs, tech, manager, today, corrected):
    """Nothing was installed: the hand row is entered in error, the record left on 5.6 (the toast: correct it with Record facts). A job's
    patches run the version the record names, 5.6 as left or 5.2 once corrected; a later install names only what was installed."""
    wrong = hand(pump, INSTALLED, r56, on=today - 3 * DAY, by=tech)
    remove(wrong, by=manager)
    if corrected:
        record_facts(pump, by=None, release=r52, release_since=today - 30 * DAY, checked_on=today - 3 * DAY)
    wo, row = update_wo(pump, reference="ICAR-2026-09", assigned_to=techs["tom"])
    complete(wo, tech, choice(PATCHES, reference="ICAR-2026-09", basis=VL))
    row.refresh_from_db()
    assert row.release_after == (r52 if corrected else r56) == sec_of(pump)[0]  # the row and the record agree
    newer = hand(pump, INSTALLED, r57, on=today, by=tech)
    assert words(newer) == f"5.7 (demo) installed {day(today)}; installed by information technology"


@pytest.mark.parametrize("first", [PATCHES, INSTALLED], ids=["PE: patches first", "F: installed 5.6 by mistake first"])
def test_pe_f_a_re_completion_as_patches_after_a_later_install(pump, r52, r56, r57, on52, techs, tech, kim, today, first):
    """A patches job completed three days ago (as patches, or as installed 5.6 by mistake); 5.7 installed and recorded by hand yesterday;
    the job reopened and completed again as patches: naming 5.7 a plain refusal, with none the version the record names (5.7), the row
    and the facts agreeing."""
    wo, row = update_wo(pump, reference="ICAR-2026-09", assigned_to=techs["tom"], opened_on=today - 5 * DAY)
    sent = choice(PATCHES, reference="ICAR-2026-09", basis=VL) if first == PATCHES else choice(INSTALLED, r56, basis=MR)
    complete(wo, tech, sent, today=today - 3 * DAY)
    hand(pump, INSTALLED, r57, on=today - DAY, by=tech, installed_by=MAKER)
    wo = reopen(wo, kim)
    assert refused(complete, wo, tech, choice(PATCHES, r57, reference="ICAR-2026-09", basis=VL), "Patched again") == NO_RELEASE
    complete(wo, tech, choice(PATCHES, reference="ICAR-2026-09", basis=VL), "Patched again")
    row.refresh_from_db()
    assert (row.release_after, row.applied_on) == (r57, today)
    assert words(row) == (f"Patches on the manufacturer's validated patch list applied {day(today)}, version unchanged (5.7 (demo)); "
                          "ref ICAR-2026-09")
    assert sec_of(pump)[:3] == (r57, today - DAY, today)


def test_n6_a_re_completion_after_a_hand_install_the_same_day(pump, r52, r56, on52, techs, tech, kim, today):
    wo, row = update_wo(pump, reference="ICAR-2026-09", assigned_to=techs["tom"])
    complete(wo, tech, choice(PATCHES, reference="ICAR-2026-09", basis=VL))
    wo = reopen(wo, kim)
    hand(pump, INSTALLED, r56, on=today, by=tech)
    assert refused(complete, wo, tech, choice(PATCHES, r56, reference="ICAR-2026-09", basis=VL), "Patched") == NO_RELEASE
    complete(wo, tech, choice(PATCHES, reference="ICAR-2026-09", basis=VL), "Patched")
    row.refresh_from_db()
    assert row.release_after == r56


@pytest.mark.parametrize("facts_first", [False, True], ids=["no Record facts", "Record facts first"])
@pytest.mark.parametrize("door", ["PD: Confirm version", "D: a re-completion"])
def test_pd_d_a_stale_record_and_a_mistaken_install_then_already(pump, r52, r56, on52, job, tech, kim, manager, today, door, facts_first):
    """The record said 5.2, but the pump ran 5.6 already: completed as installed 5.6 out of habit, then corrected to Already on the
    update (Entered in error and Confirm version, or a reopen and a re-completion), with or without Record facts first: accepted, the
    row and the record agreeing; another release than the target is a plain refusal."""
    wo, row = job
    complete(wo, tech, choice(INSTALLED, r56, basis=MR))
    if door.startswith("PD"):
        remove(row, by=manager)
    else:
        wo = reopen(wo, kim)
    if facts_first:
        record_facts(pump, by=None, release=r56, release_since=today - 20 * DAY)  # checked today
    if door.startswith("PD"):
        assert refused(confirm, row, choice(ALREADY, r52), by=tech) == {
            "software_release": updates.NOT_THE_TARGET_DONE.format(version="5.2 (demo)", target="5.6 (demo)")}
        confirm(row, choice(ALREADY, r56), by=tech)
    else:
        assert refused(complete, wo, tech, choice(ALREADY, r52), "Checked") == {
            "software_release": updates.NOT_THE_TARGET.format(version="5.2 (demo)", target="5.6 (demo)")}
        complete(wo, tech, choice(ALREADY), "Checked")  # none sent: the target
    row.refresh_from_db()
    assert (row.outcome, row.release_after, row.later_kept) == (ALREADY, r56, False)
    assert words(row) == f"Checked {day(today)}: already on 5.6 (demo); nothing installed"
    assert sec_of(pump)[0] == r56


@pytest.mark.parametrize("corrected", [False, True], ids=["record as left", "Record facts cleared it"])
@pytest.mark.parametrize("door", ["PC: Confirm version", "PC2: a re-completion", "PC3: by hand"])
def test_pc_no_release_recorded_a_mistaken_install_corrected_to_patches(pump, r52, r56, techs, tech, kim, manager, today, door,
                                                                         corrected):
    """No release recorded; installed 5.6 by mistake (the record: 5.6 since then), corrected to patches: naming 5.2 is a plain refusal;
    with none, the row runs what the record names: 5.6 as the error left it, "version not recorded" once Record facts cleared it."""
    done_on = today - 3 * DAY if door.startswith("PC:") else today
    ref = "ICAR-2026-09"
    if door.startswith("PC3"):
        wrong = hand(pump, INSTALLED, r56, on=done_on, by=tech)
        remove(wrong, by=manager)
    else:
        wo, row = update_wo(pump, reference=ref, assigned_to=techs["tom"], opened_on=today - 5 * DAY)
        complete(wo, tech, choice(INSTALLED, r56, basis=MR), today=done_on)
        if door.startswith("PC:"):
            remove(row, by=manager)
        else:
            wo = reopen(wo, kim)
    if corrected:
        record_facts(pump, by=None, release=None, checked_on=done_on)
    patches = choice(PATCHES, reference=ref, basis=VL)
    if door.startswith("PC:"):
        assert refused(confirm, row, choice(PATCHES, r52, reference=ref, basis=VL), by=tech) == NO_RELEASE
        confirm(row, patches, by=tech)
    elif door.startswith("PC2"):
        assert refused(complete, wo, tech, choice(PATCHES, r52, reference=ref, basis=VL), "Patched") == NO_RELEASE
        complete(wo, tech, patches, "Patched")
    else:
        assert refused(hand, pump, PATCHES, r52, reference=ref, on=done_on, by=tech) == NO_RELEASE
        row = hand(pump, PATCHES, reference=ref, on=done_on, by=tech, basis=VL)
    row.refresh_from_db()
    assert (row.outcome, row.later_kept, row.release_after) == (PATCHES, False, None if corrected else r56)
    assert row.release_after == sec_of(pump)[0]  # the row and the record agree
    version = "version not recorded" if corrected else "5.6 (demo)"
    assert words(row).startswith(f"Patches on the manufacturer's validated patch list applied {day(done_on)}, version unchanged "
                                 f"({version}); ref {ref}")


@pytest.mark.parametrize("door", ["PF: by hand", "PF2: a work order"])
def test_pf_a_real_install_after_one_entered_in_error_moves_the_since(pump, r52, r56, on52, techs, tech, manager, today, door):
    if door.startswith("PF:"):
        wrong = hand(pump, INSTALLED, r56, on=today - 10 * DAY, by=tech)
        remove(wrong, by=manager)
        right = hand(pump, INSTALLED, r56, on=today, by=tech)
        assert words(right) == f"5.6 (demo) installed {day(today)}; installed by information technology"
    else:
        first, first_row = update_wo(pump, r56, assigned_to=techs["tom"], opened_on=today - 12 * DAY)
        complete(first, tech, choice(INSTALLED, r56, basis=MR), today=today - 10 * DAY)
        remove(first_row, by=manager)
        second, second_row = update_wo(pump, r56, assigned_to=techs["tom"])
        complete(second, tech, choice(INSTALLED, r56, basis=MR))
        assert words(second_row) == f"5.6 (demo) installed {day(today)}"
    assert sec_of(pump)[:3] == (r56, today, today)


def test_n2_a_hand_row_on_the_wrong_day_corrected_to_the_right_one(pump, r52, r56, on52, tech, manager, today):
    wrong = hand(pump, INSTALLED, r56, on=today - 2 * DAY, by=tech)
    remove(wrong, by=manager)
    right = hand(pump, INSTALLED, r56, on=today - DAY, by=tech)
    assert sec_of(pump)[:2] == (r56, today - DAY)
    assert words(right) == f"5.6 (demo) installed {day(today - DAY)}; installed by information technology"


@pytest.mark.parametrize("door", ["N8b: a re-completion", "N8d: Confirm version"])
def test_n8_a_model_change_in_between(pump, r52, on52, techs, tech, kim, manager, vendor, today, door):
    """The pump's model corrected (its record cleared) between a recording and its correction: patches run no release of the old model,
    and Already names a release of the new one."""
    other = DeviceModel.objects.create(manufacturer="BD", model="Alaris 8100", description="Pump", category="Infusion pumps")
    r91 = release(other, "9.1 (demo)")
    if door.startswith("N8b"):
        wo, row = update_wo(pump, reference="ICAR-2026-09", assigned_to=techs["tom"])
        complete(wo, tech, choice(PATCHES, reference="ICAR-2026-09", basis=VL))
        wo = reopen(wo, kim)
    else:
        wo, row = update_wo(pump, reference="ICAR-2026-09", vendor_service=True, vendor_name="BD field service", opened_on=today - 10 * DAY)
        complete(wo, vendor, resolution="Patched per our visit", today=today - 5 * DAY)
        confirm(row, choice(PATCHES, reference="ICAR-2026-09", basis=VL), by=tech)
        remove(row, by=manager)
    row.refresh_from_db()
    eq.update_asset(Asset.objects.get(pk=pump.pk), by=None, device_model=other)
    assert sec_of(pump)[0] is None
    patches = choice(PATCHES, reference="ICAR-2026-09", basis=VL)
    if door.startswith("N8b"):
        assert refused(complete, wo, tech, choice(PATCHES, r91, reference="ICAR-2026-09", basis=VL), "Patched") == NO_RELEASE
        complete(wo, tech, patches, "Patched")
    else:
        assert refused(confirm, row, choice(PATCHES, r91, reference="ICAR-2026-09", basis=VL), by=tech) == NO_RELEASE
        confirm(row, patches, by=tech)
    row.refresh_from_db()
    assert row.release_after is None and "5.2" not in words(row) and "version not recorded" in words(row)
    if door.startswith("N8b"):
        wo = reopen(wo, kim)
        complete(wo, tech, choice(ALREADY, r91), "Checked")
        row.refresh_from_db()
        assert row.release_after == r91 and sec_of(pump)[:2] == (r91, today)


def test_j_a_back_filled_install_names_only_what_was_installed(pump, pump_model, r52, r56, on52, tech, manager, today):
    r50 = release(pump_model, "5.0 (demo)")
    wrong = hand(pump, INSTALLED, r56, on=today - 5 * DAY, by=tech)
    remove(wrong, by=manager)
    hand(pump, INSTALLED, r56, on=today - 5 * DAY, by=tech, installed_by=MAKER)
    back = hand(pump, INSTALLED, r50, on=today - 200 * DAY, by=tech, installed_by=CE)
    assert back.later_kept
    assert words(back) == f"5.0 (demo) installed {day(today - 200 * DAY)}; installed by clinical engineering{LATER}"
