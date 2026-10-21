"""
AEM's pull-in against an incident's Recorded in error, on PostgreSQL (a slice 37 check's finding): both write several devices' work, so
both take every device's open work orders first, in one number order (workorders.services.lock_devices_work, incidents.services.
_lock_device_work), then the devices in the database's tag order. The pull-in used to sweep one device's work and row after another's in
tag order: with the lower-tagged device holding the higher-numbered work order, it held CE-1's work and row while Recorded in error held
CE-2's lower number and waited for CE-1's, and the pull-in then waited for CE-2's: PostgreSQL ended one of them ("deadlock detected", a
500 on the incident and AEM screens and the API). Now they take turns, in either order.
"""
from datetime import timedelta

import pytest
from django.utils import timezone
from pg_helpers import needs_postgres
from test_incidents_review import pause_at, race

from apps.equipment import services as eq
from apps.equipment.models import Asset, DeviceModel
from apps.incidents import services as inc
from apps.incidents.models import Affected, Incident, Outcome, Status
from apps.pm import aem
from apps.pm.dates import add_months
from apps.workorders import services as wo_services
from apps.workorders.models import Priority, WorkOrder, WoStatus, WoType
from apps.workorders.services import create_work_order, lock_devices_work, lock_work

DAY = timedelta(days=1)


@pytest.fixture
def world(ctx, dept, pump_model):
    """Two pumps of one model on an 18-month AEM interval (the OEM's is 12), each due a PM 18 months after its last one, so ending the
    AEM brings both in. An incident recorded on CE-2 opens its investigation first (the lower number); a repair on CE-1 comes after (the
    higher number), and CE-1 is then held by the same incident: the lower-tagged device holds the higher-numbered work order."""
    today = timezone.localdate()
    last = today - 400 * DAY
    one, two = (Asset.objects.create(tag=tag, device_model=pump_model, department=dept, last_pm_on=last, next_pm_on=add_months(last, 18))
                for tag in ("CE-1", "CE-2"))
    incident = inc.record_incident(asset=two, outcome=Outcome.UNKNOWN, affected=Affected.PATIENT)
    repair = create_work_order(asset=one, type=WoType.REPAIR, priority=Priority.NORMAL, problem="Alarm", opened_on=today - DAY)
    inc.hold_device(incident, one)
    assert incident.work_order.number < repair.number
    assert [a.tag for a, _due in aem.pull_in_plan(pump_model, pump_model.oem_pm_interval_months, today)] == ["CE-1", "CE-2"]
    return {"one": one, "two": two, "incident": incident, "repair": repair, "model": pump_model, "today": today}


def _end(world):
    return lambda: aem.end(world["model"], reason="Back to the OEM interval")


def _in_error(world):
    return lambda: inc.recorded_in_error(Incident.objects.get(pk=world["incident"].pk))


def _both_done(world) -> None:
    today = world["today"]
    assert Incident.objects.get(pk=world["incident"].pk).status == Status.IN_ERROR
    assert not Asset.objects.filter(pk__in=[world["one"].pk, world["two"].pk], incident_hold=True).exists()
    assert set(Asset.objects.filter(pk__in=[world["one"].pk, world["two"].pk]).values_list("next_pm_on", flat=True)) == {today}
    world["model"].refresh_from_db()
    assert world["model"].aem_interval_months is None
    assert WorkOrder.objects.get(pk=world["incident"].work_order_id).status == WoStatus.CANCELLED


@needs_postgres
@pytest.mark.django_db(transaction=True)
def test_the_pull_in_first_then_recorded_in_error(tenant, world, monkeypatch):
    """The probe's order: the pull-in stops after its first device (CE-1: its work and row held); Recorded in error waits for the work
    orders the pull-in took first, never holding CE-2's lower number while the pull-in wants it. Both finish."""
    pause = pause_at(monkeypatch, eq, "update_asset", after=True)
    errors = race(tenant, pause, _end(world), _in_error(world))
    assert not errors, errors
    _both_done(world)


@needs_postgres
@pytest.mark.django_db(transaction=True)
def test_recorded_in_error_first_then_the_pull_in(tenant, world, monkeypatch):
    """The other order: Recorded in error stops once it holds every held device's work orders; the pull-in waits for them before any
    device's row. Both finish."""
    pause = pause_at(monkeypatch, inc, "_lock_device_work", after=True)
    errors = race(tenant, pause, _in_error(world), _end(world))
    assert not errors, errors
    _both_done(world)


@pytest.mark.django_db
def test_the_pull_in_takes_every_planned_devices_work_before_any_device(world, monkeypatch):
    """Both databases: the pull-in's first lock of work orders names every device of the model in use, before update_asset touches the
    first."""
    calls = []
    real_many, real_one = lock_devices_work, lock_work
    monkeypatch.setattr(wo_services, "lock_devices_work", lambda ids, *also: (calls.append(("many", sorted(ids))), real_many(ids, *also))[1])
    monkeypatch.setattr(wo_services, "lock_work", lambda asset_id, *also: (calls.append(("one", asset_id)), real_one(asset_id, *also))[1])
    assert aem.end(world["model"], reason="Back to the OEM interval") == 2
    assert calls[0] == ("many", sorted([world["one"].pk, world["two"].pk]))
    assert [c for c in calls if c[0] == "one"][:2] == [("one", world["one"].pk), ("one", world["two"].pk)]  # then each, in tag order


# --- the other writers of several devices' work: each takes it all first, in number order (lock_devices_work) ------------------------

def _pumps_on_aem(dept, model, today, *, next_pm_on=None):
    """CE-1 and CE-2 of `model` (18-month AEM, 12-month OEM), last PM 400 days ago: both in the pull-in's plan."""
    last = today - 400 * DAY
    return [Asset.objects.create(tag=tag, device_model=model, department=dept, last_pm_on=last,
                                 next_pm_on=next_pm_on or add_months(last, 18)) for tag in ("CE-1", "CE-2")]


def _repair(asset):
    return create_work_order(asset=asset, type=WoType.REPAIR, priority=Priority.NORMAL, problem="Alarm")


@needs_postgres
@pytest.mark.django_db(transaction=True)
def test_a_devices_import_chunk_and_the_pull_in_take_turns(tenant, ctx, dept, pump_model, make_user, monkeypatch):
    """A devices import (Settings, Import data) changes CE-1 then CE-2 in its file's order, in one chunk; CE-2 holds the lower-numbered
    open work order. The chunk stops after CE-1; End AEM runs. The chunk took both devices' work first (its load), so the pull-in waits
    for it, never holding CE-2's lower number while the chunk wants it: both finish, and the check reads both rows as updates (a
    deadlock the chunk lost would skip a row as a value the database refuses)."""
    from apps.imports import services as imports
    from apps.imports.models import ImportRun

    today = timezone.localdate()
    one, two = _pumps_on_aem(dept, pump_model, today)
    _repair(two), _repair(one)  # CE-2's first: the lower number
    director = make_user("director")
    upload = imports.upload(director, "devices", "rooms.csv", b"Tag,Manufacturer,Model,Room\r\nCE-1,,,A1\r\nCE-2,,,B2\r\n")
    run = imports.confirm_columns(upload, director, imports.mapping_of(upload))
    pause = pause_at(monkeypatch, eq, "update_asset", after=True)
    errors = race(tenant, pause, lambda: imports.process(ImportRun.objects.get(pk=run.pk), director),
                  lambda: aem.end(pump_model, reason="Back to the OEM interval"))
    assert not errors, errors
    assert ImportRun.objects.get(pk=run.pk).counts == {"update": 2}
    assert set(Asset.objects.filter(pk__in=[one.pk, two.pk]).values_list("next_pm_on", flat=True)) == {today}


@needs_postgres
@pytest.mark.django_db(transaction=True)
def test_holding_a_device_while_its_investigation_opens_and_the_pull_in_take_turns(tenant, ctx, dept, pump_model, monkeypatch):
    """An incident recorded on CE-1 without an investigation or a hold; CE-2 holds the lower-numbered open repair. Hold device (CE-2)
    takes both devices' work first, then opens CE-1's investigation, and stops there; End AEM runs and waits for it, never holding
    CE-2's lower number while hold_device wants it. Both finish, CE-2 held."""
    today = timezone.localdate()
    one, two = _pumps_on_aem(dept, pump_model, today)
    _repair(two), _repair(one)
    incident = inc.record_incident(asset=one, outcome=Outcome.UNKNOWN, affected=Affected.PATIENT, hold=False, open_work_order=False)
    assert incident.work_order_id is None
    pause = pause_at(monkeypatch, inc, "_open_work_order", after=True)
    errors = race(tenant, pause, lambda: inc.hold_device(Incident.objects.get(pk=incident.pk), Asset.objects.get(pk=two.pk)),
                  lambda: aem.end(pump_model, reason="Back to the OEM interval"))
    assert not errors, errors
    assert Asset.objects.get(pk=two.pk).incident_hold and Incident.objects.get(pk=incident.pk).work_order_id is not None
    assert set(Asset.objects.filter(pk__in=[one.pk, two.pk]).values_list("next_pm_on", flat=True)) == {today}


@needs_postgres
@pytest.mark.django_db(transaction=True)
def test_auto_assign_week_and_the_pull_in_take_turns(tenant, ctx, dept, pump_model, techs, monkeypatch):
    """CE-1 and CE-2 are due tomorrow with open PMs nobody has; CE-2's has the lower number. Auto-assign week takes every step device's
    work first, then assigns CE-1's PM (the plan's order: day, risk, tag) and stops; End AEM waits for it, never holding CE-2's lower
    number while the week wants it. Both finish, both PMs assigned."""
    from apps.pm.services import assign_week

    today = timezone.localdate()
    one, two = _pumps_on_aem(dept, pump_model, today, next_pm_on=today + DAY)
    pms = [create_work_order(asset=a, type=WoType.PM, priority=Priority.NORMAL, problem="Scheduled PM", due_on=today + DAY) for a in (two, one)]
    pause = pause_at(monkeypatch, wo_services, "assign", after=True)
    errors = race(tenant, pause, lambda: assign_week(today=today), lambda: aem.end(pump_model, reason="Back to the OEM interval"))
    assert not errors, errors
    assert all(wo.assigned_to_id is not None for wo in WorkOrder.objects.filter(pk__in=[p.pk for p in pms]))


@needs_postgres
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("pause_after", ["lock_devices_work", "update_asset"])
def test_an_import_pass_adding_a_device_of_the_model_and_end_aem_take_turns(tenant, ctx, dept, pump_model, make_user, monkeypatch,
                                                                            pause_after):
    """The import pass (committed, unlike the check) changes CE-1 and adds CE-NEW of the pump model. It stops once it holds its devices'
    work (or after CE-1's row); End AEM locks the model's row and waits for that work. The chunk's COMMIT then checks CE-NEW's model (a
    deferred foreign key: the model FOR KEY SHARE), which the model's lock (FOR NO KEY UPDATE, aem._lock) never blocks: both finish."""
    from apps.imports import services as imports
    from apps.imports.kinds import devices as kind
    from apps.imports.models import ImportRun

    today = timezone.localdate()
    one, two = _pumps_on_aem(dept, pump_model, today)
    _repair(one)
    director = make_user("director")
    upload = imports.upload(director, "devices", "devices.csv", b"Tag,Manufacturer,Model,Room\r\nCE-1,,,A1\r\nCE-NEW,BD,Alaris 8015 PCU,A2\r\n")
    run = imports.confirm_columns(upload, director, imports.mapping_of(upload))
    run = imports.start_import(imports.process(run, director), director)  # the check, then the import pass raced
    pause = pause_at(monkeypatch, kind if pause_after == "lock_devices_work" else eq, pause_after, after=True)
    errors = race(tenant, pause, lambda: imports.process(ImportRun.objects.get(pk=run.pk), director),
                  lambda: aem.end(pump_model, reason="Back to the OEM interval"))
    assert not errors, errors
    assert ImportRun.objects.get(pk=run.pk).counts == {"create": 1, "update": 1}, ImportRun.objects.get(pk=run.pk).results
    assert Asset.objects.filter(tag="CE-NEW").exists()
    pump_model.refresh_from_db()
    assert pump_model.aem_interval_months is None


FILES = {
    "a device of the model added": (b"Tag,Manufacturer,Model,Room\r\nCE-1,,,A1\r\nCE-NEW,BD,Alaris 8015 PCU,A2\r\n", {"create": 1, "update": 1}),
    "a device saved twice": (b"Tag,Manufacturer,Model,Room,Status\r\nCE-1,,,A1,Out of service\r\n", {"update": 1}),
}


@needs_postgres
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("first", ["the import pass", "the rename"])
@pytest.mark.parametrize("file", list(FILES))
def test_an_import_pass_and_a_rename_that_ends_aem_take_turns(tenant, ctx, dept, pump_model, make_user, monkeypatch, first, file):
    """Edit details renames the pump model and marks it for the OEM schedule in one save: the rename changes the model's key (its row
    then held FOR UPDATE) and the mark ends AEM (the pull-in waits for the devices' work). The import pass adds CE-NEW of the model, or
    saves CE-1 twice (its status, then its room: the deferred foreign key is checked again at COMMIT, the model unchanged); it takes the
    models its rows name and its devices' models FOR KEY SHARE at its load, before its devices' work, so either one waits for the other
    at its first lock: both finish, AEM ended."""
    from apps.imports import services as imports
    from apps.imports.kinds import devices as kind
    from apps.imports.models import ImportRun

    today = timezone.localdate()
    one, two = _pumps_on_aem(dept, pump_model, today)
    _repair(one)
    director = make_user("director")
    text, counts = FILES[file]
    upload = imports.upload(director, "devices", "devices.csv", text)
    run = imports.confirm_columns(upload, director, imports.mapping_of(upload))
    run = imports.start_import(imports.process(run, director), director)
    chunk = lambda: imports.process(ImportRun.objects.get(pk=run.pk), director)  # noqa: E731
    rename = lambda: eq.update_device_model(DeviceModel.objects.get(pk=pump_model.pk), model="Alaris 8015 PCU-2",  # noqa: E731
                                            oem_schedule_required=True)
    if first == "the import pass":
        errors = race(tenant, pause_at(monkeypatch, kind, "lock_devices_work", after=True), chunk, rename)
    else:
        errors = race(tenant, pause_at(monkeypatch, aem, "model_changed"), rename, chunk)
    assert not errors, errors
    assert ImportRun.objects.get(pk=run.pk).counts == counts, ImportRun.objects.get(pk=run.pk).results
    pump_model.refresh_from_db()
    assert (pump_model.model, pump_model.aem_interval_months) == ("Alaris 8015 PCU-2", None)
    assert all(a.device_model_id == pump_model.pk for a in Asset.objects.all())


@needs_postgres
@pytest.mark.django_db(transaction=True)
def test_a_rename_waiting_for_an_import_that_adds_its_new_name_is_refused_in_words(tenant, ctx, dept, pump_model, make_user, monkeypatch):
    """The import pass holds the BD models FOR KEY SHARE from its load and adds BD Alaris 9000 for CE-NEW. Edit details renames the pump
    model to Alaris 9000 meanwhile: its name was free when checked, then its rename waits for the import (FOR UPDATE), and the name is
    checked again under that lock: refused in Edit details' words, never a database error. Both models stay, CE-NEW on the new one."""
    from django.core.exceptions import ValidationError

    from apps.imports import services as imports
    from apps.imports.kinds import devices as kind
    from apps.imports.models import ImportRun

    director = make_user("director")
    upload = imports.upload(director, "devices", "devices.csv", b"Tag,Manufacturer,Model,Room\r\nCE-NEW,BD,Alaris 9000,A2\r\n")
    run = imports.confirm_columns(upload, director, imports.mapping_of(upload))
    run = imports.start_import(imports.process(run, director), director)
    errors = race(tenant, pause_at(monkeypatch, kind, "_key_share", after=True),
                  lambda: imports.process(ImportRun.objects.get(pk=run.pk), director),
                  lambda: eq.update_device_model(DeviceModel.objects.get(pk=pump_model.pk), model="Alaris 9000"))
    assert set(errors) == {"second"} and isinstance(errors["second"], ValidationError), errors
    assert errors["second"].message_dict == {"model": ["BD Alaris 9000 is already in the catalog; choose it from the list."]}
    assert sorted(DeviceModel.objects.values_list("model", flat=True)) == ["Alaris 8015 PCU", "Alaris 9000"]
    assert Asset.objects.get(tag="CE-NEW").device_model.model == "Alaris 9000"
