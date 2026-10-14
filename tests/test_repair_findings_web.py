"""
Slice 33, what a repair found, the screens (wave 2A): the completion modal's "What was found" (one native select after the checks,
directly above the resolution, with findings.GROUPS as optgroups limited to findings.choices_for this user and repair; the chosen one's
hint; "What each means" with the order to decide by; a failed PM's repair's "Found on PM <number>: choose what failed."; the device's
earlier service for a user who sees the whole facility), required and refused with the values kept and the select focused first, a
reopened repair's finding prefilled, a scoped user's stored finding never shown (kept as the facility recorded it), Mark the rest pass,
My work's Complete; the drawer's "What was found" row (a user who sees the whole facility only, full page and partial) and its Change
(Edit, Approve once closed, never a scoped user) through services.set_repair_finding; the print; and the finding kept out of the work
order list's CSV and the done email. The last test runs the screens under the row-level security policies.
"""
import html
import json
import re
from datetime import timedelta
from pathlib import Path

import pytest
from django.utils import timezone
from incident_fixtures import make_incident
from pg_helpers import as_app_role, needs_postgres
from repair_fixtures import CHECKED, checked_post

from apps.facility import services as fs
from apps.tenants.context import tenant_context
from apps.workorders import findings
from apps.workorders.completion import complete_work_order
from apps.workorders.models import Priority, RepairFinding, Source, WorkOrder, WoStatus, WoType
from apps.workorders.services import assign, change_status, create_service_request, create_work_order, set_repair_finding

F = RepairFinding
HX = {"HTTP_HX_REQUEST": "true"}
VENDOR = "Hamilton Medical field service"
CSS = Path(__file__).resolve().parent.parent / "apps/web/static/web/cadence.css"
# Every value the facility may record on in-house work, in the select's order (findings.GROUPS)
IN_HOUSE = ["no_problem_found", "use_error", "environment", "damage", "battery", "accessory", "software", "wear", "pm_preventable",
            "service_induced"]
OBSERVED = ["no_problem_found", "environment", "damage", "battery", "accessory", "software", "wear"]


def repair(asset, *, tech=None, vendor="", start=True, **fields):
    wo = create_work_order(asset=asset, type=WoType.REPAIR, priority=Priority.HIGH, problem="Low tidal volume alarm", **fields)
    if tech is not None or vendor:
        assign(wo, technician=tech, vendor_name=vendor)
    if start:
        change_status(wo, WoStatus.IN_PROGRESS)
    return wo


def done(wo, **kwargs):
    complete_work_order(wo, **{"resolution": "Replaced the flow sensor", **CHECKED, **kwargs})
    wo.refresh_from_db()
    return wo


def reopened(wo):
    wo.refresh_from_db()
    change_status(wo, WoStatus.IN_PROGRESS)
    return wo


def finished(asset, wo_type, days_ago, **fields):
    """A work order on `asset` done `days_ago` days ago (written as an import would: only the evidence reads it)."""
    wo = create_work_order(asset=asset, type=wo_type, priority=Priority.NORMAL, problem="Earlier work", **fields)
    day = timezone.localdate() - timedelta(days=days_ago)
    WorkOrder.objects.filter(pk=wo.pk).update(status=WoStatus.CLOSED, opened_on=day, completed_on=day)
    wo.refresh_from_db()
    return wo


def url(wo, what="complete") -> str:
    return f"/work-orders/{wo.number}/{what}/"


def triggers(r, header="HX-Trigger") -> dict:
    return json.loads(r[header]) if header in r else {}


def day(d) -> str:
    return f"{d:%b} {d.day}, {d.year}"


def select(body: str) -> str:
    m = re.search(r'<select id="(wc|wf)-repair_finding" name="repair_finding".*?</select>', body, re.S)
    return m.group(0) if m else ""


def offered(body: str) -> list[str]:
    """The select's values, the blank apart, in order."""
    return [v for v in re.findall(r'<option value="([^"]*)"', select(body)) if v]


def chosen(body: str) -> str:
    m = re.search(r'<option value="([^"]*)" selected>', select(body))
    return m.group(1) if m else None


def groups(body: str) -> list[str]:
    return [html.unescape(g) for g in re.findall(r'<optgroup label="([^"]*)">', select(body))]


def blank(body: str) -> str:
    return re.search(r'<option value=""[^>]*>([^<]*)</option>', select(body)).group(1)


def hint_on(body: str) -> str:
    m = re.search(r'<span class="wf-hint on" data-for="([^"]*)">', body)
    return m.group(1) if m else ""


def row(body: str) -> str:
    """The drawer's "What was found" row ("" when there is none)."""
    if '<div id="wo-finding"' not in body:
        return ""
    part = body[body.index('<div id="wo-finding"'):]
    return part[:part.index("</dd></div>")]


@pytest.fixture
def signed_in(client, make_user):
    def _as(role_slug, **fields):
        user = make_user(role_slug)
        for k, v in fields.items():
            setattr(user, k, v)
        user.save()
        client.force_login(user)
        return user

    return _as


@pytest.fixture
def manager(make_user):
    return make_user("manager", username="ce-manager@riverside.example")


@pytest.fixture
def today(ctx):
    return timezone.localdate()


# --- the modal ------------------------------------------------------------------------------------------------------------------


def test_the_modal_asks_what_was_found_after_the_checks_directly_above_the_resolution(client, signed_in, vent, techs):
    wo = repair(vent, tech=techs["dana"])
    signed_in("technician")
    body = client.get(url(wo), **HX).content.decode()
    # after the outcome and the checks (whatever the outcome: outside the checks' section), directly above the resolution
    assert body.index("After the repair") < body.index('name="step_7"') < body.index('id="wc-repair_finding"') < body.index('name="resolution"')
    between = body[body.index('id="wc-repair_finding"'):body.index('name="resolution"')]
    assert between.count("<label") == 1 and "Resolution" in between  # nothing else is asked in between
    assert body.index('class="wc-sec wc-checks"') < body.index('<div class="full wc-find" id="wc-find">')
    assert '<label class="fld" for="wc-repair_finding">What was found' in body
    # nothing preselected: a blank "Choose what was found", then the groups limited to this user's choices
    assert blank(body) == "Choose what was found" and chosen(body) == ""
    assert offered(body) == IN_HOUSE == findings.choices_for(wo)
    assert groups(body) == ["No device fault", "Device fault"]  # the vendor's report only on vendor service
    assert all(len(label) <= 30 for label in re.findall(r'<option value="[a-z_]+">([^<]+)</option>', select(body)))
    # the chosen one's hint under it (none yet), every offered choice's hint, and "What each means" with the order to decide by
    assert hint_on(body) == "" and 'aria-describedby="wc-find-hint"' in body
    for value in IN_HOUSE:
        assert f'<span class="wf-hint" data-for="{value}">{findings.HINTS[value]}' in body.replace("&#x27;", "'"), value
    assert "<summary>What each means</summary>" in body and findings.ORDER_WORDS in body
    assert "Found on PM" not in body and "An incident&#x27;s investigation" not in body


def test_the_choices_follow_the_user_and_the_repair(client, signed_in, vent, pump, techs, make_user):
    vendor_job = repair(vent, vendor=VENDOR)
    signed_in("manager")
    body = client.get(url(vendor_job), **HX).content.decode()
    assert offered(body) == [*IN_HOUSE, "not_stated"] and groups(body) == ["No device fault", "Device fault", "The vendor's report"]
    # a vendor's account: only what it sees on the device, never the facility's judgments, and no earlier service (the facility's numbers)
    signed_in("vendor", company="Hamilton Medical")
    body = client.get(url(vendor_job), **HX).content.decode()
    assert offered(body) == OBSERVED and groups(body) == ["No device fault", "Device fault"]
    assert "Use error" not in body and "Caused by earlier service" not in body and "Not stated by the vendor" not in body
    assert "Earlier service" not in body and blank(body) == "Choose what was found"
    # an incident's investigation: no use error, and the modal says why without the incident's number
    incident = make_incident(pump)
    investigation = incident.work_order
    assign(investigation, technician=techs["dana"])
    change_status(investigation, WoStatus.IN_PROGRESS)
    signed_in("technician")
    body = client.get(url(investigation), **HX).content.decode()
    assert offered(body) == [v for v in IN_HOUSE if v != "use_error"]
    assert "An incident&#x27;s investigation: use error is not offered" in body and incident.number not in body.split('id="wc-find"')[1]
    # a failed PM's repair: never "PM could have caught it" (the PM caught it), and it says which PM
    pm = create_work_order(asset=vent, type=WoType.PM, priority=Priority.NORMAL, problem="Scheduled PM", assigned_to=techs["dana"])
    from_pm = create_work_order(asset=vent, type=WoType.REPAIR, priority=Priority.HIGH, problem=f"PM {pm.number} failed", assigned_to=techs["dana"],
                                follow_up_of=pm)
    change_status(from_pm, WoStatus.IN_PROGRESS)
    body = client.get(url(from_pm), **HX).content.decode()
    assert offered(body) == [v for v in IN_HOUSE if v != "pm_preventable"]
    assert f'<span class="hint wf-from-pm">Found on PM {pm.number}: choose what failed.</span>' in body


def test_a_vendor_is_told_a_pm_found_it_without_a_number_outside_its_share(client, signed_in, vent, techs):
    pm = create_work_order(asset=vent, type=WoType.PM, priority=Priority.NORMAL, problem="Scheduled PM", assigned_to=techs["dana"])
    wo = create_work_order(asset=vent, type=WoType.REPAIR, priority=Priority.HIGH, problem="PM failed", follow_up_of=pm)
    assign(wo, vendor_name=VENDOR)
    change_status(wo, WoStatus.IN_PROGRESS)
    signed_in("vendor", company="Hamilton Medical")
    body = client.get(url(wo), **HX).content.decode()
    assert "Found on a PM: choose what failed." in body and pm.number not in body
    assert offered(body) == OBSERVED


def test_the_modal_names_the_devices_earlier_service(client, signed_in, vent, pump, techs, today):
    last_pm = finished(vent, WoType.PM, 40)
    older_pm = finished(vent, WoType.PM, 200)
    recent = finished(vent, WoType.REPAIR, 70)
    long_ago = finished(vent, WoType.REPAIR, 120)
    inspection = finished(vent, WoType.INSPECTION, 10)
    wo = repair(vent, tech=techs["dana"])
    signed_in("technician")
    body = client.get(url(wo), **HX).content.decode()
    line = re.search(r'<span class="hint wf-evidence">(.*?)</span>', body).group(1)
    assert line == (f"Earlier service on CE-10001 before this repair was opened {day(today)}: last PM {day(last_pm.completed_on)}, "
                    f"{last_pm.number}; in the 90 days before, repair {recent.number} done {day(recent.completed_on)}.")
    assert older_pm.number not in line and long_ago.number not in line and inspection.number not in line
    # none on record; the device's own last PM date (an import's) when no PM work order says it
    other = repair(pump, tech=techs["dana"])
    line = re.search(r'<span class="hint wf-evidence">(.*?)</span>', client.get(url(other), **HX).content.decode()).group(1)
    assert line == f"Earlier service on CE-10002 before this repair was opened {day(today)}: no PM on record; no other repair or PM in the 90 days before."
    pump.last_pm_on = today - timedelta(days=300)
    pump.save()
    line = re.search(r'<span class="hint wf-evidence">(.*?)</span>', client.get(url(other), **HX).content.decode()).group(1)
    assert f"last PM {day(pump.last_pm_on)}, from the device's record;" in line
    # judged from the day the repair was opened: work done since is not earlier service
    backdated = repair(vent, tech=techs["dana"], opened_on=today - timedelta(days=50))
    line = re.search(r'<span class="hint wf-evidence">(.*?)</span>', client.get(url(backdated), **HX).content.decode()).group(1)
    assert last_pm.number not in line and f"last PM {day(older_pm.completed_on)}, {older_pm.number}" in line and recent.number in line


def test_a_missing_finding_is_refused_and_focused_first_with_the_values_kept(client, signed_in, vent, techs):
    wo = repair(vent, tech=techs["dana"])
    signed_in("technician")
    r = client.post(url(wo), checked_post(resolution="", repair_finding=""), **HX)
    body = r.content.decode()
    assert r.status_code == 200 and "HX-Retarget" not in r
    assert f'<span class="err" id="wc-repair_finding_error">{findings.REQUIRED}</span>' in body
    # gathered with the rest; the select (above the resolution) gets the focus, never the resolution
    assert r.context["form"].errors["resolution"] and r.context["form"].first_error == "repair_finding"
    assert re.search(r'<select id="wc-repair_finding"[^>]*aria-invalid="true" autofocus>', body)
    assert body.count("autofocus") == 1
    assert 'name="step_3" value="pass" aria-label="Step 3: Pass" checked' in body  # the checks as typed
    # a refusal elsewhere keeps the choice, its hint shown
    r = client.post(url(wo), checked_post(resolution="", repair_finding="battery"), **HX)
    body = r.content.decode()
    assert chosen(body) == "battery" and hint_on(body) == "battery" and "wc-repair_finding_error" not in body
    assert WorkOrder.objects.get(pk=wo.pk).status == WoStatus.IN_PROGRESS
    # one not offered is refused in findings' words and shown as the blank choice
    r = client.post(url(wo), checked_post(resolution="Replaced the flow sensor", repair_finding="user_error"), **HX)
    assert findings.NOT_OFFERED in r.content.decode() and chosen(r.content.decode()) == ""
    r = client.post(url(wo), checked_post(resolution="Replaced the flow sensor", repair_finding="battery"), **HX)
    assert r["HX-Retarget"] == "#drawer"
    assert WorkOrder.objects.get(pk=wo.pk).repair_finding == F.BATTERY


def test_a_failed_pms_repair_refuses_pm_could_have_caught_it_in_words(client, signed_in, vent, techs):
    pm = create_work_order(asset=vent, type=WoType.PM, priority=Priority.NORMAL, problem="Scheduled PM", assigned_to=techs["dana"])
    wo = create_work_order(asset=vent, type=WoType.REPAIR, priority=Priority.HIGH, problem="PM failed", assigned_to=techs["dana"], follow_up_of=pm)
    change_status(wo, WoStatus.IN_PROGRESS)
    signed_in("technician")
    r = client.post(url(wo), checked_post(resolution="Replaced the line cord", repair_finding="pm_preventable"), **HX)
    body = r.content.decode()
    assert findings.FOUND_ON_PM in body.replace("&#x27;", "'") and chosen(body) == "" and "Replaced the line cord" in body
    assert WorkOrder.objects.get(pk=wo.pk).status == WoStatus.IN_PROGRESS


def test_a_reopened_repair_prefills_its_finding_and_keeps_or_replaces_it(client, signed_in, vent, techs):
    wo = repair(vent, tech=techs["dana"])
    signed_in("technician")
    client.post(url(wo), checked_post(resolution="Replaced the battery", repair_finding="battery"), **HX)
    reopened(wo)
    body = client.get(url(wo), **HX).content.decode()
    assert chosen(body) == "battery" and hint_on(body) == "battery" and blank(body) == "Choose what was found"
    drawer = client.get(f"/work-orders/{wo.number}/", **HX).content.decode()
    assert ("When last completed this repair was recorded as checked and ready for use; completing it again records its checks after repair "
            "anew. What was found: Battery failed, kept unless another is chosen.") in drawer
    assert row(drawer) == ""  # the row is a completed repair's
    client.post(url(wo), checked_post(resolution="Replaced the battery again", repair_finding=""), **HX)  # blank keeps it
    assert WorkOrder.objects.get(pk=wo.pk).repair_finding == F.BATTERY
    reopened(wo)
    client.post(url(wo), checked_post(resolution="Updated the firmware", repair_finding="software"), **HX)
    assert WorkOrder.objects.get(pk=wo.pk).repair_finding == F.SOFTWARE


def test_a_vendors_account_never_sees_the_facilitys_finding(client, signed_in, vent, manager):
    judged = done(repair(vent, vendor=VENDOR), repair_finding=F.USE_ERROR, by=manager)
    reopened(judged)
    vendor = signed_in("vendor", company="Hamilton Medical")
    body = client.get(url(judged), **HX).content.decode()
    # one they cannot choose: no select, kept as the facility recorded it, never named
    assert 'name="repair_finding"' not in body and '<div class="hint wf-kept">Recorded by the facility: it stays as recorded.</div>' in body
    assert "Use error" not in body and "What each means" not in body
    drawer = client.get(f"/work-orders/{judged.number}/", **HX).content.decode()
    assert "what was found" not in drawer.lower()
    r = client.post(url(judged), checked_post(resolution="Replaced the flow sensor", repair_finding=""), **HX)
    assert r["HX-Retarget"] == "#drawer" and WorkOrder.objects.get(pk=judged.pk).repair_finding == F.USE_ERROR
    assert "what was found" not in r.content.decode().lower()
    # posted anyway: refused in findings' words, nothing saved
    reopened(judged)
    r = client.post(url(judged), checked_post(resolution="Replaced the flow sensor", repair_finding="wear"), **HX)
    assert findings.FACILITY_RECORDED in r.content.decode().replace("&#x27;", "'")
    assert WorkOrder.objects.get(pk=judged.pk).status == WoStatus.IN_PROGRESS
    # one they may choose: kept by the blank, which never names it; they may replace it with another of their own
    observed = done(repair(vent, vendor=VENDOR), repair_finding=F.BATTERY, by=manager)
    reopened(observed)
    body = client.get(url(observed), **HX).content.decode()
    assert blank(body) == "Keep what was recorded" and chosen(body) == "" and offered(body) == OBSERVED and hint_on(body) == ""
    client.post(url(observed), checked_post(resolution="Replaced the battery", repair_finding=""), **HX)
    assert WorkOrder.objects.get(pk=observed.pk).repair_finding == F.BATTERY
    reopened(observed)
    client.post(url(observed), checked_post(resolution="Replaced the bezel", repair_finding="damage"), **HX)
    assert WorkOrder.objects.get(pk=observed.pk).repair_finding == F.DAMAGE
    assert vendor.pk  # the vendor's own account did all of it


def test_a_stored_finding_the_repairs_limits_refuse_now_is_chosen_again_by_anyone(client, signed_in, vent):
    manager = signed_in("manager")
    wo = done(repair(vent, vendor=VENDOR), repair_finding=F.PM_PREVENTABLE, by=manager)
    reopened(wo)
    pm = create_work_order(asset=vent, type=WoType.PM, priority=Priority.NORMAL, problem="Scheduled PM")
    WorkOrder.objects.filter(pk=wo.pk).update(follow_up_of=pm)  # a later PM's failure recorded on it (completion's open_repair=False)
    body = client.get(url(wo), **HX).content.decode()
    assert chosen(body) == "" and "pm_preventable" not in offered(body) and f"Found on PM {pm.number}: choose what failed." in body
    # a vendor's account: never "Recorded by the facility" here (completion would not keep it), the select blank with its observations
    signed_in("vendor", company="Hamilton Medical")
    body = client.get(url(wo), **HX).content.decode()
    assert "wf-kept" not in body and blank(body) == "Choose what was found" and chosen(body) == "" and offered(body) == OBSERVED
    assert "Found on a PM: choose what failed." in body and pm.number not in body
    r = client.post(url(wo), checked_post(resolution="Replaced the flow sensor", repair_finding=""), **HX)
    assert findings.NOT_OFFERED in r.content.decode() and WorkOrder.objects.get(pk=wo.pk).status == WoStatus.IN_PROGRESS


def test_a_vendor_chooses_again_when_the_repairs_limits_refuse_the_facilitys_finding(vent, make_user):
    manager = make_user("manager")
    wo = done(repair(vent, vendor=VENDOR), repair_finding=F.PM_PREVENTABLE, by=manager)
    reopened(wo)
    pm = create_work_order(asset=vent, type=WoType.PM, priority=Priority.NORMAL, problem="Scheduled PM")
    WorkOrder.objects.filter(pk=wo.pk).update(follow_up_of=pm)
    vendor = make_user("vendor")
    vendor.company = "Hamilton Medical"
    vendor.save()
    assert done(wo, repair_finding=F.WEAR, by=vendor).repair_finding == F.WEAR  # refused today: findings.FACILITY_RECORDED


def test_mark_the_rest_pass_keeps_the_choice(client, signed_in, vent, techs):
    wo = repair(vent, tech=techs["dana"])
    signed_in("technician")
    r = client.get(url(wo), {"fill": "pass", "repair_check": "passed", "repair_finding": "accessory", "resolution": "Replaced the SpO2 cable"},
                   **HX)
    body = r.content.decode()
    assert chosen(body) == "accessory" and hint_on(body) == "accessory" and "Replaced the SpO2 cable" in body
    assert 'name="step_1" value="pass" aria-label="Step 1: Pass" checked' in body


def test_my_works_complete_is_the_same_modal(client, signed_in, vent, techs):
    wo = repair(vent, tech=techs["dana"])
    user = signed_in("technician")
    techs["dana"].user = user
    techs["dana"].save()
    page = client.get("/my-work/").content.decode()
    assert f'hx-get="/work-orders/{wo.number}/complete/" hx-vals=\'{{"from": "my_work"}}\'' in page
    body = client.get(url(wo), {"from": "my_work"}, **HX).content.decode()
    assert offered(body) == IN_HOUSE and '<input type="hidden" name="from" value="my_work">' in body
    r = client.post(url(wo), checked_post(resolution="Replaced the flow sensor", repair_finding="", **{"from": "my_work"}), **HX)
    assert findings.REQUIRED in r.content.decode() and '<input type="hidden" name="from" value="my_work">' in r.content.decode()
    r = client.post(url(wo), checked_post(resolution="Replaced the flow sensor", repair_finding="wear", **{"from": "my_work"}), **HX)
    assert r["HX-Reswap"] == "none" and WorkOrder.objects.get(pk=wo.pk).repair_finding == F.WEAR


def test_only_a_repair_asks(client, signed_in, vent, techs):
    pm = create_work_order(asset=vent, type=WoType.PM, priority=Priority.NORMAL, problem="Scheduled PM", assigned_to=techs["dana"])
    recall = create_work_order(asset=vent, type=WoType.RECALL, priority=Priority.NORMAL, problem="Recall", assigned_to=techs["dana"])
    change_status(recall, WoStatus.IN_PROGRESS)
    signed_in("technician")
    for wo in (pm, recall):
        body = client.get(url(wo), **HX).content.decode()
        assert "repair_finding" not in body and "wc-find" not in body and "What each means" not in body, wo.type


# --- the drawer's row --------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("value, words", [
    (F.WEAR, "Counted as a device failure."),
    (F.USE_ERROR, "Not a device failure: never counted as one."),
    (F.NOT_STATED, "Counted as a device failure: no cause stated."),
])
def test_the_row_names_the_finding_and_whether_it_counts(client, signed_in, vent, techs, manager, value, words):
    wo = done(repair(vent, vendor=VENDOR) if value == F.NOT_STATED else repair(vent, tech=techs["dana"]), repair_finding=value, by=manager)
    signed_in("technician")
    for body in (client.get(f"/work-orders/{wo.number}/", **HX).content.decode(),  # the drawer
                 client.get(f"/work-orders/{wo.number}/").content.decode()):  # the full page
        r = row(body)
        assert "<dt>What was found</dt>" in r and F(value).label in r and words in r
        assert f'hx-get="/work-orders/{wo.number}/finding/" hx-target="#modal-card">Change</button>' in r


def test_a_repair_completed_before_findings_says_not_recorded_with_record(client, signed_in, vent, techs):
    wo = done(repair(vent, tech=techs["dana"]))
    WorkOrder.objects.filter(pk=wo.pk).update(repair_finding="")
    imported = done(repair(vent, tech=techs["dana"]))
    WorkOrder.objects.filter(pk=imported.pk).update(repair_finding="", source=Source.IMPORTED)
    signed_in("technician")
    r = row(client.get(f"/work-orders/{wo.number}/", **HX).content.decode())
    assert '<span class="muted">Not recorded</span>' in r and ">Record</button>" in r
    assert "Completed before Cadence recorded what repairs found. Counted as a device failure until what was found is recorded." in r
    r = row(client.get(f"/work-orders/{imported.number}/", **HX).content.decode())
    assert "Imported from the previous system. Counted as a device failure" in r


def test_the_row_is_for_completed_repairs_and_the_whole_facility_only(client, signed_in, vent, dept, techs, manager):
    open_ = repair(vent, vendor=VENDOR)
    finished_ = done(repair(vent, vendor=VENDOR), repair_finding=F.USE_ERROR, by=manager)
    cancelled = repair(vent, tech=techs["dana"], start=False)
    change_status(cancelled, WoStatus.CANCELLED)
    pm = finished(vent, WoType.PM, 3)
    signed_in("technician")
    assert row(client.get(f"/work-orders/{finished_.number}/", **HX).content.decode())
    for wo in (open_, cancelled, pm):
        assert row(client.get(f"/work-orders/{wo.number}/", **HX).content.decode()) == "", wo.number
    # a vendor's account and a requester see their work orders, never the facility's finding
    signed_in("vendor", company="Hamilton Medical")
    body = client.get(f"/work-orders/{finished_.number}/", **HX).content.decode()
    assert row(body) == "" and "Use error" not in body
    signed_in("requester", department=dept.name)
    r = client.get(f"/work-orders/{finished_.number}/", **HX)
    assert r.status_code == 200 and row(r.content.decode()) == "" and "Use error" not in r.content.decode()


# --- Change ---------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("role, status, offered_", [
    ("technician", WoStatus.COMPLETED, True), ("technician", WoStatus.CLOSED, False), ("manager", WoStatus.CLOSED, True),
    ("analyst", WoStatus.COMPLETED, False), ("vendor", WoStatus.COMPLETED, False),
])
def test_change_is_offered_at_edit_and_approve_once_closed_never_to_a_vendor(client, signed_in, vent, role, status, offered_):
    wo = done(repair(vent, vendor=VENDOR))
    if status == WoStatus.CLOSED:
        change_status(wo, WoStatus.CLOSED)
    signed_in(role, **({"company": "Hamilton Medical"} if role == "vendor" else {}))
    body = client.get(f"/work-orders/{wo.number}/", **HX).content.decode()
    assert (f'hx-get="/work-orders/{wo.number}/finding/"' in body) == offered_
    r = client.get(url(wo, "finding"), **HX)
    if role in ("analyst", "vendor"):
        assert r.status_code == 403
        assert client.post(url(wo, "finding"), {"repair_finding": "battery"}, **HX).status_code == 403
    elif not offered_:
        body = r.content.decode()
        assert f"{wo.number} is closed: recording what was found now needs Work orders Approve." in body and "wf-form" not in body
        client.post(url(wo, "finding"), {"repair_finding": "battery"}, **HX)
    else:
        assert 'id="wf-form"' in r.content.decode() and chosen(r.content.decode()) == "wear"
    assert WorkOrder.objects.get(pk=wo.pk).repair_finding == F.WEAR  # nothing posted above saved


def test_change_corrects_or_records_a_finding_audited(client, signed_in, vent, techs):
    wo = done(repair(vent, tech=techs["dana"]))
    user = signed_in("technician")
    body = client.get(url(wo, "finding"), **HX).content.decode()
    assert "What was found on " + wo.number in body and offered(body) == IN_HOUSE and chosen(body) == "wear"
    assert "The change is kept in the work order's history." in body and "Earlier service on CE-10001" in body
    r = client.post(url(wo, "finding"), {"repair_finding": "battery"}, **HX)
    assert r["HX-Retarget"] == "#drawer" and triggers(r)["toast"]["value"] == f"What was found on {wo.number}: Battery failed"
    assert "wo-changed" in triggers(r) and "modal-close" in triggers(r, "HX-Trigger-After-Settle")
    assert "Battery failed" in row(r.content.decode())
    latest = WorkOrder.history.filter(id=wo.pk).latest("history_date")
    assert (latest.repair_finding, latest.history_change_reason, latest.history_user) == (F.BATTERY, "What was found: Battery failed", user)
    # one completed before findings: recorded later, from the record of the repair
    old = done(repair(vent, tech=techs["dana"]))
    WorkOrder.objects.filter(pk=old.pk).update(repair_finding="")
    body = client.get(url(old, "finding"), **HX).content.decode()
    assert chosen(body) == "" and "Recorded later, from the record of the repair" in body
    client.post(url(old, "finding"), {"repair_finding": "no_problem_found"}, **HX)
    assert WorkOrder.history.filter(id=old.pk).latest("history_date").history_change_reason == "Recorded later: No problem found"


def test_changes_refusals_say_why_with_the_choice_kept(client, signed_in, vent, pump, techs):
    wo = done(repair(vent, tech=techs["dana"]))
    signed_in("technician")
    r = client.post(url(wo, "finding"), {"repair_finding": ""}, **HX)
    assert findings.REQUIRED in r.content.decode() and "HX-Retarget" not in r and 'id="wf-form"' in r.content.decode()
    assert re.search(r'<select id="wf-repair_finding"[^>]*aria-invalid="true" autofocus>', r.content.decode())
    r = client.post(url(wo, "finding"), {"repair_finding": "not_stated"}, **HX)  # in-house work: never the vendor's report
    assert findings.NOT_OFFERED in r.content.decode() and chosen(r.content.decode()) == ""
    investigation = make_incident(pump, hold=False).work_order
    assign(investigation, technician=techs["dana"])
    change_status(investigation, WoStatus.IN_PROGRESS)
    done(investigation)
    body = client.get(url(investigation, "finding"), **HX).content.decode()
    assert "use_error" not in offered(body) and "An incident&#x27;s investigation: use error is not offered" in body
    r = client.post(url(investigation, "finding"), {"repair_finding": "use_error"}, **HX)
    assert findings.INVESTIGATION in r.content.decode().replace("&#x27;", "'")
    assert WorkOrder.objects.get(pk=wo.pk).repair_finding == F.WEAR == WorkOrder.objects.get(pk=investigation.pk).repair_finding


def test_change_says_why_not_and_a_visit_opens_the_work_order(client, signed_in, vent, techs):
    open_ = repair(vent, tech=techs["dana"])
    cancelled = repair(vent, tech=techs["dana"], start=False)
    change_status(cancelled, WoStatus.CANCELLED)
    pm = finished(vent, WoType.PM, 2)
    signed_in("manager")
    for wo, words in ((open_, f"{open_.number} is not completed yet: record what was found when completing it."),
                      (cancelled, f"{cancelled.number} was cancelled: it records no finding."), (pm, findings.NOT_A_REPAIR)):
        body = client.get(url(wo, "finding"), **HX).content.decode()
        assert words in body and "wf-form" not in body, wo.number
        assert words in client.post(url(wo, "finding"), {"repair_finding": "battery"}, **HX).content.decode()
        assert WorkOrder.objects.get(pk=wo.pk).repair_finding == ""
    r = client.get(url(open_, "finding"))
    assert r.status_code == 302 and r["Location"] == f"/work-orders/{open_.number}/"


# --- the print ------------------------------------------------------------------------------------------------------------------


def test_the_print_names_the_finding_for_the_whole_facility_only(client, signed_in, vent, techs, manager):
    wo = done(repair(vent, vendor=VENDOR), repair_finding=F.USE_ERROR, by=manager)
    signed_in("technician")
    assert "<b>What was found:</b> Use error</p>" in client.get(f"/print/work-orders/{wo.number}/").content.decode()
    old = done(repair(vent, tech=techs["dana"]))
    WorkOrder.objects.filter(pk=old.pk).update(repair_finding="", source=Source.IMPORTED)
    body = client.get(f"/print/work-orders/{old.number}/").content.decode()
    assert "<b>What was found:</b> Not recorded (imported from the previous system)</p>" in body
    signed_in("vendor", company="Hamilton Medical")
    body = client.get(f"/print/work-orders/{wo.number}/").content.decode()
    assert "What was found" not in body and "Use error" not in body


def test_an_open_repair_prints_a_box_per_finding_the_reader_may_record(client, signed_in, vent, techs, manager):
    wo = repair(vent, vendor=VENDOR)
    signed_in("technician")
    body = client.get(f"/print/work-orders/{wo.number}/").content.decode()
    pick = body[body.index('<div class="rf-pick keep">'):body.index("Checks after repair</h2>")]
    assert "<i>No device fault</i>" in pick and "<i>The vendor&#x27;s report</i>" in pick
    assert all(f'<span class="box"></span> {F(v).label}</span>' in pick for v in [*IN_HOUSE, "not_stated"])
    signed_in("vendor", company="Hamilton Medical")
    body = client.get(f"/print/work-orders/{wo.number}/").content.decode()
    pick = body[body.index('<div class="rf-pick keep">'):body.index("Checks after repair</h2>")]
    assert [label for label in re.findall(r'<span class="box"></span> ([^<]+)</span>', pick)] == [F(v).label for v in OBSERVED]
    judged = done(repair(vent, vendor=VENDOR), repair_finding=F.PM_PREVENTABLE, by=manager)
    reopened(judged)
    body = client.get(f"/print/work-orders/{judged.number}/").content.decode()
    assert "Recorded by the facility: it stays as recorded." in body and "PM could have caught it" not in body


# --- kept out of the list's CSV and the done email ----------------------------------------------------------------------------


def test_the_finding_stays_out_of_the_list_csv_and_the_done_email(client, signed_in, vent, techs, mailoutbox, django_capture_on_commit_callbacks):
    fs.update_settings(portal_confirmation="email", portal_email_domains="rrmc.org")
    sr = create_service_request(asset=vent, department=vent.department, problem="Alarm", urgency="high", requester_email="kim.lee@rrmc.org")
    mailoutbox.clear()
    wo = sr.work_order
    assign(wo, technician=techs["dana"])
    change_status(wo, WoStatus.IN_PROGRESS)
    signed_in("technician")
    with django_capture_on_commit_callbacks(execute=True):
        r = client.post(url(wo), checked_post(resolution="Showed the unit the alarm settings", repair_finding="use_error"), **HX)
    assert r["HX-Retarget"] == "#drawer" and WorkOrder.objects.get(pk=wo.pk).repair_finding == F.USE_ERROR
    assert len(mailoutbox) == 1 and "Use error" not in mailoutbox[0].body and "use_error" not in mailoutbox[0].body
    csv = b"".join(client.get("/export/work-orders.csv", {"status": "completed", "open": "0"}).streaming_content).decode("utf-8-sig")
    assert wo.number in csv and "Use error" not in csv and "use_error" not in csv and "found" not in csv.splitlines()[0].lower()


# --- the CSS ------------------------------------------------------------------------------------------------------------------


def test_the_css_shows_the_chosen_findings_hint_for_every_value():
    css = CSS.read_text()
    block = css[css.index("/* slice 33: repair findings"):css.index("/* Slice 11: printing app pages */")]
    for value in F.values:
        assert f'.wc-find:has(option[value="{value}"]:checked) .wf-hint[data-for="{value}"]' in block, value
    assert ".wf-hint{display:none}" in block and ".wf-hint.on{display:inline}" in block and "@media (max-width:760px)" in block


# --- under the policies -----------------------------------------------------------------------------------------------------------


@needs_postgres
def test_the_screens_under_the_policies(client, signed_in, tenant, vent, pump, techs):
    """As the runtime role: the modal (with the investigation and earlier-service reads), the completion, the row, Change, the print."""
    finished(vent, WoType.PM, 30)
    wo = repair(vent, tech=techs["dana"])
    investigation = make_incident(pump).work_order
    assign(investigation, technician=techs["dana"])
    change_status(investigation, WoStatus.IN_PROGRESS)
    signed_in("technician")
    as_app_role()
    body = client.get(url(wo), **HX).content.decode()
    assert offered(body) == IN_HOUSE and "Earlier service on CE-10001" in body
    assert "use_error" not in offered(client.get(url(investigation), **HX).content.decode())
    r = client.post(url(wo), checked_post(resolution="Replaced the flow sensor", repair_finding="battery"), **HX)
    assert r["HX-Retarget"] == "#drawer" and "Battery failed" in row(r.content.decode())
    r = client.post(url(wo, "finding"), {"repair_finding": "accessory"}, **HX)
    assert triggers(r)["toast"]["value"] == f"What was found on {wo.number}: Accessory or cable"
    assert "<b>What was found:</b> Accessory or cable</p>" in client.get(f"/print/work-orders/{wo.number}/").content.decode()
    with tenant_context(tenant):
        assert WorkOrder.objects.get(pk=wo.pk).repair_finding == F.ACCESSORY
    assert set_repair_finding  # the service the Change posts to
