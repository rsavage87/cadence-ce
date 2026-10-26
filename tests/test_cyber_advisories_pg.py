"""
Slice 40 wave 1A on PostgreSQL: the advisories' writers against the writers they meet, two threads each (skipped on SQLite, which runs
one writer at a time; critique40_locks B3). The first pauses holding what it has locked until the second waits on a lock (read from
pg_stat_activity and pg_locks), or, where the second must never wait, until the second has finished; then it goes on. Each finishes,
never in a deadlock, and answers in words where it refuses. Every connection sets a lock_timeout, so a wrong order fails, never hangs.

1. Set models naming a model, against a rename of that model (equipment.services.update_device_model: FOR UPDATE, never the register),
   both orders: set models first holds nothing on the model until COMMIT (a deferred foreign key), so the rename never waits; the rename
   first holds the model FOR UPDATE, so set models' COMMIT waits for it (its KEY SHARE), then both commit.
2. Two Record advisory with one CVE ID: the second waits on the register, then reads the first's and is refused, keyed cves.
3. Removing an advisory's last two sources at once: the second waits on the register, then reads the last live source and is refused.
4. A release removed while Set models names it, listed or as the fix, both orders (test_cyber_services' "named while it is being
   removed" for advisories): the removal refuses in the advisory's words, or Set models refuses the release entered in error.
5. Close against services.record_facts moving a device onto a release the advisory names, both orders: the facts first, Close waits on
   the register and refuses in words; Close first, the facts wait and then record, and the closed advisory reads the device as named
   since (the predicate Close refuses by).
6. The lock-free readers outside any transaction (as a GET runs): never FOR UPDATE, so never PostgreSQL's error there.
"""
import threading
import time
from datetime import timedelta
from types import SimpleNamespace

import pytest
from cyber_fixtures import facts, release
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.utils import timezone
from pg_helpers import needs_postgres

from apps.cybersecurity import advisories, rules, services
from apps.cybersecurity.models import (
    AdvisoryModel,
    AdvisoryRemedy,
    AdvisorySource,
    AdvisoryStatus,
    AdvisoryVersions,
    DeviceSecurity,
    SecurityAdvisory,
    SoftwareRelease,
)
from apps.equipment import services as eq
from apps.equipment.models import Asset, Department, DeviceModel, RiskClass
from apps.tenants.context import tenant_context

DAY = timedelta(days=1)
REGISTER = ("core_sequence", "AccessExclusiveLock")  # the register lock (its Sequence row FOR UPDATE)

pytestmark = [needs_postgres]


def _thread(target, errors, results, key):
    def run():
        try:
            with connection.cursor() as cur:
                cur.execute("SET lock_timeout = '20s'")  # a wrong order fails, never hangs the suite
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
    [("statement", its first 27 characters)], else [] after `timeout` (tests/test_cyber_decisions_locks.py's)."""
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


def _pause_first(monkeypatch, module, name: str, started: threading.Event, seen: dict, *, when=None):
    """The thread named "first" pauses just after its first call of module.<name> (for which `when(args)` holds, if given), holding
    what it has locked so far, until another connection waits on a lock (seen["waiting"]: what it waits for)."""
    real = getattr(module, name)

    def paused(*args, **kwargs):
        out = real(*args, **kwargs)
        if (threading.current_thread().name == "first" and not started.is_set() and (when is None or when(args))):
            started.set()
            seen["waiting"] = _waiting()
        return out

    monkeypatch.setattr(module, name, paused)


def _only_words(errors: dict) -> None:
    assert all(isinstance(e, ValidationError) for e in errors.values()), errors


def _setup(tenant):
    """A pump model with 5.2 (demo) and 5.6 (demo); CE-1 of ours on 5.6; the register's and this year's advisory numbering's rows
    exist (a facility's first write would otherwise make the second wait on its insert, not its lock)."""
    from apps.core.models import Sequence

    today = timezone.localdate()
    with tenant_context(tenant):
        dept = Department.objects.create(name="ICU")
        model = DeviceModel.objects.create(manufacturer="BD", model="Alaris A", description="Pump", category="Infusion pumps",
                                           risk_class=RiskClass.HIGH, oem_pm_interval_months=12)
        r52, r56 = release(model, "5.2 (demo)"), release(model, "5.6 (demo)")
        ce1 = Asset.objects.create(tag="CE-1", device_model=model, department=dept, next_pm_on=today + 30 * DAY)
        facts(ce1, r56)
        with transaction.atomic():
            services.lock_register()
            Sequence.objects.get_or_create(key=f"advisory-{today.year % 100:02d}")
    return SimpleNamespace(today=today, model=model, r52=r52, r56=r56, ce1=ce1, dept=dept)


def _advisory(tenant, s, **fields):
    """An advisory recorded through its writer (with its first source), as the system."""
    values = {"received_on": s.today - DAY, "cves": "", "source": "manufacturer_bulletin", "reference": "DEMO-ADV-0001", "by": None,
              "today": s.today, **fields}
    with tenant_context(tenant):
        return advisories.record_advisory(**values)


# --- 1. Set models and a rename of its model ------------------------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("first_is", ["set_model", "rename"])
def test_set_model_and_a_model_rename(tenant, monkeypatch, first_is):
    s = _setup(tenant)
    a = _advisory(tenant, s)
    started, seen, renamed = threading.Event(), {}, threading.Event()
    if first_is == "rename":
        _pause_first(monkeypatch, eq, "_save_model", started, seen)  # the rename holds the model FOR UPDATE
    else:
        real = services._save

        def holding(obj, by, reason):  # set models pauses after its row's insert, holding the register, the advisory, the releases
            out = real(obj, by, reason)
            if threading.current_thread().name == "first" and isinstance(obj, AdvisoryModel) and not started.is_set():
                started.set()
                seen["renamed_meanwhile"] = renamed.wait(20)  # the rename never waits on anything set models holds
            return out

        monkeypatch.setattr(services, "_save", holding)
    errors, results = {}, {}

    def setting():
        if first_is == "rename":
            assert started.wait(20)
        with tenant_context(tenant):
            return advisories.set_model(a.pk, device_model=s.model.pk, versions=AdvisoryVersions.LISTED, release_ids=[s.r52.pk],
                                        remedy=AdvisoryRemedy.RELEASE, fix_release=s.r56.pk, by=None, today=s.today).pk

    def renaming():
        if first_is == "set_model":
            assert started.wait(20)
        try:
            with tenant_context(tenant):
                return eq.update_device_model(DeviceModel.objects.get(pk=s.model.pk), model="Alaris B").model
        finally:
            renamed.set()

    first, second = (setting, renaming) if first_is == "set_model" else (renaming, setting)
    _both(first, second, errors, results)
    assert not errors, errors
    if first_is == "rename":
        assert {table for table, _mode in seen["waiting"]} == {"equipment_devicemodel"}, seen  # set models' COMMIT, on the model only
    else:
        assert seen["renamed_meanwhile"] is True
    with tenant_context(tenant):
        row = AdvisoryModel.objects.select_related("device_model").get()
        assert (row.device_model.model, row.fix_release_id, list(row.releases.values_list("release_id", flat=True))) == (
            "Alaris B", s.r56.pk, [s.r52.pk])


# --- 2. two Record advisory with one CVE ID ---------------------------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
def test_two_record_advisories_with_one_cve(tenant, monkeypatch):
    s = _setup(tenant)
    started, seen = threading.Event(), {}
    _pause_first(monkeypatch, services, "lock_register", started, seen)
    errors, results = {}, {}

    def recording(reference):
        def run():
            if threading.current_thread().name == "second":
                assert started.wait(20)
            with tenant_context(tenant):
                return advisories.record_advisory(received_on=s.today, cves="CVE-2026-1111 CVE-2026-2222", source="manufacturer_bulletin",
                                                  reference=reference, by=None, today=s.today).number
        return run

    _both(recording("DEMO-ADV-0001"), recording("DEMO-ADV-0002"), errors, results)
    _only_words(errors)
    assert seen["waiting"] == [REGISTER], seen
    number = results["first"]
    assert errors["second"].message_dict == {
        "cves": [f"CVE-2026-1111 and 1 more are also on {number}: add this as a source of {number}, or record it as its own advisory."]}
    with tenant_context(tenant):
        assert list(SecurityAdvisory.objects.values_list("number", flat=True)) == [number]


# --- 3. the last two sources at once ----------------------------------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
def test_removing_the_last_two_sources_at_once(tenant, monkeypatch):
    s = _setup(tenant)
    a = _advisory(tenant, s)
    with tenant_context(tenant):
        other = advisories.add_source(a, source="other", reference="DEMO-ADV-0001-N", by=None, today=s.today)
        first_source = AdvisorySource.objects.exclude(pk=other.pk).get()
    started, seen = threading.Event(), {}
    _pause_first(monkeypatch, services, "lock_register", started, seen)
    errors, results = {}, {}

    def removing(source):
        def run():
            if threading.current_thread().name == "second":
                assert started.wait(20)
            with tenant_context(tenant):
                return advisories.remove_source(source.pk, by=None, today=s.today).removed_on
        return run

    _both(removing(first_source), removing(other), errors, results)
    _only_words(errors)
    assert seen["waiting"] == [REGISTER], seen
    assert results == {"first": s.today} and errors["second"].message_dict == {"source": [advisories.LAST_SOURCE]}
    with tenant_context(tenant):
        assert AdvisorySource.objects.filter(removed_on__isnull=True).count() == 1


# --- 4. a release removed while Set models names it -------------------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("named_as", ["listed", "fix"])
@pytest.mark.parametrize("first_is", ["set_model", "remove"])
def test_a_release_removed_while_set_model_names_it(tenant, monkeypatch, first_is, named_as):
    s = _setup(tenant)
    a = _advisory(tenant, s)
    with tenant_context(tenant):
        lone = release(s.model, "4.0 (demo)")  # no device runs it: only the advisory can stop its removal
    started, seen = threading.Event(), {}
    _pause_first(monkeypatch, services, "lock_register", started, seen)
    errors, results = {}, {}
    if named_as == "listed":
        values = {"release_ids": [lone.pk], "fix_release": s.r56.pk}
    else:
        values = {"release_ids": [s.r52.pk], "fix_release": lone.pk}

    def setting():
        if first_is == "remove":
            assert started.wait(20)
        with tenant_context(tenant):
            return advisories.set_model(a.pk, device_model=s.model.pk, versions=AdvisoryVersions.LISTED, remedy=AdvisoryRemedy.RELEASE,
                                        by=None, today=s.today, **values).pk

    def removing():
        if first_is == "set_model":
            assert started.wait(20)
        with tenant_context(tenant):
            return services.remove_release(SoftwareRelease.objects.get(pk=lone.pk), by=None).removed_on

    first, second = (setting, removing) if first_is == "set_model" else (removing, setting)
    _both(first, second, errors, results)
    _only_words(errors)
    assert seen["waiting"] == [REGISTER], seen
    with tenant_context(tenant):
        if first_is == "set_model":
            assert errors["second"].message_dict == {"release": [f"{a.number} names this release: change it there first."]}
            assert SoftwareRelease.objects.get(pk=lone.pk).removed_on is None and AdvisoryModel.objects.exists()
        else:
            field = "release_ids" if named_as == "listed" else "fix_release"
            assert errors["second"].message_dict == {field: ["Choose a release of the Alaris A on file."]}
            assert not AdvisoryModel.objects.exists()


# --- 5. Close and a device moved onto a release it names ----------------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("first_is", ["facts", "close"])
def test_close_and_a_device_moved_onto_a_release_it_names(tenant, monkeypatch, first_is):
    s = _setup(tenant)
    a = _advisory(tenant, s)
    with tenant_context(tenant):
        advisories.set_model(a, device_model=s.model, versions=AdvisoryVersions.LISTED, release_ids=[s.r52], remedy=AdvisoryRemedy.RELEASE,
                             fix_release=s.r56, confirm=True, by=None, today=s.today)
        assert advisories.close_refusal(a, s.today) is None  # CE-1 runs the fix: nothing needs a response
    started, seen = threading.Event(), {}
    _pause_first(monkeypatch, services, "lock_register", started, seen)
    errors, results = {}, {}

    def closing():
        if first_is == "facts":
            assert started.wait(20)
        with tenant_context(tenant):
            return advisories.close(a.pk, by=None, today=s.today).status

    def moving():
        if first_is == "close":
            assert started.wait(20)
        with tenant_context(tenant):
            return services.record_facts(Asset.objects.get(pk=s.ce1.pk), by=None, release=s.r52.pk, today=s.today).release_id

    first, second = (moving, closing) if first_is == "facts" else (closing, moving)
    _both(first, second, errors, results)
    _only_words(errors)
    assert seen["waiting"] == [REGISTER], seen
    with tenant_context(tenant):
        assert DeviceSecurity.objects.get(asset=s.ce1).release_id == s.r52.pk
        found = SecurityAdvisory.objects.get(pk=a.pk)
        if first_is == "facts":
            assert errors["second"].message_dict == {"advisory": [
                "Not closed: 1 device on a release it names, or with no release recorded, has no response recorded (CE-1). Open updates "
                "to what the advisory offers, or record a decision for it."]}
            assert found.status == AdvisoryStatus.OPEN
        else:
            assert not errors and found.status == AdvisoryStatus.CLOSED
            assert advisories.close_refusal(found, s.today) is not None  # named since, by the predicate Close refuses by
            assert rules.advisory_counts([found], today=s.today, devices_visible=True)[found.pk].named_since == 1


# --- 6. the readers outside a transaction -----------------------------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
def test_the_lock_free_readers_run_outside_a_transaction(tenant, make_user):
    s = _setup(tenant)
    a = _advisory(tenant, s, cves="CVE-2026-1111")
    with tenant_context(tenant):
        row = advisories.set_model(a, device_model=s.model, versions=AdvisoryVersions.LISTED, release_ids=[s.r52],
                                   remedy=AdvisoryRemedy.RELEASE, fix_release=s.r56, confirm=True, by=None, today=s.today)
        facts(s.ce1, s.r52)
        manager = make_user("manager")
        assert connection.in_atomic_block is False  # autocommit, as a GET runs (no ATOMIC_REQUESTS)
        found = services._locked_advisory(a.number, lock=False)
        assert advisories.close_refusal(found, s.today, user=manager).startswith("Not closed: 1 device")
        assert advisories.not_applicable_refusals(found, s.today)["none_here"] == "Not marked: 1 Alaris A device is recorded on site."
        assert advisories.not_applicable_refusal(found, user=manager) is None
        assert advisories.reopen_refusal(found, user=manager) == f"{a.number} is open."
        assert advisories.entered_in_error_refusal(found, user=manager) is None
        assert advisories.model_removal_refusal(row.pk, user=manager) is None
        assert advisories.source_removal_refusal(found.sources.get()) == advisories.LAST_SOURCE
        assert advisories.set_model_refusal(found) is None
        assert list(advisories.overlaps(["CVE-2026-1111"])) == [a.number]
        assert [h.key for h in rules.advisory_attention(s.today, user=manager).headings] == ["no_response"]
        assert rules.advisory_counts([found], today=s.today, devices_visible=True)[found.pk].words == (
            "Open: 1 device with no response recorded")
        assert rules.advisory_checks(found, today=s.today, devices_visible=True) == []
        assert rules.advisory_badge(s.today) == {"count": 0}
