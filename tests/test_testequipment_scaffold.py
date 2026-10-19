"""
Slice 35 scaffold: the CE department's test equipment and its calibration (apps.testequipment). What the scaffold fixes before the waves:
the models' own constraints, a device never taking a test equipment item's tag (in any letter case, under the facility's tag lock), the
change log's four areas and who reads them, the binder's section in its place, the URL names, the views refusing scoped users, the
Equipment screen's tab strip, and the read-only API's scoped refusal.
"""
from datetime import date

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.urls import reverse

from apps.accounts.models import Level, Module, Role, User
from apps.core.history import ALL_AREAS, can_read
from apps.equipment.services import create_asset
from apps.reports.survey import NOT_COVERED, section_keys
from apps.testequipment.models import (
    AsFound,
    Calibration,
    Kind,
    Mark,
    Reason,
    Result,
    Status,
    TestEquipment,
)
from apps.testequipment.permissions import can_decide, can_edit, can_retire, can_view

DAY0 = date(2026, 1, 15)


def _item(tag="TE-01", **over):
    """A row made directly (the scaffold's tests only: every real write is apps.testequipment.services)."""
    fields = {"tag": tag, "tag_key": tag.casefold(), "kind": Kind.ELECTRICAL_SAFETY, "manufacturer": "Fluke Biomedical", "model": "ESA612",
              "serial": "1234567", "status_since": DAY0, **over}
    return TestEquipment.objects.create(**fields)


def _calibration(item, day, **over):
    fields = {"test_equipment": item, "calibrated_on": day, "due_on": date(day.year + 1, day.month, day.day), "provider": "Fluke Biomedical",
              "certificate": "C-1", "accreditation": Mark.SHOWN, "as_found": AsFound.IN_TOLERANCE, "result": Result.PASSED, **over}
    return Calibration.objects.create(**fields)


def test_the_status_takes_its_own_reason(ctx):
    _item("TE-01", status=Status.OUT_OF_SERVICE, status_reason=Reason.DAMAGED)
    _item("TE-02", status=Status.RETIRED, status_reason=Reason.RETURNED_TO_OWNER)
    for i, (status, reason) in enumerate([(Status.IN_USE, Reason.DAMAGED), (Status.OUT_OF_SERVICE, ""), (Status.RETIRED, Reason.DAMAGED),
                                          (Status.OUT_OF_SERVICE, Reason.LOST)]):
        with pytest.raises(IntegrityError), transaction.atomic():
            _item(f"TE-1{i}", status=status, status_reason=reason)


def test_one_live_calibration_per_day_and_a_due_day_exactly_when_passed(ctx):
    item = _item()
    first = _calibration(item, DAY0)
    with pytest.raises(IntegrityError), transaction.atomic():
        _calibration(item, DAY0, certificate="C-2")
    Calibration.objects.filter(pk=first.pk).update(entered_in_error=True)
    _calibration(item, DAY0, certificate="C-2")  # the other was marked entered in error
    with pytest.raises(IntegrityError), transaction.atomic():
        _calibration(item, date(2026, 2, 1), result=Result.FAILED)  # a failed certificate has no due day
    with pytest.raises(IntegrityError), transaction.atomic():
        _calibration(item, date(2026, 2, 2), due_on=None)  # a passed one has one
    _calibration(item, date(2026, 2, 3), result=Result.FAILED, due_on=None, as_found=AsFound.OUT_OF_TOLERANCE)


def test_a_tag_is_unique_in_any_letter_case_among_test_equipment(ctx):
    _item("TE-01")
    with pytest.raises(IntegrityError), transaction.atomic():
        _item("te-01")


def test_a_device_never_takes_a_test_equipment_tag(ctx, dept, vent_model):
    _item("TE-01")
    with pytest.raises(ValidationError) as e:
        create_asset(tag="te-01", device_model=vent_model, department=dept)
    assert e.value.message_dict == {"tag": ["te-01 is test equipment's tag."]}
    assert create_asset(tag="CE-1", device_model=vent_model, department=dept).tag == "CE-1"


def test_the_levels(ctx, make_user):
    director, manager, technician, analyst = (make_user(slug) for slug in ("director", "manager", "technician", "analyst"))
    assert [can_view(u) for u in (director, manager, technician, analyst)] == [True, True, True, True]
    assert [can_edit(u) for u in (director, manager, technician, analyst)] == [True, True, True, False]
    assert [can_decide(u) for u in (director, manager, technician, analyst)] == [True, True, False, False]  # Work orders Approve
    assert [can_retire(u) for u in (director, manager, technician, analyst)] == [True, False, False, False]  # Equipment Approve


def test_the_change_log_areas(ctx, tenant):
    assert [ALL_AREAS[k].module for k in ("test_equipment", "calibrations", "tolerance_reviews", "test_equipment_uses")] == [
        "equipment", "equipment", "equipment", "workorders"]
    assert ALL_AREAS["test_equipment_uses"].also == ("equipment",)
    role = Role.objects.create(name="Work only", slug="work-only")
    role.set_levels({Module.WORKORDERS: Level.VIEW, Module.USERS: Level.VIEW})
    user = User.objects.create_user(username="work@riverside.example", password="Test-Pass-2026-x", tenant=tenant, role=role)
    assert not can_read(user, "test_equipment_uses") and not can_read(user, "calibrations")


def test_the_binder_section_comes_after_repairs():
    keys = section_keys()
    assert keys[keys.index("repairs") + 1] == "testequipment" and keys[-2:] == ["staff", "vendors"]
    assert not any("calibration of electrical safety analyzers" in item for item in NOT_COVERED)
    assert "The vendors' own test equipment (their service reports carry it)" in NOT_COVERED


def test_the_url_names():
    assert reverse("web:test_equipment") == "/test-equipment/"
    assert reverse("web:test_equipment_item", args=["TE-01"]) == "/test-equipment/TE-01/"
    assert reverse("web:test_equipment_csv") == "/test-equipment/export.csv"
    assert reverse("web:wo_test_equipment", args=["WO-26-0001"]) == "/work-orders/WO-26-0001/test-equipment/"


def test_scoped_users_are_refused_the_screens_and_the_api(client, ctx, tenant):
    role = Role.objects.create(name="Vendor", slug="vendor-co", scope="company")
    role.set_levels({Module.EQUIPMENT: Level.FULL, Module.WORKORDERS: Level.FULL})
    user = User.objects.create_user(username="v@acme.example", password="Test-Pass-2026-x", tenant=tenant, role=role, company="Acme")
    client.force_login(user)
    assert client.get("/test-equipment/").status_code == 403
    assert client.get("/api/v1/test-equipment/").status_code == 403
    assert "Test equipment</a>" not in client.get("/equipment/").content.decode()  # no tab strip for a scoped user


def test_the_equipment_screen_shows_the_tabs(client, ctx, make_user):
    client.force_login(make_user("technician"))
    body = client.get("/equipment/").content.decode()
    assert 'href="/test-equipment/"' in body and ">Devices</a>" in body
