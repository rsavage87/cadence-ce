"""
Slice 38 scaffold: the software update work order type and its touch list. Its record stays in its facility and keeps its shape; the
Overview's series, the lists' short type, and retire/return's cancellable set cover it; it is never a failure, a finding, a re-check, or a
PM tab entry; nothing but the Device security screen opens one (New work order, the API's create, create_work_order without the opener's
keyword, a legacy open row); the API never moves one to another device or type or rewrites its problem, never makes another work order
one, and refuses what it did anywhere but the completion; a delete answers in words; retiring cancels an open one and says so; the
history area needs Device security View and Equipment View.
"""
from datetime import timedelta

import pytest
from cyber_fixtures import release, update_wo
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Level, Module, Role
from apps.core.history import AREAS
from apps.cybersecurity.models import InstalledBy, SoftwareUpdate, UpdateOutcome, UpdateTarget
from apps.cybersecurity.updates import UPDATE_PROBLEM
from apps.equipment import services as eq
from apps.equipment.models import Asset, AssetStatus, Department, DeviceModel
from apps.imports.kinds.work_orders import TYPE_WORDS
from apps.reports.services import WO_TYPE_SERIES
from apps.tenants.context import tenant_context
from apps.testequipment.rules import RECHECK_TYPES
from apps.web.asset_tabs import MAINTENANCE_TYPES
from apps.web.forms import NewWorkOrderForm
from apps.web.templatetags.web import WO_TYPE_SHORT
from apps.web.views_equipment import status_toast
from apps.web.views_temporary import CANCELLABLE
from apps.workorders import findings, legacy
from apps.workorders.completion import starts_on_completion
from apps.workorders.models import Priority, WorkOrder, WoStatus, WoType
from apps.workorders.services import UPDATE_OPENED_ELSEWHERE, change_status, create_work_order


@pytest.fixture
def pump_release(pump_model):
    return release(pump_model, "5.6")


def api_url(wo, tail=""):
    return f"/api/v1/work-orders/{wo.pk}/{tail}"


def custom_role(slug, levels):
    role = Role.objects.create(name=slug.title(), slug=slug)
    role.set_levels(levels)
    return role


def test_an_update_record_stays_in_its_facility(pump, pump_release, other_tenant):
    wo, update = update_wo(pump, pump_release)
    with tenant_context(other_tenant):
        assert not SoftwareUpdate.objects.exists() and not WorkOrder.objects.exists()
        dept = Department.objects.create(name="Their ICU")
        model = DeviceModel.objects.create(manufacturer="BD", model="Alaris 8015 PCU", description="Pump", category="Infusion pumps")
        theirs = Asset.objects.create(tag="THEIRS-1", device_model=model, department=dept)
        update_wo(theirs, release(model, "5.6"))
        assert SoftwareUpdate.objects.count() == 1
    assert SoftwareUpdate.objects.get() == update and update.work_order == wo and wo.problem == UPDATE_PROBLEM


def test_the_record_keeps_its_shape(pump, pump_release):
    wo, _ = update_wo(pump, pump_release)
    hand = {"asset": pump, "outcome": UpdateOutcome.PATCHES, "reference": "ICAR-2026-09", "installed_by": InstalledBy.IT,
            "applied_on": timezone.localdate()}
    SoftwareUpdate.objects.create(**hand)  # a hand row: an install with an installer, no work order, no target
    bad = [
        {**hand, "installed_by": ""},  # a hand row names who installed it
        {**hand, "outcome": UpdateOutcome.ALREADY},  # and is an install
        {**hand, "target": UpdateTarget.PATCHES},  # and has no target
        {"asset": pump, "work_order": create_work_order(asset=pump, type=WoType.SOFTWARE_UPDATE, priority=Priority.NORMAL,
                                                        problem=UPDATE_PROBLEM, _register_opener=True),
         "target": UpdateTarget.RELEASE, "opened_on": timezone.localdate()},  # a release target names its release
    ]
    for fields in bad:
        with pytest.raises(IntegrityError), transaction.atomic():
            SoftwareUpdate.objects.create(**fields)
    with pytest.raises(IntegrityError), transaction.atomic():  # one record per work order
        SoftwareUpdate.objects.create(asset=pump, work_order=wo, target=UpdateTarget.PATCHES, opened_on=timezone.localdate())


def test_the_type_is_in_every_list_that_names_the_types():
    series = [t for _key, _label, types in WO_TYPE_SERIES for t in types]
    assert sorted(series) == sorted(WoType.values) and len(series) == len(set(series))
    assert [key for key, *_ in WO_TYPE_SERIES] == ["pm", "repair", "other"]  # never a fourth key: the Overview's colors
    assert set(WO_TYPE_SHORT) == set(WoType.values) and WO_TYPE_SHORT[WoType.SOFTWARE_UPDATE] == "Update"
    assert WoType.SOFTWARE_UPDATE.label == "Software update"
    assert set(CANCELLABLE) == {WoType.PM, WoType.INSPECTION, WoType.SOFTWARE_UPDATE}
    assert WoType.SOFTWARE_UPDATE not in RECHECK_TYPES and WoType.SOFTWARE_UPDATE not in MAINTENANCE_TYPES
    assert TYPE_WORDS["firmware update"] == TYPE_WORDS["patch"] == TYPE_WORDS["software upgrade"] == WoType.SOFTWARE_UPDATE
    assert "upgrade" not in TYPE_WORDS  # a legacy system's hardware field modification


def test_an_update_is_never_a_failure_or_a_finding(pump, pump_release, techs):
    wo, _ = update_wo(pump, pump_release, assigned_to=techs["tom"])
    done, _ = update_wo(pump, pump_release, assigned_to=techs["tom"])
    change_status(done, WoStatus.IN_PROGRESS)
    change_status(done, WoStatus.COMPLETED)
    assert not WorkOrder.objects.filter(findings.failure_q()).exists()
    assert not WorkOrder.objects.filter(findings.recorded_q()).exists()
    assert starts_on_completion(wo) and not starts_on_completion(done)


def test_only_the_device_security_screen_opens_one(pump, client, make_user):
    with pytest.raises(ValidationError) as e:
        create_work_order(asset=pump, type=WoType.SOFTWARE_UPDATE, priority=Priority.NORMAL, problem="Patch it")
    assert e.value.message_dict == {"type": [UPDATE_OPENED_ELSEWHERE]}
    form = NewWorkOrderForm(data={"asset": pump.tag, "type": WoType.SOFTWARE_UPDATE, "priority": Priority.NORMAL, "problem": "x"},
                            can_assign=False)
    assert not form.is_valid() and form.errors["type"] == [UPDATE_OPENED_ELSEWHERE]
    assert WoType.SOFTWARE_UPDATE not in [c[0] for c in NewWorkOrderForm(can_assign=False).fields["type"].choices]
    client.force_login(make_user("director"))
    due = str(timezone.localdate() + timedelta(days=5))
    r = client.post("/api/v1/work-orders/", {"asset": str(pump.pk), "type": WoType.SOFTWARE_UPDATE, "problem": "x", "due_on": due},
                    content_type="application/json")
    assert r.status_code == 400 and r.json() == {"type": [UPDATE_OPENED_ELSEWHERE]}
    r = client.post("/api/v1/work-orders/", {"asset": str(pump.pk), "type": WoType.REPAIR, "problem": "x", "due_on": due,
                                             "software_outcome": "installed"}, content_type="application/json")
    assert r.status_code == 400 and list(r.json()) == ["software_outcome"]
    assert not WorkOrder.objects.exists()


def test_a_legacy_open_update_is_refused(pump):
    today = timezone.localdate()
    with pytest.raises(ValidationError) as e:
        legacy.import_work_order(asset=pump, legacy_number="sw-1", type=WoType.SOFTWARE_UPDATE, status=WoStatus.OPEN,
                                 opened_on=today - timedelta(days=5), today=today)
    assert e.value.messages == [legacy.OPEN_UPDATE]
    done = legacy.import_work_order(asset=pump, legacy_number="sw-2", type=WoType.SOFTWARE_UPDATE, status=WoStatus.CLOSED,
                                    opened_on=today - timedelta(days=5), completed_on=today - timedelta(days=4), today=today).work_order
    assert done.type == WoType.SOFTWARE_UPDATE and not SoftwareUpdate.objects.exists()  # history: no record, nothing on the device


def test_the_api_never_moves_rewrites_or_makes_an_update(client, make_user, pump, vent, pump_release):
    client.force_login(make_user("director"))
    wo, _ = update_wo(pump, pump_release)

    def patch(target, body):
        return client.patch(api_url(target), body, content_type="application/json")

    r = patch(wo, {"asset": str(vent.pk)})
    assert r.status_code == 400 and r.json() == {"asset": [f"{wo.number} is a software update: it stays with {pump.tag}."]}
    r = patch(wo, {"problem": "Install 5.6 per ICSMA-26-001-01"})
    assert r.status_code == 400 and list(r.json()) == ["problem"]
    r = patch(wo, {"type": WoType.REPAIR})
    assert r.status_code == 400 and list(r.json()) == ["type"]
    repair = create_work_order(asset=pump, type=WoType.REPAIR, priority=Priority.NORMAL, problem="Alarm")
    r = patch(repair, {"type": WoType.SOFTWARE_UPDATE})
    assert r.status_code == 400 and r.json() == {"type": [UPDATE_OPENED_ELSEWHERE]}
    assert patch(wo, {"priority": Priority.HIGH, "problem": UPDATE_PROBLEM}).status_code == 200  # the rest stays editable
    wo.refresh_from_db()
    assert (wo.asset, wo.type, wo.problem, wo.priority) == (pump, WoType.SOFTWARE_UPDATE, UPDATE_PROBLEM, Priority.HIGH)
    r = client.post(api_url(wo, "transition/"), {"status": WoStatus.IN_PROGRESS, "software_outcome": "installed"},
                    content_type="application/json")
    assert r.status_code == 400 and list(r.json()) == ["software_outcome"]


def test_deleting_an_update_work_order_answers_in_words(client, make_user, tenant, pump, pump_release):
    wo, _ = update_wo(pump, pump_release)
    client.force_login(make_user("director"))
    r = client.delete(api_url(wo))
    assert r.status_code == 400 and r.json() == {"detail": f"{wo.number} was opened from the Device security screen: cancel it instead."}
    from apps.accounts.models import User

    reader = User.objects.create_user(username="wo-full", password="Test-Pass-2026-x", tenant=tenant,
                                      role=custom_role("wo-full", {Module.WORKORDERS: Level.FULL, Module.EQUIPMENT: Level.VIEW}))
    client.force_login(reader)
    r = client.delete(api_url(wo))
    assert r.status_code == 400 and r.json() == {"detail": f"{wo.number} has a record on the Device security screen: cancel it instead."}
    assert WorkOrder.objects.filter(pk=wo.pk).exists()


def test_retiring_cancels_an_open_update_and_says_so(pump, pump_release, make_user, client):
    wo, _ = update_wo(pump, pump_release)
    director = make_user("director")
    client.force_login(director)
    r = client.post(reverse("web:asset_status", args=[pump.tag]), {"to": AssetStatus.RETIRED}, HTTP_HX_REQUEST="true")
    assert r.status_code == 200
    wo.refresh_from_db()
    assert wo.status == WoStatus.CANCELLED
    assert status_toast("CE-1", AssetStatus.IN_SERVICE, AssetStatus.RETIRED, updates=1) == "CE-1 retired; 1 software update cancelled"
    assert "1 software update cancelled" in r.headers.get("HX-Trigger", "")


def test_an_in_progress_update_blocks_retiring(pump, pump_release, techs):
    wo, _ = update_wo(pump, pump_release, assigned_to=techs["tom"])
    change_status(wo, WoStatus.IN_PROGRESS)
    with pytest.raises(ValidationError):
        eq.set_status(pump, AssetStatus.RETIRED)


def test_the_history_area_needs_device_security_and_equipment_view():
    area = AREAS["software_updates"]
    assert (area.module, area.also, area.model_path) == ("cybersecurity", ("equipment",), "cybersecurity.SoftwareUpdate")
