"""What PostgreSQL refuses and SQLite takes (slice 17 review), checked on both: a NUL character in a request is a 400, never a 500;
over-long text from the portal, the API, the importer, and derived names never reaches a length-limited column; and the PM
generation takes the planner lock Create and Auto-assign week take."""
from io import StringIO

import pytest
from django.core.management import call_command

from apps.equipment.models import Asset
from apps.workorders.models import WorkOrder, WoStatus
from apps.workorders.services import add_note, create_work_order


@pytest.fixture
def director(client, make_user):
    user = make_user("director")
    client.force_login(user)
    return user


@pytest.mark.parametrize("url", ["/r/riverside/?asset=CE%00", "/r/riverside/?dept=I%00CU", "/equipment/?q=CE%00", "/search/?q=a%00",
                                 "/equipment/CE%00/", "/work-orders/?q=a%00"])
def test_a_nul_character_in_a_request_is_a_400(client, director, url):
    assert client.get(url).status_code == 400


def test_a_nul_character_in_a_posted_body_is_a_400(client, director, vent):
    form = "asset_tag=CE-1%00&problem=x"  # a browser's form post (multipart uploads are left to the form fields, which refuse NUL)
    assert client.post("/r/riverside/", form, content_type="application/x-www-form-urlencoded").status_code == 400
    r = client.post("/api/v1/work-orders/", '{"problem": "a\\u0000b"}', content_type="application/json")
    assert r.status_code == 400


def test_a_long_name_and_department_on_the_portal_fit_their_columns(client, ctx, dept, vent):
    dept.name = "D" * 80
    dept.save()
    r = client.post("/r/riverside/", {"asset_tag": vent.tag, "department": str(dept.pk), "room": "R" * 40, "requester_name": "N" * 120,
                                      "callback": "4410", "problem": "Alarm", "urgency": "normal"})
    assert r.status_code == 302
    wo = WorkOrder.objects.get(asset=vent)
    assert len(wo.requester) <= 120 and len(wo.reported_location) <= 120


def test_the_api_refuses_an_over_long_note_or_vendor_name(client, director, vent, techs):
    from apps.workorders.services import assign

    wo = create_work_order(asset=vent, type="repair", priority="normal", problem="Alarm")
    assign(wo, technician=techs["dana"])
    r = client.post(f"/api/v1/work-orders/{wo.id}/transition/", {"status": "in_progress", "note": "x" * 301})
    assert r.status_code == 400
    r = client.post(f"/api/v1/work-orders/{wo.id}/assign/", {"vendor_name": "V" * 121})
    assert r.status_code == 400
    wo.refresh_from_db()
    assert wo.status == WoStatus.OPEN and not wo.vendor_service


def test_derived_names_fit_their_columns(ctx, vent, make_user):
    user = make_user("technician")
    user.first_name, user.last_name = "A" * 70, "B" * 70
    user.save()
    wo = create_work_order(asset=vent, type="repair", priority="normal", problem="Alarm")
    assert len(add_note(wo, "Checked", by=user).author_name) <= 120


def test_the_importer_skips_a_row_with_a_value_too_long_for_its_column(ctx, tmp_path):
    f = tmp_path / "inventory.csv"
    f.write_text("Asset Tag,Manufacturer,Model,Department,Room,PM Interval,Cost\n"
                 f"ZZ-1,Acme,M1,ICU,{'R' * 45},12,100\nZZ-2,Acme,M1,ICU,4,99999,100\nZZ-3,Acme,M1,ICU,5,12,99999999999\n")
    out, err = StringIO(), StringIO()
    call_command("import_assets", "--tenant", "riverside", str(f), stdout=out, stderr=err)
    # Slice 23: a value too long for its column still skips the row; a cost it cannot read is left blank with a note
    assert "2 created, 0 updated, 1 skipped" in out.getvalue() and "Room is longer than 40 characters" in err.getvalue()
    assert "Acquisition cost not read" in out.getvalue() and Asset.objects.get(tag="ZZ-3").acquisition_cost == 0
    assert Asset.objects.get(tag="ZZ-2").device_model.oem_pm_interval_months == 12  # out of range: the default


def test_pm_generation_takes_the_planner_lock(ctx, vent, monkeypatch):
    from apps.pm import services

    taken = []
    monkeypatch.setattr(services, "lock_planner", lambda: taken.append(1))
    services.generate_pm_work_orders(lead_days=400)
    assert taken == [1]
