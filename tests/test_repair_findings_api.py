"""
Slice 33, wave 2B: what a repair found over the API (apps/api/views.py's work order viewset, serializers.WorkOrderSerializer).

A work order reads repair_finding and repair_finding_label: the finding recorded on a repair ("Not recorded" on a completed or closed
one with none), null on another type and for a scoped user (the facility's record). An open repair's completion_checklist lists the
findings this user may send (findings.choices_for, each with its label and hint), found_on_pm, and finding_required, read once for a
list's page. The transition to completed takes repair_finding: left out, null, or blank keeps the one recorded; required when none is;
a missing one or one this user may not send answers naming the values to send; a list, number, or object is refused by name; every
other refusal keeps the service's words, and nothing is saved. Another move, an edit, and a create never write it.
POST work-orders/<id>/repair-finding/ records or corrects one on a completed or closed repair (Edit; Approve once closed; never a
scoped user), audited. Under the policies as the runtime role, by token.
"""
import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from incident_fixtures import make_incident
from pg_helpers import as_app_role, needs_postgres
from repair_fixtures import POST_REPAIR_ALL_PASS
from rest_framework.authtoken.models import Token

from apps.api.permissions import ModulePermission
from apps.api.serializers import finding_limits
from apps.api.views import FINDING_RECORDED
from apps.equipment.models import Asset
from apps.incidents.models import Status
from apps.tenants.context import tenant_context
from apps.workorders import findings
from apps.workorders.models import OPEN_STATUSES, Priority, RepairFinding, WorkOrder, WoStatus, WoType
from apps.workorders.services import assign, change_status, create_work_order

F = RepairFinding
API = "/api/v1/work-orders/"
VENDOR = "Hamilton Medical field service"
# What the facility may record on in-house work, in the select's order (findings.GROUPS)
IN_HOUSE = ["no_problem_found", "use_error", "environment", "damage", "battery", "accessory", "software", "wear", "pm_preventable",
            "service_induced"]
OBSERVED = ["no_problem_found", "environment", "damage", "battery", "accessory", "software", "wear"]  # a vendor's account
CHECKLIST_HINT = "; the work order's completion_checklist gives each one's hint"
# The words a vendor's account reads, in full
VENDOR_SEND = ('Send repair_finding, what the repair found: one of "no_problem_found" (No problem found), "environment" (Power, gas, '
               'network, room), "damage" (Damaged in handling), "battery" (Battery failed), "accessory" (Accessory or cable), "software" '
               '(Software or firmware fault), "wear" (Wore out or failed in use); the work order\'s completion_checklist gives each one\'s hint.')


def send(values, where=CHECKLIST_HINT) -> list[str]:
    named = ", ".join(f'"{v}" ({F(v).label})' for v in values)
    return [f"Send repair_finding, what the repair found: one of {named}{where}."]


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
def device(ctx, dept, vent_model):
    """Another ventilator, by tag."""
    return lambda tag: Asset.objects.create(tag=tag, device_model=vent_model, department=dept)


def repair(asset, *, tech=None, vendor="", start=True):
    wo = create_work_order(asset=asset, type=WoType.REPAIR, priority=Priority.HIGH, problem="Low tidal volume alarm")
    if tech is not None or vendor:
        assign(wo, technician=tech, vendor_name=vendor)
    if start:
        change_status(wo, WoStatus.IN_PROGRESS)
    return wo


def failed_pms_repair(asset, tech):
    """A repair opened from a failed PM (completion's follow-up: follow_up_of the PM), started."""
    pm = create_work_order(asset=asset, type=WoType.PM, priority=Priority.NORMAL, problem="Scheduled preventive maintenance", assigned_to=tech)
    wo = create_work_order(asset=asset, type=WoType.REPAIR, priority=Priority.HIGH, problem=f"PM {pm.number} failed", assigned_to=tech,
                           follow_up_of=pm)
    change_status(wo, WoStatus.IN_PROGRESS)
    return wo


def investigation(asset, tech, **kwargs):
    """An incident's investigation (the tagged-out repair the incident opens), assigned and started."""
    wo = make_incident(asset, **kwargs).work_order
    assign(wo, technician=tech)
    change_status(wo, WoStatus.IN_PROGRESS)
    return wo


def reopened(wo):
    wo.refresh_from_db()
    change_status(wo, WoStatus.IN_PROGRESS)
    return wo


def done_before_findings(wo, status=WoStatus.COMPLETED):
    """A repair completed before slice 33 (or imported): no finding recorded."""
    WorkOrder.objects.filter(pk=wo.pk).update(status=status, completed_on=timezone.localdate())
    wo.refresh_from_db()
    return wo


def get(client, wo, **headers) -> dict:
    r = client.get(f"{API}{wo.id}/", **headers)
    assert r.status_code == 200, r.content
    return r.json()


def rows(client, **headers) -> list[dict]:
    r = client.get(API, **headers)
    assert r.status_code == 200, r.content
    body = r.json()
    return body["results"] if isinstance(body, dict) else body


def complete(client, wo, body=None, **headers):
    """The transition to completed, checked and ready for use; `body` adds or replaces fields (no finding unless it sends one)."""
    data = {"status": "completed", "resolution": "Replaced the flow sensor", "repair_check": "passed", "results": POST_REPAIR_ALL_PASS, **(body or {})}
    return client.post(f"{API}{wo.id}/transition/", data, content_type="application/json", **headers)


def completed(client, wo, finding, **headers) -> dict:
    r = complete(client, wo, {"repair_finding": finding}, **headers)
    assert r.status_code == 200, r.content
    wo.refresh_from_db()
    return r.json()


def refused(client, wo, body=None, **headers) -> dict:
    """The transition to completed refused, and nothing saved."""
    before = WorkOrder.objects.get(pk=wo.pk)
    r = complete(client, wo, body, **headers)
    assert r.status_code == 400, r.content
    wo.refresh_from_db()
    assert (wo.status, wo.repair_finding, wo.repair_check) == (before.status, before.repair_finding, before.repair_check)
    return r.json()


def record(client, wo, body, **headers):
    return client.post(f"{API}{wo.id}/repair-finding/", body, content_type="application/json", **headers)


def not_recorded(client, wo, body, status=400) -> dict:
    """POST repair-finding refused, and nothing saved."""
    before = WorkOrder.objects.get(pk=wo.pk).repair_finding
    r = record(client, wo, body)
    assert r.status_code == status, r.content
    assert WorkOrder.objects.get(pk=wo.pk).repair_finding == before
    return r.json()


def reasons(wo) -> list[str]:
    return [r.history_change_reason for r in WorkOrder.history.filter(id=wo.pk).order_by("history_date", "history_id")
            if r.history_change_reason]


def values(checklist) -> list[str]:
    return [f["value"] for f in checklist["findings"]]


def finding_of(data) -> tuple:
    return data["repair_finding"], data["repair_finding_label"]


# --- reading ------------------------------------------------------------------------------------------------------------------------


def test_a_repair_reads_what_it_found(client, vent, pump, techs, make_user):
    client.force_login(make_user("technician"))
    wo = repair(vent, tech=techs["dana"])
    assert finding_of(get(client, wo)) == ("", "")  # open, nothing recorded yet
    data = completed(client, wo, F.BATTERY)
    assert finding_of(data) == ("battery", "Battery failed") and data["completion_checklist"] is None
    assert finding_of(get(client, wo)) == ("battery", "Battery failed")
    change_status(wo, WoStatus.CLOSED)
    assert finding_of(get(client, wo)) == ("battery", "Battery failed")
    old = done_before_findings(repair(vent, tech=techs["dana"]))
    assert finding_of(get(client, old)) == ("", "Not recorded")
    closed_old = done_before_findings(repair(pump, tech=techs["tom"]), WoStatus.CLOSED)
    assert finding_of(get(client, closed_old)) == ("", "Not recorded")
    cancelled = repair(vent, tech=techs["dana"], start=False)
    change_status(cancelled, WoStatus.CANCELLED)
    assert finding_of(get(client, cancelled)) == ("", "")
    pm = create_work_order(asset=pump, type=WoType.PM, priority=Priority.NORMAL, problem="Annual PM")
    inspection = create_work_order(asset=pump, type=WoType.INSPECTION, priority=Priority.NORMAL, problem="Incoming inspection")
    assert finding_of(get(client, pm)) == finding_of(get(client, inspection)) == (None, None)
    listed = {r["id"]: finding_of(r) for r in rows(client)}
    assert listed == {str(wo.id): ("battery", "Battery failed"), str(old.id): ("", "Not recorded"), str(closed_old.id): ("", "Not recorded"),
                      str(cancelled.id): ("", ""), str(pm.id): (None, None), str(inspection.id): (None, None)}


def test_a_scoped_user_never_reads_the_finding(client, vent, pump, make_user, vendor_user):
    theirs, other = repair(vent, vendor=VENDOR), repair(pump, vendor=VENDOR)
    manager = make_user("manager")
    client.force_login(manager)
    completed(client, theirs, F.USE_ERROR)
    client.force_login(vendor_user)
    assert finding_of(get(client, theirs)) == (None, None)
    data = completed(client, other, F.WEAR)  # their own choice, sent; the answer shows none
    assert finding_of(data) == (None, None) and other.repair_finding == F.WEAR
    assert {r["id"]: finding_of(r) for r in rows(client)} == {str(theirs.id): (None, None), str(other.id): (None, None)}
    client.force_login(manager)
    assert finding_of(get(client, theirs)) == ("use_error", "Use error") and finding_of(get(client, other)) == ("wear", "Wore out or failed in use")


def test_the_completion_checklist_offers_what_this_user_may_record(client, vent, pump, techs, device, make_user, vendor_user):
    client.force_login(make_user("technician"))
    in_house = repair(vent, tech=techs["dana"], start=False)
    checklist = get(client, in_house)["completion_checklist"]
    assert checklist["findings"] == [{"value": v, "label": F(v).label, "hint": findings.HINTS[v]} for v in IN_HOUSE]
    assert checklist["findings"][1] == {"value": "use_error", "label": "Use error", "hint": findings.HINTS[F.USE_ERROR]}
    assert (checklist["found_on_pm"], checklist["finding_required"]) == (False, True)
    # an incident's investigation never records a use error; an incident recorded in error makes none
    held = investigation(pump, techs["tom"])
    assert values(get(client, held)["completion_checklist"]) == [v for v in IN_HOUSE if v != "use_error"]
    in_error = investigation(device("CE-10009"), techs["dana"], status=Status.IN_ERROR, hold=False)
    assert values(get(client, in_error)["completion_checklist"]) == IN_HOUSE
    # a failed PM's repair: the PM caught it
    from_pm = failed_pms_repair(device("CE-10010"), techs["dana"])
    checklist = get(client, from_pm)["completion_checklist"]
    assert values(checklist) == [v for v in IN_HOUSE if v != "pm_preventable"] and checklist["found_on_pm"] is True
    # vendor service: not stated by the vendor, for the facility; a vendor's account its observations only
    vendor_job = repair(device("CE-10011"), vendor=VENDOR)
    client.force_login(make_user("manager"))
    assert values(get(client, vendor_job)["completion_checklist"]) == [*IN_HOUSE, "not_stated"]
    client.force_login(vendor_user)
    checklist = get(client, vendor_job)["completion_checklist"]
    assert values(checklist) == OBSERVED and checklist["finding_required"] is True


def test_the_list_reads_what_limits_the_findings_once_a_page(client, vent, pump, techs, device, make_user):
    client.force_login(make_user("manager"))
    repair(vent, tech=techs["dana"])

    def queries() -> int:
        with CaptureQueriesContext(connection) as q:
            rows(client)
        return len(q)

    one = queries()
    investigation(pump, techs["tom"])
    investigation(device("CE-10009"), techs["dana"], status=Status.IN_ERROR, hold=False)
    failed_pms_repair(device("CE-10010"), techs["dana"])
    repair(device("CE-10011"), vendor=VENDOR)
    done_before_findings(repair(vent, tech=techs["dana"]))
    assert queries() == one
    listed = rows(client)
    assert sum(1 for r in listed if r["completion_checklist"]) == 5  # the open repairs
    for row in listed:  # the page's read says what each one's own does
        assert row["completion_checklist"] == get(client, WorkOrder.objects.get(pk=row["id"]))["completion_checklist"], row["number"]
    # and what the one rule's own reads say (findings.is_investigation, findings.found_on_pm)
    every = list(WorkOrder.objects.all())
    assert finding_limits(every) == {w.pk: (findings.is_investigation(w), findings.found_on_pm(w)) for w in every
                                     if w.type == WoType.REPAIR and w.status in OPEN_STATUSES}
    assert sorted(finding_limits(every).values()) == [(False, False)] * 3 + [(False, True), (True, False)]


# --- completing ---------------------------------------------------------------------------------------------------------------------


def test_a_missing_finding_names_what_to_send(client, vent, pump, techs, make_user, vendor_user):
    client.force_login(make_user("technician"))
    wo = repair(vent, tech=techs["dana"])
    assert refused(client, wo) == {"repair_finding": send(IN_HOUSE)}
    for blank in (None, ""):
        assert refused(client, wo, {"repair_finding": blank}) == {"repair_finding": send(IN_HOUSE)}
    assert refused(client, wo, {"resolution": ""}) == {"repair_finding": send(IN_HOUSE), "resolution": ["Say what was found and done."]}
    # one this user may not send: the same words
    for value in ("worn_out", "not_stated", "WEAR"):
        assert refused(client, wo, {"repair_finding": value}) == {"repair_finding": send(IN_HOUSE)}, value
    # vendor service: the facility may say the vendor's report states none; a vendor's account its observations
    theirs = repair(pump, vendor=VENDOR)
    client.force_login(make_user("manager"))
    assert refused(client, theirs) == {"repair_finding": send([*IN_HOUSE, "not_stated"])}
    client.force_login(vendor_user)
    assert refused(client, theirs) == {"repair_finding": [VENDOR_SEND]}
    for value in ("use_error", "pm_preventable", "service_induced", "not_stated"):
        assert refused(client, theirs, {"repair_finding": value}) == {"repair_finding": [VENDOR_SEND]}, value


def test_every_other_finding_refusal_keeps_the_services_words(client, vent, pump, techs, device, make_user):
    client.force_login(make_user("technician"))
    held = investigation(pump, techs["tom"])
    assert refused(client, held, {"repair_finding": "use_error"}) == {"repair_finding": [findings.INVESTIGATION]}
    from_pm = failed_pms_repair(device("CE-10010"), techs["dana"])
    assert refused(client, from_pm, {"repair_finding": "pm_preventable"}) == {"repair_finding": [findings.FOUND_ON_PM]}
    wo = repair(vent, tech=techs["dana"])
    for value in (["wear"], 5, {"value": "wear"}, True):
        assert refused(client, wo, {"repair_finding": value}) == {"repair_finding": ["Send this as text."]}, value
    pm = create_work_order(asset=device("CE-10011"), type=WoType.PM, priority=Priority.NORMAL, problem="Annual PM")
    assign(pm, technician=techs["dana"])
    r = client.post(f"{API}{pm.id}/transition/", {"status": "completed", "pm_result": "pass", "repair_finding": "wear"},
                    content_type="application/json")
    assert r.status_code == 400 and r.json()["repair_finding"] == [findings.NOT_A_REPAIR]
    pm.refresh_from_db()
    assert pm.status != WoStatus.COMPLETED


def test_a_reopened_repair_keeps_its_finding_unless_another_is_sent(client, vent, techs, make_user):
    client.force_login(make_user("technician"))
    wo = repair(vent, tech=techs["dana"])
    completed(client, wo, F.ACCESSORY)
    reopened(wo)
    checklist = get(client, wo)["completion_checklist"]
    assert values(checklist) == IN_HOUSE and checklist["finding_required"] is False
    for body in ({}, {"repair_finding": None}, {"repair_finding": ""}):  # left out, null, or blank: kept
        r = complete(client, wo, body)
        assert r.status_code == 200 and finding_of(r.json()) == ("accessory", "Accessory or cable"), body
        reopened(wo)
    assert finding_of(completed(client, wo, F.SOFTWARE)) == ("software", "Software or firmware fault")
    assert wo.repair_finding == F.SOFTWARE


def test_a_finding_the_repair_no_longer_allows_is_required_again(client, vent, techs, make_user):
    """Recorded as "PM could have caught it", then found to follow a failed PM: the checklist asks for one, and leaving it out is
    refused in the service's words."""
    client.force_login(make_user("technician"))
    wo = repair(vent, tech=techs["dana"])
    completed(client, wo, F.PM_PREVENTABLE)
    pm = create_work_order(asset=vent, type=WoType.PM, priority=Priority.NORMAL, problem="Scheduled preventive maintenance")
    WorkOrder.objects.filter(pk=wo.pk).update(follow_up_of=pm)
    reopened(wo)
    checklist = get(client, wo)["completion_checklist"]
    assert (checklist["found_on_pm"], checklist["finding_required"]) == (True, True) and "pm_preventable" not in values(checklist)
    assert refused(client, wo) == {"repair_finding": [findings.FOUND_ON_PM]}
    assert finding_of(completed(client, wo, F.WEAR)) == ("wear", "Wore out or failed in use")


def test_the_facilitys_finding_stays_as_recorded_for_a_vendor(client, vent, pump, make_user, vendor_user):
    judged, observed = repair(vent, vendor=VENDOR), repair(pump, vendor=VENDOR)
    client.force_login(make_user("manager"))
    completed(client, judged, F.USE_ERROR)
    completed(client, observed, F.BATTERY)
    reopened(judged), reopened(observed)
    client.force_login(vendor_user)
    checklist = get(client, judged)["completion_checklist"]
    assert (checklist["findings"], checklist["finding_required"]) == ([], False)  # none to send: it stays as the facility recorded it
    assert refused(client, judged, {"repair_finding": "wear"}) == {"repair_finding": [findings.FACILITY_RECORDED]}
    assert finding_of(completed(client, judged, None)) == (None, None) and judged.repair_finding == F.USE_ERROR
    # an observation the facility recorded is the vendor's to change
    checklist = get(client, observed)["completion_checklist"]
    assert (values(checklist), checklist["finding_required"]) == (OBSERVED, False)
    completed(client, observed, F.DAMAGE)
    assert observed.repair_finding == F.DAMAGE


# --- never written another way ----------------------------------------------------------------------------------------------------


def test_another_move_an_edit_or_a_create_never_writes_the_finding(client, vent, pump, techs, make_user):
    client.force_login(make_user("manager"))
    wo = repair(vent, tech=techs["dana"], start=False)
    r = client.post(f"{API}{wo.id}/transition/", {"status": "in_progress", "repair_finding": "wear"}, content_type="application/json")
    assert r.status_code == 400 and r.json() == {"repair_finding": [FINDING_RECORDED]}
    assert WorkOrder.objects.get(pk=wo.pk).status == WoStatus.OPEN
    r = client.post(f"{API}{wo.id}/transition/", {"status": "in_progress", "repair_finding": ""}, content_type="application/json")
    assert r.status_code == 200
    # an edit: a different finding refused by name; what a GET returned sends back fine
    r = client.patch(f"{API}{wo.id}/", {"repair_finding": "wear"}, content_type="application/json")
    assert r.status_code == 400 and r.json() == {"repair_finding": [FINDING_RECORDED]}
    for sent in ("", None):
        r = client.patch(f"{API}{wo.id}/", {"repair_finding": sent, "repair_finding_label": "Use error", "priority": "critical"},
                         content_type="application/json")
        assert r.status_code == 200, r.content
    wo.refresh_from_db()
    assert (wo.repair_finding, wo.priority) == ("", Priority.CRITICAL)
    body = {k: v for k, v in get(client, wo).items() if k != "resolution"}  # a resolution is the completion's
    r = client.put(f"{API}{wo.id}/", {**body, "repair_finding": "battery"}, content_type="application/json")
    assert r.status_code == 400 and r.json() == {"repair_finding": [FINDING_RECORDED]}
    completed(client, wo, F.BATTERY)
    r = client.patch(f"{API}{wo.id}/", {"repair_finding": "wear"}, content_type="application/json")
    assert r.status_code == 400 and r.json() == {"repair_finding": [FINDING_RECORDED]}
    assert client.patch(f"{API}{wo.id}/", {"repair_finding": "battery"}, content_type="application/json").status_code == 200
    pm = create_work_order(asset=pump, type=WoType.PM, priority=Priority.NORMAL, problem="Annual PM")
    assert client.patch(f"{API}{pm.id}/", {"repair_finding": None, "priority": "high"}, content_type="application/json").status_code == 200
    assert client.patch(f"{API}{pm.id}/", {"repair_finding": "wear"}, content_type="application/json").status_code == 400
    assert WorkOrder.objects.get(pk=wo.pk).repair_finding == F.BATTERY
    # a create: a new repair found nothing yet
    before = WorkOrder.objects.count()
    new = {"asset": str(pump.id), "type": "repair", "priority": "normal", "problem": "Alarm", "due_on": timezone.localdate().isoformat()}
    r = client.post(API, {**new, "repair_finding": "wear"}, content_type="application/json")
    assert r.status_code == 400 and r.json() == {"repair_finding": [FINDING_RECORDED]} and WorkOrder.objects.count() == before
    r = client.post(API, {**new, "repair_finding": ""}, content_type="application/json")
    assert r.status_code == 201 and finding_of(r.json()) == ("", "")


# --- POST repair-finding/ ---------------------------------------------------------------------------------------------------------


def test_the_repair_finding_action(client, vent, pump, techs, make_user):
    client.force_login(make_user("technician"))  # Edit
    wo = repair(vent, tech=techs["dana"])
    completed(client, wo, F.BATTERY)
    r = record(client, wo, {"repair_finding": "damage"})
    assert r.status_code == 200 and finding_of(r.json()) == ("damage", "Damaged in handling")
    assert reasons(wo)[-1] == "What was found: Damaged in handling"
    n = WorkOrder.history.filter(id=wo.pk).count()
    assert record(client, wo, {"repair_finding": "damage"}).status_code == 200  # the same again: nothing written
    assert WorkOrder.history.filter(id=wo.pk).count() == n
    # one completed before findings were recorded
    old = done_before_findings(repair(pump, tech=techs["tom"]))
    r = record(client, old, {"repair_finding": "wear"})
    assert r.status_code == 200 and finding_of(r.json()) == ("wear", "Wore out or failed in use")
    assert reasons(old)[-1] == "Recorded later: Wore out or failed in use"
    # closed: Approve
    change_status(wo, WoStatus.CLOSED)
    assert not_recorded(client, wo, {"repair_finding": "accessory"}, 403) == {
        "detail": f"{wo.number} is closed: recording what the repair found now needs Work orders Approve."}
    client.force_login(make_user("manager"))
    r = record(client, wo, {"repair_finding": "accessory"})
    assert r.status_code == 200 and WorkOrder.objects.get(pk=wo.pk).repair_finding == F.ACCESSORY


def test_the_repair_finding_actions_refusals(client, vent, pump, techs, device, make_user):
    client.force_login(make_user("technician"))
    wo = done_before_findings(repair(vent, tech=techs["dana"]))
    for body in ({}, {"repair_finding": None}, {"repair_finding": ""}, {"repair_finding": "not_stated"}, {"repair_finding": "worn_out"}):
        assert not_recorded(client, wo, body) == {"repair_finding": send(IN_HOUSE, where="")}, body
    assert not_recorded(client, wo, {"repair_finding": 5}) == {"repair_finding": ["Send this as text."]}
    assert not_recorded(client, wo, {"repair_finding": "wear", "note": "x"}) == {"note": ["Unknown field."]}
    r = client.post(f"{API}{wo.id}/repair-finding/", ["wear"], content_type="application/json")
    assert r.status_code == 400 and r.json() == {"detail": "Send the fields as a JSON object."}
    # not completed, cancelled, another type: the service's words
    open_wo = repair(pump, tech=techs["tom"])
    assert not_recorded(client, open_wo, {"repair_finding": "wear"}) == {
        "repair_finding": [f"{open_wo.number} is not completed yet: record what was found when completing it."]}
    cancelled = repair(pump, tech=techs["tom"], start=False)
    change_status(cancelled, WoStatus.CANCELLED)
    assert not_recorded(client, cancelled, {"repair_finding": "wear"}) == {
        "repair_finding": [f"{cancelled.number} was cancelled: it records no finding."]}
    pm = create_work_order(asset=pump, type=WoType.PM, priority=Priority.NORMAL, problem="Annual PM")
    WorkOrder.objects.filter(pk=pm.pk).update(status=WoStatus.COMPLETED)
    for body in ({}, {"repair_finding": "wear"}):
        assert not_recorded(client, pm, body) == {"repair_finding": [findings.NOT_A_REPAIR]}
    # an incident's investigation and a failed PM's repair, once completed
    held = investigation(device("CE-10009"), techs["dana"])
    completed(client, held, F.NO_PROBLEM_FOUND)
    assert not_recorded(client, held, {"repair_finding": "use_error"}) == {"repair_finding": [findings.INVESTIGATION]}
    from_pm = failed_pms_repair(device("CE-10010"), techs["dana"])
    completed(client, from_pm, F.WEAR)
    assert not_recorded(client, from_pm, {"repair_finding": "pm_preventable"}) == {"repair_finding": [findings.FOUND_ON_PM]}


def test_the_repair_finding_action_is_never_a_scoped_users(client, vent, techs, make_user, vendor_user):
    theirs = repair(vent, vendor=VENDOR)
    client.force_login(make_user("manager"))
    completed(client, theirs, F.BATTERY)
    client.force_login(vendor_user)  # Work orders Edit, their own repair: still the facility's record
    assert not_recorded(client, theirs, {"repair_finding": "wear"}, 403) == {"detail": ModulePermission.SCOPED_REFUSAL}
    requester = make_user("requester")
    requester.department = "ICU"
    requester.save()
    client.force_login(requester)
    assert not_recorded(client, theirs, {"repair_finding": "wear"}, 403)
    client.force_login(make_user("analyst"))  # View
    assert not_recorded(client, theirs, {"repair_finding": "wear"}, 403)


# --- under the policies -----------------------------------------------------------------------------------------------------------


@needs_postgres
def test_findings_over_the_api_under_the_policies(client, ctx, vent, pump, techs, make_user):
    """As the runtime role, by token: an investigation's choices (the incident read under the policies), on a list's page too, a 400
    naming what to send, the investigation's refusal, the completion, and a correction afterwards. A request leaves the facility's
    setting as it found none, so the test reads the rows back inside the facility."""
    token = Token.objects.create(user=make_user("technician"))
    auth = {"HTTP_AUTHORIZATION": f"Token {token.key}"}
    held = investigation(pump, techs["tom"])
    wo = repair(vent, tech=techs["dana"])
    as_app_role()
    no_use_error = [v for v in IN_HOUSE if v != "use_error"]
    assert values(get(client, held, **auth)["completion_checklist"]) == no_use_error
    listed = {r["id"]: values(r["completion_checklist"]) for r in rows(client, **auth)}
    assert listed == {str(held.id): no_use_error, str(wo.id): IN_HOUSE}
    r = complete(client, held, **auth)
    assert r.status_code == 400 and r.json() == {"repair_finding": send(no_use_error)}
    r = complete(client, held, {"repair_finding": "use_error"}, **auth)
    assert r.status_code == 400 and r.json() == {"repair_finding": [findings.INVESTIGATION]}
    r = complete(client, held, {"repair_finding": "no_problem_found"}, **auth)
    assert r.status_code == 200 and finding_of(r.json()) == ("no_problem_found", "No problem found")
    r = complete(client, wo, {"repair_finding": "battery"}, **auth)
    assert r.status_code == 200 and finding_of(r.json()) == ("battery", "Battery failed")
    r = record(client, wo, {"repair_finding": "damage"}, **auth)
    assert r.status_code == 200 and finding_of(r.json()) == ("damage", "Damaged in handling")
    with tenant_context(ctx):
        assert WorkOrder.objects.get(pk=held.pk).repair_finding == F.NO_PROBLEM_FOUND
        assert WorkOrder.objects.get(pk=wo.pk).repair_finding == F.DAMAGE
        assert reasons(wo)[-1] == "What was found: Damaged in handling"
