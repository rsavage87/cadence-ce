"""
Slice 38 wave 1 on PostgreSQL: the software updates' writers against the writers they meet, two threads each (skipped on SQLite, which
runs one writer at a time). Each pair runs in both orders: the first pauses holding what it has locked until the second waits on a lock
(read from pg_stat_activity and pg_locks), then goes on. Each finishes, never in a deadlock, and answers in words where it refuses.

1. A recording completion and Set devices to this release (the bulk set) on one release: whichever is first holds the register, the
   other waits on the register (never on a device or the release), then runs on what the first saved.
2. Confirm version and a reopen of the same work order: Confirm takes the register, then the device's work (lock_work) before the
   release and the device, so the reopen (lock_work, then the device FOR UPDATE) waits for it, or it for the reopen (then refuses: the
   work order is not completed now). Taking the device before the work order would close a cycle (critique38_rules B1).
3. Opening a release's updates and a failed PM's completion on one of its devices: the batch never locks a device before numbering, so
   the two wait on the numbering in turn; RACE in words at worst, never another error.
4. Entered in error on a work order's outcome and a reopen of that work order: the register, then the work, then the record.
"""
import threading
import time
from datetime import timedelta
from types import SimpleNamespace

import pytest
from cyber_fixtures import facts, release, update_wo
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.utils import timezone
from pg_helpers import needs_postgres

from apps.credentials.models import Technician
from apps.cybersecurity import rules, services, updates
from apps.cybersecurity.models import DeviceSecurity, SoftwareUpdate
from apps.cybersecurity.services import record_facts_for
from apps.cybersecurity.updates import UpdateChoice, confirm, open_updates, remove
from apps.equipment.models import Asset, Department, DeviceModel, RiskClass
from apps.tenants.context import tenant_context
from apps.workorders import services as wo_services
from apps.workorders.completion import complete_work_order
from apps.workorders.models import PmResult, Priority, WorkOrder, WoStatus, WoType

DAY = timedelta(days=1)
REGISTER = ("core_sequence", "AccessExclusiveLock")  # the register lock (its Sequence row FOR UPDATE)
NUMBERING = ("core_sequence", "AccessExclusiveLock")  # the work orders' numbering (a Sequence row FOR UPDATE too)
WORK_ORDER = ("workorders_workorder", "AccessExclusiveLock")  # a work order's row FOR UPDATE (lock_work)
INSTALLED = UpdateChoice("installed", None, basis="manufacturer_release")

pytestmark = [needs_postgres, pytest.mark.django_db(transaction=True)]


def _thread(target, errors, results, key):
    def run():
        try:
            results[key] = target()
        except Exception as e:  # noqa: BLE001 - reported by the test
            errors[key] = e
        finally:
            connection.close()
    return threading.Thread(target=run, name=key)


def _both(first, second, errors, results):
    a, b = _thread(first, errors, results, "first"), _thread(second, errors, results, "second")
    a.start()
    b.start()
    for t in (a, b):
        t.join(60)
    assert not a.is_alive() and not b.is_alive()  # never a hang


def _waiting(timeout: float = 10) -> list:
    """What another connection to this database waits for, as soon as one does: [(table, tuple lock mode)] for a row lock, else
    [("statement", its first 27 characters)], else [] after `timeout` (as tests/test_cyber_review_round3.py reads it)."""
    deadline = time.monotonic() + timeout
    with connection.cursor() as cur:
        while time.monotonic() < deadline:
            cur.execute("SELECT pg_stat_clear_snapshot()")
            cur.execute("SELECT coalesce(l.relation::regclass::text, 'statement'), coalesce(l.mode, left(a.query, 27)) "
                        "FROM pg_stat_activity a LEFT JOIN pg_locks l ON l.pid = a.pid AND l.locktype = 'tuple' "
                        "WHERE a.datname = current_database() AND a.pid <> pg_backend_pid() AND a.wait_event_type = 'Lock'")
            found = cur.fetchall()
            if found:
                return [tuple(row) for row in found]
            time.sleep(0.05)
    return []


def _pause_first(monkeypatch, module, name: str, started: threading.Event, seen: dict):
    """The thread named "first" pauses just after its first call of module.<name>, holding what it has locked so far, until another
    connection waits on a lock (seen["waiting"]: what it waits for)."""
    real = getattr(module, name)

    def paused(*args, **kwargs):
        out = real(*args, **kwargs)
        if threading.current_thread().name == "first" and not started.is_set():
            started.set()
            seen["waiting"] = _waiting()
        return out

    monkeypatch.setattr(module, name, paused)


def _only_words(errors: dict) -> None:
    """Every refusal a thread met is a ValidationError in words: never a deadlock, a serialization failure, or another error."""
    assert all(isinstance(e, ValidationError) for e in errors.values()), errors


def _setup(tenant, *, pm: bool = False):
    """Model A with releases 1.0 and 2.0; CE-1 and CE-2 on 1.0 (recorded a month ago); Tom; an update work order on CE-1 to install 2.0,
    assigned to Tom; with `pm`, an open PM on CE-1 assigned to Tom. The register's row exists (a facility's first register write would
    otherwise make the second wait on its insert)."""
    today = timezone.localdate()
    with tenant_context(tenant):
        dept = Department.objects.create(name="ICU")
        model = DeviceModel.objects.create(manufacturer="BD", model="Alaris A", description="Pump", category="Infusion pumps",
                                           risk_class=RiskClass.HIGH, oem_pm_interval_months=12)
        rel1, rel2 = release(model, "1.0"), release(model, "2.0")
        ce1 = Asset.objects.create(tag="CE-1", device_model=model, department=dept, next_pm_on=today + 30 * DAY)
        ce2 = Asset.objects.create(tag="CE-2", device_model=model, department=dept, next_pm_on=today + 30 * DAY)
        for device in (ce1, ce2):
            facts(device, rel1, release_since=today - 30 * DAY, checked_on=today - 30 * DAY)
        tom = Technician.objects.create(name="Tom Okafor")
        s = SimpleNamespace(today=today, model=model, rel1=rel1, rel2=rel2, ce1=ce1, ce2=ce2, tom=tom, wo=None, row=None, pm=None)
        if pm:
            s.pm = wo_services.create_work_order(asset=ce1, type=WoType.PM, priority=Priority.NORMAL, problem="Scheduled PM")
            wo_services.assign(s.pm, technician=tom)
        else:
            s.wo, s.row = update_wo(ce1, rel2, assigned_to=tom)
        with transaction.atomic():
            services.lock_register()
    return s


def _installed(s):
    return UpdateChoice("installed", str(s.rel2.pk), basis="manufacturer_release")


# --- 1. a recording completion and the bulk set ----------------------------------------------------------------------------------

@pytest.mark.parametrize("first_is", ["completion", "bulk"])
def test_a_recording_completion_and_the_bulk_set_take_the_register_in_turn(tenant, monkeypatch, first_is):
    s = _setup(tenant)
    started, seen = threading.Event(), {}
    _pause_first(monkeypatch, services, "lock_register", started, seen)
    errors, results = {}, {}

    def completion():
        if first_is == "bulk":
            assert started.wait(20)
        with tenant_context(tenant):
            return complete_work_order(WorkOrder.objects.get(pk=s.wo.pk), software=_installed(s), by=None).software.outcome

    def bulk():
        if first_is == "completion":
            assert started.wait(20)
        with tenant_context(tenant):
            return record_facts_for(s.model, [s.ce1, s.ce2], by=None, release=s.rel2)

    first, second = (completion, bulk) if first_is == "completion" else (bulk, completion)
    _both(first, second, errors, results)
    _only_words(errors)
    assert seen["waiting"] == [REGISTER], seen  # the second waited on the register, never on a device or the release
    if first_is == "completion":
        assert not errors and results == {"first": "installed", "second": 1}, (results, errors)  # CE-1 already ran 2.0: left as it was
    else:  # the bulk set ran first: CE-1 already runs 2.0, and the completion's installed is recorded all the same (the person states
        # what was installed: review round 2), naming no before in its words (the record named 2.0 already; stored as taken, round 3)
        assert not errors and results == {"first": 2, "second": "installed"}, (results, errors)
    with tenant_context(tenant):
        assert set(DeviceSecurity.objects.values_list("release_id", flat=True)) == {s.rel2.pk}
        row = SoftwareUpdate.objects.select_related("work_order", "target_release", "release_before", "release_after").get()
        assert row.outcome == "installed"
        assert row.release_before_id == (s.rel1.pk if first_is == "completion" else s.rel2.pk)
        assert ("recorded before" in rules.update_words(row)) is (first_is == "completion")


# --- 2. Confirm version and a reopen ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("first_is", ["confirm", "reopen"])
def test_confirm_and_a_reopen_of_the_same_work_order_wait_in_turn(tenant, monkeypatch, first_is):
    s = _setup(tenant)
    with tenant_context(tenant):
        complete_work_order(WorkOrder.objects.get(pk=s.wo.pk), resolution="Done by the vendor", by=None)  # nothing recorded
    started, seen = threading.Event(), {}
    _pause_first(monkeypatch, wo_services, "lock_work", started, seen)
    errors, results = {}, {}

    def confirming():
        if first_is == "reopen":
            assert started.wait(20)
        with tenant_context(tenant):
            return confirm(s.row.pk, _installed(s), by=None).outcome

    def reopening():
        if first_is == "confirm":
            assert started.wait(20)
        with tenant_context(tenant):
            return wo_services.change_status(WorkOrder.objects.get(pk=s.wo.pk), WoStatus.IN_PROGRESS).status

    first, second = (confirming, reopening) if first_is == "confirm" else (reopening, confirming)
    _both(first, second, errors, results)
    _only_words(errors)
    assert seen["waiting"] == [WORK_ORDER], seen  # the second waited on the work order's row, holding nothing the first wants
    with tenant_context(tenant):
        row, wo = SoftwareUpdate.objects.get(), WorkOrder.objects.get(pk=s.wo.pk)
        assert wo.status == WoStatus.IN_PROGRESS
        if first_is == "confirm":
            assert not errors and results == {"first": "installed", "second": WoStatus.IN_PROGRESS}, (results, errors)
            assert row.outcome == "installed" and DeviceSecurity.objects.get(asset=s.ce1).release_id == s.rel2.pk
        else:
            assert results == {"first": WoStatus.IN_PROGRESS} and errors["second"].message_dict == {
                "software_outcome": [updates.REOPENED.format(number=wo.number)]}, (results, errors)
            assert row.outcome == "" and DeviceSecurity.objects.get(asset=s.ce1).release_id == s.rel1.pk


# --- 3. the batch and a failed PM's completion -------------------------------------------------------------------------------------

@pytest.mark.parametrize("first_is", ["pm", "batch"])
def test_the_batch_and_a_failed_pms_completion_on_one_of_its_devices(tenant, monkeypatch, first_is):
    s = _setup(tenant, pm=True)
    started, seen = threading.Event(), {}
    _pause_first(monkeypatch, wo_services, "create_work_order", started, seen)  # the PM's repair, or the batch's first update
    errors, results = {}, {}

    def failed_pm():
        if first_is == "batch":
            assert started.wait(20)
        with tenant_context(tenant):
            done = complete_work_order(WorkOrder.objects.get(pk=s.pm.pk), pm_result=PmResult.FAIL, resolution="Occlusion alarm failed",
                                       by=None)
            return done.repair.type

    def batch():
        if first_is == "pm":
            assert started.wait(20)
        with tenant_context(tenant):
            return open_updates(s.rel2, from_releases=[s.rel1], due_on=s.today, priority="normal", by=None).created

    first, second = (failed_pm, batch) if first_is == "pm" else (batch, failed_pm)
    _both(first, second, errors, results)
    _only_words(errors)
    assert seen["waiting"] == [NUMBERING], seen  # the second waited on the numbering the first held, never on a device
    batch_key = "second" if first_is == "pm" else "first"
    pm_key = "first" if first_is == "pm" else "second"
    if batch_key in errors:  # PostgreSQL ended the batch in a lock conflict: words, nothing opened
        assert errors[batch_key].message_dict == {"release": [services.RACE]}
    else:
        assert results[batch_key] == 2
    assert results.get(pm_key) == WoType.REPAIR, (results, errors)
    with tenant_context(tenant):
        assert WorkOrder.objects.filter(type=WoType.REPAIR).count() == 1
        assert SoftwareUpdate.objects.count() == (0 if batch_key in errors else 2)


# --- 4. Entered in error and a reopen -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("first_is", ["remove", "reopen"])
def test_entered_in_error_and_a_reopen_of_the_same_work_order_wait_in_turn(tenant, monkeypatch, first_is):
    s = _setup(tenant)
    with tenant_context(tenant):
        complete_work_order(WorkOrder.objects.get(pk=s.wo.pk), software=_installed(s), by=None)
    started, seen = threading.Event(), {}
    _pause_first(monkeypatch, wo_services, "lock_work", started, seen)
    errors, results = {}, {}

    def removing():
        if first_is == "reopen":
            assert started.wait(20)
        with tenant_context(tenant):
            return remove(s.row.pk, by=None).outcome

    def reopening():
        if first_is == "remove":
            assert started.wait(20)
        with tenant_context(tenant):
            return wo_services.change_status(WorkOrder.objects.get(pk=s.wo.pk), WoStatus.IN_PROGRESS).status

    first, second = (removing, reopening) if first_is == "remove" else (reopening, removing)
    _both(first, second, errors, results)
    assert not errors, errors
    assert seen["waiting"] == [WORK_ORDER], seen
    expected = {"first": "", "second": WoStatus.IN_PROGRESS} if first_is == "remove" else {"first": WoStatus.IN_PROGRESS, "second": ""}
    assert results == expected
    with tenant_context(tenant):
        assert SoftwareUpdate.objects.get().outcome == "" and WorkOrder.objects.get(pk=s.wo.pk).status == WoStatus.IN_PROGRESS
        assert DeviceSecurity.objects.get(asset=s.ce1).release_id == s.rel2.pk  # the facts stay
