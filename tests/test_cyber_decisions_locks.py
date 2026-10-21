"""
Slice 39 wave 1 on PostgreSQL: the decisions' writers against the writers they meet, two threads each (skipped on SQLite, which runs one
writer at a time). The first pauses holding what it has locked until the second waits on a lock (read from pg_stat_activity and
pg_locks), then goes on. Each finishes, never in a deadlock, and answers in words where it refuses (build39 section 5).

1. A device's decision and a retire of that device, in both orders: they meet only on the device's row (equipment's writers never take
   the register lock). The retire first: the decision's FOR KEY SHARE waits for its FOR UPDATE, then reads the device retired and refuses
   in words. The decision first: the retire's FOR UPDATE waits for its KEY SHARE, then retires the device; the decision then governs no
   device here (its Check says to end it).
2. Two decisions for one device at once: the second waits on the register lock, then reads the first's and refuses (in force).
3. Review and renew while another ends the same decision: the second waits on the register, then reads the decision ended.
4. Under the row-level security policies as the runtime role: the writers and readers see this facility's rows only.
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
from pg_helpers import as_app_role, needs_postgres

from apps.cybersecurity import decisions, rules, services
from apps.cybersecurity.decisions import end_decision, record_decision
from apps.cybersecurity.models import AcceptedBy, Connects, DecisionEnd, DecisionResponse, DeviceSecurityDecision
from apps.equipment import services as eq
from apps.equipment.models import Asset, AssetStatus, Department, DeviceModel, RiskClass
from apps.tenants.context import tenant_context

DAY = timedelta(days=1)
REGISTER = ("core_sequence", "AccessExclusiveLock")  # the register lock (its Sequence row FOR UPDATE)
BASE = {"reasons": ["security_updates"], "response": DecisionResponse.KEEP_WITHOUT_ADDED_CONTROLS, "accepted_by": AcceptedBy.CE_LEADER,
        "by": None}

pytestmark = [needs_postgres]


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
    [("statement", its first 27 characters)], else [] after `timeout` (tests/test_cyber_updates_pg.py's)."""
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
    assert all(isinstance(e, ValidationError) for e in errors.values()), errors


def _setup(tenant):
    """A pump model that can connect, 5.2 (demo) past its security updates, CE-1 of ours on it; the register's and the decision
    numbering's rows exist (a facility's first write would otherwise make the second wait on its insert)."""
    today = timezone.localdate()
    with tenant_context(tenant):
        dept = Department.objects.create(name="ICU")
        model = DeviceModel.objects.create(manufacturer="BD", model="Alaris A", description="Pump", category="Infusion pumps",
                                           risk_class=RiskClass.HIGH, oem_pm_interval_months=12)
        release(model, "", connects=Connects.WIRELESS)
        r52 = release(model, "5.2 (demo)", security_updates_end_on=today - 30 * DAY)
        ce1 = Asset.objects.create(tag="CE-1", device_model=model, department=dept, next_pm_on=today + 30 * DAY)
        facts(ce1, r52)
        with transaction.atomic():
            services.lock_register()
            from apps.core.models import Sequence

            Sequence.objects.get_or_create(key=f"decision-{today.year % 100:02d}")
    return SimpleNamespace(today=today, model=model, r52=r52, ce1=ce1)


# --- 1. a device's decision and its retire --------------------------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("first_is", ["retire", "decision"])
def test_a_devices_decision_and_its_retire_meet_on_its_row(tenant, monkeypatch, first_is):
    s = _setup(tenant)
    started, seen = threading.Event(), {}
    if first_is == "retire":
        _pause_first(monkeypatch, eq, "_locked_row", started, seen)  # the retire holds the device FOR UPDATE
    else:
        _pause_first(monkeypatch, services, "_locked_devices", started, seen)  # the decision holds the device FOR KEY SHARE
    calls: list = []
    real_register = services.lock_register

    def register_spy():
        calls.append(threading.current_thread().name)
        return real_register()

    monkeypatch.setattr(services, "lock_register", register_spy)
    errors, results = {}, {}

    def deciding():
        if first_is == "retire":
            assert started.wait(20)
        with tenant_context(tenant):
            return record_decision(asset=s.ce1.pk, **BASE).number

    def retiring():
        if first_is == "decision":
            assert started.wait(20)
        with tenant_context(tenant):
            return eq.set_status(Asset.objects.get(pk=s.ce1.pk), AssetStatus.RETIRED).status

    first, second = (retiring, deciding) if first_is == "retire" else (deciding, retiring)
    _both(first, second, errors, results)
    _only_words(errors)
    assert seen["waiting"] and {table for table, _mode in seen["waiting"]} == {"equipment_asset"}, seen  # the device's row only
    retire_key, decision_key = ("first", "second") if first_is == "retire" else ("second", "first")
    assert calls == [decision_key]  # the retire never takes the register lock
    assert results[retire_key] == AssetStatus.RETIRED
    with tenant_context(tenant):
        if first_is == "retire":
            assert errors[decision_key].message_dict == {"asset": ["CE-1 is retired: a decision is for a device on site."]}
            assert not DeviceSecurityDecision.objects.exists()
        else:
            assert not errors and results[decision_key].startswith("SD-")
            d = DeviceSecurityDecision.objects.get()
            assert [c.key for c in rules.decision_checks(d, s.today)] == [rules.CHECK_NO_DEVICE]  # it governs no device here now


# --- 2. two decisions for one device ------------------------------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
def test_two_decisions_for_one_device_take_the_register_in_turn(tenant, monkeypatch):
    s = _setup(tenant)
    started, seen = threading.Event(), {}
    _pause_first(monkeypatch, services, "lock_register", started, seen)
    errors, results = {}, {}

    def deciding(response):
        def run():
            if threading.current_thread().name == "second":
                assert started.wait(20)
            with tenant_context(tenant):
                return record_decision(asset=s.ce1.pk, **{**BASE, "response": response}).number
        return run

    _both(deciding(DecisionResponse.KEEP_WITHOUT_ADDED_CONTROLS), deciding(DecisionResponse.TAKE_OUT_OF_USE), errors, results)
    _only_words(errors)
    assert seen["waiting"] == [REGISTER], seen
    number = results["first"]
    assert errors["second"].message_dict == {"asset": [decisions.IN_FORCE.format(number=number)]}
    with tenant_context(tenant):
        assert DeviceSecurityDecision.objects.get().response == DecisionResponse.KEEP_WITHOUT_ADDED_CONTROLS


# --- 3. review and renew while another ends it -----------------------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
def test_renewing_a_decision_another_is_ending(tenant, monkeypatch):
    s = _setup(tenant)
    with tenant_context(tenant):
        old = record_decision(asset=s.ce1.pk, **BASE)
    started, seen = threading.Event(), {}
    _pause_first(monkeypatch, decisions, "_decision_row", started, seen)  # the end holds the register and the decision's row
    errors, results = {}, {}

    def ending():
        with tenant_context(tenant):
            return end_decision(old.pk, by=None).end_reason

    def renewing():
        assert started.wait(20)
        with tenant_context(tenant):
            return record_decision(asset=s.ce1.pk, supersedes=old.pk, **BASE).number

    _both(ending, renewing, errors, results)
    _only_words(errors)
    assert seen["waiting"] == [REGISTER], seen
    assert results == {"first": DecisionEnd.NO_LONGER_NEEDED}
    assert errors["second"].message_dict == {"supersedes": [decisions.ALREADY_ENDED.format(number=old.number,
                                                                                           day=rules.day_words(s.today))]}


# --- 4. under the policies -------------------------------------------------------------------------------------------------------------

@pytest.mark.django_db
def test_the_writers_and_readers_under_the_policies(ctx, dept, pump_model, other_tenant, make_user, django_assert_num_queries):
    today = timezone.localdate()
    with tenant_context(other_tenant):
        their_dept = Department.objects.create(name="Their ICU")
        theirs = DeviceModel.objects.create(manufacturer="BD", model="Alaris 8015 PCU", description="Pump", category="Infusion pumps")
        release(theirs, "", connects=Connects.WIRELESS)
        their_release = release(theirs, "5.2 (demo)", security_updates_end_on=today - 30 * DAY)
        their_pump = Asset.objects.create(tag="P-1", device_model=theirs, department=their_dept)
        facts(their_pump, their_release)
        their = record_decision(release=their_release, **BASE, today=today)
    release(pump_model, "", connects=Connects.WIRELESS)
    r52 = release(pump_model, "5.2 (demo)", security_updates_end_on=today - 30 * DAY)
    ours = Asset.objects.create(tag="P-1", device_model=pump_model, department=dept)
    facts(ours, r52)
    manager = make_user("manager")
    as_app_role()
    d = record_decision(release=r52, **{**BASE, "by": manager, "decided_on": today - 400 * DAY, "review_by": today - DAY}, today=today)
    assert d.number.endswith("-0001") and their.number.endswith("-0001")  # each facility counts its own
    assert list(DeviceSecurityDecision.objects.values_list("pk", flat=True)) == [d.pk]
    assert rules.governing_decisions(Asset.objects.all(), today) == {ours.pk: d}
    with django_assert_num_queries(1):
        assert rules.badge(today) == {"count": 1, "hot": True}
    panel = rules.needs_attention(today, user=manager)
    assert [h.key for h in panel.headings] == ["review_due"]
    with pytest.raises(ValidationError):
        end_decision(their.pk, by=manager)  # another facility's: never found
    end_decision(d, by=manager, today=today)
    assert rules.badge(today)["count"] == 0
