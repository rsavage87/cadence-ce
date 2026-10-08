"""
Slice 28, wave 2B: device incidents over the API (apps/api/views_incidents.py), and what the slice changes in the other endpoints:
a device's incident_hold (read-only), a work order that is an incident's investigation never deleted (a 400 in words), the work
order transition's refusal in words, and Auto-assign week's on_hold.

Each endpoint has the screen's door (apps.incidents.permissions: View reads, Edit records, Approve decides), then the services' own
checks, refused in their words: a 403 for a level, a 400 keyed by field for a rule, a 404 for another facility's incident or another
incident's hold; scoped users are refused everything. By session, by token, and under PostgreSQL's row-level security.
"""
from datetime import date, timedelta

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from pg_helpers import as_app_role, needs_postgres
from rest_framework.authtoken.models import Token

from apps.accounts.models import DataScope, Level, Module, Role, User, create_default_roles
from apps.api import views_incidents
from apps.core.workdays import add_work_days
from apps.equipment.models import Asset, AssetStatus, Department, DeviceModel, RiskClass
from apps.incidents import services as inc
from apps.incidents.models import Affected, Basis, DecidedBy, Finding, Incident, Outcome, Release, Status
from apps.tenants.context import tenant_context
from apps.workorders.models import Priority, Urgency, WorkOrder, WoStatus, WoType
from apps.workorders.services import assign, create_service_request, create_work_order, held_work_message

API = "/api/v1/"
INCIDENTS = f"{API}incidents/"
DAY = timedelta(days=1)
ALL_FULL = {m: Level.FULL for m in Module.values}


# --- helpers ---------------------------------------------------------------------------------------------------------------------

def send(client, method, url, body=None, token=None):
    extra = {"HTTP_AUTHORIZATION": f"Token {token.key}"} if token else {}
    if method == "get":
        return client.get(url, **extra)
    if method == "delete":
        return client.delete(url, **extra)
    return getattr(client, method)(url, body if body is not None else {}, content_type="application/json", **extra)


def post(client, url, body=None, token=None):
    return send(client, "post", url, body, token)


def patch(client, url, body=None, token=None):
    return send(client, "patch", url, body, token)


def ok(r, code=200):
    assert r.status_code == code, (r.status_code, r.content)
    return r.json()


def refused(r, words):
    assert r.status_code == 403, (r.status_code, r.content)
    assert r.json()["detail"] == words, r.json()
    return r


def bad(r, field, match=""):
    """A 400 keyed by `field`, with `match` in its words (never a list's brackets)."""
    assert r.status_code == 400, (r.status_code, r.content)
    data = r.json()
    assert field in data, data
    text = " ".join(data[field]) if isinstance(data[field], list) else data[field]
    assert match in text and "['" not in text, (match, text)
    return data


def url(incident, act=""):
    key = incident["id"] if isinstance(incident, dict) else incident.pk
    return f"{INCIDENTS}{key}/" + (f"{act}/" if act else "")


def hold_url(incident, hold, act):
    return url(incident, f"holds/{hold}/{act}")


def recorded(client, device, **body):
    """Record an incident on `device` over the API (by its tag; outcome not known yet, a patient, held, unless the body says
    otherwise): its JSON."""
    body = {"asset": device.tag, "outcome": Outcome.UNKNOWN, "affected": Affected.PATIENT} | body
    return ok(post(client, INCIDENTS, body), 201)


@pytest.fixture
def today(ctx):
    return timezone.localdate()


@pytest.fixture
def signed_in(client, make_user):
    n = iter(range(1000))

    def _as(role):
        if isinstance(role, Role):
            user = User.objects.create_user(username=f"{role.slug}-{next(n)}@riverside.example", password="Test-Pass-2026-x",
                                            tenant=role.tenant, role=role)
        else:
            user = make_user(role, username=f"{role}-{next(n)}@riverside.example")
        client.force_login(user)
        return user

    return _as


def custom_role(slug, levels, scope=""):
    role = Role.objects.create(name=slug.title(), slug=slug, scope=scope)
    role.set_levels(levels)
    return role


def finish(client, techs, incident):
    """The investigation done over the API, as a technician does it: assigned (a manager's), started, completed."""
    wo = WorkOrder.objects.get(pk=incident["work_order"])
    assign(wo, technician=techs["dana"])
    ok(post(client, f"{API}work-orders/{wo.pk}/transition/", {"status": WoStatus.IN_PROGRESS}))
    ok(post(client, f"{API}work-orders/{wo.pk}/transition/", {"status": WoStatus.COMPLETED, "resolution": "No fault found"}))


# --- recording -------------------------------------------------------------------------------------------------------------------

def test_a_technician_records_an_incident_by_the_devices_tag_and_holds_it(client, signed_in, vent, pump, today):
    tech = signed_in("technician")
    data = recorded(client, vent, asset="ce-10001", accessories="kept", event_log="saved")  # a tag in any letter case
    incident = Incident.objects.get(pk=data["id"])
    assert data["number"] == incident.number == f"IN-{today.year % 100:02d}-0001"
    assert (data["asset_tag"], data["device_model_name"], data["status"], data["status_label"]) == ("CE-10001", "Hamilton Medical Hamilton-G5",
                                                                                                     "open", "Open")
    assert (data["occurred_on"], data["aware_on"], data["outcome_label"], data["accessories_label"]) == (
        today.isoformat(), today.isoformat(), Outcome.UNKNOWN.label, "Kept with the device")
    due = add_work_days(today, 10)
    assert data["report_due_on"] == due.isoformat()
    assert data["clock"] == {"due": due.isoformat(), "left": 10, "overdue": False, "soon": False, "pending": True, "recipients": ["manufacturer"]}
    assert data["needs_decision"] and data["reportable"] is None and data["required_recipients"] == [] and data["reports_missing"] == []
    assert data["opened_work_order"] and data["work_order_number"] == incident.work_order.number and data["work_order_status"] == WoStatus.OPEN
    assert data["created_by_name"] == str(tech) and data["url"] == f"/incidents/{incident.number}/?facility=riverside"
    [hold] = data["holds"]
    assert (hold["asset_tag"], hold["active"], hold["status_before"], hold["held_on"], hold["release_note"]) == (
        "CE-10001", True, AssetStatus.IN_SERVICE, today.isoformat(), "")
    vent.refresh_from_db()
    assert vent.incident_hold and vent.status == AssetStatus.OUT_OF_SERVICE
    quiet = recorded(client, pump, asset=str(pump.pk), outcome=Outcome.NO_HARM, affected=Affected.NONE, hold=False)  # by id
    assert quiet["clock"] is None and quiet["holds"] == [] and quiet["work_order"] is None and not quiet["needs_decision"]


def test_recording_adopts_the_open_repair_it_was_reported_as(client, signed_in, vent, dept):
    signed_in("technician")
    request = create_service_request(asset=vent, department=dept, problem="Alarm sounded, then stopped", urgency=Urgency.HIGH, tagged_out=True)
    wo = request.work_order
    data = recorded(client, vent, work_order=wo.number.lower())
    assert (data["work_order"], data["work_order_number"], data["opened_work_order"]) == (str(wo.pk), wo.number, False)
    assert data["occurred_on"] == wo.opened_on.isoformat() and WorkOrder.objects.count() == 1
    bad(post(client, INCIDENTS, {"asset": vent.tag, "outcome": Outcome.NO_HARM, "affected": Affected.NONE, "work_order": wo.number}),
        "work_order", f"already the investigation of incident {data['number']}")


def test_recording_refusals_are_400s_keyed_by_field(client, signed_in, vent, pump, today, other_tenant):
    signed_in("director")
    with tenant_context(other_tenant):
        d = Department.objects.create(name="ICU")
        m = DeviceModel.objects.create(manufacturer="GE", model="B650", description="Monitor", category="Monitors", risk_class=RiskClass.MEDIUM)
        theirs = Asset.objects.create(tag="THEIRS-1", device_model=m, department=d)
    Asset.objects.filter(pk=pump.pk).update(status=AssetStatus.RETIRED)
    base = {"asset": vent.tag, "outcome": Outcome.UNKNOWN, "affected": Affected.PATIENT}
    for body, field, words in (
        ({"asset": ""}, "asset", "Required"), ({"asset": "CE-99999"}, "asset", "No device CE-99999 in this facility"),
        ({"asset": str(theirs.pk)}, "asset", "in this facility"), ({"asset": 10001}, "asset", "as text"),
        ({"occurred_on": "10/08/2026"}, "occurred_on", "YYYY-MM-DD"), ({"aware_on": (today + DAY).isoformat()}, "aware_on", "not a day to come"),
        ({"outcome": ""}, "outcome", "Choose the outcome"), ({"outcome": Outcome.INJURY, "affected": Affected.NONE}, "affected", "choose who"),
        ({"event_reference": "MRN 12345"}, "event_reference", "without spaces"), ({"accessories": 3}, "accessories", "Send this as text"),
        ({"hold": "maybe"}, "hold", "true or false"), ({"work_order": "WO-00-9999"}, "work_order", "No work order WO-00-9999"),
        ({"work_order": "WO-00-9999", "open_work_order": True}, "work_order", "No work order"),
        ({"asset": pump.tag}, "hold", "record the incident without holding it"),
        ({"notes": "The patient in bed 4"}, "notes", "Unknown field."),
    ):
        bad(post(client, INCIDENTS, base | body), field, words)
    wo = create_work_order(asset=vent, type=WoType.REPAIR, priority=Priority.NORMAL, problem="Alarm")
    bad(post(client, INCIDENTS, base | {"work_order": wo.number, "open_work_order": True}), "open_work_order", "not both")
    assert post(client, INCIDENTS, [base]).json() == {"detail": "Send the fields as a JSON object."}
    vent.refresh_from_db()
    assert not Incident.objects.exists() and WorkOrder.objects.count() == 1 and not vent.incident_hold


# --- levels ----------------------------------------------------------------------------------------------------------------------

def test_each_endpoint_has_the_screens_door(client, signed_in, vent, pump, today):
    signed_in("technician")
    incident = recorded(client, vent)
    hold = incident["holds"][0]["id"]
    approve_only = [
        ("decide", {"outcome": Outcome.NO_HARM, "basis": Basis.NOT_SERIOUS, "decided_on": today.isoformat(), "decided_by": DecidedBy.CE},
         inc.DECIDE_PERMISSION),
        ("reports", {"fda_reported_on": today.isoformat(), "report_number": f"0123456789-{today.year}-0001"}, inc.REPORTS_PERMISSION),
        (f"holds/{hold}/sent", {"sent_on": today.isoformat()}, inc.CUSTODY_PERMISSION),
        (f"holds/{hold}/back", {"back_on": today.isoformat()}, inc.CUSTODY_PERMISSION),
        (f"holds/{hold}/release", {"release": Release.KEEP_OUT}, inc.RELEASE_PERMISSION),
        ("close", None, inc.CLOSE_PERMISSION), ("reopen", None, inc.CLOSE_PERMISSION), ("in-error", None, inc.IN_ERROR_PERMISSION),
    ]
    edit = [("facts", {"event_log": "saved"}, inc.FACTS_PERMISSION), ("finding", {"finding": Finding.MET_SPECS}, inc.FINDING_PERMISSION),
            ("hold", {"asset": pump.tag}, inc.HOLD_PERMISSION), ("open-investigation", None, inc.OPEN_WORK_ORDER_PERMISSION)]
    for act, body, words in approve_only:  # the technician (Incidents Edit) records, never decides
        refused(post(client, url(incident, act), body), words)
    signed_in("analyst")  # Incidents View reads, and records nothing
    assert ok(client.get(INCIDENTS))["count"] == 1 and ok(client.get(url(incident)))["number"] == incident["number"]
    refused(post(client, INCIDENTS, {"asset": pump.tag}), inc.RECORD_PERMISSION)
    for act, body, words in edit + approve_only:
        refused(post(client, url(incident, act), body), words)
    refused(patch(client, url(incident, "facts"), {"event_log": "saved"}), inc.FACTS_PERMISSION)
    for slug in ("requester", "vendor"):  # no Incidents level, and scoped besides
        signed_in(slug)
        refused(client.get(INCIDENTS), views_incidents.VIEW_REFUSAL)
        refused(client.get(url(incident)), views_incidents.VIEW_REFUSAL)
    # The services' own doors: holding needs Equipment Edit, returning a device to use too
    signed_in(custom_role("safety", {Module.INCIDENTS: Level.APPROVE, Module.EQUIPMENT: Level.VIEW, Module.WORKORDERS: Level.EDIT}))
    refused(post(client, INCIDENTS, {"asset": pump.tag, "outcome": Outcome.NO_HARM, "affected": Affected.NONE}), inc.HOLD_PERMISSION)
    refused(post(client, url(incident, "hold"), {"asset": pump.tag}), inc.HOLD_PERMISSION)
    refused(post(client, url(incident, f"holds/{hold}/release"), {"release": Release.RETURN_TO_USE}), inc.RETURN_PERMISSION)
    stored = Incident.objects.get(pk=incident["id"])
    assert (stored.status, stored.reportable, stored.event_log, stored.finding, stored.holds.count()) == (Status.OPEN, None, "", "", 1)
    assert Incident.objects.count() == 1 and stored.holds.get().active


def test_a_scoped_role_with_full_levels_reaches_no_incident(client, signed_in, ctx, vent):
    incident = inc.record_incident(asset=vent, outcome=Outcome.UNKNOWN, affected=Affected.PATIENT)
    user = signed_in(custom_role("unit-lead", ALL_FULL, DataScope.DEPARTMENT))
    user.department = "ICU"
    user.save()
    for method, address, body in (("get", INCIDENTS, None), ("get", url(incident), None), ("post", INCIDENTS, {"asset": vent.tag}),
                                  ("post", url(incident, "finding"), {"finding": Finding.MET_SPECS}),
                                  ("post", url(incident, "in-error"), None)):
        r = send(client, method, address, body)
        assert r.status_code == 403 and "part of this facility" in r.json()["detail"], (method, address)
    incident.refresh_from_db()
    assert incident.status == Status.OPEN and incident.finding == ""
    # The device itself (theirs to read) says it is held, and never which incident holds it
    data = ok(client.get(f"{API}assets/{vent.pk}/"))
    assert data["incident_hold"] is True and data["status_label"] == "Held for incident" and incident.number not in str(data)


def test_another_facilitys_incident_is_a_404(client, signed_in, vent, other_tenant, today):
    create_default_roles(other_tenant)
    with tenant_context(other_tenant):
        d = Department.objects.create(name="ICU")
        m = DeviceModel.objects.create(manufacturer="GE", model="B650", description="Monitor", category="Monitors", risk_class=RiskClass.MEDIUM)
        their_device = Asset.objects.create(tag="CE-10001", device_model=m, department=d)
        theirs = inc.record_incident(asset=their_device, outcome=Outcome.UNKNOWN, affected=Affected.PATIENT)
        their_hold = theirs.holds.get()
    signed_in("director")
    mine = recorded(client, vent)
    assert mine["number"] == theirs.number  # numbers repeat across facilities: the address reads this facility's
    assert ok(client.get(f"{INCIDENTS}{theirs.number.lower()}/"))["id"] == mine["id"]
    assert client.get(url(theirs)).status_code == 404
    for act, body in (("finding", {"finding": Finding.MET_SPECS}), ("close", None), ("in-error", None),
                      (f"holds/{their_hold.pk}/release", {"release": Release.KEEP_OUT})):
        assert post(client, url(theirs, act), body).status_code == 404, act
    assert post(client, url(mine, f"holds/{their_hold.pk}/release"), {"release": Release.KEEP_OUT}).status_code == 404  # not this one's hold
    assert ok(client.get(INCIDENTS))["count"] == 1
    with tenant_context(other_tenant):
        theirs.refresh_from_db()
        assert theirs.status == Status.OPEN and theirs.finding == "" and theirs.holds.get().active


# --- the life of an incident -----------------------------------------------------------------------------------------------------

def test_an_incident_from_recording_to_closing(client, signed_in, vent, techs, today):
    signed_in("technician")
    incident = recorded(client, vent, occurred_on=(today - 2 * DAY).isoformat())
    data = ok(post(client, url(incident, "facts"), {"event_reference": "EV-2026-0412", "outcome": Outcome.SERIOUS_INJURY}))
    assert (data["event_reference"], data["outcome"]) == ("EV-2026-0412", Outcome.SERIOUS_INJURY)
    data = ok(post(client, url(incident, "finding"), {"finding": Finding.MET_SPECS}))
    assert (data["finding"], data["decision_cleared"], data["message"]) == (Finding.MET_SPECS, False, "")
    signed_in("manager")
    decided = {"outcome": Outcome.SERIOUS_INJURY, "basis": Basis.MAY_HAVE, "decided_on": today.isoformat(), "decided_by": DecidedBy.RISK}
    data = ok(post(client, url(incident, "decide"), decided))
    assert (data["reportable"], data["basis"], data["decided_by_label"], data["required_recipients"], data["reports_missing"]) == (
        True, Basis.MAY_HAVE, DecidedBy.RISK.label, ["manufacturer"], ["manufacturer"])
    assert data["clock"]["pending"] and not data["needs_decision"]
    hold = data["holds"][0]["id"]
    bad(post(client, url(incident, f"holds/{hold}/release"), {"release": Release.RETURN_TO_USE}), "release", "Complete investigation")
    number = f"0123456789-{today.year}-0001"
    data = ok(post(client, url(incident, "reports"), {"manufacturer_reported_on": today.isoformat(), "report_number": number}))
    assert (data["report_number"], data["reports_missing"], data["clock"]["pending"]) == (number, [], False)
    bad(post(client, url(incident, "close")), "holds", "Release CE-10001 first")
    signed_in("technician")
    finish(client, techs, incident)
    signed_in("manager")
    data = ok(post(client, url(incident, f"holds/{hold}/release"), {"release": Release.RETURN_TO_USE}))
    assert data["release_note"] == "" and data["holds"][0]["release"] == Release.RETURN_TO_USE and not data["holds"][0]["active"]
    device = ok(client.get(f"{API}assets/{vent.pk}/"))
    assert (device["incident_hold"], device["status"]) == (False, AssetStatus.IN_SERVICE)
    data = ok(post(client, url(incident, "close")))
    assert (data["status"], data["closed_on"], data["work_order_status"]) == (Status.CLOSED, today.isoformat(), WoStatus.COMPLETED)
    bad(post(client, url(incident, "finding"), {"finding": Finding.UTILITY}), "detail", "closed: reopen it first")
    bad(post(client, url(incident, "reopen"), {"why": "x"}), "why", "Unknown field.")
    assert ok(post(client, url(incident, "reopen")))["status"] == Status.OPEN
    bad(post(client, url(incident, "reopen")), "detail", "is open")


def test_facts_change_only_what_is_sent_and_lowering_needs_approve(client, signed_in, pump, today):
    signed_in("technician")
    incident = recorded(client, pump, outcome=Outcome.NO_HARM, hold=False, occurred_on=(today - 3 * DAY).isoformat())
    assert incident["clock"] is None
    data = ok(patch(client, url(incident, "facts"), {"outcome": Outcome.UNKNOWN}))
    assert data["report_due_on"] == add_work_days(today - 3 * DAY, 10).isoformat() and data["occurred_on"] == (today - 3 * DAY).isoformat()
    refused(patch(client, url(incident, "facts"), {"outcome": Outcome.INJURY}), inc.LOWER_PERMISSION)
    refused(post(client, url(incident, "facts"), {"aware_on": (today - DAY).isoformat(), "aware_reason": "correction"}), inc.LATER_PERMISSION)
    bad(post(client, url(incident, "facts"), {"aware_on": "yesterday"}), "aware_on", "YYYY-MM-DD")
    bad(post(client, url(incident, "facts"), {"event_reference": 412}), "event_reference", "Send this as text")
    bad(post(client, url(incident, "facts"), {"finding": Finding.MET_SPECS}), "finding", "Unknown field.")
    signed_in("manager")
    bad(post(client, url(incident, "facts"), {"aware_on": (today - DAY).isoformat()}), "aware_reason", "Choose why")
    data = ok(post(client, url(incident, "facts"), {"aware_on": (today - DAY).isoformat(), "aware_reason": "serious_later"}))
    assert data["aware_on"] == (today - DAY).isoformat() and data["report_due_on"] == add_work_days(today - DAY, 10).isoformat()
    data = ok(patch(client, url(incident, "facts"), {"outcome": Outcome.INJURY}))
    assert data["clock"] is None and data["outcome_label"] == "Injury, not serious"


def test_a_failure_found_after_a_no_suggestion_decision_says_it_cleared_the_decision(client, signed_in, pump, today):
    signed_in("manager")
    incident = recorded(client, pump, hold=False, event_reference="EV-5", occurred_on=(today - DAY).isoformat())
    ok(post(client, url(incident, "decide"), {"outcome": Outcome.SERIOUS_INJURY, "basis": Basis.NO_SUGGESTION, "decided_on": today.isoformat(),
                                               "decided_by": DecidedBy.RISK}))
    signed_in("technician")
    bad(post(client, url(incident, "finding"), {"finding": "broken"}), "finding", "Choose what the device evaluation found")
    data = ok(post(client, url(incident, "finding"), {"finding": Finding.DEVICE_FAILURE}))
    assert (data["decision_cleared"], data["message"]) == (True, inc.DECISION_CLEARED)
    assert data["reportable"] is None and data["basis"] == "" and data["needs_decision"] and data["clock"]["pending"]
    data = ok(post(client, url(incident, "finding"), {"finding": Finding.DEVICE_FAILURE}))
    assert (data["decision_cleared"], data["message"]) == (False, "")


def test_the_decision_and_the_reports_refused_keyed_by_field(client, signed_in, pump, vent, today):
    signed_in("manager")
    incident = recorded(client, pump, outcome=Outcome.DEATH, hold=False, occurred_on=(today - 2 * DAY).isoformat())
    decided = {"outcome": Outcome.DEATH, "basis": Basis.MAY_HAVE, "decided_on": today.isoformat(), "decided_by": DecidedBy.COMMITTEE}
    bad(post(client, url(incident, "decide"), decided), "event_reference", "Enter the facility's event report number first")
    ok(post(client, url(incident, "facts"), {"event_reference": "EV-1"}))
    bad(post(client, url(incident, "decide"), decided | {"decided_on": None}), "decided_on", "Enter the day it was decided")
    bad(post(client, url(incident, "decide"), decided | {"outcome": Outcome.UNKNOWN}), "outcome", "never leaves it not known")
    bad(post(client, url(incident, "decide"), decided | {"basis": Basis.NOT_PATIENT}), "basis", "not a visitor")
    data = ok(post(client, url(incident, "decide"), decided))
    assert data["required_recipients"] == ["fda", "manufacturer"] and data["reports_missing"] == ["fda", "manufacturer"]
    number = f"0123456789-{today.year}-0003"
    bad(post(client, url(incident, "reports"), {"report_number": number}), "fda_reported_on", "Enter the day the report went")
    bad(post(client, url(incident, "reports"), {"fda_reported_on": today.isoformat(), "report_number": "12-34"}), "report_number", "4-digit")
    data = ok(post(client, url(incident, "reports"), {"manufacturer_reported_on": today.isoformat(), "report_number": number}))
    assert data["reports_missing"] == ["fda"]
    data = ok(post(client, url(incident, "reports"), {"fda_reported_on": today.isoformat()}))  # what is left out stays
    assert (data["fda_reported_on"], data["manufacturer_reported_on"], data["report_number"], data["reports_missing"]) == (
        today.isoformat(), today.isoformat(), number, [])
    data = ok(post(client, url(incident, "reports"), {"manufacturer_reported_on": None}))  # null clears
    assert data["manufacturer_reported_on"] is None and data["reports_missing"] == ["manufacturer"]
    other = recorded(client, vent, hold=False, outcome=Outcome.SERIOUS_INJURY)
    bad(post(client, url(other, "reports"), {"fda_reported_on": today.isoformat(), "report_number": number}), "report_number", incident["number"])


# --- holds -----------------------------------------------------------------------------------------------------------------------

def test_another_part_held_its_trips_to_the_manufacturer_and_the_release_note(client, signed_in, vent, pump, today):
    signed_in("technician")
    incident = recorded(client, vent)
    data = ok(post(client, url(incident, "hold"), {"asset": "ce-10002"}))
    assert [h["asset_tag"] for h in data["holds"]] == ["CE-10001", "CE-10002"]
    bad(post(client, url(incident, "hold"), {"asset": pump.tag}), "asset", "already holds CE-10002")
    bad(post(client, url(incident, "hold"), {}), "asset", "Required")
    second = recorded(client, pump, outcome=Outcome.NO_HARM, affected=Affected.NONE)  # the pump is held twice
    signed_in("manager")
    bad(post(client, hold_url(incident, pump.tag, "sent"), {}), "sent_on", "Enter the day it went to the manufacturer")
    bad(post(client, hold_url(incident, pump.tag, "sent"), {"sent_on": "today"}), "sent_on", "YYYY-MM-DD")
    data = ok(post(client, hold_url(incident, "ce-10002", "sent"), {"sent_on": today.isoformat()}))  # a hold by its device's tag
    pump_hold = data["holds"][1]
    assert (pump_hold["sent_on"], pump_hold["with_manufacturer"]) == (today.isoformat(), True)
    bad(post(client, hold_url(incident, pump_hold["id"], "release"), {"release": Release.KEEP_OUT}), "release", "with the manufacturer")
    bad(post(client, hold_url(incident, pump_hold["id"], "release"), {"release": "lost"}), "release", "Choose how the hold ends")
    data = ok(post(client, hold_url(incident, pump_hold["id"], "back"), {"back_on": today.isoformat()}))
    assert data["holds"][1]["back_on"] == today.isoformat() and not data["holds"][1]["with_manufacturer"]
    data = ok(post(client, hold_url(incident, pump_hold["id"], "release"), {"release": Release.KEEP_OUT}))
    assert data["release_note"] == f"CE-10002 stays held: incident {second['number']} still holds it."
    assert data["holds"][1]["release_note"] == data["release_note"] and data["holds"][1]["release_label"] == "Kept out of service"
    pump.refresh_from_db()
    assert pump.incident_hold
    assert post(client, hold_url(incident, "CE-99999", "release"), {"release": Release.KEEP_OUT}).status_code == 404
    listed = ok(client.get(INCIDENTS))["results"]
    assert all("release_note" not in h for row in listed for h in row["holds"])  # read on the incident itself


def test_opening_an_investigation_and_recorded_in_error(client, signed_in, pump):
    signed_in("technician")
    incident = recorded(client, pump, outcome=Outcome.NO_HARM, affected=Affected.NONE, hold=False)
    data = ok(post(client, url(incident, "open-investigation")))
    wo = WorkOrder.objects.get(pk=data["work_order"])
    assert data["opened_work_order"] and wo.problem == inc.NOT_HELD_PROBLEM and data["work_order_number"] == wo.number
    bad(post(client, url(incident, "open-investigation")), "work_order", wo.number)
    bad(post(client, url(incident, "open-investigation"), {"problem": "Patient fell"}), "problem", "Unknown field.")
    signed_in("manager")
    data = ok(post(client, url(incident, "in-error")))
    assert (data["status"], data["status_label"], data["work_order_status"]) == (Status.IN_ERROR, "Recorded in error", WoStatus.CANCELLED)
    bad(post(client, url(incident, "in-error")), "detail", "was recorded in error")


# --- the list --------------------------------------------------------------------------------------------------------------------

def test_the_list_filters_by_status_and_year_and_the_address_takes_the_number(client, signed_in, ctx, vent, pump, today):
    last_year = date(today.year - 1, 6, 15)
    old = inc.record_incident(asset=pump, outcome=Outcome.NO_HARM, affected=Affected.NONE, hold=False, occurred_on=last_year)
    wrong = inc.record_incident(asset=pump, outcome=Outcome.NO_HARM, affected=Affected.NONE, hold=False)
    inc.recorded_in_error(wrong)
    open_now = inc.record_incident(asset=vent, outcome=Outcome.UNKNOWN, affected=Affected.PATIENT)
    signed_in("analyst")

    def numbers(query=""):
        return [row["number"] for row in ok(client.get(f"{INCIDENTS}{query}"))["results"]]

    assert numbers() == numbers("?status=all") == [open_now.number, wrong.number, old.number]  # newest first
    assert numbers("?status=open") == [open_now.number, old.number]
    assert numbers("?status=in_error") == [wrong.number] and numbers("?status=closed") == []
    assert numbers(f"?year={today.year - 1}") == [old.number] and numbers(f"?year={today.year}&status=open") == [open_now.number]
    assert numbers("?search=ce-10001") == [open_now.number]
    bad(client.get(f"{INCIDENTS}?status=pending"), "status", "One of open, closed, in_error, all")
    for year in ("26", "0000", "２０２６"):
        bad(client.get(f"{INCIDENTS}?year={year}"), "year", "A year")
    data = ok(client.get(f"{INCIDENTS}{open_now.number.lower()}/"))
    assert data["id"] == str(open_now.pk) and data["holds"][0]["release_note"] == ""
    assert client.get(f"{INCIDENTS}IN-00-0000/").status_code == 404
    for address in (INCIDENTS, url(open_now)):  # the browsable API's page, which reads while it renders
        assert client.get(address, HTTP_ACCEPT="text/html").status_code == 200


def test_the_list_reads_a_fixed_number_of_queries(client, signed_in, ctx, vent, dept, vent_model):
    manager = signed_in("manager")

    def queries() -> int:
        with CaptureQueriesContext(connection) as q:
            assert ok(client.get(INCIDENTS))["count"] == Incident.objects.count()
        return len(q)

    inc.record_incident(asset=vent, outcome=Outcome.UNKNOWN, affected=Affected.PATIENT, by=manager)
    first = queries()
    for n in range(3):
        device = Asset.objects.create(tag=f"CE-4000{n}", device_model=vent_model, department=dept)
        incident = inc.record_incident(asset=device, outcome=Outcome.NO_HARM, affected=Affected.NONE, by=manager)
        inc.hold_device(incident, vent, by=manager)
        inc.release(incident.holds.get(asset=device), Release.KEEP_OUT, by=manager)
    assert queries() == first


def test_the_clock_reads_the_facilitys_today(client, signed_in, ctx, vent, today, monkeypatch):
    aware = today - 20 * DAY
    incident = inc.record_incident(asset=vent, outcome=Outcome.SERIOUS_INJURY, affected=Affected.STAFF, occurred_on=aware)
    due = add_work_days(aware, 10)
    signed_in("analyst")
    monkeypatch.setattr(views_incidents, "_today", lambda: due - DAY)
    clock = ok(client.get(url(incident)))["clock"]
    assert (clock["due"], clock["left"], clock["overdue"], clock["soon"]) == (due.isoformat(), 1, False, True)
    monkeypatch.setattr(views_incidents, "_today", lambda: due + DAY)
    clock = ok(client.get(INCIDENTS))["results"][0]["clock"]
    assert (clock["left"], clock["overdue"], clock["pending"], clock["recipients"]) == (0, True, True, ["manufacturer"])


# --- the other endpoints the slice changes ---------------------------------------------------------------------------------------

def test_an_incidents_investigation_is_never_deleted(client, signed_in, ctx, vent, pump):
    incident = inc.record_incident(asset=vent, outcome=Outcome.UNKNOWN, affected=Affected.PATIENT)
    investigation = incident.work_order
    signed_in("director")
    r = send(client, "delete", f"{API}work-orders/{investigation.pk}/")
    assert r.status_code == 400 and r.json() == {"detail": f"{investigation.number} is the investigation of incident {incident.number}, so it "
                                                           "cannot be deleted: the incident's file keeps it."}
    signed_in(custom_role("wo-admin", {Module.WORKORDERS: Level.FULL, Module.EQUIPMENT: Level.VIEW}))  # no Incidents View: no number
    r = send(client, "delete", f"{API}work-orders/{investigation.pk}/")
    assert r.status_code == 400 and r.json()["detail"].startswith(f"{investigation.number} is the investigation of an incident,")
    assert incident.number not in r.json()["detail"] and WorkOrder.objects.filter(pk=investigation.pk).exists()
    plain = create_work_order(asset=pump, type=WoType.REPAIR, priority=Priority.NORMAL, problem="Door latch")
    assert send(client, "delete", f"{API}work-orders/{plain.pk}/").status_code == 204


def test_the_work_order_transition_refuses_in_words(client, signed_in, ctx, vent, techs):
    repair = create_work_order(asset=vent, type=WoType.REPAIR, priority=Priority.NORMAL, problem="Alarm", assigned_to=techs["dana"])
    inc.record_incident(asset=vent, outcome=Outcome.UNKNOWN, affected=Affected.PATIENT)
    signed_in("director")
    vent.refresh_from_db()
    r = post(client, f"{API}work-orders/{repair.pk}/transition/", {"status": WoStatus.IN_PROGRESS})
    assert r.status_code == 400 and r.json() == {"detail": held_work_message(vent)}
    r = post(client, f"{API}work-orders/{repair.pk}/transition/", {"status": WoStatus.CLOSED})
    assert r.status_code == 400 and r.json() == {"detail": f"Cannot move {repair.number} from Open to closed."}
    r = post(client, f"{API}work-orders/{repair.pk}/transition/", {"status": WoStatus.CANCELLED, "note": "x" * 2001})
    assert r.status_code == 400 and list(r.json()) == ["note"] and "['" not in r.json()["note"][0]
    bad(post(client, f"{API}work-orders/{repair.pk}/transition/", {"status": ["in_progress"]}), "status", "Required; one of open")
    bad(post(client, f"{API}work-orders/{repair.pk}/transition/", ["in_progress"]), "detail", "JSON object")
    repair.refresh_from_db()
    assert repair.status == WoStatus.OPEN


def test_a_devices_hold_is_read_only_over_the_devices_api(client, signed_in, ctx, vent, pump, dept, vent_model):
    inc.record_incident(asset=vent, outcome=Outcome.UNKNOWN, affected=Affected.PATIENT)
    signed_in("director")
    data = ok(client.get(f"{API}assets/{vent.pk}/"))
    assert (data["incident_hold"], data["status_label"]) == (True, "Held for incident")
    assert ok(client.get(f"{API}assets/{pump.pk}/"))["incident_hold"] is False
    bad(patch(client, f"{API}assets/{vent.pk}/", {"incident_hold": False}), "incident_hold", "by recording an incident")
    bad(patch(client, f"{API}assets/{pump.pk}/", {"incident_hold": True}), "incident_hold", "by recording an incident")
    assert ok(patch(client, f"{API}assets/{vent.pk}/", {"incident_hold": True, "room": "ICU-4"}))["room"] == "ICU-4"  # what GET said: fine
    body = {"tag": "CE-30001", "device_model": str(vent_model.pk), "department": str(dept.pk), "incident_hold": True}
    bad(post(client, f"{API}assets/", body), "incident_hold", "by recording an incident")
    assert ok(post(client, f"{API}assets/", body | {"incident_hold": False}), 201)["incident_hold"] is False
    pump.refresh_from_db()
    assert not pump.incident_hold


def test_auto_assign_week_lists_the_held_devices_it_leaves_alone(client, signed_in, ctx, vent, techs, today):
    Asset.objects.filter(pk=vent.pk).update(next_pm_on=today + DAY)
    inc.record_incident(asset=vent, outcome=Outcome.NO_HARM, affected=Affected.NONE)
    signed_in("manager")
    week = ok(client.get(f"{API}pm/week/"))
    assert week["on_hold"] == [{"asset_id": str(vent.pk), "tag": "CE-10001", "description": "ICU ventilator", "due_on": (today + DAY).isoformat(),
                                "has_open_pm": False}]
    assert week["nothing_to_do"] and week["uncovered"] == [] and week["assigned"] == 0
    assert ok(post(client, f"{API}pm/assign-week/")) == week
    assert not WorkOrder.objects.filter(asset=vent, type=WoType.PM).exists()


# --- by token, and under row-level security --------------------------------------------------------------------------------------

@pytest.fixture
def token_rows(tenant, make_user, other_tenant):
    """Rows made inside the facility; the requests then run with no tenant set, as a token request arrives."""
    create_default_roles(other_tenant)
    with tenant_context(tenant):
        dept = Department.objects.create(name="ICU")
        dm = DeviceModel.objects.create(manufacturer="BD", model="Alaris 8015 PCU", description="Infusion pump", category="Infusion pumps",
                                        risk_class=RiskClass.HIGH, oem_pm_interval_months=12)
        pcu = Asset.objects.create(tag="PCU-1", device_model=dm, department=dept, next_pm_on=timezone.localdate() + timedelta(days=60))
        channel = Asset.objects.create(tag="CH-1", device_model=dm, department=dept, next_pm_on=timezone.localdate() + timedelta(days=60))
    tokens = {slug: Token.objects.create(user=make_user(slug, username=f"tok-{slug}@riverside.example")) for slug in ("technician", "manager")}
    theirs = Token.objects.create(user=make_user("director", other_tenant, username="their-director@other.example"))
    return {"tenant": tenant, "pcu": pcu, "channel": channel, "tokens": tokens, "theirs": theirs}


def walk_by_token(client, rows):
    """Every write by token, outside any tenant context: record, hold the channel, the facts, the finding, decide, the custody, the
    releases, close."""
    tech, manager = rows["tokens"]["technician"], rows["tokens"]["manager"]
    today = timezone.localdate()
    incident = ok(post(client, INCIDENTS, {"asset": "PCU-1", "outcome": Outcome.NO_HARM, "affected": Affected.NONE}, token=tech), 201)
    assert ok(post(client, url(incident, "hold"), {"asset": "CH-1"}, token=tech))["holds"][1]["asset_tag"] == "CH-1"
    ok(post(client, url(incident, "facts"), {"event_reference": "EV-9", "event_log": "saved"}, token=tech))
    ok(post(client, url(incident, "finding"), {"finding": Finding.MET_SPECS}, token=tech))
    decided = {"outcome": Outcome.NO_HARM, "basis": Basis.NOT_SERIOUS, "decided_on": today.isoformat(), "decided_by": DecidedBy.CE}
    assert ok(post(client, url(incident, "decide"), decided, token=manager))["reportable"] is False
    ok(post(client, hold_url(incident, "CH-1", "sent"), {"sent_on": today.isoformat()}, token=manager))
    ok(post(client, hold_url(incident, "CH-1", "release"), {"release": Release.KEPT_BY_MANUFACTURER}, token=manager))
    ok(post(client, hold_url(incident, "PCU-1", "release"), {"release": Release.KEEP_OUT}, token=manager))
    wo = incident["work_order"]
    ok(post(client, f"{API}work-orders/{wo}/transition/", {"status": WoStatus.CANCELLED}, token=tech))
    assert ok(post(client, url(incident, "close"), token=manager))["status"] == Status.CLOSED
    assert ok(send(client, "get", INCIDENTS, token=tech))["count"] == 1
    assert send(client, "get", url(incident), token=rows["theirs"]).status_code == 404
    assert ok(send(client, "get", INCIDENTS, token=rows["theirs"]))["count"] == 0
    return incident


def test_every_write_by_token(client, token_rows):
    incident = walk_by_token(client, token_rows)
    stored = Incident.unscoped.get(pk=incident["id"])  # unscoped: checking which facility the row landed in
    assert stored.tenant_id == token_rows["tenant"].id and stored.status == Status.CLOSED
    with tenant_context(token_rows["tenant"]):
        assert {h.asset.tag: h.release for h in stored.holds.all()} == {"PCU-1": Release.KEEP_OUT, "CH-1": Release.KEPT_BY_MANUFACTURER}
        assert not Asset.objects.filter(incident_hold=True).exists()


@needs_postgres
def test_the_incidents_api_under_the_policies(client, token_rows):
    """As the runtime role: every write by token, the list, the browsable API's page, and another facility's token reading nothing."""
    as_app_role()
    incident = walk_by_token(client, token_rows)
    tech = token_rows["tokens"]["technician"]
    for address in (INCIDENTS, url(incident)):
        assert client.get(address, HTTP_ACCEPT="text/html", HTTP_AUTHORIZATION=f"Token {tech.key}").status_code == 200, address
    with tenant_context(token_rows["tenant"]):
        assert Incident.objects.get().status == Status.CLOSED
