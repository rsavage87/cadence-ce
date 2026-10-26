"""
Slice 40 wave 1B on PostgreSQL: a decision for an advisory's devices against the writers that never take the register (critique40_locks
B3 items 3 and 4), two threads each (skipped on SQLite, which runs one writer at a time). The first pauses holding what it has locked
until the second waits on a lock (read from pg_stat_activity and pg_locks), then goes on. Each finishes, never in a deadlock, and answers
in words where it refuses, never a 500.

1. An advisory decision for a device and a model change of that device (equipment.services.update_asset: the device FOR UPDATE, its
   release cleared through cybersecurity.services.model_changed), in both orders. They meet only on the device's row (the advisory's row
   comes before it in the decision's lock order, and the change takes neither it nor the register). The change first: the decision's
   FOR KEY SHARE waits, then reads the new model, which the advisory does not name, and refuses keyed asset. The decision first: the
   change waits for its KEY SHARE, then commits; the decision then governs no device that needs a response (its Check says to end it).
2. An advisory decision for a device and its retire, in both orders (slice 39's test, for the advisory key): the same meeting point.
3. refusal_for, would_govern, and network_refusal read outside any transaction (the Record decision modal's GET): no lock taken (FOR
   UPDATE there is an error on PostgreSQL).
4. Under the row-level security policies as the runtime role: an advisory decision recorded and read for this facility only; another
   facility's advisory of the same number is never found.
"""
import threading
from datetime import timedelta
from types import SimpleNamespace

import pytest
from cyber_fixtures import facts, make_advisory, model_row, release
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from pg_helpers import as_app_role, needs_postgres
from test_cyber_decisions_locks import _both, _only_words, _pause_first

from apps.cybersecurity import decisions, rules, services
from apps.cybersecurity.decisions import record_decision, refusal_for
from apps.cybersecurity.models import AcceptedBy, AppliesTo, Connects, DecisionResponse, DeviceSecurityDecision, Network
from apps.equipment import services as eq
from apps.equipment.models import Asset, AssetStatus, Department, DeviceModel, RiskClass
from apps.tenants.context import tenant_context

DAY = timedelta(days=1)
BASE = {"applies_to": AppliesTo.ADVISORY, "response": DecisionResponse.KEEP_WITHOUT_ADDED_CONTROLS, "accepted_by": AcceptedBy.CE_LEADER,
        "by": None}

pytestmark = [needs_postgres]


def _setup(tenant):
    """A pump model that can connect, CE-1 of ours on 5.2 (demo); AD-<yy>-0001 names the pump on 5.2 with the fix 5.6 (demo); another
    pump model it does not name. The register's and the decision numbering's rows exist (a facility's first write would otherwise make
    the second wait on its insert)."""
    today = timezone.localdate()
    yy = today.year % 100
    with tenant_context(tenant):
        dept = Department.objects.create(name="ICU")
        model = DeviceModel.objects.create(manufacturer="BD", model="Alaris A", description="Pump", category="Infusion pumps",
                                           risk_class=RiskClass.HIGH, oem_pm_interval_months=12)
        other = DeviceModel.objects.create(manufacturer="BD", model="Alaris B", description="Pump", category="Infusion pumps",
                                           risk_class=RiskClass.HIGH, oem_pm_interval_months=12)
        release(model, "", connects=Connects.WIRELESS)
        r52, r56 = release(model, "5.2 (demo)"), release(model, "5.6 (demo)")
        ce1 = Asset.objects.create(tag="CE-1", device_model=model, department=dept, next_pm_on=today + 30 * DAY)
        facts(ce1, r52)
        adv = make_advisory(number=f"AD-{yy:02d}-0001", received_on=today - 5 * DAY)
        model_row(adv, model, releases=[r52], fix=r56)
        with transaction.atomic():
            services.lock_register()
            from apps.core.models import Sequence

            Sequence.objects.get_or_create(key=f"decision-{yy:02d}")
    return SimpleNamespace(today=today, model=model, other=other, r52=r52, r56=r56, ce1=ce1, adv=adv)


def _register_spy(monkeypatch) -> list:
    calls: list = []
    real = services.lock_register

    def spied():
        calls.append(threading.current_thread().name)
        return real()

    monkeypatch.setattr(services, "lock_register", spied)
    return calls


# --- 1. an advisory decision and a model change of its device ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("first_is", ["change", "decision"])
def test_an_advisory_decision_and_a_model_change_of_its_device(tenant, monkeypatch, first_is):
    s = _setup(tenant)
    started, seen = threading.Event(), {}
    if first_is == "change":
        _pause_first(monkeypatch, eq, "_locked_row", started, seen)  # the change holds the device FOR UPDATE
    else:
        _pause_first(monkeypatch, services, "_locked_devices", started, seen)  # the decision holds the device FOR KEY SHARE
    calls = _register_spy(monkeypatch)
    errors, results = {}, {}

    def deciding():
        if first_is == "change":
            assert started.wait(20)
        with tenant_context(tenant):
            return record_decision(asset=s.ce1.pk, advisory=s.adv.pk, **BASE).number

    def changing():
        if first_is == "decision":
            assert started.wait(20)
        with tenant_context(tenant):
            return eq.update_asset(Asset.objects.get(pk=s.ce1.pk), device_model=s.other).device_model_id

    first, second = (changing, deciding) if first_is == "change" else (deciding, changing)
    _both(first, second, errors, results)
    _only_words(errors)
    assert seen["waiting"] and {table for table, _mode in seen["waiting"]} == {"equipment_asset"}, seen  # the device's row only
    change_key, decision_key = ("first", "second") if first_is == "change" else ("second", "first")
    assert calls == [decision_key]  # the change never takes the register lock
    assert results[change_key] == s.other.pk
    with tenant_context(tenant):
        if first_is == "change":
            assert errors[decision_key].message_dict == {"asset": [f"{s.adv.number} does not name CE-1's model, the Alaris B."]}
            assert not DeviceSecurityDecision.objects.exists()
        else:
            assert not errors and results[decision_key].startswith("SD-")
            d = DeviceSecurityDecision.objects.select_related(*rules._DECISION_RELATED).get()
            assert rules.release_of(Asset.objects.get(pk=s.ce1.pk)) is None  # model_changed cleared its release
            assert [(c.key, c.words) for c in rules.decision_checks(d, s.today)] == [
                (rules.CHECK_NO_NEED, f"Governs no device that needs a response to {s.adv.number} now: end it")]
            got = refusal_for(asset=s.ce1, supersedes=d, applies_to=AppliesTo.ADVISORY, advisory=s.adv, today=s.today)
            assert got.ends_it and got.words == f"{s.adv.number} does not name CE-1's model, the Alaris B."


# --- 2. an advisory decision and a retire of its device -------------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("first_is", ["retire", "decision"])
def test_an_advisory_decision_and_a_retire_meet_on_the_devices_row(tenant, monkeypatch, first_is):
    s = _setup(tenant)
    started, seen = threading.Event(), {}
    if first_is == "retire":
        _pause_first(monkeypatch, eq, "_locked_row", started, seen)
    else:
        _pause_first(monkeypatch, services, "_locked_devices", started, seen)
    calls = _register_spy(monkeypatch)
    errors, results = {}, {}

    def deciding():
        if first_is == "retire":
            assert started.wait(20)
        with tenant_context(tenant):
            return record_decision(asset=s.ce1.pk, advisory=s.adv.number, **BASE).number

    def retiring():
        if first_is == "decision":
            assert started.wait(20)
        with tenant_context(tenant):
            return eq.set_status(Asset.objects.get(pk=s.ce1.pk), AssetStatus.RETIRED).status

    first, second = (retiring, deciding) if first_is == "retire" else (deciding, retiring)
    _both(first, second, errors, results)
    _only_words(errors)
    assert seen["waiting"] and {table for table, _mode in seen["waiting"]} == {"equipment_asset"}, seen
    retire_key, decision_key = ("first", "second") if first_is == "retire" else ("second", "first")
    assert calls == [decision_key]
    assert results[retire_key] == AssetStatus.RETIRED
    with tenant_context(tenant):
        if first_is == "retire":
            assert errors[decision_key].message_dict == {"asset": ["CE-1 is retired: a decision is for a device on site."]}
            assert not DeviceSecurityDecision.objects.exists()
        else:
            assert not errors and results[decision_key].startswith("SD-")
            d = DeviceSecurityDecision.objects.select_related(*rules._DECISION_RELATED).get()
            assert (d.advisory_id, d.advisory_changes) == (s.adv.pk, 0)
            assert [c.key for c in rules.decision_checks(d, s.today)] == [rules.CHECK_NO_DEVICE]


# --- 3. the GET doors' readers outside any transaction ---------------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
def test_the_scope_readers_take_no_lock_outside_a_transaction(tenant):
    """No test transaction wraps these, as none wraps a GET in production (no ATOMIC_REQUESTS): a FOR UPDATE here would raise."""
    s = _setup(tenant)
    with tenant_context(tenant):
        d = record_decision(release=s.r52, advisory=s.adv, **BASE)
        kw = {"applies_to": AppliesTo.ADVISORY, "advisory": s.adv.number.lower(), "today": s.today}
        assert refusal_for(release=s.r52, supersedes=d.number, **kw) is None
        assert refusal_for(release=s.r52, **kw).kind == decisions.KIND_IN_FORCE
        assert refusal_for(asset=s.ce1, **kw) is None  # a decision of its own may stand beside 5.2's
        assert refusal_for(release=s.r56, **kw).kind == decisions.KIND_GONE
        members = list(rules.scope_devices(device_model=s.model).select_related(*rules._WITH_RECORD))
        assert decisions.would_govern(members, device_model=s.model, applies_to=AppliesTo.ADVISORY, advisory=s.adv.number) == []
        assert decisions.network_refusal(members, s.today).startswith("CE-1 has no connection recorded here")


# --- 4. under the policies -------------------------------------------------------------------------------------------------------------

@pytest.mark.django_db
def test_an_advisory_decision_under_the_policies(ctx, dept, pump_model, other_tenant, make_user):
    today = timezone.localdate()
    yy = today.year % 100
    with tenant_context(other_tenant):
        their_dept = Department.objects.create(name="Their ICU")
        theirs = DeviceModel.objects.create(manufacturer="BD", model="Alaris 8015 PCU", description="Pump", category="Infusion pumps")
        their_release = release(theirs, "5.2 (demo)")
        their_pump = Asset.objects.create(tag="P-1", device_model=theirs, department=their_dept)
        facts(their_pump, their_release)
        their_adv = make_advisory(number=f"AD-{yy:02d}-0001", received_on=today - DAY)
        model_row(their_adv, theirs, versions="all", remedy="no_fix_yet")
        their = record_decision(release=their_release, advisory=their_adv, **BASE, today=today)
    r52 = release(pump_model, "5.2 (demo)")
    ours = Asset.objects.create(tag="P-1", device_model=pump_model, department=dept)
    facts(ours, r52, network=Network.NOT_CONNECTED)
    adv = make_advisory(number=f"AD-{yy:02d}-0001", received_on=today - DAY)
    model_row(adv, pump_model, versions="all", remedy="no_fix_yet")
    manager = make_user("manager")
    as_app_role()
    d = record_decision(release=r52, advisory=adv.number, **{**BASE, "by": manager, "response": DecisionResponse.KEEP_WITH_CONTROLS,
                                                            "controls": ["network_removed"]}, today=today)
    assert d.number.endswith("-0001") and their.number.endswith("-0001")  # each facility counts its own
    assert list(DeviceSecurityDecision.objects.values_list("pk", flat=True)) == [d.pk]
    assert rules.governing_decisions(Asset.objects.all(), today, key=rules.advisory_key(adv)) == {ours.pk: d}
    assert [dd.asset for dd in rules.decision_devices(d, today) if dd.governed] == [ours]
    with pytest.raises(ValidationError) as e:
        record_decision(asset=ours, advisory=their_adv.pk, **{**BASE, "by": manager}, today=today)
    assert e.value.message_dict == {"advisory": [services.CHOOSE_ADVISORY]}
