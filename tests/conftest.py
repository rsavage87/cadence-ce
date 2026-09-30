from datetime import date, timedelta

import pytest
from django.core.cache import cache

from apps.accounts.models import Role, User, create_default_roles
from apps.credentials.models import Credential, Scope, Technician
from apps.equipment.models import Asset, Department, DeviceModel, RiskClass
from apps.recalls.models import Alert, AlertMatch
from apps.tenants.context import tenant_context
from apps.tenants.models import Tenant


@pytest.fixture(autouse=True)
def _empty_cache():
    """Sign-in lockouts and the portal's rate limit count in the cache (process memory in tests); start every test at zero."""
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def tenant(db):
    t = Tenant.objects.create(name="Riverside Regional", slug="riverside")
    create_default_roles(t)
    return t


@pytest.fixture
def other_tenant(db):
    return Tenant.objects.create(name="Other Hospital", slug="other")


@pytest.fixture
def ctx(tenant):
    with tenant_context(tenant):
        yield tenant


@pytest.fixture
def dept(ctx):
    return Department.objects.create(name="ICU")


@pytest.fixture
def vent_model(ctx):
    return DeviceModel.objects.create(manufacturer="Hamilton Medical", model="Hamilton-G5", description="ICU ventilator", category="Ventilators",
                                      risk_class=RiskClass.LIFE_SUPPORT, oem_pm_interval_months=6, expected_life_years=10, list_cost=38000)


@pytest.fixture
def pump_model(ctx):
    return DeviceModel.objects.create(manufacturer="BD", model="Alaris 8015 PCU", description="Infusion pump", category="Infusion pumps",
                                      risk_class=RiskClass.HIGH, oem_pm_interval_months=12, aem_interval_months=18, list_cost=3200)


@pytest.fixture
def vent(ctx, dept, vent_model):
    return Asset.objects.create(tag="CE-10001", device_model=vent_model, department=dept, acquisition_cost=38000,
                                installed_on=date.today() - timedelta(days=800), next_pm_on=date.today() + timedelta(days=10))


@pytest.fixture
def pump(ctx, dept, pump_model):
    return Asset.objects.create(tag="CE-10002", device_model=pump_model, department=dept, acquisition_cost=3200, next_pm_on=date.today() + timedelta(days=90))


@pytest.fixture
def techs(ctx):
    dana = Technician.objects.create(name="Dana Whitfield", title="Lead BMET")
    Credential.objects.create(technician=dana, scope=Scope.MODEL, value="Hamilton-G5", expires_on=date.today() + timedelta(days=400))
    Credential.objects.create(technician=dana, scope=Scope.CATEGORY, value="Infusion pumps")
    tom = Technician.objects.create(name="Tom Okafor", title="BMET I")
    Credential.objects.create(technician=tom, scope=Scope.CATEGORY, value="Infusion pumps", expires_on=date.today() + timedelta(days=30))
    return {"dana": dana, "tom": tom}


@pytest.fixture
def make_user(tenant):
    """Users are not tenant-scoped rows, so this works with or without a tenant context."""

    def _make(role_slug, tenant_=None, username=None):
        t = tenant_ or tenant
        role = Role.unscoped.get(tenant=t, slug=role_slug)  # unscoped: fixtures may run before a tenant context is entered
        return User.objects.create_user(username=username or f"{role_slug}@{t.slug}.example", password="Test-Pass-2026-x", tenant=t, role=role,
                                        first_name=role_slug.title(), last_name="User")

    return _make


@pytest.fixture
def pump_recall(ctx, pump_model):
    alert = Alert.objects.create(source=Alert.Source.FDA, external_id="Z-TEST-1", classification="Class II", manufacturer="BD", product="Alaris pump",
                                 title="Keypad membrane may allow fluid ingress", published_on=date.today() - timedelta(days=3))
    return AlertMatch.objects.create(alert=alert, device_model=pump_model)


@pytest.fixture
def freeze_today(monkeypatch):
    """Pin the clock the Reports views use (views_reports._today), so render tests built on fixed dates pass on any day."""

    def _at(today):
        monkeypatch.setattr("apps.web.views_reports._today", lambda: today)

    return _at
