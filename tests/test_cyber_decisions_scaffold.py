"""
Slice 39 scaffold: a security decision stays in its facility; its shape holds in the database on SQLite and PostgreSQL (exactly one scope,
an end with its reason, a review after the decision, one live decision per scope for devices past support, one number per facility);
the history area needs Device security View and Equipment View; deciding needs Approve and Equipment View, never a scoped user.
"""
from datetime import timedelta

import pytest
from cyber_fixtures import release
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.accounts.models import Level, Module, Role
from apps.core.history import AREAS
from apps.cybersecurity import permissions as cyber_perms
from apps.cybersecurity.models import AcceptedBy, AppliesTo, DecisionEnd, DecisionResponse, DeviceSecurityDecision
from apps.equipment.models import Asset, Department, DeviceModel
from apps.tenants.context import tenant_context

TODAY = timezone.localdate


def decision(n=1, **fields) -> DeviceSecurityDecision:
    today = TODAY()
    values = {"number": f"SD-26-{n:04d}", "applies_to": AppliesTo.SUPPORT_ENDED, "reasons": ["security_updates"],
              "response": DecisionResponse.KEEP_WITHOUT_ADDED_CONTROLS, "accepted_by": AcceptedBy.CE_LEADER, "decided_on": today,
              "review_by": today + timedelta(days=365), **fields}
    return DeviceSecurityDecision.objects.create(**values)


def refused(**fields) -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        decision(99, **fields)


def test_a_decision_stays_in_its_facility(ctx, pump, other_tenant):
    decision(asset=pump)
    with tenant_context(other_tenant):
        assert not DeviceSecurityDecision.objects.exists()
        dept = Department.objects.create(name="Their ICU")
        model = DeviceModel.objects.create(manufacturer="BD", model="Alaris 8015 PCU", description="Pump", category="Infusion pumps")
        theirs = Asset.objects.create(tag="THEIRS-1", device_model=model, department=dept)
        decision(asset=theirs)  # the same number in another facility
        assert DeviceSecurityDecision.objects.count() == 1
    assert DeviceSecurityDecision.objects.count() == 1


def test_exactly_one_scope(ctx, pump, pump_model):
    five_two = release(pump_model, "5.2")
    decision(1, asset=pump)
    decision(2, device_model=pump_model, release=five_two)
    decision(3, device_model=pump_model)
    refused()  # none
    refused(asset=pump, device_model=pump_model)
    refused(release=five_two)  # a release without its model
    refused(asset=pump, release=five_two)


def test_one_live_decision_per_scope_for_devices_past_support(ctx, pump, pump_model):
    five_two = release(pump_model, "5.2")
    first = decision(1, asset=pump)
    refused(asset=pump)
    decision(2, device_model=pump_model, release=five_two)
    refused(device_model=pump_model, release=five_two)
    decision(3, device_model=pump_model)
    refused(device_model=pump_model)
    DeviceSecurityDecision.objects.filter(pk=first.pk).update(ended_on=TODAY(), end_reason=DecisionEnd.SUPERSEDED)
    decision(4, asset=pump)  # the ended one no longer counts


def test_the_number_is_one_per_facility(ctx, pump, vent):
    decision(1, asset=pump)
    with pytest.raises(IntegrityError), transaction.atomic():
        decision(1, asset=vent)


def test_an_end_has_its_reason_and_a_review_comes_after_the_decision(ctx, pump, vent):
    refused(asset=pump, ended_on=TODAY())
    refused(asset=pump, end_reason=DecisionEnd.NO_LONGER_NEEDED)
    refused(asset=pump, review_by=TODAY())
    decision(1, asset=pump, decided_on=TODAY() - timedelta(days=400), review_by=TODAY() - timedelta(days=35))  # an exception already lapsed
    decision(2, asset=vent, ended_on=TODAY(), end_reason=DecisionEnd.ENTERED_IN_ERROR)


def test_the_history_area_needs_device_security_and_equipment_view():
    area = AREAS["device_security_decisions"]
    assert (area.module, area.also, area.model_path) == ("cybersecurity", ("equipment",), "cybersecurity.DeviceSecurityDecision")


@pytest.mark.parametrize(("cyber", "equipment", "decides"), [
    (Level.APPROVE, Level.VIEW, True),
    (Level.FULL, Level.FULL, True),
    (Level.EDIT, Level.VIEW, False),
    (Level.APPROVE, Level.NONE, False),
])
def test_deciding_needs_approve_and_equipment_view(ctx, make_user, cyber, equipment, decides):
    role = Role.objects.create(name="Custom", slug="custom")
    role.set_levels({Module.CYBERSECURITY: cyber, Module.EQUIPMENT: equipment})
    user = make_user("custom")
    assert cyber_perms.can_decide(user) is decides


@pytest.mark.parametrize("slug", ["vendor", "requester"])
def test_a_scoped_user_never_decides(ctx, make_user, slug):
    role = Role.objects.get(slug=slug)
    role.set_levels({Module.CYBERSECURITY: Level.FULL, Module.EQUIPMENT: Level.FULL})
    assert not cyber_perms.can_decide(make_user(slug))
