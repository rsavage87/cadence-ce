"""
A status move's level, checked on the device's row as it is when the move is made (equipment.services.set_status's `level`). The drawer,
the API, and the devices import check the person's Equipment level against the device as they read it (permissions.can_set_status:
Approve to retire or reinstate, Edit for the rest); a device retired by someone else after that read, then "found" or tagged out by a
person with Edit only, used to be reinstated, a move that needs Approve. Now the level is checked again on the locked row: the drawer and
the API refuse (403), the import keeps the status with its level note. A work order's own move (a completion's tag-out) passes no level.
The same holds for the tag-out when a work order is opened (New work order, the API, the portal, an incident's investigation): it moves
the device's row as it is now, locked in the facility's order, and refuses a device retired since it was read (workorders.services.
_tag_out_row). The refusals every level gets (a held device, a returned temporary one) keep their own words, before the level.
"""
import json
from datetime import timedelta

import pytest
from django.core.exceptions import PermissionDenied
from django.utils import timezone

from apps.equipment import permissions as eq_perms
from apps.equipment import services as eq
from apps.equipment.models import Asset, AssetStatus

HX = {"HTTP_HX_REQUEST": "true"}


def _missing(dept, model, tag="CE-2", *, waiting=False):
    return Asset.objects.create(tag=tag, device_model=model, department=dept, status=AssetStatus.MISSING, awaiting_inspection=waiting)


def _retire_after_the_check(monkeypatch, tag="CE-2"):
    """can_set_status as the door calls it (on the device it read), then the device retired by someone else before the move."""
    real = eq_perms.can_set_status

    def check(user, from_status, to_status):
        allowed = real(user, from_status, to_status)
        Asset.objects.filter(tag=tag).update(status=AssetStatus.RETIRED, next_pm_on=None)
        return allowed

    monkeypatch.setattr(eq_perms, "can_set_status", check)


@pytest.mark.django_db
def test_set_status_checks_the_level_on_the_row_as_it_is_now(ctx, dept, pump_model):
    """The caller's copy says missing (Found needs Edit); the row says retired (Reinstate needs Approve)."""
    from apps.accounts.models import Level

    seen = _missing(dept, pump_model)
    Asset.objects.filter(pk=seen.pk).update(status=AssetStatus.RETIRED, next_pm_on=None)
    with pytest.raises(PermissionDenied) as e:
        eq.set_status(seen, AssetStatus.OUT_OF_SERVICE, level=Level.EDIT)
    assert str(e.value) == eq.STATUS_PERMISSION and Asset.objects.get(pk=seen.pk).status == AssetStatus.RETIRED
    eq.set_status(seen, AssetStatus.OUT_OF_SERVICE, level=Level.APPROVE)
    assert Asset.objects.get(pk=seen.pk).status == AssetStatus.OUT_OF_SERVICE
    eq.set_status(seen, AssetStatus.MISSING, level=Level.EDIT)  # a move Edit makes, on the row as it is
    assert Asset.objects.get(pk=seen.pk).status == AssetStatus.MISSING


@pytest.mark.django_db
def test_set_status_with_no_level_is_never_checked(ctx, dept, pump_model):
    """The system and a work order's own moves pass no level (a completion's tag-out acts for a technician who may lack Edit)."""
    seen = _missing(dept, pump_model)
    Asset.objects.filter(pk=seen.pk).update(status=AssetStatus.RETIRED, next_pm_on=None)
    eq.set_status(seen, AssetStatus.OUT_OF_SERVICE)
    assert Asset.objects.get(pk=seen.pk).status == AssetStatus.OUT_OF_SERVICE


@pytest.mark.django_db
def test_the_drawers_found_on_a_device_retired_meanwhile_is_refused_for_edit(client, make_user, ctx, dept, pump_model, monkeypatch):
    client.force_login(make_user("manager"))  # Equipment Edit: Found, not Reinstate
    _missing(dept, pump_model)
    _retire_after_the_check(monkeypatch)
    r = client.post("/equipment/CE-2/status/", {"to": AssetStatus.OUT_OF_SERVICE}, **HX)
    assert r.status_code == 200 and json.loads(r["HX-Trigger"])["toast"] == {"value": eq.STATUS_PERMISSION}  # the drawer as it is now
    assert Asset.objects.get(tag="CE-2").status == AssetStatus.RETIRED


@pytest.mark.django_db
def test_the_drawers_found_on_a_device_retired_meanwhile_reinstates_it_for_approve(client, make_user, ctx, dept, pump_model, monkeypatch):
    client.force_login(make_user("director"))
    _missing(dept, pump_model)
    _retire_after_the_check(monkeypatch)
    r = client.post("/equipment/CE-2/status/", {"to": AssetStatus.OUT_OF_SERVICE}, **HX)
    assert r.status_code == 200
    assert Asset.objects.get(tag="CE-2").status == AssetStatus.OUT_OF_SERVICE


@pytest.mark.django_db
def test_the_apis_status_action_on_a_device_retired_meanwhile_is_refused_for_edit(client, make_user, ctx, dept, pump_model, monkeypatch):
    client.force_login(make_user("manager"))
    device = _missing(dept, pump_model)
    _retire_after_the_check(monkeypatch)
    r = client.post(f"/api/v1/assets/{device.id}/status/", {"to": AssetStatus.OUT_OF_SERVICE}, content_type="application/json")
    assert r.status_code == 403 and r.json()["detail"] == eq.STATUS_PERMISSION
    assert Asset.objects.get(tag="CE-2").status == AssetStatus.RETIRED


@pytest.mark.django_db
@pytest.mark.parametrize("waiting", [False, True], ids=["in use before", "waiting for its incoming inspection"])
@pytest.mark.parametrize("slug", ["manager", "director"])
def test_a_devices_import_keeps_the_status_of_a_device_retired_since_it_read_it(ctx, dept, pump_model, make_user, monkeypatch, waiting, slug):
    """CE-2 is missing when the chunk reads it (Found: Edit) and retired right after; the file says Out of service. For Equipment Edit
    the status is kept with the level note (also for a device waiting for its incoming inspection, which set_status refuses first as
    changed meanwhile: the chunk took no numbering); for Approve it is reinstated, or, waiting, kept as changed meanwhile."""
    from test_numbering_lock_order import _import

    from apps.imports.kinds import devices as kind
    from apps.workorders import inspections

    _missing(dept, pump_model, waiting=waiting)
    real = kind.lock_devices_work

    def then_retired(ids, *also):
        real(ids, *also)
        Asset.objects.filter(tag="CE-2").update(status=AssetStatus.RETIRED, next_pm_on=None)

    monkeypatch.setattr(kind, "lock_devices_work", then_retired)
    run = _import(make_user(slug), b"Tag,Manufacturer,Model,Room,Status\r\nCE-2,,,B2,Out of service\r\n")
    notes = [note for row in run.results for note in row[3]]
    device = Asset.objects.get(tag="CE-2")
    if slug == "manager":
        assert kind.LEVEL_NOTE in notes and device.status == AssetStatus.RETIRED, run.results
    elif waiting:
        assert "Status kept: CE-2 changed meanwhile: try again." in notes and device.status == AssetStatus.RETIRED, run.results
        assert inspections.open_inspection(device) is None
    else:
        assert device.status == AssetStatus.OUT_OF_SERVICE and not any(n.startswith("Status kept") for n in notes), run.results
    assert device.room == "B2"  # the row's other changes stay


def _rental_missing(dept, model, tag="CE-9"):
    from apps.equipment.models import Ownership

    return Asset.objects.create(tag=tag, device_model=model, department=dept, status=AssetStatus.MISSING, ownership=Ownership.RENTAL,
                                owner="RentCo Medical")


@pytest.mark.django_db
def test_a_rental_returned_meanwhile_keeps_its_own_words_before_the_level(ctx, dept, pump_model):
    """Returned to its owner since the caller read it: no level reinstates it, so set_status says so, never "needs Approve"."""
    from django.core.exceptions import ValidationError

    from apps.accounts.models import Level

    seen = _rental_missing(dept, pump_model)
    Asset.objects.filter(pk=seen.pk).update(status=AssetStatus.RETIRED)
    with pytest.raises(ValidationError) as e:
        eq.set_status(seen, AssetStatus.OUT_OF_SERVICE, level=Level.EDIT)
    assert e.value.messages == [eq.temporary_status_refusal(Asset.objects.get(pk=seen.pk), AssetStatus.OUT_OF_SERVICE)]


@pytest.mark.django_db
def test_a_devices_import_words_a_rental_returned_since_it_read_it_as_returned(ctx, dept, pump_model, make_user, monkeypatch):
    from test_numbering_lock_order import _import

    from apps.imports.kinds import devices as kind

    _rental_missing(dept, pump_model)
    real = kind.lock_devices_work

    def then_returned(ids, *also):
        real(ids, *also)
        Asset.objects.filter(tag="CE-9").update(status=AssetStatus.RETIRED)

    monkeypatch.setattr(kind, "lock_devices_work", then_returned)
    run = _import(make_user("manager"), b"Tag,Manufacturer,Model,Room,Status\r\nCE-9,,,B2,Out of service\r\n")
    notes = [note for row in run.results for note in row[3]]
    assert kind.RETURNED_KEPT_NOTE in notes and kind.LEVEL_NOTE not in notes, run.results


# --- the tag-out when a work order is opened ------------------------------------------------------------------------------------------

@pytest.mark.django_db
@pytest.mark.parametrize("rental", [False, True], ids=["retired", "returned to its owner"])
def test_a_tag_out_on_a_device_retired_since_it_was_read_is_refused(ctx, dept, pump_model, rental):
    from django.core.exceptions import ValidationError

    from apps.workorders.models import Priority, WorkOrder, WoType
    from apps.workorders.services import TAG_OUT_REFUSED, create_work_order

    seen = (_rental_missing(dept, pump_model) if rental else
            Asset.objects.create(tag="CE-9", device_model=pump_model, department=dept, status=AssetStatus.IN_SERVICE))
    seen.status = AssetStatus.IN_SERVICE  # the copy the door read
    Asset.objects.filter(pk=seen.pk).update(status=AssetStatus.RETIRED)
    with pytest.raises(ValidationError) as e:
        create_work_order(asset=seen, type=WoType.REPAIR, priority=Priority.HIGH, problem="Alarm", tag_out=True)
    state = "returned to its owner" if rental else "retired"
    assert e.value.message_dict == {"asset": [TAG_OUT_REFUSED.format(tag="CE-9", state=state)]}
    assert Asset.objects.get(pk=seen.pk).status == AssetStatus.RETIRED and not WorkOrder.objects.exists()


@pytest.mark.django_db
def test_a_tag_out_moves_the_row_as_it_is(ctx, dept, pump_model):
    """In service on the row: out of service (the caller's copy follows). Missing on the row though the copy says in service: the work
    order opens and the status stays missing (the tag-out moves only a device in service)."""
    from apps.workorders.models import Priority, WoType
    from apps.workorders.services import create_work_order

    one = Asset.objects.create(tag="CE-1", device_model=pump_model, department=dept)
    create_work_order(asset=one, type=WoType.REPAIR, priority=Priority.HIGH, problem="Alarm", tag_out=True)
    assert one.status == AssetStatus.OUT_OF_SERVICE and Asset.objects.get(pk=one.pk).status == AssetStatus.OUT_OF_SERVICE
    two = Asset.objects.create(tag="CE-2", device_model=pump_model, department=dept)
    Asset.objects.filter(pk=two.pk).update(status=AssetStatus.MISSING)
    wo = create_work_order(asset=two, type=WoType.REPAIR, priority=Priority.HIGH, problem="Alarm", tag_out=True)
    assert wo.tagged_out and Asset.objects.get(pk=two.pk).status == AssetStatus.MISSING


@pytest.mark.django_db
def test_new_work_order_tagging_out_a_device_retired_since_the_form_read_it(client, make_user, ctx, dept, pump_model, monkeypatch):
    from apps.web.forms import NewWorkOrderForm
    from apps.workorders.models import WorkOrder

    client.force_login(make_user("technician"))
    Asset.objects.create(tag="CE-9", device_model=pump_model, department=dept)
    real = NewWorkOrderForm._lookup_asset

    def then_retired(tag):
        found = real(tag)
        Asset.objects.filter(tag="CE-9").update(status=AssetStatus.RETIRED, next_pm_on=None)
        return found

    monkeypatch.setattr(NewWorkOrderForm, "_lookup_asset", staticmethod(then_retired))
    r = client.post("/work-orders/new/", {"asset": "CE-9", "type": "repair", "priority": "high", "problem": "Alarm will not clear",
                                          "tag_out": "on"}, **HX)
    assert r.status_code == 200 and "CE-9 is retired, so no work order tags it out." in r.content.decode()
    assert Asset.objects.get(tag="CE-9").status == AssetStatus.RETIRED and not WorkOrder.objects.exists()


@pytest.mark.django_db
def test_the_apis_create_tagging_out_a_retired_device_is_a_400_on_asset(client, make_user, ctx, dept, pump_model):
    from apps.workorders.models import WorkOrder

    client.force_login(make_user("technician"))
    device = Asset.objects.create(tag="CE-9", device_model=pump_model, department=dept, status=AssetStatus.RETIRED)
    due = (timezone.localdate() + timedelta(days=7)).isoformat()
    r = client.post("/api/v1/work-orders/", {"asset": str(device.id), "type": "repair", "priority": "high", "problem": "Alarm", "due_on": due,
                                             "tagged_out": True}, content_type="application/json")
    assert r.status_code == 400 and r.json() == {"asset": ["CE-9 is retired, so no work order tags it out."]}
    assert not WorkOrder.objects.exists()


@pytest.mark.django_db
def test_the_portal_tagging_out_a_device_retired_since_the_form_read_it(client, tenant, ctx, dept, pump_model, monkeypatch):
    from apps.portal.forms import ServiceRequestForm
    from apps.workorders.models import ServiceRequest

    Asset.objects.create(tag="CE-9", device_model=pump_model, department=dept)
    real = ServiceRequestForm.clean_asset_tag

    def then_retired(form):
        tag = real(form)
        Asset.objects.filter(tag="CE-9").update(status=AssetStatus.RETIRED, next_pm_on=None)
        return tag

    monkeypatch.setattr(ServiceRequestForm, "clean_asset_tag", then_retired)
    r = client.post(f"/r/{tenant.slug}/", {"asset_tag": "CE-9", "department": str(dept.pk), "room": "Bed 4", "requester_name": "RN Lee",
                                           "callback": "x2210", "problem": "Alarm will not clear", "urgency": "high", "tagged_out": "on"})
    assert r.status_code == 200 and "CE-9 is retired, so no work order tags it out." in r.content.decode()
    assert Asset.objects.get(tag="CE-9").status == AssetStatus.RETIRED and not ServiceRequest.objects.exists()
