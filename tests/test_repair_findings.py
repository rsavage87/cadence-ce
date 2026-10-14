"""
Slice 33, wave 1: what a repair found (WorkOrder.repair_finding, RepairFinding; apps.workorders.findings, the one rule), through the
services. complete_work_order requires one on a repair with none stored, keyed repair_finding and reported with every other problem;
left out (KEEP), None, or blank keeps the one stored when a reopened repair is completed again; a value is one of findings.choices_for
(the user, an incident's investigation, a failed PM's repair, vendor service), refused in findings' words, except that a scoped user
leaves the facility's finding as stored; another type refuses one. workorders.services.set_repair_finding records or corrects one on a
completed or closed repair (never open, cancelled, another type, blank, or a scoped user's), audited "What was found: <label>" or
"Recorded later: <label>" within the history's 100 characters. assign clears "not stated by the vendor" when the work leaves that vendor;
an incident adopting a repair as its investigation clears a use error, audited. The groups partition every value and "", and failure_q
and recorded_q count by them in the database. The API and the completion modal pass the finding through (their screens are wave 2's).
Everything runs on both databases; the last test under the row-level security policies as the runtime role.
"""
import inspect

import pytest
from django.core.exceptions import ValidationError
from django.db.models import Count
from django.utils import timezone
from incident_fixtures import make_incident
from pg_helpers import as_app_role, needs_postgres
from repair_fixtures import CHECKED, FINDING, POST_REPAIR_ALL_PASS, checked_post

from apps.equipment.models import Asset
from apps.incidents import services as inc
from apps.incidents.models import Affected, Outcome, Status
from apps.tenants.context import tenant_context
from apps.workorders import findings
from apps.workorders.completion import complete_work_order, keep_open
from apps.workorders.models import Priority, RepairCheck, RepairFinding, WorkOrder, WoStatus, WoType
from apps.workorders.services import USE_ERROR_CLEARED, assign, change_status, create_work_order, set_repair_finding

F = RepairFinding
HX = {"HTTP_HX_REQUEST": "true"}
VENDOR = "Hamilton Medical field service"
# Every value the facility may record on in-house work, in the select's order (findings.GROUPS)
IN_HOUSE = ["no_problem_found", "use_error", "environment", "damage", "battery", "accessory", "software", "wear", "pm_preventable",
            "service_induced"]
OBSERVED = ["no_problem_found", "environment", "damage", "battery", "accessory", "software", "wear"]


@pytest.fixture
def today(ctx):
    return timezone.localdate()


@pytest.fixture
def vendor_user(make_user):
    user = make_user("vendor")
    user.company = "Hamilton Medical"
    user.save()
    return user


@pytest.fixture
def manager(make_user):
    return make_user("manager")


def repair(asset, *, tech=None, vendor="", start=True, **fields):
    wo = create_work_order(asset=asset, type=WoType.REPAIR, priority=Priority.HIGH, problem="Low tidal volume alarm", **fields)
    if tech is not None or vendor:
        assign(wo, technician=tech, vendor_name=vendor)
    if start:
        change_status(wo, WoStatus.IN_PROGRESS)
    return wo


def done(wo, **kwargs):
    """Complete `wo` as checked and ready for use (CHECKED's finding unless `kwargs` say otherwise), and read it again."""
    complete_work_order(wo, **{"resolution": "Replaced the flow sensor", **CHECKED, **kwargs})
    wo.refresh_from_db()
    return wo


def reopened(wo):
    wo.refresh_from_db()
    change_status(wo, WoStatus.IN_PROGRESS)
    return wo


def refused(wo, **kwargs) -> dict:
    """complete_work_order refused, and nothing saved."""
    before = WorkOrder.objects.get(pk=wo.pk)
    with pytest.raises(ValidationError) as e:
        complete_work_order(wo, **{"resolution": "Replaced the flow sensor", **CHECKED, **kwargs})
    wo.refresh_from_db()
    assert (wo.status, wo.repair_finding, wo.repair_check) == (before.status, before.repair_finding, before.repair_check)
    return {k: " ".join(v) for k, v in e.value.message_dict.items()}


def not_set(wo, finding, **kwargs) -> str:
    """set_repair_finding refused (keyed repair_finding), and nothing saved."""
    before = WorkOrder.objects.get(pk=wo.pk).repair_finding
    with pytest.raises(ValidationError) as e:
        set_repair_finding(wo, finding, **kwargs)
    assert WorkOrder.objects.get(pk=wo.pk).repair_finding == before
    assert list(e.value.message_dict) == ["repair_finding"]
    return " ".join(e.value.message_dict["repair_finding"])


def reasons(wo) -> list[str]:
    return [r.history_change_reason for r in WorkOrder.history.filter(id=wo.pk).order_by("history_date", "history_id")
            if r.history_change_reason]


def failed_pms_repair(asset, tech):
    """A repair opened from a failed PM (completion's follow-up: follow_up_of the PM), started."""
    pm = create_work_order(asset=asset, type=WoType.PM, priority=Priority.NORMAL, problem="Scheduled preventive maintenance", assigned_to=tech)
    wo = create_work_order(asset=asset, type=WoType.REPAIR, priority=Priority.HIGH, problem=f"PM {pm.number} failed", assigned_to=tech,
                           follow_up_of=pm)
    change_status(wo, WoStatus.IN_PROGRESS)
    return wo


# --- the groups --------------------------------------------------------------------------------------------------------------------


def test_the_groups_partition_every_finding_and_blank():
    every = ["", *F.values]
    for value in every:
        assert sum(value in group for group in findings.PARTITION) == 1, value
    assert sorted(v for group in findings.PARTITION for v in group) == sorted(every)
    assert set(findings.USE_RELATED) == {F.USE_ERROR, F.DAMAGE} and set(findings.PM_RELATED) <= set(findings.RELIABILITY)
    assert not set(findings.OBSERVATIONS) & {F.USE_ERROR, F.PM_PREVENTABLE, F.SERVICE_INDUCED, F.NOT_STATED}
    assert [v for _label, values in findings.GROUPS for v in values] == list(F.values)  # the select offers every value once
    assert set(findings.ORDER) == set(findings.OTHER_FAULT) | set(findings.RELIABILITY) | {F.ENVIRONMENT} and set(findings.HINTS) == set(F.values)
    assert all(len(label) <= 30 for label in F.labels) and findings.label("") == "Not recorded" and findings.label(F.WEAR) == F.WEAR.label


# --- required, and reported with the rest --------------------------------------------------------------------------------------------


def test_a_repair_needs_a_finding_reported_with_every_other_problem(vent, techs):
    wo = repair(vent, tech=techs["dana"])
    assert refused(wo, repair_finding=None) == {"repair_finding": findings.REQUIRED}
    for left_out in ({}, {"repair_finding": ""}, {"repair_finding": "  "}):
        kwargs = {"repair_check": RepairCheck.PASSED, "results": POST_REPAIR_ALL_PASS, **left_out}
        with pytest.raises(ValidationError) as e:
            complete_work_order(wo, resolution="Fixed", **kwargs)
        assert e.value.message_dict == {"repair_finding": [findings.REQUIRED]}
    errors = refused(wo, resolution="", repair_check="", repair_finding=None, hours="30")
    assert set(errors) == {"resolution", "repair_check", "repair_finding", "hours"} and errors["repair_finding"] == findings.REQUIRED
    # not returned to use takes one too (the repair is over); keep_open never takes one (the repair is not)
    assert refused(wo, repair_check=RepairCheck.NOT_RETURNED, results=None, repair_finding=None) == {"repair_finding": findings.REQUIRED}
    assert "repair_finding" not in inspect.signature(keep_open).parameters
    assert refused(wo, repair_finding="user_error") == {"repair_finding": findings.NOT_OFFERED}


def test_the_finding_is_stored_with_the_completion(vent, techs):
    wo = done(repair(vent, tech=techs["dana"], tag_out=True))
    assert (wo.status, wo.repair_check, wo.repair_finding) == (WoStatus.COMPLETED, RepairCheck.PASSED, FINDING)
    row = WorkOrder.history.filter(id=wo.pk).latest("history_date")
    assert (row.status, row.repair_finding) == (WoStatus.COMPLETED, FINDING)  # in the completion's own save
    gone = done(repair(vent, tech=techs["dana"]), repair_check=RepairCheck.NOT_RETURNED, results=None, repair_finding=F.DAMAGE)
    assert (gone.repair_check, gone.repair_finding) == (RepairCheck.NOT_RETURNED, F.DAMAGE)


def test_a_reopened_repair_keeps_its_finding_unless_another_is_chosen(vent, techs):
    wo = done(repair(vent, tech=techs["dana"]), repair_finding=F.BATTERY)
    for kept in ({}, {"repair_finding": None}, {"repair_finding": ""}, {"repair_finding": F.BATTERY}):
        reopened(wo)
        args = {**CHECKED, **kept}
        if "repair_finding" not in kept:
            del args["repair_finding"]  # left out: complete_work_order's KEEP
        complete_work_order(wo, resolution="Replaced the battery again", **args)
        wo.refresh_from_db()
        assert (wo.status, wo.repair_finding) == (WoStatus.COMPLETED, F.BATTERY), kept
    assert done(reopened(wo), repair_finding=F.SOFTWARE).repair_finding == F.SOFTWARE


# --- who may choose what -----------------------------------------------------------------------------------------------------------


def test_choices_per_user_and_repair(vent, pump, techs, manager, vendor_user, make_user):
    in_house = repair(vent, tech=techs["dana"])
    vendor = repair(vent, vendor=VENDOR)
    requester = make_user("requester")
    assert findings.choices_for(in_house, manager) == IN_HOUSE == findings.choices_for(in_house)
    assert findings.choices_for(vendor, manager) == [*IN_HOUSE, "not_stated"]
    assert findings.choices_for(vendor, vendor_user) == OBSERVED == findings.choices_for(in_house, requester)
    investigation = make_incident(pump).work_order
    assert findings.choices_for(investigation, manager) == [v for v in IN_HOUSE if v != "use_error"]
    from_pm = failed_pms_repair(vent, techs["dana"])
    assert findings.choices_for(from_pm, manager) == [v for v in IN_HOUSE if v != "pm_preventable"]
    pm = create_work_order(asset=vent, type=WoType.PM, priority=Priority.NORMAL, problem="PM")
    assert findings.choices_for(pm, manager) == []


def test_a_scoped_user_records_only_what_it_sees_on_the_device(vent, vendor_user):
    wo = repair(vent, vendor=VENDOR)
    for judged in (F.USE_ERROR, F.PM_PREVENTABLE, F.SERVICE_INDUCED, F.NOT_STATED):
        assert refused(wo, repair_finding=judged, by=vendor_user) == {"repair_finding": findings.NOT_OFFERED}, judged
    assert done(wo, repair_finding=F.DAMAGE, by=vendor_user).repair_finding == F.DAMAGE


def test_a_scoped_user_leaves_the_facilitys_finding_as_stored(vent, manager, vendor_user):
    wo = done(repair(vent, vendor=VENDOR), repair_finding=F.USE_ERROR, by=manager)
    # sent nothing: kept, though it is not theirs to choose; the stored value sent back is refused like any other (review fix: the
    # answer never tells a vendor's account what the facility recorded)
    for kept in ({"repair_finding": None}, {"repair_finding": ""}):
        assert done(reopened(wo), by=vendor_user, **kept).repair_finding == F.USE_ERROR, kept
    reopened(wo)
    assert refused(wo, repair_finding=F.USE_ERROR, by=vendor_user) == {"repair_finding": findings.FACILITY_RECORDED}
    done(wo, by=vendor_user, repair_finding=None)
    reopened(wo)
    # merge fix: the facility's judgment is never replaced from a vendor's account, not even by one of its own observations
    for other in (F.PM_PREVENTABLE, F.WEAR):
        assert refused(wo, repair_finding=other, by=vendor_user) == {"repair_finding": findings.FACILITY_RECORDED}, other
    assert done(wo, repair_finding=F.WEAR, by=manager).repair_finding == F.WEAR  # the facility changes it
    stated = done(repair(vent, vendor=VENDOR), repair_finding=F.NOT_STATED, by=manager)
    assert done(reopened(stated), repair_finding=None, by=vendor_user).repair_finding == F.NOT_STATED


def test_not_stated_only_on_vendor_service_by_the_facility(vent, techs, manager, vendor_user):
    assert refused(repair(vent, tech=techs["dana"]), repair_finding=F.NOT_STATED, by=manager) == {"repair_finding": findings.NOT_OFFERED}
    assert refused(repair(vent, vendor=VENDOR), repair_finding=F.NOT_STATED, by=vendor_user) == {"repair_finding": findings.NOT_OFFERED}
    wo = done(repair(vent, vendor=VENDOR), repair_check=RepairCheck.VENDOR_REPORT, results=None, checks_reference="FSO-1",
              repair_finding=F.NOT_STATED, by=manager)
    assert (wo.repair_check, wo.repair_finding) == (RepairCheck.VENDOR_REPORT, F.NOT_STATED)
    checked_here = done(repair(vent, vendor=VENDOR), repair_finding=F.NOT_STATED)  # CE checked it after the vendor: any outcome
    assert (checked_here.repair_check, checked_here.repair_finding) == (RepairCheck.PASSED, F.NOT_STATED)


def test_an_incidents_investigation_never_records_a_use_error(vent, pump, manager, vendor_user):
    incident = make_incident(vent)
    wo = incident.work_order
    assign(wo, vendor_name=VENDOR)
    change_status(wo, WoStatus.IN_PROGRESS)
    assert refused(wo, repair_finding=F.USE_ERROR, by=manager) == {"repair_finding": findings.INVESTIGATION}
    assert "an incident's investigation" in findings.INVESTIGATION and incident.number not in findings.INVESTIGATION
    assert refused(wo, repair_finding=F.USE_ERROR, by=vendor_user) == {"repair_finding": findings.NOT_OFFERED}  # never theirs anyway
    assert done(wo, repair_finding=F.DAMAGE, by=manager).repair_finding == F.DAMAGE  # damage is the device's: allowed
    # a closed incident's investigation stays one; one recorded in error is not
    closed = make_incident(pump, hold=False, status=Status.CLOSED).work_order
    assign(closed, vendor_name="BD field service")
    change_status(closed, WoStatus.IN_PROGRESS)
    assert refused(closed, repair_finding=F.USE_ERROR) == {"repair_finding": findings.INVESTIGATION}
    error = make_incident(pump, hold=False, status=Status.IN_ERROR).work_order
    assign(error, vendor_name="BD field service")
    change_status(error, WoStatus.IN_PROGRESS)
    assert done(error, repair_finding=F.USE_ERROR).repair_finding == F.USE_ERROR


def test_a_failed_pms_repair_never_records_pm_could_have_caught_it(vent, techs, manager, vendor_user):
    wo = failed_pms_repair(vent, techs["dana"])
    assert findings.found_on_pm(wo) and not findings.found_on_pm(repair(vent, tech=techs["dana"]))
    assert refused(wo, repair_finding=F.PM_PREVENTABLE, by=manager) == {"repair_finding": findings.FOUND_ON_PM}
    assert done(wo, repair_finding=F.WEAR, by=manager).repair_finding == F.WEAR  # what failed
    assert done(reopened(wo), repair_finding=F.SERVICE_INDUCED).repair_finding == F.SERVICE_INDUCED
    # a stored finding the repair's limits refuse is chosen again (a repair a later PM's failure was recorded on)
    other = done(repair(vent, tech=techs["dana"]), repair_finding=F.PM_PREVENTABLE)
    reopened(other)
    pm = create_work_order(asset=vent, type=WoType.PM, priority=Priority.NORMAL, problem="PM")
    WorkOrder.objects.filter(pk=other.pk).update(follow_up_of=pm)
    other.refresh_from_db()
    assert refused(other, repair_finding=None) == {"repair_finding": findings.FOUND_ON_PM}


def test_another_type_refuses_a_finding(vent, techs):
    recall = create_work_order(asset=vent, type=WoType.RECALL, priority=Priority.NORMAL, problem="Recall", assigned_to=techs["dana"])
    change_status(recall, WoStatus.IN_PROGRESS)
    with pytest.raises(ValidationError) as e:
        complete_work_order(recall, resolution="Done", repair_finding=F.WEAR)
    assert e.value.message_dict == {"repair_finding": [findings.NOT_A_REPAIR]}
    with pytest.raises(ValidationError) as e:
        complete_work_order(recall, resolution="", repair_finding=F.WEAR)
    assert set(e.value.message_dict) == {"resolution", "repair_finding"}  # with the rest
    complete_work_order(recall, resolution="Done", repair_finding="")  # nothing sent: nothing to refuse
    recall.refresh_from_db()
    assert (recall.status, recall.repair_finding) == (WoStatus.COMPLETED, "")


# --- set_repair_finding ------------------------------------------------------------------------------------------------------------


def test_set_repair_finding_corrects_or_records_one_later_audited(vent, techs, manager):
    wo = done(repair(vent, tech=techs["dana"]))
    assert set_repair_finding(wo, F.BATTERY, by=manager).repair_finding == F.BATTERY and wo.repair_finding == F.BATTERY
    row = WorkOrder.history.filter(id=wo.pk).latest("history_date")
    assert (row.history_change_reason, row.history_user, row.repair_finding) == ("What was found: Battery failed", manager, F.BATTERY)
    count = WorkOrder.history.filter(id=wo.pk).count()
    set_repair_finding(wo, F.BATTERY, by=manager)  # nothing changed: nothing saved
    assert WorkOrder.history.filter(id=wo.pk).count() == count
    change_status(wo, WoStatus.CLOSED)
    set_repair_finding(wo, F.ACCESSORY, by=manager)  # closed: the caller checks Approve
    old = done(repair(vent, tech=techs["dana"]))
    WorkOrder.objects.filter(pk=old.pk).update(repair_finding="")  # completed before slice 33
    set_repair_finding(old, F.NO_PROBLEM_FOUND)
    assert reasons(wo)[-2:] == ["What was found: Battery failed", "What was found: Accessory or cable"]
    assert reasons(old)[-1] == "Recorded later: No problem found"
    reason_max = WorkOrder.history.model._meta.get_field("history_change_reason").max_length
    assert reason_max == 100 and max(len(f"Recorded later: {label}") for label in F.labels) <= reason_max
    assert len(USE_ERROR_CLEARED) <= reason_max


def test_set_repair_finding_refusals(vent, pump, techs, manager, vendor_user):
    wo = done(repair(vent, vendor=VENDOR))
    assert not_set(wo, F.WEAR, by=vendor_user) == "The facility's CE staff record what a repair found after it is completed."
    assert not_set(wo, "") == findings.REQUIRED and not_set(wo, None) == findings.REQUIRED
    assert not_set(wo, "user_error") == findings.NOT_OFFERED
    set_repair_finding(wo, F.NOT_STATED, by=manager)  # vendor service
    open_ = repair(vent, tech=techs["dana"])
    assert not_set(open_, F.WEAR) == f"{open_.number} is not completed yet: record what was found when completing it."
    assert not_set(repair(vent, tech=techs["dana"], start=False), F.WEAR).endswith("record what was found when completing it.")
    cancelled = repair(vent, tech=techs["dana"], start=False)
    change_status(cancelled, WoStatus.CANCELLED)
    assert not_set(cancelled, F.WEAR) == f"{cancelled.number} was cancelled: it records no finding."
    recall = create_work_order(asset=vent, type=WoType.RECALL, priority=Priority.NORMAL, problem="Recall", assigned_to=techs["dana"])
    change_status(recall, WoStatus.IN_PROGRESS)
    complete_work_order(recall, resolution="Done")
    assert not_set(recall, F.WEAR) == findings.NOT_A_REPAIR
    in_house = done(repair(vent, tech=techs["dana"]))
    assert not_set(in_house, F.NOT_STATED, by=manager) == findings.NOT_OFFERED
    investigation = make_incident(pump, hold=False).work_order
    assign(investigation, vendor_name="BD field service")
    change_status(investigation, WoStatus.IN_PROGRESS)
    done(investigation)
    assert not_set(investigation, F.USE_ERROR, by=manager) == findings.INVESTIGATION
    from_pm = done(failed_pms_repair(vent, techs["dana"]))
    assert not_set(from_pm, F.PM_PREVENTABLE, by=manager) == findings.FOUND_ON_PM


# --- the writes that keep a stored finding true -------------------------------------------------------------------------------------


def test_assign_clears_not_stated_when_the_work_leaves_that_vendor(vent, techs, manager):
    wo = done(repair(vent, vendor=VENDOR), repair_finding=F.NOT_STATED, by=manager)
    reopened(wo)
    assign(wo, vendor_name="hamilton medical")  # the same vendor (vendor_key): kept
    assert WorkOrder.objects.get(pk=wo.pk).repair_finding == F.NOT_STATED
    assign(wo, technician=techs["dana"])
    wo.refresh_from_db()
    assert wo.repair_finding == ""
    assert WorkOrder.history.filter(id=wo.pk).latest("history_date").repair_finding == ""
    assert refused(wo, repair_finding=None) == {"repair_finding": findings.REQUIRED}  # in-house now: what was found, again
    other = done(repair(vent, vendor=VENDOR), repair_finding=F.NOT_STATED, by=manager)
    reopened(other)
    assign(other, vendor_name="TechCare Biomedical Services")
    assert WorkOrder.objects.get(pk=other.pk).repair_finding == ""
    stated = done(repair(vent, vendor=VENDOR), repair_finding=F.SOFTWARE, by=manager)
    reopened(stated)
    assign(stated, technician=techs["dana"])  # a stated finding is the device's, whoever finishes the work
    assert WorkOrder.objects.get(pk=stated.pk).repair_finding == F.SOFTWARE


def test_an_incident_adopting_a_repair_clears_a_use_error_audited(vent, pump, techs, manager, today):
    wo = done(repair(vent, tech=techs["dana"]), repair_finding=F.USE_ERROR)
    reopened(wo)  # it happened again: the unit reports it as an incident
    incident = inc.record_incident(asset=vent, outcome=Outcome.UNKNOWN, affected=Affected.PATIENT, work_order=wo, by=manager, today=today)
    wo.refresh_from_db()
    assert incident.work_order_id == wo.pk and wo.repair_finding == ""
    row = WorkOrder.history.filter(id=wo.pk).latest("history_date")
    assert (row.history_change_reason, row.history_user, row.repair_finding) == (USE_ERROR_CLEARED, manager, "")
    assert done(wo, repair_finding=F.NO_PROBLEM_FOUND).repair_finding == F.NO_PROBLEM_FOUND
    damaged = done(repair(pump, tech=techs["dana"]), repair_finding=F.DAMAGE)
    reopened(damaged)
    inc.record_incident(asset=pump, outcome=Outcome.NO_HARM, affected=Affected.NONE, work_order=damaged, hold=False, today=today)
    assert WorkOrder.objects.get(pk=damaged.pk).repair_finding == F.DAMAGE  # only a use error is taken off


# --- failure_q and recorded_q -------------------------------------------------------------------------------------------------------


def test_failure_q_and_recorded_q_count_by_the_groups(vent, pump, techs):
    made = {}
    for value in ["", *F.values]:
        wo = repair(vent, tech=techs["dana"], start=False)
        WorkOrder.objects.filter(pk=wo.pk).update(status=WoStatus.CLOSED, repair_finding=value)
        made[value] = wo.pk
    open_use_error = repair(vent, tech=techs["dana"])  # reopened with a finding stored: being worked on again, so unknown
    WorkOrder.objects.filter(pk=open_use_error.pk).update(repair_finding=F.USE_ERROR)
    pm = create_work_order(asset=vent, type=WoType.PM, priority=Priority.NORMAL, problem="PM")
    WorkOrder.objects.filter(pk=pm.pk).update(status=WoStatus.CLOSED)
    on_pump = repair(pump, tech=techs["dana"], start=False)
    WorkOrder.objects.filter(pk=on_pump.pk).update(status=WoStatus.COMPLETED, repair_finding=F.ENVIRONMENT)
    failures = set(WorkOrder.objects.filter(findings.failure_q()).values_list("pk", flat=True))
    counted = {made[v] for v in (*findings.RELIABILITY, *findings.UNKNOWN)} | {open_use_error.pk}
    assert failures == counted
    recorded = set(WorkOrder.objects.filter(findings.recorded_q()).values_list("pk", flat=True))
    assert recorded == {made[v] for v in F.values if v != F.NOT_STATED} | {open_use_error.pk, on_pump.pk}
    by_asset = dict(Asset.objects.annotate(n=Count("work_orders", filter=findings.failure_q("work_orders__"))).values_list("tag", "n"))
    assert by_asset == {"CE-10001": len(counted), "CE-10002": 0}
    by_asset = dict(Asset.objects.annotate(n=Count("work_orders", filter=findings.recorded_q("work_orders__"))).values_list("tag", "n"))
    assert by_asset == {"CE-10001": len(F.values), "CE-10002": 1}


# --- the doors pass it through --------------------------------------------------------------------------------------------------


def test_the_api_and_the_modal_pass_the_finding_through(client, vent, techs, make_user):
    client.force_login(make_user("technician"))
    wo = repair(vent, tech=techs["dana"])
    url = f"/api/v1/work-orders/{wo.id}/transition/"
    body = {"status": "completed", "resolution": "Fixed", "repair_check": "passed", "results": POST_REPAIR_ALL_PASS}
    r = client.post(url, body, content_type="application/json")
    assert r.status_code == 400 and list(r.json()) == ["repair_finding"]  # the API names what to send (tests/test_repair_findings_api.py)
    assert r.json()["repair_finding"][0].startswith("Send repair_finding, what the repair found: one of")
    r = client.post(url, {**body, "repair_finding": ["wear"]}, content_type="application/json")
    assert r.status_code == 400 and r.json() == {"repair_finding": ["Send this as text."]}
    r = client.post(url, {**body, "repair_finding": "accessory"}, content_type="application/json")
    assert r.status_code == 200 and WorkOrder.objects.get(pk=wo.pk).repair_finding == F.ACCESSORY
    reopened(wo)
    r = client.post(url, body, content_type="application/json")  # left out: kept
    assert r.status_code == 200 and WorkOrder.objects.get(pk=wo.pk).repair_finding == F.ACCESSORY
    other = repair(vent, tech=techs["dana"])
    r = client.post(f"/work-orders/{other.number}/complete/", checked_post(resolution="Fixed", repair_finding=""), **HX)
    assert r.context["form"].errors["repair_finding"] == [findings.REQUIRED]
    client.post(f"/work-orders/{other.number}/complete/", checked_post(resolution="Fixed", repair_finding="battery"), **HX)
    assert WorkOrder.objects.get(pk=other.pk).repair_finding == F.BATTERY


# --- the demo ---------------------------------------------------------------------------------------------------------------------


def test_the_demo_records_what_each_repair_found(db):
    from io import StringIO

    from django.core.management import call_command

    from apps.demo.management.commands import seed_demo
    from apps.incidents.models import Incident
    from apps.tenants.models import Tenant

    assert len(seed_demo.REPAIR_RESOLUTIONS) == 6 and set(seed_demo.REPAIR_FINDINGS) == set(seed_demo.REPAIR_RESOLUTIONS)
    call_command("seed_demo", stdout=StringIO())
    for slug in ("riverside", "riverside-north"):
        with tenant_context(Tenant.objects.get(slug=slug)):
            repairs = WorkOrder.objects.filter(type=WoType.REPAIR, status__in=(WoStatus.COMPLETED, WoStatus.CLOSED))
            assert repairs.exists() and not repairs.filter(repair_finding="").exists(), slug
            drawn = repairs.filter(resolution__in=seed_demo.REPAIR_RESOLUTIONS)
            assert all(w.repair_finding == seed_demo.REPAIR_FINDINGS[w.resolution] for w in drawn), slug
    with tenant_context(Tenant.objects.get(slug="riverside")):
        repairs = WorkOrder.objects.filter(type=WoType.REPAIR, status__in=(WoStatus.COMPLETED, WoStatus.CLOSED))
        assert repairs.filter(repair_finding=F.USE_ERROR).exists()  # the figures by finding have one
        found_on_pm = repairs.get(follow_up_of__type=WoType.PM)
        assert (found_on_pm.resolution, found_on_pm.repair_finding) == (seed_demo.FAIL_REPAIR, F.WEAR)
        vendor = repairs.get(repair_check=RepairCheck.VENDOR_REPORT)
        assert vendor.repair_finding == seed_demo.VENDOR_FINDING != F.NOT_STATED
        assert repairs.get(repair_check=RepairCheck.NOT_RETURNED).repair_finding == seed_demo.NOT_RETURNED_FINDING
        closed = Incident.objects.get(status=Status.CLOSED).work_order
        assert closed.repair_finding == F.NO_PROBLEM_FOUND
        assert not WorkOrder.objects.filter(pk__in=Incident.objects.values("work_order"), repair_finding=F.USE_ERROR).exists()


# --- under the policies -----------------------------------------------------------------------------------------------------------


@needs_postgres
def test_findings_under_the_policies(ctx, vent, pump, techs, manager):
    wo = repair(vent, tech=techs["dana"])
    investigation = make_incident(pump).work_order
    assign(investigation, technician=techs["dana"])
    change_status(investigation, WoStatus.IN_PROGRESS)
    as_app_role()
    assert refused(wo, repair_finding=None) == {"repair_finding": findings.REQUIRED}
    assert refused(investigation, repair_finding=F.USE_ERROR) == {"repair_finding": findings.INVESTIGATION}  # the incident read under RLS
    done(wo, repair_finding=F.USE_ERROR)
    set_repair_finding(wo, F.DAMAGE, by=manager)
    done(investigation, repair_finding=F.NO_PROBLEM_FOUND)
    with tenant_context(ctx):
        assert WorkOrder.objects.get(pk=wo.pk).repair_finding == F.DAMAGE
        assert set(WorkOrder.objects.filter(findings.recorded_q()).values_list("pk", flat=True)) == {wo.pk, investigation.pk}
        assert not WorkOrder.objects.filter(findings.failure_q()).exists()
        assert reasons(wo)[-1] == "What was found: Damaged in handling"
