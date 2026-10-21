"""
Slice 38 review fixes, round 3 (H1: the services, rules, history, and API serializer), on SQLite and PostgreSQL, as review round 4 left
them (round 4 removed what was recorded before, the judging against it, a hand row's correction taking it, and the kept since: those tests
are gone, and tests/test_cyber_review38_round4.py holds the rules now):

1. History never names what was recorded before: the record's History, the change log (screen, CSV, print), and the API change log,
   after a first completion, a Confirm version, and a hand record.
5. Installed sets the since to the install's day, also when none was recorded or the release changes.
6. The duplicate rule: patches by day and reference (never the version); a work order's outcome is never refused for a hand row's.
7. A scoped user who sends an outcome learns only RECORD_PERMISSION, before whether the work order has a record or its device is on
   site (complete_work_order and the API transition); an unscoped user's answers keep their order.
"""
import csv
import io

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

from apps.core import history
from apps.cybersecurity import updates
from apps.cybersecurity.models import DeviceSecurity, InstalledBy, SoftwareUpdate
from apps.cybersecurity.services import record_facts
from apps.cybersecurity.updates import NOT_CONFIRMED, confirm, record
from apps.equipment.models import Asset, AssetStatus
from apps.equipment.services import set_status
from apps.workorders import services as wo_services
from apps.workorders.models import Priority, Source, WorkOrder, WoStatus, WoType

BEFORE_TAKEN, RECORDED_BEFORE = "Before taken", "Recorded before"


def sec_of(asset) -> tuple:
    sec = DeviceSecurity.objects.get(asset=asset)
    return sec.release, sec.release_since, sec.checked_on


# --- 1. history never names what was recorded before -----------------------------------------------------------------------------------

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
    for row in (completed, confirmed, hand):
        entries, _ = history.record_history(SoftwareUpdate.objects.get(pk=row.pk))
        fields = [c.field for e in entries for c in e.changes]
        assert "What it did" in fields and "Runs after" in fields, fields
        assert BEFORE_TAKEN not in fields and RECORDED_BEFORE not in fields, fields  # review round 4: no such field


def test_h1_the_change_log_csv_and_print_never_name_it(client, pump, r52, r56, on52, job, vendor_job, tech, kim, today):  # noqa: F811
    _recordings(pump, r56, job, vendor_job, tech, today)
    entries, _ = history.change_log(kim, areas=["software_updates"], limit=200)
    fields = [c.field for e in entries for c in e.changes]
    assert "What it did" in fields and BEFORE_TAKEN not in fields and RECORDED_BEFORE not in fields, fields
    client.force_login(kim)
    screen = client.get("/users/log/?area=software_updates").content.decode()
    assert "What it did" in screen and BEFORE_TAKEN not in screen and RECORDED_BEFORE not in screen
    printed = client.get("/users/log/print/?area=software_updates").content.decode()
    assert "What it did" in printed and BEFORE_TAKEN not in printed and RECORDED_BEFORE not in printed
    exported = client.get("/users/log/export.csv?area=software_updates")
    text = b"".join(exported.streaming_content).decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text)))
    assert len(rows) > 3 and "What it did" in text and BEFORE_TAKEN not in text and RECORDED_BEFORE not in text


def test_h1_the_api_change_log_never_names_before_taken(client, pump, r52, r56, on52, job, vendor_job, tech, kim, today):  # noqa: F811
    _recordings(pump, r56, job, vendor_job, tech, today)
    client.force_login(kim)
    r = client.get("/api/v1/change-log/?area=software_updates&limit=200")
    assert r.status_code == 200, r.content
    fields = [c["field"] for e in r.json()["results"] for c in e["changes"]]
    assert "What it did" in fields and BEFORE_TAKEN not in fields and RECORDED_BEFORE not in fields, fields


# --- 5. installed sets the since --------------------------------------------------------------------------------------------------------

def test_h5_a_since_is_set_when_none_is_recorded_or_the_release_changes(pump, r52, r56, tech, today):  # noqa: F811
    from cyber_fixtures import facts

    facts(pump, r56)  # no since, no day checked
    record(pump, choice(INSTALLED, r56, basis=NS), installed_by=InstalledBy.IT, applied_on=today - 4 * DAY, by=tech)
    assert sec_of(pump) == (r56, today - 4 * DAY, today - 4 * DAY)
    record(pump, choice(INSTALLED, r52, basis=NS), installed_by=InstalledBy.IT, applied_on=today - 2 * DAY, by=tech)
    assert sec_of(pump) == (r52, today - 2 * DAY, today - 2 * DAY)


# --- 6. the duplicate rule ---------------------------------------------------------------------------------------------------------------

def test_h6_patches_recorded_again_after_a_later_record(pump, r52, r57, on52, tech, today):  # noqa: F811
    """Skeptics' core 3 and 11: patches applied five days ago recorded (version 5.2), then a later record (5.7 today): the same patches
    on the same day recorded again is the same install."""
    first = record(pump, choice(PATCHES, reference="ICAR-2026-07", basis=NS), installed_by=InstalledBy.IT, applied_on=today - 5 * DAY,
                   by=tech)
    assert first.release_after == r52
    record_facts(pump, by=None, release=r57)
    assert refused(record, pump, choice(PATCHES, reference="icar-2026-07", basis=VL), installed_by=InstalledBy.CE,
                   applied_on=today - 5 * DAY, by=tech) == {"software_outcome": updates.DUPLICATE.format(words=words(first))}
    assert SoftwareUpdate.objects.count() == 1


def test_h6_two_records_of_one_patch_list_under_a_later_record(pump, r52, r57, on52, tech, today):  # noqa: F811
    """Skeptics' core 16: the record names 5.7 since today; patches applied four days ago recorded with no version, then again: the
    second is the same install."""
    record_facts(pump, by=None, release=r57)
    first = record(pump, choice(PATCHES, reference="ICAR-2026-09", basis=NS), installed_by=InstalledBy.IT, applied_on=today - 4 * DAY,
                   by=tech)
    assert (first.release_after, first.later_kept) == (None, True)
    assert refused(record, pump, choice(PATCHES, reference="ICAR-2026-09", basis=NS), installed_by=InstalledBy.IT,
                   applied_on=today - 4 * DAY, by=tech) == {"software_outcome": updates.DUPLICATE.format(words=words(first))}


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
