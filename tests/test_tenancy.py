from datetime import date

import pytest

from apps.equipment.models import Asset, Department, DeviceModel
from apps.tenants.context import tenant_context


def test_queries_are_scoped_to_current_tenant(tenant, other_tenant):
    with tenant_context(tenant):
        d = Department.objects.create(name="ICU")
        dm = DeviceModel.objects.create(manufacturer="BD", model="Alaris", description="Pump", category="Infusion pumps")
        Asset.objects.create(tag="CE-1", device_model=dm, department=d)
        assert Asset.objects.count() == 1
    with tenant_context(other_tenant):
        assert Asset.objects.count() == 0
        assert Department.objects.filter(name="ICU").count() == 0
    assert Asset.unscoped.count() == 1  # unscoped: the test proves the row exists at all


def test_no_tenant_context_returns_nothing(tenant):
    with tenant_context(tenant):
        Department.objects.create(name="ED")
    assert Department.objects.count() == 0


def test_save_without_tenant_raises(db):
    with pytest.raises(RuntimeError):
        Department.objects.create(name="Nowhere")


def test_same_tag_allowed_in_different_tenants(tenant, other_tenant):
    for t in (tenant, other_tenant):
        with tenant_context(t):
            d = Department.objects.create(name="ICU")
            dm = DeviceModel.objects.create(manufacturer="BD", model="Alaris", description="Pump", category="Infusion pumps")
            Asset.objects.create(tag="CE-1", device_model=dm, department=d, installed_on=date.today())
    assert Asset.unscoped.filter(tag="CE-1").count() == 2
