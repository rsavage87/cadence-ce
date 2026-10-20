"""
Slice 37 review fixes, round 3 (commit 40b3330), each on its own scenario:

1. Every register writer takes the facility's register lock first (services.lock_register, a Sequence row): a bulk set and a single
   Record facts on one new device, two Record facts on one new device, two adds of one version. The second waits on the register lock
   (never on a device's or a release's row), then runs on what the first saved: both save, in turn; an add of a version the first just
   added is refused in words by the look for it (never the unique key's IntegrityError). model_changed (inside update_asset) never
   takes it: a model change of a device outside a bulk set completes while the bulk set holds the register.
2. The device lock (FOR KEY SHARE in tag order, one query on PostgreSQL): a model change in progress is waited for, then the old model's
   release refused (with and without a security row); a contract's Add model completes while the bulk set holds the devices; AEM's
   pull-in waits for the bulk set, then completes; an import chunk changing models in tag order is waited for (the bulk set refuses
   "not <model>"), and one against tag order ends in a deadlock PostgreSQL breaks: the bulk set answers RACE or the chunk reports its
   row, never a 500. Each device's tag is set against its pk (ids drawn, sorted, then tagged), so no test leans on random UUIDs.
3. The MDS2 link: every key=value part of a fragment (split on & ; ?): "#access_token=X?y=1" refused.
4. The filters: a category in any letter case (the screen, a model's panel, the API, narrow() and keeps()), a department id kept as
   asked (any letter case of it; another facility's or unknown answers none on the screen, as in the API).
5. Add release's hint says what a restore keeps.
"""
import threading
import time
import uuid
from datetime import timedelta

import pytest
from cyber_fixtures import release
from django.core.exceptions import ValidationError
from django.db import DatabaseError, connection, transaction
from django.db.models import F
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from pg_helpers import needs_postgres

from apps.contracts import services as contracts
from apps.contracts.models import Contract
from apps.cybersecurity import filters, rules, services
from apps.cybersecurity.models import DeviceSecurity, Mds2Edition, Mds2Status, Network, SoftwareRelease
from apps.cybersecurity.services import add_release, record_facts, record_facts_for, remove_release
from apps.equipment import services as eq
from apps.equipment.models import Asset, Department, DeviceModel, RiskClass
from apps.pm import aem
from apps.pm.dates import add_months
from apps.tenants.context import tenant_context

DAY = timedelta(days=1)
HX = {"HTTP_HX_REQUEST": "true"}
# What a connection waits for (_waiting): a row lock as the row's table and the tuple lock PostgreSQL holds while it waits on the holder's
# transaction (FOR UPDATE: AccessExclusiveLock, FOR KEY SHARE: AccessShareLock); else the start of its statement (an insert waiting on
# another transaction's uncommitted row with the same unique key).
REGISTER = ("core_sequence", "AccessExclusiveLock")  # the register lock (its Sequence row FOR UPDATE)
REGISTER_INSERT = ("statement", 'INSERT INTO "core_sequence"')  # the facility's first register writes: the lock row's insert waits
DEVICES = ("equipment_asset", "AccessShareLock")  # the device lock (FOR KEY SHARE)
DEVICE_FOR_UPDATE = ("equipment_asset", "AccessExclusiveLock")  # a model or status change, AEM's pull-in (update_asset)


def refused(fn, *args, **kwargs) -> dict:
    with pytest.raises(ValidationError) as e:
        fn(*args, **kwargs)
    return {k: v[0] if len(v) == 1 else v for k, v in e.value.message_dict.items()}


def reasons(obj) -> list:
    return list(type(obj).history.filter(id=obj.pk).order_by("history_date", "history_id").values_list("history_change_reason", flat=True))


@pytest.fixture
def kim(make_user):
    return make_user("director")


# --- PostgreSQL helpers -------------------------------------------------------------------------------------------------------------

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
    assert not a.is_alive() and not b.is_alive()


def _waiting(timeout: float = 10) -> list:
    """What the other connections to this database waiting on a lock wait for, as soon as one does: [(table, tuple lock mode)] for a
    row lock, [("statement", its first 27 characters)] otherwise (pg_stat_activity and pg_locks, read on this thread's connection with
    the statistics snapshot cleared before each look; a statement's text is cut at 1 kB, so a row lock is read from pg_locks), else []
    after `timeout`."""
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


def _waits_on(waiting: list, *locks: tuple) -> bool:
    """One connection waited, for one of `locks` (REGISTER, REGISTER_INSERT, DEVICES, DEVICE_FOR_UPDATE)."""
    return len(waiting) == 1 and waiting[0] in locks


def _pause_first(monkeypatch, name: str, started: threading.Event, seen: dict, *, before: bool = False, until=None):
    """The thread named "first" pauses at its first call of services.<name> (before it or after it), holding what it has locked so far,
    until another connection waits on a lock (seen["waiting"]: what it waits for, _waiting), or, given `until`, until that event is set
    (seen["done_while_held"])."""
    real = getattr(services, name)

    def pause():
        if threading.current_thread().name == "first" and not started.is_set():
            started.set()
            if until is None:
                seen["waiting"] = _waiting()
            else:
                seen["done_while_held"] = until.wait(10)

    def paused(*args, **kwargs):
        if before:
            pause()
        out = real(*args, **kwargs)
        if not before:
            pause()
        return out

    monkeypatch.setattr(services, name, paused)


def _tagged(model, dept, count: int, **fields) -> list:
    """`count` devices of `model` tagged CE-1.. against their pk order: the ids are drawn and sorted first, then each device is tagged by
    its place (CE-1 the highest pk), so tag order is never pk order by chance (round 3: the device lock runs in tag order). In tag
    order."""
    ids = sorted((uuid.uuid4() for _ in range(count)), reverse=True)
    devices = [Asset.objects.create(id=pk, tag=f"CE-{i}", device_model=model, department=dept, **fields) for i, pk in enumerate(ids, 1)]
    assert [d.tag for d in sorted(devices, key=lambda d: d.pk)] == [d.tag for d in reversed(devices)]
    return devices


def _models(tenant, count: int = 2, **device_fields):
    """Model A (BD Alaris A: its releases 1.0 and 2.0) with `count` devices tagged against their pk order; model B."""
    with tenant_context(tenant):
        dept = Department.objects.create(name="ICU")
        a = DeviceModel.objects.create(manufacturer="BD", model="Alaris A", description="Pump", category="Infusion pumps",
                                       risk_class=RiskClass.HIGH, oem_pm_interval_months=12)
        b = DeviceModel.objects.create(manufacturer="BD", model="Alaris B", description="Pump", category="Infusion pumps",
                                       risk_class=RiskClass.HIGH, oem_pm_interval_months=12)
        rel, rel2 = release(a, "1.0"), release(a, "2.0")
        devices = _tagged(a, dept, count, **device_fields)
    return a, b, rel, rel2, devices


def _crossed(tenant) -> list:
    """Devices whose record names a release of another model (never: a release names its model)."""
    with tenant_context(tenant):
        return list(DeviceSecurity.objects.exclude(release=None).exclude(release__device_model_id=F("asset__device_model_id"))
                    .values_list("asset__tag", flat=True))


# --- 1. The register lock: the register's writers one at a time ---------------------------------------------------------------------

@needs_postgres
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("first_is", ["bulk", "record"])
def test_round3_a_bulk_set_and_a_record_on_one_new_device_both_save_in_turn(tenant, monkeypatch, first_is):
    """Round 2's race: Set devices to this release on CE-1 and CE-2 and Record facts on CE-1 (no security row yet) at once. Whichever
    starts first pauses just before its first save (it has looked: no row); the other waits on the register lock, never on the device,
    then runs on the row the first saved. Both save, in turn: CE-1 runs 1.0 and is wired, its history in that order. (Round 2: both
    looked, found no row, and the bulk's insert lost: RACE.)"""
    a, _b, rel, _rel2, (ce1, ce2) = _models(tenant)
    started, seen = threading.Event(), {}
    _pause_first(monkeypatch, "_save", started, seen, before=True)
    errors, results = {}, {}

    def bulk():
        if first_is == "record":
            assert started.wait(20)
        with tenant_context(tenant):
            return record_facts_for(a, [ce1, ce2], by=None, release=rel)

    def single():
        if first_is == "bulk":
            assert started.wait(20)
        with tenant_context(tenant):
            return record_facts(ce1, by=None, network=Network.WIRED).network

    first, second = (bulk, single) if first_is == "bulk" else (single, bulk)
    _both(first, second, errors, results)
    expected = {"first": 2, "second": Network.WIRED} if first_is == "bulk" else {"first": Network.WIRED, "second": 2}
    assert not errors and results == expected, (results, errors)
    assert _waits_on(seen["waiting"], REGISTER, REGISTER_INSERT), seen  # the second waited on the register, not on CE-1
    bulk_reason = "Set with 2 devices of BD Alaris A"
    with tenant_context(tenant):
        assert sorted(DeviceSecurity.objects.values_list("asset__tag", "release_id", "network")) == [
            ("CE-1", rel.pk, Network.WIRED), ("CE-2", rel.pk, "")]
        expected = [bulk_reason, "Facts recorded"] if first_is == "bulk" else ["Facts recorded", bulk_reason]
        assert reasons(DeviceSecurity.objects.get(asset=ce1)) == expected
    assert _crossed(tenant) == []


@needs_postgres
@pytest.mark.django_db(transaction=True)
def test_round3_two_records_on_one_new_device_both_save(tenant, monkeypatch):
    """Two Record facts on CE-1, which has no security row: the first pauses just before it inserts the row; the second waits on the
    register lock, then records on the row the first created (its read-back on the one-to-one key is never needed). One row, both
    facts, two history rows in order."""
    a, _b, rel, _rel2, (ce1, _ce2) = _models(tenant)
    started, seen = threading.Event(), {}
    _pause_first(monkeypatch, "_save", started, seen, before=True)
    real, calls = services._reason, []

    def counted(*args, **kwargs):
        calls.append(threading.current_thread().name)
        return real(*args, **kwargs)

    monkeypatch.setattr(services, "_reason", counted)
    errors, results = {}, {}

    def first():
        with tenant_context(tenant):
            return record_facts(ce1, by=None, release=rel).release_id

    def second():
        assert started.wait(20)
        with tenant_context(tenant):
            return record_facts(ce1, by=None, network=Network.WIRELESS).network

    _both(first, second, errors, results)
    assert not errors and results == {"first": rel.pk, "second": Network.WIRELESS}, (results, errors)
    assert _waits_on(seen["waiting"], REGISTER, REGISTER_INSERT), seen
    assert calls == ["first", "second"]  # one record each: the second found the first's row
    with tenant_context(tenant):
        sec = DeviceSecurity.objects.get()
        assert (sec.asset_id, sec.release_id, sec.network) == (ce1.pk, rel.pk, Network.WIRELESS)
        assert reasons(sec) == ["Facts recorded: release set to 1.0", "Facts recorded"]


@needs_postgres
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("restoring", [False, True])
def test_round3_two_adds_of_one_version_the_second_refused_in_words(tenant, monkeypatch, restoring):
    """Two Add release of 9.0 at once ("9.0" and "V9.0"), new or entered in error before (the first restores it): the first pauses just
    before it saves; the second waits on the register lock (never on the release's row), then its look for the version finds the
    first's and refuses in words, before any insert (no IntegrityError read back). One release, on file."""
    with tenant_context(tenant):
        model = DeviceModel.objects.create(manufacturer="BD", model="Alaris", description="Pump", category="Infusion pumps")
        if restoring:
            remove_release(add_release(model, by=None, version="9.0"), by=None)
    started, seen = threading.Event(), {}
    _pause_first(monkeypatch, "_save", started, seen, before=True)
    real, checks = services._check_row, []

    def check_row(values, *, versioned, errors):
        checks.append((threading.current_thread().name, errors.get("version")))
        return real(values, versioned=versioned, errors=errors)

    monkeypatch.setattr(services, "_check_row", check_row)
    errors, results = {}, {}

    def add(typed, wait):
        def call():
            if wait:
                assert started.wait(20)
            with tenant_context(tenant):
                return add_release(model, by=None, version=typed).version
        return call

    _both(add("9.0", False), add("V9.0", True), errors, results)
    assert results == {"first": "9.0"} and isinstance(errors.get("second"), ValidationError), (results, errors)
    assert errors["second"].message_dict == {"version": ["Version 9.0 is already on file for this model."]}
    # the register lock's row: inserted by the first (the facility's first register write: the second's insert waits on it), or held
    assert _waits_on(seen["waiting"], REGISTER if restoring else REGISTER_INSERT), seen
    assert checks == [("first", None), ("second", "Version 9.0 is already on file for this model.")]  # refused by the look, not the key
    with tenant_context(tenant):
        assert list(SoftwareRelease.objects.values_list("version", "removed_on")) == [("9.0", None)]


@needs_postgres
@pytest.mark.django_db(transaction=True)
def test_round3_model_changed_never_waits_on_the_register_lock(tenant, monkeypatch):
    """A bulk set on CE-1 holds the register lock (and CE-1) while an edit moves CE-3, outside the bulk set, from model A to B: the
    edit's update_asset -> model_changed clears CE-3's release of A and commits while the bulk set still holds the register (a writer of
    devices waits on its devices, never on the register). Then the bulk set completes."""
    a, b, rel, rel2, (ce1, _ce2, ce3) = _models(tenant, count=3)
    with tenant_context(tenant):
        record_facts(ce3, by=None, release=rel2)
    started, changed, seen = threading.Event(), threading.Event(), {}
    _pause_first(monkeypatch, "_locked_devices", started, seen, until=changed)
    errors, results = {}, {}

    def bulk():
        with tenant_context(tenant):
            return record_facts_for(a, [ce1], by=None, release=rel)

    def change_model():
        assert started.wait(20)
        try:
            with tenant_context(tenant):
                return eq.update_asset(Asset.objects.get(pk=ce3.pk), by=None, device_model=b).security_release_cleared
        finally:
            changed.set()

    _both(bulk, change_model, errors, results)
    assert not errors and results == {"first": 1, "second": True}, (results, errors)
    assert seen == {"done_while_held": True}  # the model change completed while the bulk set held the register lock
    with tenant_context(tenant):
        sec = DeviceSecurity.objects.get(asset=ce3)
        assert sec.release_id is None and reasons(sec)[-1] == services.DEVICE_MODEL_CHANGED
        assert DeviceSecurity.objects.get(asset=ce1).release_id == rel.pk
    assert _crossed(tenant) == []


# --- 2. The device lock: FOR KEY SHARE in tag order ------------------------------------------------------------------------------

@needs_postgres
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("with_row", [False, True])
def test_round3_the_bulk_set_waits_for_a_model_change_and_refuses_the_old_release(tenant, with_row):
    """An edit moves CE-2 from A to B (update_asset: CE-2 FOR UPDATE until it commits) while Set devices to this release runs on A's
    CE-1..CE-3 (tagged against their pk order). The bulk set waits on its device lock (FOR KEY SHARE, not the register), then reads model
    B and refuses in words: A's release never on a model-B device, with or without a security row (model_changed cleared that one)."""
    a, b, rel, rel2, devices = _models(tenant, count=3)
    moved = devices[1]
    if with_row:
        with tenant_context(tenant):
            record_facts(moved, by=None, release=rel2)
    changing, seen = threading.Event(), {}
    errors, results = {}, {}

    def change_model():
        with tenant_context(tenant), transaction.atomic():
            eq.update_asset(Asset.objects.get(pk=moved.pk), by=None, device_model=b)
            changing.set()
            seen["waiting"] = _waiting()
        return "committed"

    def bulk():
        assert changing.wait(20)
        with tenant_context(tenant):
            return record_facts_for(a, devices, by=None, release=rel)

    _both(change_model, bulk, errors, results)
    assert results == {"first": "committed"} and isinstance(errors.get("second"), ValidationError), (results, errors)
    assert errors["second"].message_dict == {"assets": ["CE-2 is not BD Alaris A: set one model's devices at a time."]}
    assert _waits_on(seen["waiting"], DEVICES), seen
    assert _crossed(tenant) == []
    with tenant_context(tenant):
        assert Asset.objects.get(pk=moved.pk).device_model_id == b.pk
        assert not DeviceSecurity.objects.filter(release=rel).exists()
        assert list(DeviceSecurity.objects.values_list("release_id", flat=True)) == ([None] if with_row else [])


@needs_postgres
@pytest.mark.django_db(transaction=True)
def test_round3_the_device_lock_is_one_query_in_tag_order(tenant):
    """Round 3: the devices are held FOR KEY SHARE and read in the same query (the queryset's SQL, the facility's filter in it, with the
    clause), in tag order; no second read of the devices."""
    a, _b, rel, _rel2, devices = _models(tenant, count=3)
    with tenant_context(tenant), CaptureQueriesContext(connection) as q:
        assert record_facts_for(a, list(reversed(devices)), by=None, release=rel) == 3
    reads = [s["sql"] for s in q.captured_queries if 'FROM "equipment_asset"' in s["sql"]]
    assert len(reads) == 1, reads
    assert 'ORDER BY "equipment_asset"."tag"' in reads[0] and reads[0].endswith('FOR KEY SHARE OF "equipment_asset"')
    assert '"equipment_asset"."tenant_id" =' in reads[0]
    with tenant_context(tenant):
        saved = list(DeviceSecurity.history.order_by("history_id").values_list("asset_id", flat=True))
        assert saved == [d.pk for d in devices]  # saved in tag order (CE-1..CE-3; their pks run the other way)
        assert [reasons(DeviceSecurity.objects.get(asset=d)) for d in devices] == [["Set with 3 devices of BD Alaris A"]] * 3


@needs_postgres
@pytest.mark.django_db(transaction=True)
def test_round3_a_contract_add_model_completes_while_the_bulk_set_holds_the_devices(tenant, monkeypatch):
    """Set devices to this release holds A's three devices (FOR KEY SHARE, and the register lock) while Contracts > Add model saves them
    onto a contract (plain updates, in tag order): the contract completes without waiting, then the bulk set does. Both done, never RACE.
    (Round 2's test has the contract first.)"""
    a, _b, rel, _rel2, devices = _models(tenant, count=3)
    with tenant_context(tenant):
        today = timezone.localdate()
        contract = contracts.create_contract(reference="SC-2026-3", vendor="BD", start_on=today - 10 * DAY, end_on=today + 300 * DAY,
                                             annual_cost=1000)
    started, done, seen = threading.Event(), threading.Event(), {}
    _pause_first(monkeypatch, "_locked_devices", started, seen, until=done)
    errors, results = {}, {}

    def bulk():
        with tenant_context(tenant):
            return record_facts_for(a, devices, by=None, release=rel)

    def add_model():
        assert started.wait(20)
        try:
            with tenant_context(tenant), transaction.atomic():
                return contracts.add_model(Contract.objects.get(pk=contract.pk), a)
        finally:
            done.set()

    _both(bulk, add_model, errors, results)
    assert errors == {} and results == {"first": 3, "second": 3}, (results, errors)
    assert seen == {"done_while_held": True}
    with tenant_context(tenant):
        assert sorted(Asset.objects.filter(contract_id=contract.pk).values_list("tag", flat=True)) == ["CE-1", "CE-2", "CE-3"]
        assert sorted(DeviceSecurity.objects.filter(release=rel).values_list("asset__tag", flat=True)) == ["CE-1", "CE-2", "CE-3"]


@needs_postgres
@pytest.mark.django_db(transaction=True)
def test_round3_aems_pull_in_waits_for_the_bulk_set_then_completes(tenant, monkeypatch):
    """Set devices to this release holds A's three devices while a PM approver ends A's AEM interval: the pull-in's first update_asset
    (CE-1, FOR UPDATE) waits on the bulk set's device lock; the bulk set completes, then the pull-in moves every next PM in. Both done, in
    turn. (Round 2's test has the pull-in first.)"""
    with tenant_context(tenant):
        last = timezone.localdate() - 30 * DAY
    a, _b, rel, _rel2, devices = _models(tenant, count=3, last_pm_on=last, next_pm_on=add_months(last, 18))
    with tenant_context(tenant):
        DeviceModel.objects.filter(pk=a.pk).update(aem_interval_months=18)
    started, seen = threading.Event(), {}
    _pause_first(monkeypatch, "_locked_devices", started, seen)
    errors, results = {}, {}

    def bulk():
        with tenant_context(tenant):
            return record_facts_for(a, devices, by=None, release=rel)

    def end_aem():
        assert started.wait(20)
        with tenant_context(tenant):
            return aem.end(DeviceModel.objects.get(pk=a.pk), by=None, reason="Back to the manufacturer's interval")

    _both(bulk, end_aem, errors, results)
    assert errors == {} and results == {"first": 3, "second": 3}, (results, errors)
    assert _waits_on(seen["waiting"], DEVICE_FOR_UPDATE), seen  # the pull-in's update_asset, on CE-1
    with tenant_context(tenant):
        assert DeviceModel.objects.get(pk=a.pk).aem_interval_months is None
        assert set(Asset.objects.filter(device_model=a).values_list("next_pm_on", flat=True)) == {add_months(last, 12)}
        assert sorted(DeviceSecurity.objects.filter(release=rel).values_list("asset__tag", flat=True)) == ["CE-1", "CE-2", "CE-3"]


def _chunk(tenant, devices, model, changing: threading.Event, seen: dict):
    """An import chunk: one transaction, each row in its own savepoint catching DatabaseError (apps.imports.services._apply's way),
    changing each device's model in the file's order; after the first, it waits until another connection waits on a lock."""
    def run():
        outcomes = []
        with tenant_context(tenant), transaction.atomic():
            for i, d in enumerate(devices):
                try:
                    with transaction.atomic():
                        eq.update_asset(Asset.objects.get(pk=d.pk), by=None, device_model=model)
                    outcomes.append("ok")
                except DatabaseError as e:
                    outcomes.append(f"db error: {type(e).__name__}")
                if i == 0:
                    changing.set()
                    seen["waiting"] = _waiting()
        return outcomes
    return run


@needs_postgres
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("with_rows", [False, True])
def test_round3_an_import_chunk_in_tag_order_is_waited_for(tenant, with_rows):
    """A chunk changes CE-1 then CE-2 (tag order; their pks run the other way) from A to B while Set devices to this release runs on A:
    the bulk set waits on CE-1 (its device lock) before it holds CE-2, the chunk changes CE-2 and commits, and the bulk set refuses in
    words. Never a deadlock, never A's release on a model-B device."""
    a, b, rel, rel2, devices = _models(tenant)
    if with_rows:
        with tenant_context(tenant):
            for d in devices:
                record_facts(d, by=None, release=rel2)
    changing, seen = threading.Event(), {}
    errors, results = {}, {}

    def bulk():
        assert changing.wait(20)
        with tenant_context(tenant):
            return record_facts_for(a, devices, by=None, release=rel)

    _both(_chunk(tenant, devices, b, changing, seen), bulk, errors, results)
    assert results.get("first") == ["ok", "ok"], (results, errors)
    assert isinstance(errors.get("second"), ValidationError), (results, errors)
    assert errors["second"].message_dict == {"assets": ["CE-1, CE-2 are not BD Alaris A: set one model's devices at a time."]}
    assert _waits_on(seen["waiting"], DEVICES), seen
    assert _crossed(tenant) == []
    with tenant_context(tenant):
        assert set(Asset.objects.values_list("device_model_id", flat=True)) == {b.pk}
        assert list(DeviceSecurity.objects.values_list("release_id", flat=True)) == ([None, None] if with_rows else [])


@needs_postgres
@pytest.mark.django_db(transaction=True)
def test_round3_an_import_chunk_against_tag_order_never_makes_the_bulk_set_a_500(tenant):
    """A chunk changes CE-2 then CE-1 (against tag order: the documented deadlock) while Set devices to this release runs on A: the bulk
    set holds CE-1 and waits on CE-2, the chunk waits on CE-1. PostgreSQL ends one side: the bulk set answers RACE in words (the chunk
    then changes both), or the chunk reports CE-1's row (the bulk set then reads CE-2 moved and refuses in words). Never a 500 for the
    bulk set, never A's release on a model-B device, nothing of the bulk set saved."""
    a, b, rel, _rel2, (ce1, ce2) = _models(tenant)
    changing, seen = threading.Event(), {}
    errors, results = {}, {}

    def bulk():
        assert changing.wait(20)
        with tenant_context(tenant):
            return record_facts_for(a, [ce1, ce2], by=None, release=rel)

    _both(_chunk(tenant, [ce2, ce1], b, changing, seen), bulk, errors, results)
    assert "second" not in results and isinstance(errors.get("second"), ValidationError), (results, errors)
    assert _waits_on(seen["waiting"], DEVICES), seen
    with tenant_context(tenant):
        moved = dict(Asset.objects.values_list("tag", "device_model_id"))
    if results.get("first") == ["ok", "ok"]:  # PostgreSQL ended the bulk set
        assert errors["second"].message_dict == {"assets": [services.RACE]}
        assert moved == {"CE-1": b.pk, "CE-2": b.pk}
    else:  # PostgreSQL ended the chunk's CE-1 row, which it reports
        assert results.get("first") == ["ok", "db error: OperationalError"], (results, errors)
        assert errors["second"].message_dict == {"assets": ["CE-2 is not BD Alaris A: set one model's devices at a time."]}
        assert moved == {"CE-1": a.pk, "CE-2": b.pk}
    assert _crossed(tenant) == []
    with tenant_context(tenant):
        assert not DeviceSecurity.objects.exists()


# --- 3. The MDS2 link: every key=value part of a fragment ------------------------------------------------------------------------

def _mds2(url: str) -> dict:
    return {"version": "9.1", "mds2": Mds2Status.ON_FILE, "mds2_edition": Mds2Edition.E2019, "mds2_url": url}


@pytest.mark.parametrize("url", [
    "https://vendor.example.com/mds2.pdf#access_token=X?y=1",  # the skeptics' link: a credential before the fragment's "?"
    "https://vendor.example.com/mds2.pdf#sid=abc123?page=2",
    "https://vendor.example.com/mds2.pdf#x=1?y=2?sig=abc123",
    "https://vendor.example.com/portal#/route;session=abc123?x=1",
    "https://vendor.example.com/portal#/download/access_token=abc123",
    "https://vendor.example.com/portal#/docs?view=full&api_key=abc123",
])
def test_round3_a_fragment_credential_anywhere_is_refused(ctx, pump_model, kim, url):
    assert refused(add_release, pump_model, by=kim, **_mds2(url)) == {"mds2_url": services.URL_WORDS}
    assert not SoftwareRelease.objects.exists()


@pytest.mark.parametrize("url", [
    "https://vendor.example.com/mds2.pdf#page=3?view=fit",
    "https://vendor.example.com/mds2.pdf#x=1?y=2",
    "https://vendor.example.com/portal#/cdp/search?keyword=MDS2&page=2",
    "https://vendor.example.com/portal#author=acme?designId=7",
    "https://vendor.example.com/docs#/docs/key-management",  # a bare route: no key=value part
    "https://vendor.example.com/docs#session-management",  # a bare anchor
    "https://vendor.example.com/portal#/download/file=mds2.pdf?lang=en",
])
def test_round3_a_fragment_with_ordinary_keys_is_kept(ctx, pump_model, kim, url):
    assert add_release(pump_model, by=kim, **_mds2(url)).mds2_url == url


# --- 4. The filters: a category in any letter case, a department id as asked -----------------------------------------------------

def test_round3_parse_keeps_a_department_id_and_a_category_as_asked():
    known = ["Infusion pumps", "Ventilators"]
    dept = uuid.uuid4()
    assert filters.parse({"category": "INFUSION PUMPS"}, categories=known).category == "Infusion pumps"  # the facility's letter case
    assert filters.parse({"category": "  ventilators "}, categories=known).category == "Ventilators"
    assert filters.parse({"category": "Sterilizers"}, categories=known).category == "Sterilizers"  # none here: kept, answers none
    assert filters.parse({"category": "a\x00b"}, categories=known).category == ""
    assert filters.parse({"category": "x" * 121}, categories=known).category == "x" * 121  # any length, as the API: answers none
    assert filters.parse_strict({"category": " Infusion pumps "}).category == "Infusion pumps"  # trimmed, as the screen
    assert filters.parse({"dept": str(dept).upper()}).dept == str(dept)  # any department id, in its canonical form
    assert filters.parse({"dept": "nope"}).dept == ""


def _standings(qs):
    return rules.standings(qs, timezone.localdate())


def test_round3_narrow_and_keeps_compare_the_category_in_any_letter_case(ctx, dept, pump, vent):
    on_site = rules.on_site()
    assert list(filters.narrow(on_site, filters.FleetFilters(category="infusion PUMPS")).values_list("tag", flat=True)) == [pump.tag]
    assert list(filters.narrow(on_site, filters.FleetFilters(category="VENTILATORS")).values_list("tag", flat=True)) == [vent.tag]
    read = _standings(on_site)
    f = filters.FleetFilters(category="ventilators")
    assert [s.asset.tag for s in read.values() if f.keeps(s)] == [vent.tag]


@pytest.fixture
def two_departments(ctx, dept, pump, vent, pump_model, other_tenant):
    """The pump and the vent in the ICU, a second pump in 4 West; another facility's department."""
    west = Department.objects.create(name="4 West")
    Asset.objects.create(tag="CE-20001", device_model=pump_model, department=west)
    with tenant_context(other_tenant):
        theirs = Department.objects.create(name="Their ICU")
    return {"icu": dept, "west": west, "theirs": theirs}


def _screen(client, query: str) -> list:
    """The Fleet's rows for `query`: (model, devices)."""
    r = client.get(f"/security/?{query}")
    assert r.status_code == 200
    return sorted((x["r"].device_model.model, x["r"].devices) for x in r.context["rows"])


def _api(client, query: str) -> list:
    r = client.get(f"/api/v1/device-security/?{query}")
    assert r.status_code == 200, r.content
    return sorted(d["tag"] for d in r.json()["results"])


def test_round3_the_screen_and_the_api_answer_a_category_in_any_letter_case(client, make_user, two_departments, pump_model):
    client.force_login(make_user("technician"))
    assert _screen(client, "category=infusion%20PUMPS") == [("Alaris 8015 PCU", 2)]
    assert _api(client, "category=infusion%20PUMPS") == ["CE-10002", "CE-20001"]
    assert _screen(client, "category=ventilators") == [("Hamilton-G5", 1)]
    assert _api(client, "category=ventilators") == ["CE-10001"]
    body = client.get("/security/?category=infusion%20PUMPS").content.decode()
    assert '<option value="Infusion pumps" selected>Infusion pumps</option>' in body  # the facility's letter case, chosen
    # a model's panel keeps its devices by the same rule: another category's answers none of them
    panel = client.get(f"/security/models/{pump_model.pk}/?category=infusion%20pumps", **HX).context
    assert panel["devices_count"] == 2 and panel["narrowed"]
    assert client.get(f"/security/models/{pump_model.pk}/?category=ventilators", **HX).context["devices_count"] == 0


def test_round3_the_screen_and_the_api_answer_a_department_id_as_asked(client, make_user, two_departments):
    client.force_login(make_user("technician"))
    icu, west, theirs = (two_departments[k].pk for k in ("icu", "west", "theirs"))
    assert _screen(client, f"dept={str(west).upper()}") == [("Alaris 8015 PCU", 1)]  # the id in capitals: the same department
    assert _api(client, f"dept={str(west).upper()}") == ["CE-20001"]
    assert _screen(client, f"dept={icu}") == [("Alaris 8015 PCU", 1), ("Hamilton-G5", 1)]
    for other in (theirs, uuid.uuid4()):  # another facility's department, or none at all: none, on the screen as in the API
        assert _screen(client, f"dept={other}") == [] and _api(client, f"dept={other}") == []
    assert len(_screen(client, "dept=nope")) == 2  # not an id: dropped on the screen (the API refuses it in words)
    assert client.get("/api/v1/device-security/?dept=nope").status_code == 400


# --- 5. Add release's hint ------------------------------------------------------------------------------------------------------

def test_round3_the_add_release_hint_says_what_a_restore_keeps(client, ctx, make_user, pump_model):
    client.force_login(make_user("director"))
    hint = "adding a version entered in error again keeps the facts it had in each group left empty"
    assert hint in client.get(f"/security/models/{pump_model.pk}/releases/new/", **HX).content.decode()
    assert hint not in client.get(f"/security/models/{pump_model.pk}/releases/new/?profile=1", **HX).content.decode()
