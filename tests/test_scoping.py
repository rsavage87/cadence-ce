"""Slice 16: apps.workorders.scoping, who sees which devices and work orders inside their facility, on real rows.

A company-scoped user (the vendor technician) sees vendor service under their company's name or "<name> field service", in any
letter case, and the devices those work orders are on; a department-scoped user (the clinical requester) sees the devices in their
unit and those devices' work orders; a scoped user without a company or a matching department sees nothing; everyone else sees
the whole facility, and another facility never."""
from datetime import date, timedelta

import pytest

from apps.accounts import services as accounts
from apps.accounts.models import DEFAULT_SCOPES, DataScope, Role, User, create_default_roles
from apps.equipment import services as eq
from apps.equipment.models import Asset, Department, DeviceModel
from apps.tenants.context import tenant_context
from apps.workorders import scoping
from apps.workorders.models import WorkOrder
from apps.workorders.services import assign, create_work_order


def _user(make_user, slug, *, company="", department="", username=None, tenant_=None):
    user = make_user(slug, tenant_=tenant_, username=username or f"{slug}-{company or department or 'none'}@riverside.example".replace(" ", "-").lower())
    user.company, user.department = company, department
    user.save(update_fields=["company", "department"])
    return user


def _wo(asset, *, vendor_name="", vendor_service=None, problem="Alarm will not clear", technician=None):
    vendor_service = bool(vendor_name) if vendor_service is None else vendor_service
    return create_work_order(asset=asset, type="repair", priority="normal", problem=problem, vendor_service=vendor_service, vendor_name=vendor_name,
                             assigned_to=technician)


@pytest.fixture
def ed(ctx):
    return Department.objects.create(name="ED")


@pytest.fixture
def monitor(ctx, ed):
    dm = DeviceModel.objects.create(manufacturer="Philips", model="IntelliVue MX750", description="Patient monitor", category="Patient monitoring",
                                    risk_class="high", oem_pm_interval_months=12, list_cost=12500)
    return Asset.objects.create(tag="CE-10003", device_model=dm, department=ed, acquisition_cost=12500, next_pm_on=date.today() + timedelta(days=60))


def _ids(qs):
    return {o.pk for o in qs}


# --- company scope -----------------------------------------------------------------------------------------------------------

def test_a_company_sees_vendor_service_under_its_name_or_field_service_name_in_any_case(ctx, make_user, vent, pump, monitor, techs):
    philips = _wo(vent, vendor_name="Philips")
    field = _wo(pump, vendor_name="PHILIPS Field Service")
    healthcare = _wo(monitor, vendor_name="Philips Healthcare")  # another company whose name starts the same
    labelled_in_house = _wo(monitor, vendor_name="Philips", vendor_service=False)  # not dispatched to the vendor
    in_house = _wo(monitor, technician=techs["dana"])
    vendor = _user(make_user, "vendor", company="philips")
    assert _ids(scoping.work_orders(vendor)) == {philips.pk, field.pk}
    assert _ids(scoping.assets(vendor)) == {vent.pk, pump.pk}
    assert scoping.can_see_work_order(vendor, philips) and scoping.can_see_work_order(vendor, field)
    assert not any(scoping.can_see_work_order(vendor, wo) for wo in (healthcare, labelled_in_house, in_house))
    assert scoping.can_see_asset(vendor, pump) and not scoping.can_see_asset(vendor, monitor)

    other = _user(make_user, "vendor", company="Philips Healthcare", username="ph@riverside.example")
    assert _ids(scoping.work_orders(other)) == {healthcare.pk} and _ids(scoping.assets(other)) == {monitor.pk}
    # the field service name itself is a company too: it sees work orders under exactly that name
    fse = _user(make_user, "vendor", company="Philips field service", username="fse@riverside.example")
    assert _ids(scoping.work_orders(fse)) == {field.pk}
    # a name with stray spaces around it still matches; a partial name does not
    spaced = _user(make_user, "vendor", company="  PHILIPS ", username="sp@riverside.example")
    assert _ids(scoping.work_orders(spaced)) == {philips.pk, field.pk}
    partial = _user(make_user, "vendor", company="Phil", username="phil@riverside.example")
    assert not scoping.work_orders(partial).exists() and not scoping.assets(partial).exists()


def test_a_company_scope_narrows_the_queryset_it_is_given(ctx, make_user, vent, pump):
    open_wo = _wo(vent, vendor_name="Philips")
    done = _wo(pump, vendor_name="Philips")
    WorkOrder.objects.filter(pk=done.pk).update(status="closed")
    vendor = _user(make_user, "vendor", company="Philips")
    assert _ids(scoping.work_orders(vendor, WorkOrder.objects.filter(status="open"))) == {open_wo.pk}
    assert _ids(scoping.work_orders(vendor)) == {open_wo.pk, done.pk}  # closed work stays in their history
    assert _ids(scoping.assets(vendor, Asset.objects.filter(tag="CE-10002"))) == {pump.pk}
    # company_q reaches the work orders from another model
    assert set(Asset.objects.filter(scoping.company_q("philips", prefix="work_orders__")).values_list("pk", flat=True)) == {vent.pk, pump.pk}


def test_a_work_order_reassigned_in_house_leaves_the_vendors_view(ctx, make_user, vent, techs):
    wo = _wo(vent, vendor_name="Philips")
    vendor = _user(make_user, "vendor", company="Philips")
    assert scoping.can_see_work_order(vendor, wo) and scoping.can_see_asset(vendor, vent)
    assign(wo, technician=techs["dana"])
    assert not scoping.can_see_work_order(vendor, wo) and not scoping.can_see_asset(vendor, vent)
    assert not scoping.work_orders(vendor).exists() and not scoping.assets(vendor).exists()
    assign(wo, vendor_name="Philips field service")
    assert scoping.can_see_work_order(vendor, wo) and scoping.can_see_asset(vendor, vent)
    assign(wo, vendor_name="GE HealthCare")  # handed to another company
    assert not scoping.can_see_work_order(vendor, wo)


# --- department scope --------------------------------------------------------------------------------------------------------

def test_a_department_sees_its_units_devices_and_their_work_orders_in_any_case(ctx, make_user, vent, pump, monitor):
    on_vent, on_monitor = _wo(vent), _wo(monitor, vendor_name="Philips")
    icu = _user(make_user, "requester", department="icu")
    assert _ids(scoping.assets(icu)) == {vent.pk, pump.pk}
    assert _ids(scoping.work_orders(icu)) == {on_vent.pk}
    assert scoping.can_see_asset(icu, pump) and not scoping.can_see_asset(icu, monitor)
    assert scoping.can_see_work_order(icu, on_vent) and not scoping.can_see_work_order(icu, on_monitor)
    ed = _user(make_user, "requester", department=" ED ", username="ed@riverside.example")
    assert _ids(scoping.assets(ed)) == {monitor.pk} and _ids(scoping.work_orders(ed)) == {on_monitor.pk}


def test_a_device_moved_to_another_department_takes_its_work_orders_with_it(ctx, make_user, vent, pump, ed):
    old = _wo(pump, problem="Door latch loose")
    icu = _user(make_user, "requester", department="ICU")
    er = _user(make_user, "requester", department="ED", username="ed@riverside.example")
    assert scoping.can_see_work_order(icu, old) and not scoping.can_see_work_order(er, old)
    eq.update_asset(pump, department=ed)
    assert _ids(scoping.assets(icu)) == {vent.pk} and not scoping.can_see_work_order(icu, old)
    assert _ids(scoping.assets(er)) == {pump.pk} and _ids(scoping.work_orders(er)) == {old.pk}


# --- nothing, everything, and the role's own scope ----------------------------------------------------------------------------

def test_a_scoped_user_without_a_company_or_a_matching_department_sees_nothing(ctx, make_user, vent, pump):
    wo = _wo(vent, vendor_name="Philips")
    _wo(pump)
    nobody = [_user(make_user, "vendor", username="v0@riverside.example"),
              _user(make_user, "vendor", company="   ", username="v1@riverside.example"),
              _user(make_user, "requester", username="r0@riverside.example"),
              _user(make_user, "requester", department="  ", username="r1@riverside.example"),
              _user(make_user, "requester", department="Cardiology", username="r2@riverside.example")]  # no such department here
    for user in nobody:
        assert scoping.is_scoped(user), user.username
        assert not scoping.work_orders(user).exists() and not scoping.assets(user).exists(), user.username
        assert not scoping.can_see_work_order(user, wo) and not scoping.can_see_asset(user, vent), user.username


def test_superusers_and_roles_without_a_narrow_scope_see_the_whole_facility(ctx, make_user, vent, pump, monitor):
    wos = {_wo(vent).pk, _wo(pump, vendor_name="Philips").pk, _wo(monitor, vendor_name="Siemens Healthineers").pk}
    devices = {vent.pk, pump.pk, monitor.pk}
    everyone = [make_user(slug) for slug in ("director", "manager", "technician", "analyst")]
    root = _user(make_user, "vendor", username="root@riverside.example")  # a vendor role, but a superuser
    root.is_superuser = True
    root.save(update_fields=["is_superuser"])
    roleless = User.objects.create_user(username="norole@riverside.example", password="Test-Pass-2026-x", tenant=ctx)
    for user in [*everyone, root, roleless]:
        assert scoping.scope_of(user) == DataScope.FACILITY and not scoping.is_scoped(user), user.username
        assert _ids(scoping.work_orders(user)) == wos and _ids(scoping.assets(user)) == devices, user.username
        assert all(scoping.can_see_asset(user, a) for a in (vent, pump, monitor)), user.username


def test_the_default_scopes_come_from_the_slug_and_an_explicit_scope_overrides_them(ctx, make_user, vent, pump, monitor):
    roles = {r.slug: r for r in Role.objects.all()}
    assert {slug: r.effective_scope for slug, r in roles.items()} == {
        "director": "facility", "manager": "facility", "technician": "facility", "analyst": "facility", "vendor": "company", "requester": "department"}
    assert DEFAULT_SCOPES == {"vendor": DataScope.COMPANY, "requester": DataScope.DEPARTMENT} and all(not r.scope for r in roles.values())
    on_vent, on_monitor = _wo(vent, vendor_name="Philips"), _wo(monitor)

    # set on the role itself (as Admin can): the explicit scope wins over the slug's default
    roles["vendor"].scope = DataScope.FACILITY
    roles["vendor"].save()
    vendor = _user(make_user, "vendor")
    assert not scoping.is_scoped(vendor) and _ids(scoping.work_orders(vendor)) == {on_vent.pk, on_monitor.pk}
    roles["technician"].scope = DataScope.DEPARTMENT
    roles["technician"].save()
    tech = _user(make_user, "technician", department="ICU")
    assert _ids(scoping.assets(tech)) == {vent.pk, pump.pk} and _ids(scoping.work_orders(tech)) == {on_vent.pk}
    roles["requester"].scope = DataScope.COMPANY
    roles["requester"].save()
    requester = _user(make_user, "requester", company="Philips", department="ED")
    assert _ids(scoping.work_orders(requester)) == {on_vent.pk} and _ids(scoping.assets(requester)) == {vent.pk}

    # a custom role keeps the scope it was made with, whatever its name
    custom = accounts.create_role(name="Vendor", copy_from=roles["vendor"], scope=DataScope.FACILITY)
    assert custom.slug == "vendor-2" and custom.effective_scope == DataScope.FACILITY
    lead = accounts.create_role(name="Biomed contractor", copy_from=roles["technician"], scope=DataScope.COMPANY)
    contractor = make_user("technician", username="c@riverside.example")
    contractor.role, contractor.company = lead, "Philips"
    contractor.save()
    assert scoping.scope_of(contractor) == DataScope.COMPANY and _ids(scoping.work_orders(contractor)) == {on_vent.pk}


# --- facilities ------------------------------------------------------------------------------------------------------------

def test_another_facility_is_never_visible(ctx, tenant, other_tenant, make_user, vent):
    ours = _wo(vent, vendor_name="Philips")
    create_default_roles(other_tenant)
    with tenant_context(other_tenant):
        their_icu = Department.objects.create(name="ICU")
        dm = DeviceModel.objects.create(manufacturer="Philips", model="IntelliVue MX750", description="Patient monitor", category="Patient monitoring")
        their_asset = Asset.objects.create(tag="CE-10001", device_model=dm, department=their_icu, next_pm_on=date.today() + timedelta(days=30))
        theirs = _wo(their_asset, vendor_name="Philips")
        their_vendor = _user(make_user, "vendor", company="Philips", username="v@other.example", tenant_=other_tenant)
    vendor = _user(make_user, "vendor", company="Philips")
    requester = _user(make_user, "requester", department="ICU")
    director = make_user("director")
    for user in (vendor, requester, director):
        assert theirs.pk not in _ids(scoping.work_orders(user)) and their_asset.pk not in _ids(scoping.assets(user)), user.username
    # A single record reaches can_see_* through the facility's own manager (tenant isolation); for a scoped user the check itself
    # also stays inside the facility. (For a facility-wide user it does not look again: a record from elsewhere is the caller's bug.)
    for user in (vendor, requester):
        assert not scoping.can_see_work_order(user, theirs) and not scoping.can_see_asset(user, their_asset), user.username
    assert _ids(scoping.work_orders(vendor)) == {ours.pk} and _ids(scoping.assets(requester)) == {vent.pk}
    with tenant_context(other_tenant):
        assert _ids(scoping.work_orders(their_vendor)) == {theirs.pk} and ours.pk not in _ids(scoping.work_orders(their_vendor))
