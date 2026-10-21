"""
The work order numbering before a device's row, on PostgreSQL (a slice 38 critic's finding): every writer takes a device's work, then
the numbering (the Sequence row `wo-<year>` WorkOrder.save counts), then the device's row. A batch holds the numbering for its whole
transaction and takes its devices after it: a recall batch at COMMIT (its work orders' inserts name the devices, and Django's PostgreSQL
foreign keys are deferred). Two equipment writers used to take a device's row first and the numbering after it, when they opened an
incoming inspection: use_before_inspection on a waiting device with none open, and set_status's reinstate of a retired device still
waiting. Each then held the row while waiting for the numbering a batch held, and the batch waited for the row at its COMMIT:
PostgreSQL ended one ("deadlock detected", a 500). Now each takes the numbering before the row (workorders.services.lock_numbering), and
the two take turns.
"""
from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.db import transaction
from pg_helpers import needs_postgres
from test_incidents_review import pause_at, race

from apps.equipment import services as eq
from apps.equipment.models import Asset, AssetStatus, UseBeforeInspection
from apps.recalls import services as rc
from apps.workorders import inspections
from apps.workorders import services as wo_services
from apps.workorders.models import Priority, WorkOrder, WoType


def _waiting(dept, model, tag, status=AssetStatus.OUT_OF_SERVICE):
    """A device waiting for its incoming inspection with none open (one is opened when it is put in use or reinstated)."""
    return Asset.objects.create(tag=tag, device_model=model, department=dept, status=status, awaiting_inspection=True)


def _numbering(dept, model, row_exists: bool) -> None:
    """The year's numbering row as the race meets it: already counted (a work order opened earlier this year, on another device), or
    not yet (the year's first work order creates it: the race then meets on its unique index rather than its row)."""
    if row_exists:
        other = Asset.objects.create(tag="CE-29999", device_model=model, department=dept)
        wo_services.create_work_order(asset=other, type=WoType.REPAIR, priority=Priority.NORMAL, problem="Earlier this year")


ROW = pytest.mark.parametrize("row_exists", [True, False], ids=["counted", "first of the year"])


@needs_postgres
@pytest.mark.django_db(transaction=True)
@ROW
def test_use_before_inspection_while_a_recall_batch_numbers_its_work(tenant, ctx, dept, pump_model, pump, pump_recall, monkeypatch,
                                                                   row_exists):
    """A recall batch on the pump model stops after its first work order (the numbering held); a manager puts CE-20001, waiting for its
    incoming inspection with none open, in use before it. It waits for the numbering before the device's row; the batch then names
    CE-20001 too (its work order's insert, checked at COMMIT) and finishes; then the inspection opens. Both finish."""
    waiting = _waiting(dept, pump_model, "CE-20001")
    _numbering(dept, pump_model, row_exists)
    pause = pause_at(monkeypatch, wo_services, "create_work_order", after=True)
    errors = race(tenant, pause,
                  lambda: rc.create_recall_work_orders(type(pump_recall).objects.get(pk=pump_recall.pk)),
                  lambda: eq.use_before_inspection(Asset.objects.get(pk=waiting.pk), UseBeforeInspection.EMERGENCY))
    assert not errors, errors
    assert set(rc.recall_work_orders(pump_recall).values_list("asset__tag", flat=True)) >= {pump.tag, waiting.tag}
    waiting.refresh_from_db()
    assert waiting.status == AssetStatus.IN_SERVICE and inspections.open_inspection(waiting) is not None


@needs_postgres
@pytest.mark.django_db(transaction=True)
@ROW
def test_reinstate_while_a_work_order_is_numbered_on_the_device(tenant, ctx, dept, pump_model, monkeypatch, row_exists):
    """A writer numbers a work order on a retired device still waiting for its incoming inspection and stops before its COMMIT (the
    numbering held; the device named by the insert, checked then). The device is reinstated meanwhile: it waits for the numbering before
    the device's row, never holding the row while the writer's COMMIT waits for it. Both finish, and the reinstate opens the
    inspection."""
    retired = _waiting(dept, pump_model, "CE-20002", status=AssetStatus.RETIRED)
    _numbering(dept, pump_model, row_exists)

    def numbered_on_the_device():
        with transaction.atomic():
            wo_services.create_work_order(asset=Asset.objects.get(pk=retired.pk), type=WoType.REPAIR, priority=Priority.NORMAL,
                                          problem="Recorded on the retired device")

    pause = pause_at(monkeypatch, wo_services, "create_work_order", after=True)
    errors = race(tenant, pause, numbered_on_the_device,
                  lambda: eq.set_status(Asset.objects.get(pk=retired.pk), AssetStatus.OUT_OF_SERVICE))
    assert not errors, errors
    retired.refresh_from_db()
    assert retired.status == AssetStatus.OUT_OF_SERVICE and retired.awaiting_inspection
    assert inspections.open_inspection(retired) is not None
    assert WorkOrder.objects.filter(asset=retired, type=WoType.REPAIR).count() == 1


@pytest.mark.django_db
@pytest.mark.parametrize("door", ["use_before", "reinstate"])
def test_the_numbering_comes_after_the_work_and_before_the_row(ctx, dept, pump_model, monkeypatch, door):
    """Both databases: the order each writer takes its locks in when it opens the inspection: the device's work (or its open
    inspections), then the numbering, then the row (no other lock in between)."""
    from apps.workorders import services as wos

    seen = []
    for module, name in ((wos, "lock_work"), (inspections, "lock_open"), (wos, "lock_numbering")):
        real = getattr(module, name)
        monkeypatch.setattr(module, name, lambda *a, _real=real, _name=name, **k: (seen.append(_name), _real(*a, **k))[1])
    real_get = Asset.objects.select_for_update

    def spied():
        seen.append("row")
        return real_get()

    monkeypatch.setattr(Asset.objects, "select_for_update", spied)
    if door == "use_before":
        device = _waiting(dept, pump_model, "CE-20003")
        eq.use_before_inspection(device, UseBeforeInspection.EMERGENCY)
        assert seen[:3] == ["lock_open", "lock_numbering", "row"]
    else:
        device = _waiting(dept, pump_model, "CE-20004", status=AssetStatus.RETIRED)
        eq.set_status(device, AssetStatus.OUT_OF_SERVICE)
        assert seen[:3] == ["lock_work", "lock_numbering", "row"]
    assert inspections.open_inspection(device) is not None


@pytest.mark.django_db
def test_a_status_change_that_opens_nothing_takes_no_numbering(ctx, dept, pump_model, pump, monkeypatch):
    """The numbering is taken only by a writer that opens a work order: a reinstate of a device not waiting, a device tagged out, and
    one put in use before an inspection that is already open take none (the numbering is the facility's busiest lock)."""
    from apps.workorders import services as wos

    calls = []
    real = wos.lock_numbering
    monkeypatch.setattr(wos, "lock_numbering", lambda year: (calls.append(year), real(year))[1])
    eq.set_status(pump, AssetStatus.OUT_OF_SERVICE)
    retired = Asset.objects.create(tag="CE-20005", device_model=pump_model, department=dept, status=AssetStatus.RETIRED)
    eq.set_status(retired, AssetStatus.IN_SERVICE)
    waiting = _waiting(dept, pump_model, "CE-20006")
    inspections.open_for(waiting)  # its inspection is open already: the use reuses it
    eq.use_before_inspection(waiting, UseBeforeInspection.EMERGENCY)
    assert calls == []


# --- writers that number one device's work order while they hold, or will take, another device's work (the slice 38 skeptics') -------

@needs_postgres
@pytest.mark.django_db(transaction=True)
def test_holding_a_device_while_its_pm_fails(tenant, ctx, vent, pump, techs, monkeypatch):
    """An incident recorded on CE-10001 with no investigation and no hold; the manager holds CE-10002 too, which opens CE-10001's
    investigation (the numbering) and stops there. A technician completes CE-10002's PM as Failed meanwhile (its work, then the numbering
    for its repair). Hold device took both devices' work before the numbering, so the completion waits for it (then is refused: CE-10002
    is held), never holding CE-10002's PM while hold_device wants it."""
    from test_incidents_review import HELD

    from apps.incidents import services as inc
    from apps.incidents.models import Affected, Incident, Outcome
    from apps.workorders.completion import complete_work_order
    from apps.workorders.models import PmResult

    pm = wo_services.create_work_order(asset=pump, type=WoType.PM, priority=Priority.NORMAL, problem="Scheduled PM", assigned_to=techs["dana"])
    incident = inc.record_incident(asset=vent, outcome=Outcome.UNKNOWN, affected=Affected.PATIENT, hold=False, open_work_order=False)
    pause = pause_at(monkeypatch, wo_services, "create_work_order", after=True)
    errors = race(tenant, pause, lambda: inc.hold_device(Incident.objects.get(pk=incident.pk), Asset.objects.get(pk=pump.pk)),
                  lambda: complete_work_order(WorkOrder.objects.get(pk=pm.pk), pm_result=PmResult.FAIL, resolution="Alarm failed"))
    assert "first" not in errors, errors
    assert "second" not in errors or HELD in errors["second"].messages[0], errors
    assert Asset.objects.get(pk=pump.pk).incident_hold


@needs_postgres
@pytest.mark.django_db(transaction=True)
def test_auto_assign_week_while_an_incident_is_recorded_on_a_device_of_the_week(tenant, ctx, vent, pump, techs, monkeypatch):
    """Auto-assign week creates CE-10001's PM (the numbering held) and stops; an incident is recorded meanwhile on CE-10002, whose open PM
    the week will assign (the incident takes CE-10002's work, then the numbering for its investigation). The week took every step
    device's work before it numbered one, so the incident waits for it: both finish."""
    from django.utils import timezone

    from apps.incidents import services as inc
    from apps.incidents.models import Affected, Outcome
    from apps.pm import services as pm_services

    today = timezone.localdate()
    Asset.objects.filter(pk=vent.pk).update(next_pm_on=today)
    Asset.objects.filter(pk=pump.pk).update(next_pm_on=today + timedelta(days=1))
    pm = wo_services.create_work_order(asset=pump, type=WoType.PM, priority=Priority.NORMAL, problem="Scheduled PM",
                                       due_on=today + timedelta(days=1))
    pause = pause_at(monkeypatch, pm_services, "_create_pm", after=True)
    errors = race(tenant, pause, lambda: pm_services.assign_week(today=today),
                  lambda: inc.record_incident(asset=Asset.objects.get(pk=pump.pk), outcome=Outcome.UNKNOWN, affected=Affected.PATIENT))
    assert not errors, errors
    assert WorkOrder.objects.get(pk=pm.pk).assigned_to_id is not None


def _spy_import(monkeypatch, names=("lock_tags", "_key_share", "lock_devices_work", "lock_numbering")) -> list:
    """The locks a devices import chunk's load takes, in order (the module's own names, as it calls them)."""
    from apps.imports.kinds import devices as kind
    from apps.workorders import services as wos

    seen = []
    for name in names:
        module = eq if name == "lock_tags" else kind if name == "_key_share" else wos
        real = getattr(module, name)
        spy = lambda *a, _real=real, _name=name, **k: (seen.append(_name), _real(*a, **k))[1]  # noqa: E731
        monkeypatch.setattr(module, name, spy)
        if module is wos:
            monkeypatch.setattr(kind, name, spy)
    return seen


def _import(user, text: bytes):
    """The file checked, then imported (one chunk each)."""
    from apps.imports import services as imports
    from apps.imports.models import ImportRun

    run = imports.upload(user, "devices", "devices.csv", text)
    run = imports.process(imports.confirm_columns(run, user, imports.mapping_of(run)), user)
    run = imports.process(imports.start_import(run, user), user)
    return ImportRun.objects.get(pk=run.pk)


REINSTATES = {
    "reinstated": (AssetStatus.RETIRED, "", b"CE-2,,,B2,Out of service\r\n", True),
    "named, not reinstated": (AssetStatus.RETIRED, "", b"CE-2,,,B2,\r\n", False),
    "kept retired": (AssetStatus.RETIRED, "", b"CE-2,,,B2,Retired\r\n", False),
    "a returned rental": (AssetStatus.RETIRED, "rental", b"CE-2,,,B2,Out of service\r\n", False),
    "none waiting": (None, "", b"CE-2,,,B2,Out of service\r\n", False),
}


@pytest.mark.django_db
@pytest.mark.parametrize("case", list(REINSTATES))
def test_a_devices_import_chunk_numbers_first_only_when_a_row_may_reinstate_a_waiting_device(ctx, dept, pump_model, make_user, monkeypatch,
                                                                                               case):
    """A chunk with a row that may reinstate a device of ours retired while still waiting for its incoming inspection takes the tag lock
    first (Add device takes it before a new department, then numbers), then its devices' work, then the numbering, before any device's
    row (set_status opens the inspection then);
    any other chunk takes no numbering (the facility's busiest lock, held to the chunk's end): one naming such a device without
    reinstating it, one keeping it retired, a returned rental (the import never reinstates one), or none waiting."""
    from apps.equipment.models import Ownership

    status, ownership, rows, numbers = REINSTATES[case]
    Asset.objects.create(tag="CE-1", device_model=pump_model, department=dept)
    if status is None:
        Asset.objects.create(tag="CE-2", device_model=pump_model, department=dept, status=AssetStatus.OUT_OF_SERVICE)
    elif ownership:
        Asset.objects.create(tag="CE-2", device_model=pump_model, department=dept, status=status, awaiting_inspection=True,
                             ownership=Ownership.RENTAL, owner="RentCo Medical")
    else:
        _waiting(dept, pump_model, "CE-2", status=status)
    seen = _spy_import(monkeypatch)
    _import(make_user("director"), b"Tag,Manufacturer,Model,Room,Status\r\nCE-1,,,A1,\r\n" + rows)
    assert seen.count("lock_devices_work") >= 2  # each pass's chunk (the check, then the import) loads the same way
    if numbers:
        assert seen[:4] == ["lock_tags", "_key_share", "lock_devices_work", "lock_numbering"], seen  # before any row touches a device
        assert inspections.open_inspection(Asset.objects.get(tag="CE-2")) is not None  # the row did reinstate it
    else:  # every row's device is here and nothing numbers: no tag lock, no numbering
        assert seen[:2] == ["_key_share", "lock_devices_work"] and "lock_tags" not in seen and "lock_numbering" not in seen, seen


@pytest.mark.django_db
def test_a_devices_import_chunk_that_may_add_a_device_takes_the_tag_lock_first(ctx, dept, pump_model, make_user, monkeypatch):
    """A row that may add a device takes the facility's tag lock (create_asset), and create_asset numbers a new device's incoming
    inspection under it: the chunk takes the tag lock at its load, before its devices' work and the numbering, never after."""
    _waiting(dept, pump_model, "CE-2", status=AssetStatus.RETIRED)
    seen = _spy_import(monkeypatch)
    run = _import(make_user("director"), b"Tag,Manufacturer,Model,Room,Status\r\nCE-2,,,B2,Out of service\r\nCE-NEW,BD,Alaris 8015 PCU,A1,\r\n")
    assert seen[:4] == ["lock_tags", "_key_share", "lock_devices_work", "lock_numbering"], seen
    assert run.counts == {"create": 1, "update": 1}, run.results
    assert Asset.objects.filter(tag="CE-NEW").exists() and inspections.open_inspection(Asset.objects.get(tag="CE-2")) is not None


def _add_waiting():
    return eq.create_asset(tag="CE-W", device_model=Asset.objects.get(tag="CE-2").device_model, department=Asset.objects.get(tag="CE-2").department,
                           incoming_inspection="waiting")


@needs_postgres
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("first", ["the import chunk", "Add device"])
@pytest.mark.parametrize("order", ["reinstate, then add", "add, then reinstate"])
def test_a_devices_import_chunk_and_add_device_waiting_for_inspection_take_turns(tenant, ctx, dept, pump_model, make_user, monkeypatch,
                                                                                  first, order):
    """A chunk reinstating CE-2 (retired, still waiting for its incoming inspection: a number) and adding CE-NEW (the tag lock), against
    Add device for CE-W waiting for its incoming inspection (the tag lock, then a number). Either one stops once it holds its first
    lock; the other waits for it, never holding the numbering while the first wants the tag lock. Both finish: CE-W added with its
    inspection, the chunk's rows a create and an update (a deadlock the chunk lost would skip a row as a value the database refuses)."""
    from apps.imports import services as imports
    from apps.imports.kinds import devices as kind
    from apps.imports.models import ImportRun

    _waiting(dept, pump_model, "CE-2", status=AssetStatus.RETIRED)
    director = make_user("director")
    rows = [b"CE-2,,,B2,Out of service\r\n", b"CE-NEW,BD,Alaris 8015 PCU,A1,\r\n"]
    upload = imports.upload(director, "devices", "devices.csv",
                            b"Tag,Manufacturer,Model,Room,Status\r\n" + b"".join(rows if order.startswith("reinstate") else rows[::-1]))
    run = imports.confirm_columns(upload, director, imports.mapping_of(upload))
    chunk = lambda: imports.process(ImportRun.objects.get(pk=run.pk), director)  # noqa: E731
    if first == "the import chunk":
        pause = pause_at(monkeypatch, kind, "lock_numbering", after=True)
        errors = race(tenant, pause, chunk, _add_waiting)
    else:
        pause = pause_at(monkeypatch, eq, "lock_tags", after=True)
        errors = race(tenant, pause, _add_waiting, chunk)
    assert not errors, errors
    assert ImportRun.objects.get(pk=run.pk).counts == {"create": 1, "update": 1}, ImportRun.objects.get(pk=run.pk).results
    assert inspections.open_inspection(Asset.objects.get(tag="CE-W")) is not None


@pytest.mark.django_db
def test_a_device_retired_after_the_chunk_read_it_is_never_numbered_after_its_rows(ctx, dept, pump_model, make_user, monkeypatch):
    """The chunk decides at its load, from the devices as it read them, whether a row may reinstate a waiting device. CE-2, missing and
    waiting for its incoming inspection (none open) when read, is retired by someone else right after the load; its row ("Out of service":
    Found, as read) would now reinstate it and open an inspection. set_status refuses it in its words (may_number=False: the chunk took
    no numbering at its load), never numbering after CE-1's row the chunk holds; CE-2 stays retired, and a run of the file again reads it
    retired and reinstates it."""
    from apps.imports.kinds import devices as kind

    Asset.objects.create(tag="CE-1", device_model=pump_model, department=dept)
    _waiting(dept, pump_model, "CE-2", status=AssetStatus.MISSING)
    seen = _spy_import(monkeypatch)
    real = kind.lock_devices_work

    def then_retired(ids, *also):
        real(ids, *also)
        Asset.objects.filter(tag="CE-2").update(status=AssetStatus.RETIRED)  # another user's retire, committed after the chunk read CE-2

    monkeypatch.setattr(kind, "lock_devices_work", then_retired)
    run = _import(make_user("director"), b"Tag,Manufacturer,Model,Room,Status\r\nCE-1,,,A1,\r\nCE-2,,,B2,Out of service\r\n")
    assert "lock_numbering" not in seen, seen
    notes = [note for row in run.results for note in row[3]]
    assert "Status kept: CE-2 changed meanwhile: try again." in notes, run.results
    assert Asset.objects.get(tag="CE-2").status == AssetStatus.RETIRED and inspections.open_inspection(Asset.objects.get(tag="CE-2")) is None


@pytest.mark.django_db
def test_set_status_told_not_to_number_refuses_a_reinstate_that_would(ctx, dept, pump_model, monkeypatch):
    """set_status(may_number=False) on a device retired and waiting refuses in CHANGED_MEANWHILE's words before any lock; any other
    move it makes as before."""
    from apps.workorders import services as wos

    taken = []
    monkeypatch.setattr(wos, "lock_work", lambda *a, **k: taken.append("lock_work"))
    waiting = _waiting(dept, pump_model, "CE-2", status=AssetStatus.RETIRED)
    with pytest.raises(ValidationError) as e:
        eq.set_status(waiting, AssetStatus.OUT_OF_SERVICE, may_number=False)
    assert e.value.messages == [eq.CHANGED_MEANWHILE.format(tag="CE-2")] and taken == []
    plain = Asset.objects.create(tag="CE-1", device_model=pump_model, department=dept)
    eq.set_status(plain, AssetStatus.OUT_OF_SERVICE, may_number=False)
    assert Asset.objects.get(tag="CE-1").status == AssetStatus.OUT_OF_SERVICE


@needs_postgres
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("door", ["Add device", "Add rental or loaner"])
def test_add_device_with_a_new_department_and_an_import_adding_it_take_turns(tenant, ctx, dept, pump_model, client, make_user, monkeypatch, door):
    """An import chunk adds CE-NEW in a new department, Cardiology: it holds the facility's tag lock from its load and then inserts the
    department. Add device (or Add rental or loaner) with Cardiology typed as a new department takes the tag lock before inserting it, so
    it waits for the chunk, never holding an uncommitted Cardiology while the chunk waits on it. Both finish, one Cardiology."""
    from apps.equipment.models import Department
    from apps.imports import services as imports
    from apps.imports.models import ImportRun

    director = make_user("director")
    upload = imports.upload(director, "devices", "devices.csv",
                            b"Tag,Manufacturer,Model,Department,Room\r\nCE-NEW,BD,Alaris 8015 PCU,Cardiology,A1\r\n")
    run = imports.confirm_columns(upload, director, imports.mapping_of(upload))
    client.force_login(director)
    if door == "Add device":
        from test_devices_ui import NEW_URL, new_post

        url, data, tag = NEW_URL, new_post(pump_model, None, new_department="Cardiology"), "CE-20001"
    else:
        from test_temporary_ui import NEW_URL, add_post

        url, data, tag = NEW_URL, add_post(pump_model, dept, department="new", new_department="Cardiology"), "T-0100"
    answers = {}
    pause = pause_at(monkeypatch, eq, "lock_tags", after=True)
    errors = race(tenant, pause, lambda: imports.process(ImportRun.objects.get(pk=run.pk), director),
                  lambda: answers.setdefault("add", client.post(url, data, HTTP_HX_REQUEST="true")))
    assert not errors, errors
    assert answers["add"].status_code == 200 and Asset.objects.filter(tag=tag).exists(), answers["add"].content.decode()[:2000]
    assert ImportRun.objects.get(pk=run.pk).counts == {"create": 1}, ImportRun.objects.get(pk=run.pk).results
    assert Department.objects.filter(name__iexact="Cardiology").count() == 1


@needs_postgres
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("order", ["new department first", "reinstate first"])
def test_add_device_numbering_and_an_import_numbering_with_no_new_tag_take_turns(tenant, ctx, dept, pump_model, client, make_user, monkeypatch,
                                                                                order):
    """A chunk whose devices are all here reinstates CE-2 (retired, still waiting: it numbers) and puts CE-1 in a new department,
    Cardiology. It takes the tag lock with the numbering, so Add device (new, waiting for its incoming inspection) with Cardiology typed
    as new waits for it at its first lock, never holding an uncommitted Cardiology while waiting for the numbering. Both finish."""
    from test_incoming_equipment_ui import NEW_URL, new_post

    from apps.equipment.models import Department
    from apps.imports import services as imports
    from apps.imports.kinds import devices as kind
    from apps.imports.models import ImportRun

    Asset.objects.create(tag="CE-1", device_model=pump_model, department=dept)
    _waiting(dept, pump_model, "CE-2", status=AssetStatus.RETIRED)
    director = make_user("director")
    rows = [b"CE-1,,,Cardiology,A1,\r\n", b"CE-2,,,,B2,Out of service\r\n"]
    upload = imports.upload(director, "devices", "devices.csv",
                            b"Tag,Manufacturer,Model,Department,Room,Status\r\n" + b"".join(rows if order.startswith("new") else rows[::-1]))
    run = imports.confirm_columns(upload, director, imports.mapping_of(upload))
    client.force_login(director)
    answers = {}
    pause = pause_at(monkeypatch, kind, "lock_numbering", after=True)
    errors = race(tenant, pause, lambda: imports.process(ImportRun.objects.get(pk=run.pk), director),
                  lambda: answers.setdefault("add", client.post(NEW_URL, new_post(pump_model, dept, department="new", new_department="Cardiology"),
                                                                HTTP_HX_REQUEST="true")))
    assert not errors, errors
    assert answers["add"].status_code == 200 and Asset.objects.filter(awaiting_inspection=True, department__name="Cardiology").exists(), \
        answers["add"].content.decode()[:2000]
    assert ImportRun.objects.get(pk=run.pk).counts == {"update": 2}, ImportRun.objects.get(pk=run.pk).results
    assert Department.objects.filter(name__iexact="Cardiology").count() == 1


@pytest.mark.django_db
def test_a_devices_import_gives_the_services_words_for_a_device_changed_meanwhile(ctx, dept, pump_model, make_user, monkeypatch):
    """A status the file asks for, refused because the device changed between the service's check and its locks, keeps the service's
    words ("<tag> changed meanwhile: try again"), never a guessed reason."""
    from apps.imports import services as imports
    from apps.imports.models import ImportRun

    Asset.objects.create(tag="CE-1", device_model=pump_model, department=dept, status=AssetStatus.RETIRED)

    def changed(asset, *args, **kwargs):
        from django.core.exceptions import ValidationError

        raise ValidationError(eq.CHANGED_MEANWHILE.format(tag=asset.tag))

    monkeypatch.setattr(eq, "set_status", changed)
    director = make_user("director")
    run = imports.upload(director, "devices", "status.csv", b"Tag,Manufacturer,Model,Status\r\nCE-1,,,Out of service\r\n")
    run = imports.process(imports.confirm_columns(run, director, imports.mapping_of(run)), director)
    notes = [note for row in ImportRun.objects.get(pk=run.pk).results for note in row[3]]
    assert "Status kept: CE-1 changed meanwhile: try again." in notes, notes
