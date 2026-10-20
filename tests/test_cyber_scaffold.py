"""
Slice 37 scaffold: the register's rows stay in their facility; the Device security module's defaults reach roles made before it (a
custom role reads None); the role matrix shows it beside Equipment; a device whose model changes loses its release (with history);
the version key; the product text rule; the history areas need the module (a device's facts, Equipment View too); the nav entry.
"""
import pytest
from cyber_fixtures import facts, release
from django.core.exceptions import ValidationError

from apps.accounts.models import Level, Module, Role, RolePermission
from apps.core.history import AREAS
from apps.core.validators import product_text
from apps.cybersecurity.models import DeviceSecurity, SoftwareRelease
from apps.cybersecurity.services import DEVICE_MODEL_CHANGED, version_key
from apps.equipment import services as eq
from apps.equipment.models import Asset, Department, DeviceModel
from apps.tenants.context import tenant_context


def test_releases_and_facts_stay_in_their_facility(vent, vent_model, other_tenant):
    r = release(vent_model, "3.1")
    facts(vent, release=r, network="wireless")
    with tenant_context(other_tenant):
        assert not SoftwareRelease.objects.exists() and not DeviceSecurity.objects.exists()
        dept = Department.objects.create(name="Their ICU")
        model = DeviceModel.objects.create(manufacturer="Hamilton Medical", model="Hamilton-G5", description="Ventilator", category="Ventilators")
        theirs = Asset.objects.create(tag="THEIRS-1", device_model=model, department=dept)
        facts(theirs, release=release(model, "3.1"))  # the same version: unique per facility, never across facilities
        assert SoftwareRelease.objects.count() == 1 and DeviceSecurity.objects.count() == 1
    assert SoftwareRelease.objects.get() == r and DeviceSecurity.objects.get().asset == vent


@pytest.mark.parametrize("slug,level", [("director", Level.FULL), ("manager", Level.APPROVE), ("technician", Level.EDIT), ("analyst", Level.NONE),
                                        ("requester", Level.NONE), ("vendor", Level.NONE)])
def test_a_role_made_before_device_security_takes_its_default(ctx, slug, level):
    role = Role.objects.get(slug=slug)
    RolePermission.objects.filter(role=role, module=Module.CYBERSECURITY).delete()  # a facility from before slice 37
    role = Role.objects.get(pk=role.pk)
    assert role.levels()[Module.CYBERSECURITY] == level == role.level_for(Module.CYBERSECURITY)


def test_a_custom_role_reads_none_and_the_matrix_shows_the_module(ctx):
    from apps.web.views_users import ROLE_MATRIX_MODULES

    custom = Role.objects.create(name="Quality", slug="quality")
    custom.set_levels({Module.REPORTS: Level.VIEW})
    assert custom.levels()[Module.CYBERSECURITY] == Level.NONE
    assert ROLE_MATRIX_MODULES[:2] == [Module.EQUIPMENT, Module.CYBERSECURITY]
    assert Module.CYBERSECURITY.label == "Device security"


def test_a_device_whose_model_changes_loses_its_release(vent, vent_model, pump_model, make_user):
    r = release(vent_model, "3.1")
    facts(vent, release=r, release_since=vent.installed_on)
    kim = make_user("director")
    same = eq.update_asset(vent, by=kim, room="12")  # no model change: kept
    assert not same.security_release_cleared and DeviceSecurity.objects.get(asset=vent).release == r
    changed = eq.update_asset(vent, by=kim, device_model=pump_model)
    sec = DeviceSecurity.objects.get(asset=vent)
    assert changed.security_release_cleared and (sec.release, sec.release_since) == (None, None)
    last = sec.history.order_by("-history_date").first()
    assert (last.history_change_reason, last.history_user) == (DEVICE_MODEL_CHANGED, kim)
    assert not eq.update_asset(vent, by=kim, device_model=vent_model).security_release_cleared  # nothing left to clear


def test_a_device_with_no_facts_changes_model_quietly(vent, pump_model):
    assert not eq.update_asset(vent, device_model=pump_model).security_release_cleared and not DeviceSecurity.objects.exists()


@pytest.mark.parametrize("typed,key", [("12.1.2", "12.1.2"), ("V12.1.2", "12.1.2"), ("v 12.1.2", "12.1.2"), ("Rev. 12.1.2", "12.1.2"),
                                       ("rev12.1.2", "12.1.2"), ("  Rel.  M.00.03 ", "rel. m.00.03"), ("Version 2", "version 2"),
                                       ("vX", "vx"), ("", "")])
def test_the_version_key(typed, key):
    assert version_key(typed) == key


@pytest.mark.parametrize("text,ok", [("Windows 10 IoT Enterprise LTSC 2021", True), ("Rel. M.00.03", True), ("9.33.1 (build 77)", True),
                                     ("C++ runtime", True), ("admin@device", False), ("http://x", False), (" leading", False),
                                     ("pass:word", False), ("a;b", False)])
def test_product_text(text, ok):
    check = product_text("no")
    if ok:
        check(text)
    else:
        with pytest.raises(ValidationError):
            check(text)


def test_the_history_areas_need_device_security():
    assert (AREAS["software_releases"].module, AREAS["software_releases"].also) == ("cybersecurity", ())
    assert (AREAS["device_security"].module, AREAS["device_security"].also) == ("cybersecurity", ("equipment",))


def test_the_nav_offers_device_security_only_to_its_readers(client, make_user):
    from apps.web.context_processors import nav_entries

    assert "cybersecurity" in [row[0] for row in nav_entries(make_user("technician"))]
    assert "cybersecurity" not in [row[0] for row in nav_entries(make_user("analyst"))]
    assert "cybersecurity" not in [row[0] for row in nav_entries(make_user("vendor"))]
