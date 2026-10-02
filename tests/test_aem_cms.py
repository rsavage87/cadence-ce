"""The CMS rule (slice 18, part C): imaging and radiologic equipment (diagnostic or therapeutic) and medical lasers stay on the
manufacturer's schedule, never AEM (CMS S&C 14-07). The facility marks those models (DeviceModel.oem_schedule_required).

Covered: the interval in force and PM generation follow the OEM interval; proposing is refused with the rule's reason, and so is
approving a proposal still open once the model is marked; marking a model on AEM ends it and brings its devices' next PMs (and
their open PM work orders) in, through equipment.services._save_model and apps.pm.aem.model_changed as for life support; clearing
the mark changes no interval; setting or clearing it needs Equipment Approve in the service, the form, and the API, and is
audited; what the model drawer, the AEM tab and its modals, the PM library, and the device drawer say; the demo seed's marks; the
importer's column; and the same under PostgreSQL's row-level security."""
import html
import json
from datetime import date, timedelta
from io import StringIO

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from pg_helpers import as_app_role, needs_postgres

from apps.accounts.models import Level, Role
from apps.equipment import services as eq
from apps.equipment.models import Asset, DeviceModel, RiskClass
from apps.pm import aem
from apps.pm.dates import add_months
from apps.pm.models import AemDecision, AemStatus
from apps.pm.services import generate_pm_work_orders
from apps.tenants.context import tenant_context
from apps.tenants.models import Tenant
from apps.web.pm_panels import panels_context
from apps.workorders.models import WorkOrder, WoStatus, WoType
from apps.workorders.services import change_status, create_work_order

HX = {"HTTP_HX_REQUEST": "true"}
TODAY = date.today()
NEW_URL = "/pm/models/new/"
NO_EFFECT = {"ended": False, "withdrawn": False, "cleared": False, "moved": 0, "rule": ""}


def months_ago(n: int) -> date:
    return add_months(TODAY, -n)


@pytest.fixture
def people(make_user):
    return {"tech": make_user("technician"), "manager": make_user("manager"), "director": make_user("director")}


@pytest.fixture
def c_arm(ctx, dept):
    """An imaging model, not marked yet: OEM every 6 months, four years of history, one device last serviced 10 months ago."""
    dm = DeviceModel.objects.create(manufacturer="Siemens Healthineers", model="Cios Spin", description="Mobile C-arm", category="Imaging",
                                    risk_class=RiskClass.HIGH, oem_pm_interval_months=6)
    Asset.objects.create(tag="CA-1", device_model=dm, department=dept, installed_on=months_ago(48), last_pm_on=months_ago(10),
                         next_pm_on=add_months(months_ago(10), 6))
    return dm


def propose(dm, people, months=12):
    return aem.propose(dm, interval_months=months, rationale="Few failures in four years.", by=people["tech"])


def approve(decision, people):
    return aem.approve(decision, by=people["manager"], decided_on=TODAY, note="EMC minutes, item 4")


def on_aem(dm, people, months=12):
    """An approved AEM interval, and the devices' next PMs set on it, as a year on the interval would leave them."""
    d = approve(propose(dm, people, months), people)
    dm.refresh_from_db()
    for a in Asset.objects.filter(device_model=dm, last_pm_on__isnull=False):
        Asset.objects.filter(pk=a.pk).update(next_pm_on=add_months(a.last_pm_on, months))
    return d


def mark(dm, by, value=True, **fields):
    return eq.update_device_model(dm, oem_schedule_required=value, by=by, **fields)


def marked_behind_the_services_back(dm, **fields):
    """As the admin or an old API client could: the rules that read the mark must hold anyway."""
    DeviceModel.objects.filter(pk=dm.pk).update(oem_schedule_required=True, **fields)
    dm.refresh_from_db()


def refused(fn, *args, match="", field=None, **kwargs):
    with pytest.raises(ValidationError) as e:
        fn(*args, **kwargs)
    text = " ".join(e.value.message_dict[field]) if field else " ".join(e.value.messages)
    assert match in text, text
    return text


def toast(r) -> str:
    return json.loads(r["HX-Trigger"])["toast"]["value"]


def text(r) -> str:
    return html.unescape(r.content.decode())


# --- the interval in force ---------------------------------------------------------------------------------------------------


def test_a_marked_model_follows_the_oem_interval_whatever_is_on_file(ctx, dept, pump_model):
    assert pump_model.pm_interval_months == 18 and not pump_model.aem_excluded  # an AEM interval on file, in use
    marked_behind_the_services_back(pump_model)
    pump = Asset.objects.create(tag="P-1", device_model=pump_model, department=dept, next_pm_on=TODAY)
    assert pump_model.aem_excluded and pump_model.pm_interval_months == 12 and pump.pm_interval_months == 12


def test_pm_generation_and_completion_follow_the_oem_interval(ctx, dept, pump_model):
    marked_behind_the_services_back(pump_model)  # 18 months still on file
    pump = Asset.objects.create(tag="P-1", device_model=pump_model, department=dept, next_pm_on=TODAY)
    assert generate_pm_work_orders(as_of=TODAY) == 1
    wo = WorkOrder.objects.get(asset=pump, type=WoType.PM)
    assert wo.problem == "Scheduled 12-month preventive maintenance"
    change_status(wo, WoStatus.IN_PROGRESS, as_of=TODAY)
    change_status(wo, WoStatus.COMPLETED, as_of=TODAY)
    pump.refresh_from_db()
    assert pump.last_pm_on == TODAY and pump.next_pm_on == add_months(TODAY, 12)


def test_a_new_model_can_be_marked(ctx, people):
    dm = eq.create_device_model(manufacturer="GE HealthCare", model="OEC 3D", description="Mobile C-arm", category="Imaging", risk_class="high",
                                oem_pm_interval_months=6, oem_schedule_required=True, by=people["director"])
    assert dm.oem_schedule_required and dm.pm_interval_months == 6
    laser = eq.create_device_model(manufacturer="Lumenis", model="UltraPulse", description="Surgical CO2 laser", category="Lasers",
                                   risk_class="high", oem_schedule_required=True)  # no user: the operator's own (the importer, the seed)
    assert laser.oem_schedule_required
    plain = eq.create_device_model(manufacturer="BD", model="Alaris 8100", description="Pump module", category="Infusion pumps", risk_class="high")
    assert not plain.oem_schedule_required


# --- proposing and approving ---------------------------------------------------------------------------------------------------


def test_aem_is_never_proposed_for_a_marked_model(c_arm, people):
    mark(c_arm, people["director"])
    reason = refused(aem.propose, c_arm, interval_months=12, rationale="Few failures.", by=people["tech"])
    assert reason == aem.OEM_SCHEDULE_REFUSAL
    assert "CMS requires the manufacturer's maintenance schedule for imaging, radiologic, and medical laser equipment" in reason
    assert aem.propose_blocker(c_arm, TODAY) == aem.exclusion(c_arm) == aem.OEM_SCHEDULE_REFUSAL
    assert not AemDecision.objects.exists()


def test_life_support_keeps_its_own_reason_first(ctx, vent_model):
    marked_behind_the_services_back(vent_model)
    assert aem.exclusion(vent_model) == aem.LIFE_SUPPORT_REFUSAL


def test_marking_withdraws_an_open_proposal_which_can_no_longer_be_approved(c_arm, people):
    d = propose(c_arm, people)
    mark(c_arm, people["director"])
    assert c_arm.aem_effect == {**NO_EFFECT, "withdrawn": True, "rule": "oem_schedule"}
    d.refresh_from_db()
    assert d.status == AemStatus.WITHDRAWN and d.end_reason == aem.OEM_SCHEDULE_ENDED and d.ended_by == people["director"]
    refused(approve, d, people, match="This proposal is no longer open: it was withdrawn.")


def test_approval_refuses_a_proposal_once_the_model_is_marked(c_arm, people):
    d = propose(c_arm, people)
    marked_behind_the_services_back(c_arm)  # approval checks again, whatever path the mark took
    refused(approve, d, people, match=aem.OEM_SCHEDULE_REFUSAL)
    d.refresh_from_db()
    assert d.status == AemStatus.PROPOSED
    aem.reject(d, by=people["manager"], decided_on=TODAY, note="CMS: the manufacturer's schedule")  # the committee can still turn it down
    d.refresh_from_db()
    c_arm.refresh_from_db()
    assert d.status == AemStatus.REJECTED and c_arm.aem_interval_months is None and c_arm.pm_interval_months == 6


# --- marking a model on AEM, and clearing the mark -----------------------------------------------------------------------------


def test_marking_a_model_on_aem_ends_it_and_brings_its_pms_in(c_arm, people, dept):
    d = on_aem(c_arm, people)  # every 12 months instead of the OEM's 6
    far = Asset.objects.get(tag="CA-1")  # last PM 10 months ago: due 2 months from now on the AEM interval
    near = Asset.objects.create(tag="CA-2", device_model=c_arm, department=dept, installed_on=months_ago(30), last_pm_on=months_ago(1),
                                next_pm_on=add_months(months_ago(1), 6))  # already within the OEM interval
    pm = create_work_order(asset=far, type=WoType.PM, priority="normal", problem="Scheduled PM", due_on=far.next_pm_on)
    mark(c_arm, people["director"])
    assert c_arm.aem_effect == {**NO_EFFECT, "ended": True, "moved": 1, "rule": "oem_schedule"}
    d.refresh_from_db()
    assert d.status == AemStatus.ENDED and d.end_reason == aem.OEM_SCHEDULE_ENDED and d.ended_by == people["director"] and d.ended_on == TODAY
    c_arm.refresh_from_db()
    assert c_arm.oem_schedule_required and c_arm.aem_interval_months is None and c_arm.pm_interval_months == 6
    far.refresh_from_db()
    near.refresh_from_db()
    pm.refresh_from_db()
    assert far.next_pm_on == TODAY and pm.due_on == TODAY  # 6 months after its last PM has passed: due now, its open PM with it
    assert near.next_pm_on == add_months(months_ago(1), 6)


def test_the_mark_is_audited(c_arm, people):
    on_aem(c_arm, people)
    mark(c_arm, people["director"])
    entry = c_arm.history.filter(oem_schedule_required=True).order_by("history_date", "history_id").first()
    assert entry.history_user == people["director"] and entry.history_change_reason == eq.OEM_SCHEDULE_MARKED
    latest = c_arm.history.first()  # the AEM's end is its own entry, after the mark
    assert latest.aem_interval_months is None and latest.history_change_reason.startswith("AEM ended: The model is now marked as imaging")
    mark(c_arm, people["director"], False)
    latest = c_arm.history.first()
    assert not latest.oem_schedule_required and latest.history_change_reason == eq.OEM_SCHEDULE_CLEARED
    assert latest.history_user == people["director"]


def test_marking_ends_an_interval_on_file_without_a_recorded_approval(ctx, dept, pump_model, people):
    pump = Asset.objects.create(tag="P-1", device_model=pump_model, department=dept, last_pm_on=months_ago(13),
                                next_pm_on=add_months(months_ago(13), 18))
    mark(pump_model, people["director"])
    assert pump_model.aem_effect == {**NO_EFFECT, "ended": True, "moved": 1, "rule": "oem_schedule"}
    pump_model.refresh_from_db()
    pump.refresh_from_db()
    assert pump_model.aem_interval_months is None and pump.next_pm_on == TODAY and not AemDecision.objects.exists()


def test_clearing_the_mark_changes_no_interval(c_arm, people):
    on_aem(c_arm, people)
    mark(c_arm, people["director"])
    device = Asset.objects.get(tag="CA-1")
    decisions = list(AemDecision.objects.values_list("pk", "status"))
    mark(c_arm, people["director"], False)
    assert c_arm.aem_effect == NO_EFFECT
    c_arm.refresh_from_db()
    assert not c_arm.oem_schedule_required and c_arm.aem_interval_months is None and c_arm.pm_interval_months == 6
    assert Asset.objects.get(tag="CA-1").next_pm_on == device.next_pm_on
    assert list(AemDecision.objects.values_list("pk", "status")) == decisions
    # AEM is proposed and approved again
    assert aem.propose_blocker(c_arm, TODAY) == ""
    approve(propose(c_arm, people, 9), people)
    c_arm.refresh_from_db()
    assert c_arm.pm_interval_months == 9


def test_clearing_the_mark_drops_an_unused_interval_on_file(ctx, pump_model, people):
    marked_behind_the_services_back(pump_model)  # 18 months still on file, never used while marked
    mark(pump_model, people["director"], False)
    pump_model.refresh_from_db()
    assert pump_model.aem_interval_months is None and pump_model.pm_interval_months == 12  # never starts applying unapproved
    assert pump_model.aem_effect == {**NO_EFFECT, "cleared": True}
    assert pump_model.history.first().history_change_reason == "AEM on file without a recorded approval cleared: the model left the CMS exclusion"


def test_a_model_already_excluded_is_left_alone(ctx, vent_model, people):
    DeviceModel.objects.filter(pk=vent_model.pk).update(aem_interval_months=12)  # on file, never used by life support
    vent_model.refresh_from_db()
    mark(vent_model, people["director"])
    assert vent_model.aem_effect == NO_EFFECT and vent_model.aem_interval_months == 12
    eq.update_device_model(vent_model, risk_class=RiskClass.HIGH, by=people["director"])  # still excluded: marked
    vent_model.refresh_from_db()
    assert vent_model.aem_excluded and vent_model.aem_interval_months == 12 and vent_model.pm_interval_months == 6
    mark(vent_model, people["director"], False)  # out of both exclusions: the unused interval goes rather than start applying
    vent_model.refresh_from_db()
    assert vent_model.aem_interval_months is None and vent_model.pm_interval_months == 6


# --- who may set the mark ------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("who", ["tech", "manager"])
def test_only_equipment_approve_sets_or_clears_the_mark(c_arm, people, who):
    user = people[who]
    with pytest.raises(PermissionDenied) as e:
        mark(c_arm, user)
    assert str(e.value) == eq.OEM_SCHEDULE_PERMISSION
    with pytest.raises(PermissionDenied):
        eq.create_device_model(manufacturer="GE HealthCare", model="OEC 3D", description="Mobile C-arm", category="Imaging", risk_class="high",
                               oem_schedule_required=True, by=user)
    assert not DeviceModel.objects.filter(model="OEC 3D").exists()
    mark(c_arm, user, False, description="Mobile C-arm, 3D")  # sending back its own value is fine
    mark(c_arm, people["director"])
    with pytest.raises(PermissionDenied):
        mark(c_arm, user, False)
    c_arm.refresh_from_db()
    assert c_arm.oem_schedule_required and c_arm.description == "Mobile C-arm, 3D"


def test_the_mark_is_yes_or_no(c_arm, people):
    refused(mark, c_arm, people["director"], "yes", field="oem_schedule_required", match="yes or no")


# --- the screens ---------------------------------------------------------------------------------------------------------------


def model_post(**over) -> dict:
    return {"manufacturer": "GE HealthCare", "model": "OEC 3D", "description": "Mobile C-arm", "category": "Imaging", "risk_class": RiskClass.HIGH,
            "oem_pm_interval_months": "6", "expected_life_years": "10", "list_cost": "180000", **over}


def edit_post(dm, **over) -> dict:
    return {"manufacturer": dm.manufacturer, "model": dm.model, "description": dm.description, "category": dm.category,
            "oem_pm_interval_months": str(dm.oem_pm_interval_months), "expected_life_years": str(dm.expected_life_years), "list_cost": str(dm.list_cost),
            **over}


def test_add_model_offers_the_mark_to_equipment_approve(client, people, ctx):
    client.force_login(people["director"])
    body = text(client.get(NEW_URL, **HX))
    assert 'type="checkbox" name="oem_schedule_required"' in body and "Manufacturer's schedule required (CMS)" in body
    assert "Imaging (diagnostic or therapeutic), radiologic, or medical laser equipment: CMS requires" in body and "dm-mark-ro" not in body
    r = client.post(NEW_URL, model_post(oem_schedule_required="on"), **HX)
    assert r["HX-Retarget"] == "#drawer" and toast(r) == "GE HealthCare OEC 3D added to the catalog; it keeps the manufacturer's schedule (CMS)"
    assert DeviceModel.objects.get(model="OEC 3D").oem_schedule_required


def test_add_model_without_equipment_approve_shows_the_mark_read_only_and_refuses_it(client, people, ctx):
    client.force_login(people["tech"])
    body = text(client.get(NEW_URL, **HX))
    assert 'name="oem_schedule_required"' not in body and 'id="dm-mark-ro" type="text" value="No" readonly' in body
    assert "Set by Equipment Approve." in body
    r = client.post(NEW_URL, model_post(oem_schedule_required="on"), **HX)
    assert r.status_code == 200 and "HX-Retarget" not in r and eq.OEM_SCHEDULE_PERMISSION in text(r)
    assert not DeviceModel.objects.filter(model="OEC 3D").exists()
    r = client.post(NEW_URL, model_post(), **HX)
    assert toast(r) == "GE HealthCare OEC 3D added to the catalog" and not DeviceModel.objects.get(model="OEC 3D").oem_schedule_required


def test_a_facility_can_give_its_manager_the_mark(client, people, c_arm, tenant):
    with tenant_context(tenant):
        Role.objects.get(slug="manager").set_levels({"equipment": Level.APPROVE})
    client.force_login(people["manager"])
    assert 'type="checkbox" name="oem_schedule_required"' in text(client.get(f"/pm/models/{c_arm.pk}/edit/", **HX))
    client.post(f"/pm/models/{c_arm.pk}/edit/", edit_post(c_arm, oem_schedule_required="on"), **HX)
    c_arm.refresh_from_db()
    assert c_arm.oem_schedule_required


def test_edit_details_marks_a_model_on_aem(client, people, c_arm):
    on_aem(c_arm, people)
    client.force_login(people["director"])
    body = text(client.get(f"/pm/models/{c_arm.pk}/edit/", **HX))
    assert 'type="checkbox" name="oem_schedule_required"' in body and "checked" not in body.split('name="oem_schedule_required"')[1].split(">")[0]
    r = client.post(f"/pm/models/{c_arm.pk}/edit/", edit_post(c_arm, oem_schedule_required="on"), **HX)
    assert toast(r) == ("Siemens Healthineers Cios Spin updated: it keeps the manufacturer's schedule (CMS); its AEM interval ended (back on the "
                        "OEM interval); 1 device's next PM moved earlier")
    events = json.loads(r["HX-Trigger"])
    assert "models-changed" in events and "devices-changed" in events and "wo-changed" in events
    assert "Manufacturer's schedule by regulation: CMS requires imaging, radiologic, and medical laser equipment" in text(r)
    body = text(client.get(f"/pm/models/{c_arm.pk}/edit/", **HX))
    assert "checked" in body.split('name="oem_schedule_required"')[1].split(">")[0]
    r = client.post(f"/pm/models/{c_arm.pk}/edit/", edit_post(c_arm), **HX)  # unchecked: cleared
    assert toast(r) == ("Siemens Healthineers Cios Spin updated: the manufacturer's schedule is no longer required; it stays on the OEM "
                        "interval until an AEM is approved")
    c_arm.refresh_from_db()
    assert not c_arm.oem_schedule_required and c_arm.pm_interval_months == 6


def test_edit_details_without_equipment_approve_keeps_the_mark(client, people, c_arm):
    mark(c_arm, people["director"])
    client.force_login(people["tech"])
    body = text(client.get(f"/pm/models/{c_arm.pk}/edit/", **HX))
    assert 'name="oem_schedule_required"' not in body
    assert 'id="dm-mark-ro" type="text" value="Yes: imaging, radiologic, or medical laser equipment" readonly' in body
    r = client.post(f"/pm/models/{c_arm.pk}/edit/", edit_post(c_arm, description="Mobile C-arm, 3D"), **HX)  # no checkbox sent: kept
    assert toast(r) == "Siemens Healthineers Cios Spin updated"
    c_arm.refresh_from_db()
    assert c_arm.oem_schedule_required and c_arm.description == "Mobile C-arm, 3D"
    r = client.post(f"/pm/models/{c_arm.pk}/edit/", edit_post(c_arm, description="Changed", oem_schedule_required=""), **HX)
    assert r.status_code == 200 and "HX-Retarget" not in r and eq.OEM_SCHEDULE_PERMISSION in text(r)
    c_arm.refresh_from_db()
    assert c_arm.oem_schedule_required and c_arm.description == "Mobile C-arm, 3D"


def test_the_model_drawer_says_the_model_follows_the_manufacturers_schedule(client, people, c_arm):
    client.force_login(people["tech"])
    body = text(client.get(f"/pm/models/{c_arm.pk}/", **HX))
    assert "OEM required by CMS" not in body and "Manufacturer's schedule by regulation" not in body
    mark(c_arm, people["director"])
    body = text(client.get(f"/pm/models/{c_arm.pk}/", **HX))
    assert '<span class="chip">OEM 6 mo</span><span class="chip neutral"' in body and "OEM required by CMS</span>" in body
    assert "<dt>Interval in force</dt><dd>6 months, OEM by regulation" in body
    assert ("Manufacturer's schedule by regulation: CMS requires imaging, radiologic, and medical laser equipment to follow the manufacturer's "
            "maintenance schedule, so this model stays on the OEM's 6 months and never goes on AEM.") in body
    assert client.get(f"/pm/models/{c_arm.pk}/").status_code == 200  # the full page too


def test_the_aem_tab_says_why_instead_of_offering_propose(client, people, c_arm):
    client.force_login(people["tech"])
    assert "Propose an AEM interval</button>" in text(client.get(f"/pm/models/{c_arm.pk}/?tab=aem", **HX))
    mark(c_arm, people["director"])
    body = text(client.get(f"/pm/models/{c_arm.pk}/?tab=aem", **HX))
    assert ("Manufacturer's schedule: every 6 months, the OEM interval. CMS requires the manufacturer's maintenance schedule for imaging, "
            "radiologic, and medical laser equipment, so this model is excluded from AEM") in body
    assert "Propose an AEM interval</button>" not in body and body.count("excluded from AEM") == 1  # said once, in the interval section
    modal = text(client.get(f"/pm/models/{c_arm.pk}/aem/propose/", **HX))
    assert aem.OEM_SCHEDULE_REFUSAL in modal and "hx-post" not in modal
    r = client.post(f"/pm/models/{c_arm.pk}/aem/propose/", {"interval_months": "12", "rationale": "Few failures."}, **HX)
    assert r.status_code == 200 and "HX-Retarget" not in r and aem.OEM_SCHEDULE_REFUSAL in text(r)
    assert not AemDecision.objects.exists()


def test_a_proposal_left_on_a_marked_model_is_rejected_or_withdrawn_not_approved(client, people, c_arm):
    d = propose(c_arm, people)
    marked_behind_the_services_back(c_arm)
    client.force_login(people["manager"])
    body = text(client.get(f"/pm/models/{c_arm.pk}/?tab=aem", **HX))
    assert f"/pm/aem/{d.pk}/decide/?decision=approve" not in body and f"/pm/aem/{d.pk}/decide/?decision=reject" in body
    assert f"/pm/aem/{d.pk}/withdraw/" in body and "This model is excluded from AEM now, so this proposal cannot be approved" in body
    modal = text(client.get(f"/pm/aem/{d.pk}/decide/?decision=approve", **HX))
    assert aem.OEM_SCHEDULE_REFUSAL in modal and "hx-post" not in modal
    assert "hx-post" in client.get(f"/pm/aem/{d.pk}/decide/?decision=reject", **HX).content.decode()
    r = client.post(f"/pm/aem/{d.pk}/decide/", {"decision": "approve", "decided_on": TODAY.isoformat(), "note": "EMC"}, **HX)
    assert r.status_code == 200 and "HX-Retarget" not in r and aem.OEM_SCHEDULE_REFUSAL in text(r)
    d.refresh_from_db()
    assert d.status == AemStatus.PROPOSED


def test_ending_an_unused_interval_on_a_marked_model_moves_nothing(client, people, ctx, dept, pump_model):
    marked_behind_the_services_back(pump_model)  # 18 months still on file, never used while marked
    pump = Asset.objects.create(tag="P-1", device_model=pump_model, department=dept, installed_on=TODAY - timedelta(days=800),
                                next_pm_on=TODAY + timedelta(days=10))
    client.force_login(people["director"])
    body = text(client.get(f"/pm/aem/{pump_model.pk}/end/", **HX))
    assert "clear the unused AEM interval of 18 months (CMS requires the manufacturer's schedule: the OEM's 12)" in body
    assert "No device's next PM moves: this model's devices have always followed the OEM interval." in body
    client.post(f"/pm/aem/{pump_model.pk}/end/", {"reason": "Clearing an old value."}, **HX)
    pump_model.refresh_from_db()
    pump.refresh_from_db()
    assert pump_model.aem_interval_months is None and pump.next_pm_on == TODAY + timedelta(days=10)


def test_the_pm_library_shows_the_oem_requirement(c_arm, people, pump_model):
    on_aem(c_arm, people)
    library = panels_context(None, TODAY)["library"]
    row = next(r for r in library["rows"] if r["device_model"] == c_arm)
    assert row["aem"] and not row["oem_required"] and library["oem_required"] == 0
    mark(c_arm, people["director"])
    marked_behind_the_services_back(pump_model)  # its 18 months on file are never used
    from django.template.loader import render_to_string

    body = html.unescape(render_to_string("web/_pm_panels.html", panels_context(None, TODAY)))
    cells = [r.split("</tr>", 1)[0] for r in body.split('<tr class="row"')[1:]]
    rows = {r.split("</a>", 1)[0].rsplit(">", 1)[1]: r for r in cells}
    for name in ("Siemens Healthineers Cios Spin", "BD Alaris 8015 PCU"):
        assert ">OEM required by CMS</span>" in rows[name] and '<span class="chip acc">AEM' not in rows[name], rows[name]
    assert "OEM required by CMS: imaging, radiologic, and medical laser equipment follow the manufacturer's maintenance schedule" in body
    assert "never go on AEM (2 models)." in body


def test_the_device_drawer_says_so_on_its_pm_tab(client, people, c_arm):
    client.force_login(people["tech"])
    rule = "CMS requires the manufacturer's maintenance schedule for imaging, radiologic, and medical laser equipment, so this model never goes on AEM."
    assert rule not in text(client.get("/equipment/CA-1/?tab=pm", **HX))
    mark(c_arm, people["director"])
    body = text(client.get("/equipment/CA-1/?tab=pm", **HX))
    assert f"Following the OEM schedule: every 6 months. {rule}" in body


# --- the API -------------------------------------------------------------------------------------------------------------------


def api_patch(client, dm, body):
    return client.patch(f"/api/v1/device-models/{dm.pk}/", body, content_type="application/json")


def test_api_shows_the_mark_and_only_equipment_approve_changes_it(client, people, c_arm):
    on_aem(c_arm, people)
    client.force_login(people["tech"])
    assert client.get(f"/api/v1/device-models/{c_arm.pk}/").json()["oem_schedule_required"] is False
    r = api_patch(client, c_arm, {"oem_schedule_required": True})
    assert r.status_code == 403 and r.json() == {"detail": eq.OEM_SCHEDULE_PERMISSION}
    assert api_patch(client, c_arm, {"oem_schedule_required": False, "description": "Mobile C-arm, 3D"}).status_code == 200  # its own value
    c_arm.refresh_from_db()
    assert not c_arm.oem_schedule_required and c_arm.aem_interval_months == 12 and c_arm.description == "Mobile C-arm, 3D"
    client.force_login(people["director"])
    r = api_patch(client, c_arm, {"oem_schedule_required": True})
    assert r.status_code == 200, r.content
    data = r.json()
    assert data["oem_schedule_required"] is True and data["aem_interval_months"] is None and data["pm_interval_months"] == 6
    assert AemDecision.objects.get().status == AemStatus.ENDED and Asset.objects.get(tag="CA-1").next_pm_on == TODAY


def test_api_refuses_a_new_model_sent_marked(client, people, ctx):
    client.force_login(people["director"])
    body = {"manufacturer": "GE HealthCare", "model": "OEC 3D", "description": "Mobile C-arm", "category": "Imaging", "risk_class": "high"}
    r = client.post("/api/v1/device-models/", {**body, "oem_schedule_required": True}, content_type="application/json")
    assert r.status_code == 400 and "Set this with PATCH once the model is added" in r.json()["oem_schedule_required"][0]
    assert not DeviceModel.objects.exists()
    r = client.post("/api/v1/device-models/", {**body, "oem_schedule_required": False}, content_type="application/json")
    assert r.status_code == 201 and r.json()["oem_schedule_required"] is False


# --- the demo seed -------------------------------------------------------------------------------------------------------------


def test_the_demo_marks_its_imaging_models(db):
    call_command("seed_demo", stdout=StringIO())
    with tenant_context(Tenant.objects.get(slug="riverside")):
        assert set(DeviceModel.objects.filter(oem_schedule_required=True).values_list("model", flat=True)) == {"Cios Spin"}
        assert not DeviceModel.objects.filter(category="Imaging", oem_schedule_required=False).exists()
        assert not DeviceModel.objects.filter(oem_schedule_required=True, aem_interval_months__isnull=False).exists()
        assert not AemDecision.objects.filter(device_model__oem_schedule_required=True).exists()
        assert AemDecision.objects.filter(status=AemStatus.APPROVED).count() == 1  # the monitors' AEM is still there


# --- the importer --------------------------------------------------------------------------------------------------------------


def _import(tmp_path, rows: str, column="OEM Schedule Required") -> tuple[str, str]:
    path = tmp_path / "inventory.csv"
    path.write_text(f"Asset Tag,Manufacturer,Model,Department,{column}\n" + rows)
    out, err = StringIO(), StringIO()
    call_command("import_assets", "--tenant", "riverside", str(path), stdout=out, stderr=err)
    return out.getvalue(), err.getvalue()


def test_the_importer_marks_the_models_it_adds(ctx, tmp_path):
    out, err = _import(tmp_path, "CT-1,GE HealthCare,Revolution CT,Radiology,Yes\nLS-1,Lumenis,UltraPulse,OR,TRUE\nUS-1,Philips,EPIQ 7,Radiology,1\n"
                                 "P-1,Acme,Pump A,ICU,no\nP-2,Acme,Pump B,ICU,\nP-3,Acme,Pump C,ICU,0\nP-4,Acme,Pump D,ICU,False\n")
    assert "7 created, 0 updated, 0 skipped" in out and not err
    marked = dict(DeviceModel.objects.values_list("model", "oem_schedule_required"))
    assert marked == {"Revolution CT": True, "UltraPulse": True, "EPIQ 7": True, "Pump A": False, "Pump B": False, "Pump C": False, "Pump D": False}
    assert DeviceModel.objects.get(model="Revolution CT").pm_interval_months == 12


def test_an_import_never_changes_a_models_mark(ctx, dept, c_arm, pump_model, people, tmp_path):
    mark(c_arm, people["director"])
    out, _err = _import(tmp_path, "CA-9,Siemens Healthineers,Cios Spin,ICU,no\nP-9,BD,Alaris 8015 PCU,ICU,yes\n", column="oem schedule")
    assert "2 created" in out
    c_arm.refresh_from_db()
    pump_model.refresh_from_db()
    assert c_arm.oem_schedule_required and not pump_model.oem_schedule_required and pump_model.aem_interval_months == 18


def test_an_unreadable_mark_skips_the_row(ctx, tmp_path):
    out, err = _import(tmp_path, "CT-1,GE HealthCare,Revolution CT,Radiology,maybe\nCT-2,GE HealthCare,Revolution CT,Radiology,yes\n")
    assert "1 created, 0 updated, 1 skipped" in out
    assert "Skipped 'CT-1': OEM schedule required is 'maybe'; use yes or no (blank is no)." in err
    assert list(Asset.objects.values_list("tag", flat=True)) == ["CT-2"] and DeviceModel.objects.get().oem_schedule_required


def test_a_file_without_the_column_adds_unmarked_models(ctx, tmp_path):
    path = tmp_path / "inventory.csv"
    path.write_text("Asset Tag,Manufacturer,Model,Department\nCT-1,GE HealthCare,Revolution CT,Radiology\n")
    call_command("import_assets", "--tenant", "riverside", str(path), stdout=StringIO(), stderr=StringIO())
    assert not DeviceModel.objects.get().oem_schedule_required


# --- under row-level security (PostgreSQL) -------------------------------------------------------------------------------------


@needs_postgres
def test_marking_a_model_on_aem_under_the_policies(client, people, c_arm, tenant):
    d = on_aem(c_arm, people)
    client.force_login(people["director"])
    as_app_role()
    r = client.post(f"/pm/models/{c_arm.pk}/edit/", edit_post(c_arm, oem_schedule_required="on"), **HX)
    assert r.status_code == 200 and toast(r).endswith("1 device's next PM moved earlier"), r.content.decode()[:500]
    with tenant_context(tenant):
        d.refresh_from_db()
        c_arm.refresh_from_db()
        assert c_arm.oem_schedule_required and c_arm.aem_interval_months is None and d.status == AemStatus.ENDED
        assert Asset.objects.get(tag="CA-1").next_pm_on == TODAY
    assert "OEM required by CMS" in client.get("/pm/").content.decode()
    r = client.patch(f"/api/v1/device-models/{c_arm.pk}/", {"oem_schedule_required": False}, content_type="application/json")
    assert r.status_code == 200 and r.json()["oem_schedule_required"] is False


@needs_postgres
def test_importing_the_mark_under_the_policies(ctx, tmp_path):
    as_app_role()
    out, err = _import(tmp_path, "CT-1,GE HealthCare,Revolution CT,Radiology,yes\nCT-2,GE HealthCare,Revolution CT,Radiology,perhaps\n")
    assert "1 created, 0 updated, 1 skipped" in out and "'perhaps'" in err
    with tenant_context(ctx):
        assert DeviceModel.objects.get(model="Revolution CT").oem_schedule_required
