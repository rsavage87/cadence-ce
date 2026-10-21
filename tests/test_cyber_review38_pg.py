"""
Slice 38 review fixes C3 and C4 on PostgreSQL: opening a software update takes the device FOR KEY SHARE after its work order's insert
(updates._open_one), because the insert holds nothing (Django creates PostgreSQL's foreign keys DEFERRABLE INITIALLY DEFERRED: the
device is checked only at COMMIT). Two threads each, skipped on SQLite (one writer at a time there).

Each writer of the device (a retire, a model change, a hold as evidence, the API's device delete) against each opener (Open software
update, open_update; the release's batch, open_updates), in both orders. The first pauses holding its locks until the second waits on
the device's row (read from pg_stat_activity and pg_locks), then commits:
- the writer first: the opener waits for it, then reads the device as it committed: refused in words (open_update) or skipped (the
  batch), nothing opened on CE-2;
- the opener first: the writer waits for it, then runs as it would one after the other: a retire cancels the new update, a delete is
  refused (the device has work), a model change and a hold leave the update as opened before them.
Never an update opened on a device retired, re-modelled, held, or deleted before it, never a 500. And the backstop: with the device's lock
taken away, a delete that commits first fails the work order's deferred key at the opener's COMMIT, answered as services.RACE in words.
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
from rest_framework.exceptions import ValidationError as ApiValidationError

from apps.api.views import AssetViewSet
from apps.cybersecurity import services, updates
from apps.cybersecurity.models import SoftwareUpdate
from apps.equipment import services as eq
from apps.equipment.models import Asset, AssetStatus, Department, DeviceModel, RiskClass
from apps.tenants.context import tenant_context
from apps.workorders.models import OPEN_STATUSES, WorkOrder, WoStatus, WoType

DAY = timedelta(days=1)
DEVICE_ROW = "equipment_asset"  # what the second waits on: the device's row

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
    [("statement", its first 27 characters)], else [] after `timeout` (as tests/test_cyber_updates_pg.py reads it)."""
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


def _setup(tenant):
    """Model A (releases 1.0 and 2.0) and model B; CE-1 and CE-2 of model A on 1.0 (recorded a month ago), in service; an open update to
    2.0 on CE-1 (so the batch opens one, on CE-2, and skips CE-1). The register's row exists."""
    today = timezone.localdate()
    with tenant_context(tenant):
        dept = Department.objects.create(name="ICU")
        model = DeviceModel.objects.create(manufacturer="BD", model="Alaris A", description="Pump", category="Infusion pumps",
                                           risk_class=RiskClass.HIGH, oem_pm_interval_months=12)
        model_b = DeviceModel.objects.create(manufacturer="BD", model="Alaris B", description="Pump", category="Infusion pumps",
                                             risk_class=RiskClass.HIGH, oem_pm_interval_months=12)
        rel1, rel2 = release(model, "1.0"), release(model, "2.0")
        ce1 = Asset.objects.create(tag="CE-1", device_model=model, department=dept, next_pm_on=today + 30 * DAY)
        ce2 = Asset.objects.create(tag="CE-2", device_model=model, department=dept, next_pm_on=today + 30 * DAY)
        for device in (ce1, ce2):
            facts(device, rel1, release_since=today - 30 * DAY, checked_on=today - 30 * DAY)
        update_wo(ce1, rel2)
        with transaction.atomic():
            services.lock_register()
    return SimpleNamespace(today=today, model=model, model_b=model_b, rel1=rel1, rel2=rel2, ce1=ce1, ce2=ce2)


# The writers of CE-2, as each runs inside its caller's transaction (the caller commits)
WRITERS = {
    "retire": lambda s: eq.set_status(Asset.objects.get(pk=s.ce2.pk), AssetStatus.RETIRED),
    "model": lambda s: eq.update_asset(Asset.objects.get(pk=s.ce2.pk), device_model=s.model_b),
    "hold": lambda s: eq.set_incident_hold(Asset.objects.get(pk=s.ce2.pk)),
    "delete": lambda s: AssetViewSet().perform_destroy(Asset.objects.get(pk=s.ce2.pk)),  # the API's DELETE /api/v1/assets/<id>/
}

# The openers
OPENERS = {
    "one": lambda s: updates.open_update(s.ce2.pk, target="release", target_release=s.rel2.pk, due_on=s.today, priority="normal",
                                         by=None),
    "batch": lambda s: updates.open_updates(s.rel2.pk, from_releases=[s.rel1.pk], due_on=s.today, priority="normal", by=None),
}

# The writer first, then open_update: refused in words, keyed by field
REFUSED = {
    "retire": {"asset": ["CE-2 is retired: its facts stay as they were."]},
    "model": {"target_release": [updates.MODEL_CHANGED.format(tag="CE-2")]},
    "hold": {"asset": [updates.HELD.format(tag="CE-2")]},
    "delete": {"asset": [services.CHOOSE_DEVICE]},
}
# ... or the batch: CE-2 skipped (held as evidence, else "changed": its model changed, or it was retired or deleted, meanwhile)
SKIPPED = {"retire": "changed", "model": "changed", "hold": "held", "delete": "changed"}


def _open_updates_on_ce2(s) -> list:
    return list(WorkOrder.objects.filter(asset_id=s.ce2.pk, type=WoType.SOFTWARE_UPDATE, status__in=OPEN_STATUSES)
                .values_list("number", flat=True))


@pytest.mark.parametrize("opener", ["one", "batch"])
@pytest.mark.parametrize("writer", ["retire", "model", "hold", "delete"])
def test_a_writer_in_flight_first_is_waited_for_and_seen(tenant, writer, opener):
    s = _setup(tenant)
    started, seen = threading.Event(), {}
    errors, results = {}, {}

    def writing():
        with tenant_context(tenant):
            with transaction.atomic():
                WRITERS[writer](s)
                started.set()
                seen["waiting"] = _waiting()  # holding its locks until the opener waits on the device
        return writer

    def opening():
        assert started.wait(20)
        with tenant_context(tenant):
            return OPENERS[opener](s)

    _both(writing, opening, errors, results)
    assert not errors.get("first"), errors
    assert seen["waiting"] and seen["waiting"][0][0] == DEVICE_ROW, seen  # the opener waited on the device's row, not at its COMMIT
    if opener == "one":
        assert "second" not in results and isinstance(errors["second"], ValidationError), (results, errors)
        assert errors["second"].message_dict == REFUSED[writer]
    else:
        assert not errors.get("second"), errors
        batch = results["second"]
        assert (batch.created, batch.already_open, getattr(batch, SKIPPED[writer])) == (0, ("CE-1",), ("CE-2",)), batch
    with tenant_context(tenant):
        assert _open_updates_on_ce2(s) == []  # nothing opened on CE-2
        assert SoftwareUpdate.objects.filter(asset_id=s.ce2.pk).count() == 0
        device = Asset.objects.filter(pk=s.ce2.pk).first()
        assert (device is None) == (writer == "delete")


@pytest.mark.parametrize("opener", ["one", "batch"])
@pytest.mark.parametrize("writer", ["retire", "model", "hold", "delete"])
def test_an_opener_first_holds_the_device_until_it_commits(tenant, monkeypatch, writer, opener):
    s = _setup(tenant)
    started, seen = threading.Event(), {}
    errors, results = {}, {}
    real = services._locked_devices

    def paused(ids):
        out = real(ids)  # the device FOR KEY SHARE
        if threading.current_thread().name == "first" and not started.is_set():
            started.set()
            seen["waiting"] = _waiting()  # holding it until the writer waits on it
        return out

    monkeypatch.setattr(services, "_locked_devices", paused)

    def opening():
        with tenant_context(tenant):
            return OPENERS[opener](s)

    def writing():
        assert started.wait(20)
        with tenant_context(tenant):
            with transaction.atomic():
                WRITERS[writer](s)
        return writer

    _both(opening, writing, errors, results)
    assert not errors.get("first"), errors  # the opener opened its update
    assert seen["waiting"] and seen["waiting"][0][0] == DEVICE_ROW, seen  # the writer waited on the device's row
    with tenant_context(tenant):
        (number,) = WorkOrder.objects.filter(asset_id=s.ce2.pk, type=WoType.SOFTWARE_UPDATE).values_list("number", flat=True)
        device = Asset.objects.get(pk=s.ce2.pk)
        if writer == "retire":  # then retired: the update it found is cancelled, as retiring cancels an open update
            assert not errors and device.status == AssetStatus.RETIRED and _open_updates_on_ce2(s) == []
            assert WorkOrder.objects.get(number=number).status == WoStatus.CANCELLED
        elif writer == "delete":  # then refused in words: the device has work now
            assert isinstance(errors["second"], ApiValidationError), errors
            assert "cannot be deleted" in str(errors["second"].detail["detail"])
            assert _open_updates_on_ce2(s) == [number]
        else:  # then changed as one after the other: the update was opened before the model change or the hold
            assert not errors, errors
            assert _open_updates_on_ce2(s) == [number]
            assert (device.device_model_id, device.incident_hold) == (
                (s.model_b.pk, False) if writer == "model" else (s.model.pk, True))


@pytest.mark.parametrize("opener", ["one", "batch"])
def test_a_deferred_key_failing_at_commit_answers_race(tenant, monkeypatch, opener):
    """The backstop (C4): with the device's lock taken away (a plain read in its place), a delete that holds the device first lets the
    opener through to its COMMIT, where the work order's deferred key fails once the delete commits: RACE in words, never a 500."""
    s = _setup(tenant)
    started, seen = threading.Event(), {}
    errors, results = {}, {}
    monkeypatch.setattr(services, "_locked_devices", lambda ids: list(Asset.objects.filter(pk__in=list(ids)).order_by("tag")))

    def deleting():
        with tenant_context(tenant):
            with transaction.atomic():
                WRITERS["delete"](s)
                started.set()
                seen["waiting"] = _waiting()
        return "deleted"

    def opening():
        assert started.wait(20)
        with tenant_context(tenant):
            return OPENERS[opener](s)

    _both(deleting, opening, errors, results)
    assert results.get("first") == "deleted" and isinstance(errors.get("second"), ValidationError), (results, errors)
    assert errors["second"].message_dict == {"asset" if opener == "one" else "release": [services.RACE]}
    assert seen["waiting"] and seen["waiting"][0][0] == DEVICE_ROW, seen  # the opener waited only at its COMMIT, on the device
    with tenant_context(tenant):
        assert not Asset.objects.filter(pk=s.ce2.pk).exists() and SoftwareUpdate.objects.count() == 1  # CE-1's, from the setup
