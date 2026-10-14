"""
Slice 30, wave 2B: the API of checks after a repair (apps/api/views.py's work order transition, serializers.WorkOrderSerializer).

A work order reads how a repair ended (repair_check, repair_check_label, checks_reference) and, while a repair is open, its
completion_checklist: the standard checks after a repair as {text, measure} steps, their signature, the steps never "na", and the
outcomes this user may send (vendor_report only on vendor service, for a user who sees the whole facility), with no query per row.
The transition to completed passes repair_check, checks_reference, results, and signature to complete_work_order; a list, number, or
object in a text field is a 400 keyed by it; a repair completed with no repair_check, or as passed with no results, answers one 400 naming
what to send; every other refusal keeps the service's words, and nothing is saved. An edit never writes the outcome. Under the policies
as the runtime role, by token.
"""
import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from pg_helpers import as_app_role, needs_postgres
from repair_fixtures import FINDING, POST_REPAIR_ALL_PASS, post_repair_results
from rest_framework.authtoken.models import Token

from apps.equipment.models import Asset, AssetStatus
from apps.tenants.context import tenant_context
from apps.workorders import findings, repair_checks
from apps.workorders.completion import FAILED_CHECK, REPAIR_CHECK_ONLY, REPAIR_REVISED, checklist_of, checklist_signature
from apps.workorders.models import Priority, RepairCheck, WorkOrder, WoStatus, WoType
from apps.workorders.services import assign, change_status, create_work_order

API = "/api/v1/work-orders/"
STEPS = checklist_of(repair_checks.POST_REPAIR)
SIGNATURE = checklist_signature(STEPS)
VENDOR = "Hamilton Medical field service"
PASSED_WORDS = ('"passed" (checked and ready for use) with results, one {"result", "reading"} per step of the work order\'s '
                "completion_checklist")
VENDOR_WORDS = ('"vendor_report" (checked by the vendor) with checks_reference, the vendor\'s service report or field service order '
                "number")
NOT_RETURNED_WORDS = '"not_returned" (not returned to use: the device stays out of service)'
SEND_RESULTS = ('Send results with "passed": one {"result": "pass", "fail", or "na", "reading": "..."} per step of the work order\'s '
                'completion_checklist, in order (7 steps), with a reading on each measured step that is not "na"; steps 1 and 5 are never "na".')


@pytest.fixture
def today(ctx):
    return timezone.localdate()


@pytest.fixture
def vendor_user(make_user):
    user = make_user("vendor")
    user.company = "Hamilton Medical"
    user.save()
    return user


def repair(asset, *, tech=None, vendor="", tag_out=False, start=True):
    wo = create_work_order(asset=asset, type=WoType.REPAIR, priority=Priority.HIGH, problem="Low tidal volume alarm", tag_out=tag_out)
    if tech is not None or vendor:
        assign(wo, technician=tech, vendor_name=vendor)
    if start:
        change_status(wo, WoStatus.IN_PROGRESS)
    return wo


def get(client, wo, **headers):
    r = client.get(f"{API}{wo.id}/", **headers)
    assert r.status_code == 200, r.content
    return r.json()


def complete(client, wo, body, **headers):
    finding = {"repair_finding": FINDING} if wo.type == WoType.REPAIR else {}  # slice 33: what it found, so each answer here is the checks'
    return client.post(f"{API}{wo.id}/transition/", {"status": "completed", **finding, **body}, content_type="application/json", **headers)


def refused(client, wo, body, **headers) -> dict:
    r = complete(client, wo, body, **headers)
    assert r.status_code == 400, r.content
    wo.refresh_from_db()
    assert wo.status == WoStatus.IN_PROGRESS and wo.repair_check == "" and wo.checks_reference == "" and wo.checklist_results == []
    return r.json()


def results_from(checklist: dict) -> list[dict]:
    """What a client builds from completion_checklist: every step passed, a reading on each measured one."""
    return [{"result": "pass", "reading": "0.1" if step["measure"] else ""} for step in checklist["steps"]]


# --- reading ------------------------------------------------------------------------------------------------------------------------


def test_an_open_repair_reads_its_completion_checklist(client, vent, techs, make_user):
    client.force_login(make_user("technician"))
    wo = repair(vent, tech=techs["dana"], start=False)
    data = get(client, wo)
    assert (data["repair_check"], data["repair_check_label"], data["checks_reference"]) == ("", "", "")
    checklist = data["completion_checklist"]
    assert checklist == {
        "name": "Checks after repair",
        "steps": [{"text": text, "measure": measure} for text, measure in repair_checks.POST_REPAIR_CHECKLIST],
        "signature": SIGNATURE,
        "never_na": [1, 5],
        "outcomes": [{"value": "passed", "label": "Checked: ready for use"}, {"value": "not_returned", "label": "Not returned to use"}],
        # slice 33: what the repair found (tests/test_repair_findings_api.py)
        "findings": [{"value": v, "label": findings.label(v), "hint": findings.HINTS[v]} for v in findings.choices_for(wo)],
        "found_on_pm": False, "finding_required": True,
    }
    assert [s["measure"] for s in checklist["steps"]] == [None, "Record Ω", "Record leakage µA", "Record leakage µA", None, None, None]
    change_status(wo, WoStatus.IN_PROGRESS)  # open, started: the same
    assert get(client, wo)["completion_checklist"] == checklist


def test_the_vendors_report_is_offered_on_vendor_service_to_the_facility_only(client, vent, make_user, vendor_user):
    wo = repair(vent, vendor=VENDOR)
    client.force_login(make_user("manager"))
    assert [o["value"] for o in get(client, wo)["completion_checklist"]["outcomes"]] == ["passed", "vendor_report", "not_returned"]
    client.force_login(vendor_user)  # the vendor's own repair, in their share: they record the steps
    assert [o["value"] for o in get(client, wo)["completion_checklist"]["outcomes"]] == ["passed", "not_returned"]
    listed = client.get(API).json()
    rows = listed["results"] if isinstance(listed, dict) else listed
    assert [[o["value"] for o in r["completion_checklist"]["outcomes"]] for r in rows] == [["passed", "not_returned"]]


def test_no_completion_checklist_but_on_an_open_repair(client, vent, pump, techs, make_user):
    client.force_login(make_user("manager"))
    pm = create_work_order(asset=pump, type=WoType.PM, priority=Priority.NORMAL, problem="Annual PM")
    inspection = create_work_order(asset=pump, type=WoType.INSPECTION, priority=Priority.NORMAL, problem="Incoming inspection")
    assert get(client, pm)["completion_checklist"] is None and get(client, inspection)["completion_checklist"] is None
    wo = repair(vent, tech=techs["dana"], tag_out=True)
    r = complete(client, wo, {"resolution": "Replaced the flow sensor", "repair_check": "passed", "results": POST_REPAIR_ALL_PASS})
    assert r.status_code == 200
    data = r.json()
    assert data["completion_checklist"] is None and data["status"] == "completed"
    assert (data["repair_check"], data["repair_check_label"], data["checks_reference"]) == ("passed", "Checked: ready for use", "")
    assert [s["result"] for s in data["checklist_results"]] == ["pass"] * 7
    cancelled = repair(vent, tech=techs["dana"], start=False)
    change_status(cancelled, WoStatus.CANCELLED)
    assert get(client, cancelled)["completion_checklist"] is None


def test_the_list_reads_no_rows_for_the_checklist(client, vent, pump, techs, make_user):
    client.force_login(make_user("manager"))
    repair(vent, tech=techs["dana"])

    def queries() -> int:
        with CaptureQueriesContext(connection) as q:
            assert client.get(API).status_code == 200
        return len(q)

    one = queries()
    repair(pump, tech=techs["tom"])
    repair(vent, vendor=VENDOR)
    assert queries() == one


# --- completing -------------------------------------------------------------------------------------------------------------------


def test_a_client_completes_a_repair_from_its_completion_checklist(client, vent, techs, make_user):
    client.force_login(make_user("technician"))
    wo = repair(vent, tech=techs["dana"], tag_out=True)
    checklist = get(client, wo)["completion_checklist"]
    r = complete(client, wo, {"resolution": "Replaced the flow sensor", "repair_check": "passed", "results": results_from(checklist),
                              "signature": checklist["signature"]})
    assert r.status_code == 200, r.content
    wo.refresh_from_db()
    vent.refresh_from_db()
    assert wo.status == WoStatus.COMPLETED and wo.repair_check == RepairCheck.PASSED and vent.status == AssetStatus.IN_SERVICE
    assert [(s["result"], s["reading"]) for s in wo.checklist_results] == [("pass", "0.1" if m else "") for _t, m in STEPS]
    assert wo.status_history.filter(to_status=WoStatus.COMPLETED).get().note == "Checked after repair; CE-10001 back in service"


def test_a_repair_with_no_outcome_answers_naming_what_to_send(client, vent, techs, make_user):
    client.force_login(make_user("technician"))
    wo = repair(vent, tech=techs["dana"])
    sends = [f"Send repair_check, how the repair ended: {PASSED_WORDS}; or {NOT_RETURNED_WORDS}."]
    assert refused(client, wo, {"resolution": "Fixed"}) == {"repair_check": sends}
    assert refused(client, wo, {"resolution": "Fixed", "repair_check": "ok"}) == {"repair_check": sends}  # no outcome: the same
    assert refused(client, wo, {"resolution": "Fixed", "repair_check": None, "results": POST_REPAIR_ALL_PASS}) == {"repair_check": sends}
    assert refused(client, wo, {}) == {"repair_check": sends, "resolution": ["Say what was found and done."]}  # the rest too


def test_the_vendors_report_is_named_where_the_user_may_send_it(client, vent, make_user):
    client.force_login(make_user("manager"))
    wo = repair(vent, vendor=VENDOR)
    assert refused(client, wo, {"resolution": "Vendor fixed it"}) == {
        "repair_check": [f"Send repair_check, how the repair ended: {PASSED_WORDS}; {VENDOR_WORDS}; or {NOT_RETURNED_WORDS}."]}
    assert refused(client, wo, {"resolution": "Vendor fixed it", "repair_check": "vendor_report"}) == {
        "checks_reference": ["Enter the vendor's service report or field service order number."]}


def test_passed_with_no_results_answers_naming_the_results(client, vent, techs, make_user):
    client.force_login(make_user("technician"))
    wo = repair(vent, tech=techs["dana"])
    for body in ({}, {"results": []}, {"results": None}, {"results": [{}] * 7}, {"signature": SIGNATURE}):
        assert refused(client, wo, {"resolution": "Fixed", "repair_check": "passed", **body}) == {"checklist": [SEND_RESULTS]}, body


def test_every_other_refusal_keeps_the_services_words(client, vent, pump, techs, make_user, vendor_user):
    client.force_login(make_user("technician"))
    wo = repair(vent, tech=techs["dana"], tag_out=True)
    failed = post_repair_results("pass", "pass", "fail", "pass", "pass", "pass", "pass")
    assert refused(client, wo, {"resolution": "Fixed", "repair_check": "passed", "results": failed}) == {"step_3": [FAILED_CHECK]}
    never = post_repair_results("na", *["pass"] * 6)
    assert refused(client, wo, {"resolution": "Fixed", "repair_check": "passed", "results": never}) == {
        "step_1": ["Step 1 is never N/A after a repair: mark it pass or fail."]}
    assert refused(client, wo, {"resolution": "Fixed", "repair_check": "passed", "results": POST_REPAIR_ALL_PASS[:3]}) == {
        "checklist": ["Record a result for each of the 7 steps."]}
    assert refused(client, wo, {"resolution": "Fixed", "repair_check": "passed", "results": POST_REPAIR_ALL_PASS, "signature": "0" * 16}) == {
        "checklist": [REPAIR_REVISED]}
    assert refused(client, wo, {"resolution": "Beyond repair", "repair_check": "not_returned", "results": POST_REPAIR_ALL_PASS}) == {
        "checklist": ["A device not returned to use records no checks: send no checklist with not returned to use."]}
    assert "was not done by vendor service" in refused(client, wo, {"resolution": "Fixed", "repair_check": "vendor_report",
                                                                     "checks_reference": "FSO-1"})["repair_check"][0]
    # a scoped vendor completing its own repair records the steps
    theirs = repair(pump, vendor=VENDOR)
    client.force_login(vendor_user)
    assert refused(client, theirs, {"resolution": "Fixed", "repair_check": "vendor_report", "checks_reference": "FSO-1"}) == {
        "repair_check": ["Record the checks after the repair step by step. Checked by the vendor is for the facility's CE staff, recording "
                         "the vendor's service report."]}
    # another type: the outcome is a repair's only
    client.force_login(make_user("manager"))
    pm = create_work_order(asset=pump, type=WoType.PM, priority=Priority.NORMAL, problem="Annual PM")
    assign(pm, technician=techs["tom"])
    r = complete(client, pm, {"pm_result": "pass", "repair_check": "passed"})
    assert r.status_code == 400 and r.json()["repair_check"] == [REPAIR_CHECK_ONLY]


def test_a_text_field_sent_as_anything_else_is_refused_by_name(client, vent, techs, make_user):
    client.force_login(make_user("technician"))
    wo = repair(vent, tech=techs["dana"])
    body = {"resolution": {"text": "Fixed"}, "repair_check": ["passed"], "checks_reference": 12345, "signature": [SIGNATURE], "pm_result": 1,
            "inspection_result": True, "late_reason": ["parts"], "results": POST_REPAIR_ALL_PASS}
    assert refused(client, wo, body) == {f: ["Send this as text."] for f in ("resolution", "pm_result", "inspection_result", "repair_check",
                                                                              "checks_reference", "signature", "late_reason")}
    assert refused(client, wo, {"resolution": "Fixed", "repair_check": "passed", "results": POST_REPAIR_ALL_PASS, "signature": 7}) == {
        "signature": ["Send this as text."]}


def test_a_blank_signature_is_not_checked(client, vent, techs, make_user):
    client.force_login(make_user("technician"))
    wo = repair(vent, tech=techs["dana"])
    r = complete(client, wo, {"resolution": "Fixed", "repair_check": "passed", "results": POST_REPAIR_ALL_PASS, "signature": ""})
    assert r.status_code == 200 and r.json()["repair_check"] == "passed"


def test_the_other_outcomes_through_the_api(client, vent, pump, techs, make_user):
    client.force_login(make_user("manager"))
    vendor_wo = repair(vent, vendor=VENDOR, tag_out=True)
    r = complete(client, vendor_wo, {"resolution": "Vendor replaced the blower", "repair_check": "vendor_report", "checks_reference": "FSO-26-118"})
    assert r.status_code == 200
    data = r.json()
    assert (data["repair_check"], data["repair_check_label"], data["checks_reference"]) == ("vendor_report", "Checked by the vendor", "FSO-26-118")
    vent.refresh_from_db()
    assert vent.status == AssetStatus.IN_SERVICE and data["checklist_results"] == []
    gone = repair(pump, tech=techs["tom"])
    r = complete(client, gone, {"resolution": "Cracked housing, beyond repair", "repair_check": "not_returned"})
    assert r.status_code == 200 and r.json()["repair_check_label"] == "Not returned to use" and r.json()["tagged_out"] is True
    pump.refresh_from_db()
    assert pump.status == AssetStatus.OUT_OF_SERVICE


def test_an_edit_never_writes_how_a_repair_ended(client, vent, techs, make_user):
    client.force_login(make_user("manager"))
    wo = repair(vent, tech=techs["dana"])
    for field, value in (("repair_check", "passed"), ("checks_reference", "FSO-1")):
        r = client.patch(f"{API}{wo.id}/", {field: value}, content_type="application/json")
        assert r.status_code == 400 and "recorded when it is completed" in r.json()[field][0], field
    r = client.patch(f"{API}{wo.id}/", {"repair_check": "", "checks_reference": "", "priority": "critical"}, content_type="application/json")
    assert r.status_code == 200  # sending back what a GET returned is fine
    wo.refresh_from_db()
    assert (wo.repair_check, wo.checks_reference, wo.priority) == ("", "", Priority.CRITICAL)


# --- under the policies -----------------------------------------------------------------------------------------------------------


@needs_postgres
def test_the_repair_api_under_the_policies(client, ctx, vent, techs, make_user):
    """As the runtime role, by token: the checklist read, a 400 naming what to send, and the completion. A request leaves the
    facility's setting as it found none, so the test reads the rows back inside the facility."""
    token = Token.objects.create(user=make_user("technician"))
    auth = {"HTTP_AUTHORIZATION": f"Token {token.key}"}
    wo = repair(vent, tech=techs["dana"], tag_out=True)
    as_app_role()
    checklist = get(client, wo, **auth)["completion_checklist"]
    assert checklist["signature"] == SIGNATURE and [o["value"] for o in checklist["outcomes"]] == ["passed", "not_returned"]
    r = complete(client, wo, {"resolution": "Fixed"}, **auth)
    assert r.status_code == 400 and list(r.json()) == ["repair_check"] and r.json()["repair_check"][0].startswith("Send repair_check")
    r = complete(client, wo, {"resolution": "Fixed", "repair_check": "passed", "results": results_from(checklist), "signature": SIGNATURE}, **auth)
    assert r.status_code == 200 and r.json()["repair_check"] == "passed" and r.json()["completion_checklist"] is None
    with tenant_context(ctx):
        assert WorkOrder.objects.get(pk=wo.pk).repair_check == RepairCheck.PASSED and Asset.objects.get(pk=vent.pk).status == AssetStatus.IN_SERVICE
