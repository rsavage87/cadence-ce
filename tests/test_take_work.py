"""
Slice 24, part C: taking work. A facility setting (FacilitySettings.technicians_take_work, on by default, Settings Edit, audited) lets
a technician take open, unassigned, in-house work on a device they are credentialed for: apps.workorders.services.take, through
assign() (the status history records it, nobody is emailed about their own choice), and a refusal in words for anything else.
my_work.takeable lists what they could take (most urgent first, at most ten; nothing for a vendor, a requester, or while the setting is
off). My work's Take button (web/_my_work_take.html, /work-orders/<number>/take/), the new work order form's "Assign it to me", the
Settings panel, and the API's take and settings, each with the same service and level.
"""
import json
from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.template.loader import render_to_string
from django.utils import timezone
from pg_helpers import as_app_role, needs_postgres

from apps.core import history
from apps.credentials.models import Credential, Scope, Technician
from apps.equipment.models import Asset, Department, DeviceModel, RiskClass
from apps.facility import services as fs
from apps.facility.models import FacilitySettings
from apps.tenants.context import tenant_context
from apps.workorders import my_work
from apps.workorders.models import Priority, WorkOrder, WoStatus, WoType
from apps.workorders.services import assign, change_status, create_work_order, may_take_as, take

HX = {"HTTP_HX_REQUEST": "true"}


def today():
    return timezone.localdate()


@pytest.fixture
def floor(ctx, vent_model, pump_model):
    """An ICU with two pumps, a ventilator, and a defibrillator nobody is credentialed for."""
    icu = Department.objects.create(name="ICU")
    defib_model = DeviceModel.objects.create(manufacturer="Zoll", model="R Series", description="Defibrillator", category="Defibrillators",
                                             risk_class=RiskClass.LIFE_SUPPORT, oem_pm_interval_months=12)
    return {"pump": Asset.objects.create(tag="ICU-P1", device_model=pump_model, department=icu, room="4"),
            "pump2": Asset.objects.create(tag="ICU-P2", device_model=pump_model, department=icu, room="5"),
            "vent": Asset.objects.create(tag="ICU-V1", device_model=vent_model, department=icu, room="12"),
            "defib": Asset.objects.create(tag="ICU-D1", device_model=defib_model, department=icu, room="1")}


@pytest.fixture
def people(ctx, make_user):
    """Dana (pumps by category, the Hamilton-G5 by model) and Tom (pumps) with their own accounts, and Kim, a CE manager."""
    out = {}
    for key, name in (("dana", "Dana Whitfield"), ("tom", "Tom Okafor")):
        user = make_user("technician", username=f"{key}@riverside.example")
        user.email = user.username
        user.save(update_fields=["email"])
        out[key] = user
        out[f"{key}_tech"] = Technician.objects.create(user=user, name=name)
    Credential.objects.create(technician=out["dana_tech"], scope=Scope.CATEGORY, value="Infusion pumps")
    Credential.objects.create(technician=out["dana_tech"], scope=Scope.MODEL, value="Hamilton-G5", expires_on=today() + timedelta(days=400))
    Credential.objects.create(technician=out["tom_tech"], scope=Scope.CATEGORY, value="Infusion pumps")
    out["kim"] = make_user("manager")
    return out


def wo(asset, *, priority=Priority.NORMAL, due=None, type=WoType.REPAIR, **extra):
    return create_work_order(asset=asset, type=type, priority=priority, problem="Alarm", opened_on=today() - timedelta(days=5),
                             due_on=due or today() + timedelta(days=2), **extra)


def triggers(r) -> dict:
    return json.loads(r["HX-Trigger"])


@pytest.fixture
def committed(django_capture_on_commit_callbacks):
    return lambda: django_capture_on_commit_callbacks(execute=True)


# --- the service ------------------------------------------------------------------------------------------------------------------

def test_taking_assigns_it_to_their_own_profile_and_emails_nobody(floor, people, committed, mailoutbox):
    w = wo(floor["pump"])
    with committed():
        assert take(w, people["dana"]) is w
    assert w.assigned_to == people["dana_tech"] and not w.vendor_service and w.status == WoStatus.OPEN
    entry = w.status_history.last()
    assert entry.note == "Assigned to Dana Whitfield" and entry.changed_by == people["dana"]  # through assign(): recorded as any assignment
    assert mailoutbox == []  # nobody is told about their own choice
    w.refresh_from_db()
    assert w.assigned_to == people["dana_tech"]


def test_every_refusal_is_in_words_and_changes_nothing(floor, people, make_user):
    dana = people["dana"]

    def refused(work_order, by, words):
        with pytest.raises(ValidationError, match=words):
            take(work_order, by)
        work_order.refresh_from_db()
        return work_order

    vendor_wo = wo(floor["pump"], vendor_service=True, vendor_name="BD field service")
    assert refused(vendor_wo, dana, r"assigned to vendor service \(BD field service\)").vendor_name == "BD field service"
    toms = wo(floor["pump"])
    assign(toms, technician=people["tom_tech"])
    assert refused(toms, dana, "already assigned to Tom Okafor").assigned_to == people["tom_tech"]
    assert refused(toms, people["tom"], "already yours").assigned_to == people["tom_tech"]
    started = wo(floor["pump"])
    change_status(started, WoStatus.IN_PROGRESS)
    assert refused(started, dana, "is in progress; only open work nobody has started can be taken").assigned_to is None
    done = wo(floor["pump"])
    change_status(done, WoStatus.CANCELLED)
    refused(done, dana, "is cancelled")
    assert refused(wo(floor["defib"]), dana, r"not credentialed for the Zoll R Series \(ICU-D1\)").assigned_to is None
    Credential.objects.create(technician=people["dana_tech"], scope=Scope.MODEL, value="R Series", expires_on=today() - timedelta(days=1))
    refused(wo(floor["defib"]), dana, "credentials for the Zoll R Series .* have expired")

    w = wo(floor["pump"])
    refused(w, people["kim"], "no technician profile here")  # a manager without one: nobody to assign it to
    Technician.objects.filter(pk=people["tom_tech"].pk).update(is_active=False)
    refused(w, people["tom"], "no technician profile here")  # an inactive profile is none
    analyst = make_user("analyst")  # work orders: View
    Technician.objects.create(user=analyst, name="Ana Lyst")
    refused(w, analyst, "needs Work orders Edit")
    vendor = make_user("vendor")
    vendor.company = "BD"
    vendor.save()
    Technician.objects.create(user=vendor, name="Val Vendor")
    refused(w, vendor, "the facility's own technicians")  # scoped, whatever profile the account carries
    fs.update_settings(technicians_take_work=False)
    assert refused(w, dana, "do not take unassigned work in this facility").assigned_to is None
    assert w.status_history.count() == 1  # only "Opened": no refusal wrote anything


def test_the_second_of_two_taking_at_once_is_told_who_did(floor, people):
    w = wo(floor["pump"])
    stale = WorkOrder.objects.get(pk=w.pk)  # Tom's screen, loaded before Dana took it
    take(w, people["dana"])
    with pytest.raises(ValidationError, match="already assigned to Dana Whitfield"):
        take(stale, people["tom"])
    assert stale.assigned_to == people["dana_tech"]  # read again, so it shows who has it


def test_who_may_take_any(floor, people, make_user):
    assert may_take_as(people["dana"]) == people["dana_tech"]
    assert may_take_as(people["kim"]) is None and may_take_as(make_user("requester")) is None
    fs.update_settings(technicians_take_work=False)
    assert may_take_as(people["dana"]) is None


# --- what My work offers ----------------------------------------------------------------------------------------------------------

def test_takeable_is_open_unassigned_in_house_work_they_are_credentialed_for_most_urgent_first(floor, people):
    t = today()
    late = wo(floor["pump"], due=t - timedelta(days=1))
    crit = wo(floor["pump2"], priority=Priority.CRITICAL, due=t + timedelta(days=3))
    high = wo(floor["pump"], priority=Priority.HIGH, due=t + timedelta(days=1))
    vent = wo(floor["vent"], due=t)  # by its model credential
    pm = wo(floor["pump2"], type=WoType.PM, due=t + timedelta(days=9))
    wo(floor["defib"], priority=Priority.CRITICAL)  # not credentialed
    wo(floor["pump"], vendor_service=True, vendor_name="BD field service")
    assign(wo(floor["pump"]), technician=people["tom_tech"])
    change_status(wo(floor["pump"]), WoStatus.IN_PROGRESS)  # started by someone, nobody's: not to take
    change_status(wo(floor["pump"]), WoStatus.CANCELLED)
    assert my_work.takeable(people["dana"], t) == [crit, high, late, vent, pm]  # priority, then due date
    assert my_work.takeable(people["tom"], t) == [crit, high, late, pm]  # no ventilator credential
    assert my_work.groups(people["dana"], t).takeable == [crit, high, late, vent, pm]
    for _ in range(8):
        wo(floor["pump"], priority=Priority.LOW)
    assert len(my_work.takeable(people["dana"], t)) == my_work.TAKE_LIMIT == 10
    Credential.objects.filter(technician=people["tom_tech"]).update(expires_on=t - timedelta(days=1))
    assert my_work.takeable(people["tom"], t) == []  # an expired credential covers nothing


def test_takeable_is_empty_for_whoever_may_not_take(floor, people, make_user):
    wo(floor["pump"])
    vendor = make_user("vendor")
    vendor.company = "BD"
    vendor.save()
    Technician.objects.create(user=vendor, name="Val Vendor")
    Credential.objects.create(technician=Technician.objects.get(user=vendor), scope=Scope.CATEGORY, value="Infusion pumps")
    analyst = make_user("analyst")
    Technician.objects.create(user=analyst, name="Ana Lyst")
    for user in (vendor, make_user("requester"), people["kim"], analyst):
        assert my_work.takeable(user, today()) == []
    assert my_work.takeable(people["dana"], today()) != []
    fs.update_settings(technicians_take_work=False)
    assert my_work.takeable(people["dana"], today()) == [] and my_work.groups(people["dana"], today()).takeable == []


def test_one_facilitys_setting_is_its_own(floor, people, other_tenant):
    w = wo(floor["pump"])
    with tenant_context(other_tenant):
        fs.update_settings(technicians_take_work=False)
        assert fs.technicians_may_take() is False
    assert fs.technicians_may_take() is True and FacilitySettings.objects.count() == 0  # here: never saved, the default
    assert my_work.takeable(people["dana"], today()) == [w]
    with tenant_context(other_tenant):
        assert FacilitySettings.objects.get().technicians_take_work is False


# --- My work's Take ---------------------------------------------------------------------------------------------------------------

def test_the_take_button_names_its_own_target_and_swaps_nothing(floor):
    w = wo(floor["pump"])
    html = render_to_string("web/_my_work_take.html", {"wo": w})
    assert f'hx-post="/work-orders/{w.number}/take/"' in html and 'hx-trigger="click consume"' in html
    assert 'hx-target="this" hx-swap="none"' in html and f'aria-label="Take {w.number}"' in html and ">Take</button>" in html
    assert render_to_string("web/_my_work_take.html", {}).strip() == ""  # never a broken link without a work order


def test_take_answers_with_a_toast_and_wo_changed(client, floor, people):
    w = wo(floor["pump"])
    client.force_login(people["dana"])
    r = client.post(f"/work-orders/{w.number}/take/", **HX)
    assert r.status_code == 200 and r.content == b""
    # on <body> (review fix: the card's button may be gone by the time the answer arrives)
    assert triggers(r) == {"toast": {"value": f"{w.number} assigned to you", "target": "body"}, "wo-changed": {"target": "body"}}
    w.refresh_from_db()
    assert w.assigned_to == people["dana_tech"]
    again = client.post(f"/work-orders/{w.number}/take/", **HX)
    assert triggers(again) == {"toast": {"value": f"{w.number} is already yours.", "target": "body"}, "wo-changed": {"target": "body"}}  # catches up
    defib = wo(floor["defib"])
    r = client.post(f"/work-orders/{defib.number}/take/", **HX)
    assert "not credentialed" in triggers(r)["toast"]["value"] and WorkOrder.objects.get(pk=defib.pk).assigned_to is None
    assert client.get(f"/work-orders/{w.number}/take/", **HX).status_code == 405
    assert client.post("/work-orders/WO-99-9999/take/", **HX).status_code == 404


def test_take_is_closed_to_scoped_users_and_needs_edit(client, floor, people, make_user):
    w = wo(floor["pump"], vendor_service=True, vendor_name="BD field service")
    vendor = make_user("vendor")
    vendor.company = "BD field service"
    vendor.save()
    client.force_login(vendor)
    assert client.post(f"/work-orders/{w.number}/take/", **HX).status_code == 403  # even their own company's work order
    house = wo(floor["pump"])
    for slug in ("analyst", "requester"):
        client.force_login(make_user(slug))
        assert client.post(f"/work-orders/{house.number}/take/", **HX).status_code == 403
    assert WorkOrder.objects.filter(assigned_to__isnull=False).count() == 0


# --- New work order: "Assign it to me" --------------------------------------------------------------------------------------------

def test_a_technician_opening_work_on_a_device_they_are_credentialed_for_can_take_it(client, floor, people):
    client.force_login(people["dana"])
    form = client.get(f"/work-orders/new/?asset={floor['pump'].tag}", **HX).content.decode()
    assert 'type="checkbox" name="take" id="id_take" checked' in form and "Assign it to me" in form and 'name="take_offered" value="1"' in form
    assert 'name="assignee"' not in form and "You are credentialed for this device." in form
    unticked = client.get(f"/work-orders/new/?asset={floor['pump2'].tag}&take_offered=1", **HX).content.decode()
    assert 'name="take" id="id_take">' in unticked  # unticked before another device was picked: still unticked
    assert 'name="take"' not in client.get(f"/work-orders/new/?asset={floor['defib'].tag}", **HX).content.decode()
    assert 'name="take"' not in client.get("/work-orders/new/", **HX).content.decode()  # no device yet

    post = {"asset": floor["pump"].tag, "type": "repair", "priority": "high", "problem": "Occlusion alarm", "requester": ""}
    r = client.post("/work-orders/new/", {**post, "take": "on"}, **HX)
    mine = WorkOrder.objects.get()
    assert r.status_code == 200 and r["HX-Retarget"] == "#drawer" and mine.assigned_to == people["dana_tech"] and mine.created_by == people["dana"]
    assert triggers(r)["toast"]["value"] == f"{mine.number} created and assigned to you"
    client.post("/work-orders/new/", post, **HX)  # unticked
    assert WorkOrder.objects.filter(assigned_to__isnull=True).count() == 1
    client.post("/work-orders/new/", {**post, "asset": floor["defib"].tag, "take": "on"}, **HX)  # not offered, so not taken
    assert WorkOrder.objects.get(asset=floor["defib"]).assigned_to is None


def test_no_assign_it_to_me_while_the_setting_is_off_or_for_a_manager(client, floor, people):
    fs.update_settings(technicians_take_work=False)
    client.force_login(people["dana"])
    assert 'name="take"' not in client.get(f"/work-orders/new/?asset={floor['pump'].tag}", **HX).content.decode()
    client.post("/work-orders/new/", {"asset": floor["pump"].tag, "type": "repair", "priority": "normal", "problem": "x", "take": "on"}, **HX)
    assert WorkOrder.objects.get().assigned_to is None
    fs.update_settings(technicians_take_work=True)
    kim = people["kim"]
    Credential.objects.create(technician=Technician.objects.create(user=kim, name="Kim Lead"), scope=Scope.CATEGORY, value="Infusion pumps")
    client.force_login(kim)
    form = client.get(f"/work-orders/new/?asset={floor['pump'].tag}", **HX).content.decode()
    assert 'name="assignee"' in form and 'name="take"' not in form  # the manager's form is unchanged


def test_a_refused_take_on_a_new_work_order_leaves_it_open_and_says_why(client, floor, people, monkeypatch):
    from apps.workorders import services

    def refuse(wo, by):
        raise ValidationError("Taking work needs Work orders Edit.")

    client.force_login(people["dana"])
    monkeypatch.setattr(services, "take", refuse)  # e.g. a credential lapsing between the form and the save
    r = client.post("/work-orders/new/", {"asset": floor["pump"].tag, "type": "repair", "priority": "normal", "problem": "x", "take": "on"}, **HX)
    w = WorkOrder.objects.get()
    assert w.assigned_to is None and triggers(r)["toast"]["value"] == f"{w.number} created. Taking work needs Work orders Edit."


# --- Settings -------------------------------------------------------------------------------------------------------------------

def test_the_settings_panel_and_its_switch(client, floor, make_user):
    director = make_user("director")
    client.force_login(director)
    body = client.get("/settings/").content.decode()
    assert '<div class="panel mt" id="set-take">' in body and "Technicians may take unassigned work they are credentialed for" in body
    assert 'id="set-take-work" name="technicians_take_work" value="1"' in body
    assert 'checked hx-post="/settings/take-work/" hx-trigger="change" hx-include="#set-take-work-off" hx-target="#set-take"' in body
    assert body.index('id="set-policy"') < body.index('id="set-take"') < body.index('id="set-targets"')

    fs.update_settings(portal_hotline="ext. 4400")  # saved once before, so the switch reads as a change in the log
    r = client.post("/settings/take-work/", {"technicians_take_work": "0"}, **HX)
    s = fs.get_settings()
    assert r.status_code == 200 and s.technicians_take_work is False and s.history.first().history_user == director
    assert triggers(r)["toast"]["value"] == "Technicians no longer take unassigned work; a CE manager assigns it"
    panel = r.content.decode()
    assert panel.lstrip().startswith('<div class="panel mt" id="set-take">')  # the panel alone, which replaces itself
    assert '<span class="sw"></span>Off' in panel and "A CE manager assigns all work." in panel and "checked" not in panel
    log = [e for e in history.change_log(director)[0] if e.area == "settings"]
    assert log[0].action == "changed" and log[0].who_id == director.pk
    assert [(c.field, c.before, c.after) for c in log[0].changes] == [("Technicians may take unassigned work", "Yes", "No")]

    r = client.post("/settings/take-work/", {"technicians_take_work": ["0", "1"]}, **HX)
    assert fs.get_settings().technicians_take_work is True and "credentialed for" in triggers(r)["toast"]["value"]
    for bad in ({"technicians_take_work": "maybe"}, {}):
        r = client.post("/settings/take-work/", bad, **HX)
        assert triggers(r)["toast"]["value"] == "Choose on or off." and fs.get_settings().technicians_take_work is True


def test_changing_it_needs_settings_edit(client, floor, make_user):
    client.force_login(make_user("manager"))  # Settings: View
    body = client.get("/settings/").content.decode()
    assert 'id="set-take-work" name="technicians_take_work" value="1" aria-label=' in body
    assert "credentialed for\" aria-describedby=\"set-take-work-help\" checked disabled>" in body
    assert client.post("/settings/take-work/", {"technicians_take_work": "0"}, **HX).status_code == 403
    client.force_login(make_user("technician"))
    assert client.post("/settings/take-work/", {"technicians_take_work": "0"}, **HX).status_code == 403
    assert fs.get_settings().technicians_take_work is True


# --- the API ----------------------------------------------------------------------------------------------------------------------

def test_the_api_takes_with_the_same_service_and_level(client, floor, people, make_user):
    w = wo(floor["pump"])
    client.force_login(people["dana"])
    r = client.post(f"/api/v1/work-orders/{w.pk}/take/")
    assert r.status_code == 200 and r.json()["assigned_to"] == str(people["dana_tech"].pk) and r.json()["assigned_to_name"] == "Dana Whitfield"
    r = client.post(f"/api/v1/work-orders/{w.pk}/take/")
    assert r.status_code == 400 and r.json() == {"detail": f"{w.number} is already yours."}
    other = wo(floor["pump"])
    client.force_login(make_user("analyst"))  # work orders: View
    assert client.post(f"/api/v1/work-orders/{other.pk}/take/").status_code == 403
    vendor = make_user("vendor")
    vendor.company = "BD"
    vendor.save()
    client.force_login(vendor)
    assert client.post(f"/api/v1/work-orders/{other.pk}/take/").status_code == 403  # scoped: not in scoped_actions
    assert WorkOrder.objects.get(pk=other.pk).assigned_to is None


def test_the_settings_api_reads_and_changes_it(client, ctx, make_user):
    client.force_login(make_user("director"))
    assert client.get("/api/v1/settings/").json()["technicians_take_work"] is True
    r = client.patch("/api/v1/settings/", {"technicians_take_work": False}, content_type="application/json")
    assert r.status_code == 200 and r.json()["technicians_take_work"] is False and fs.get_settings().technicians_take_work is False


# --- under the policies -----------------------------------------------------------------------------------------------------------

@needs_postgres
def test_taking_work_under_the_policies(client, ctx, floor, people, other_tenant):
    """The setting, the profile, the credentials, and the work order are read inside the facility, as the runtime role."""
    with tenant_context(other_tenant):
        fs.update_settings(technicians_take_work=False)  # another facility's choice is not this one's
    w = wo(floor["pump"])
    as_app_role()
    assert my_work.takeable(people["dana"], today()) == [w]
    client.force_login(people["dana"])
    r = client.post(f"/work-orders/{w.number}/take/", **HX)
    assert triggers(r)["toast"]["value"] == f"{w.number} assigned to you"
    form = client.get(f"/work-orders/new/?asset={floor['pump2'].tag}", **HX).content.decode()
    assert 'name="take" id="id_take" checked' in form
    r = client.post(f"/api/v1/work-orders/{w.pk}/take/")
    assert r.status_code == 400 and r.json() == {"detail": f"{w.number} is already yours."}
    with tenant_context(ctx):  # the requests reset the database's tenant when they finished
        w.refresh_from_db()
        assert w.assigned_to == people["dana_tech"] and w.status_history.last().changed_by == people["dana"]
