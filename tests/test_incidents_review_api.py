"""
Slice 28 review fixes, the API's share: a work order an incident points to (its investigation) never moves to another device or
becomes another type ([14]); the incident's url names its facility ([15]); a report date left out of POST reports keeps what the
locked row has, never get_object's older copy ([17]); a work order transition's note that is not text is a 400 ([18]); decide takes
who was affected as it is now known; and recorded in error answers the service's refusals as a 403 in its words.
"""
import threading
import time

import pytest
from django.db import connection, transaction
from django.test import Client
from django.utils import timezone
from pg_helpers import needs_postgres
from rest_framework.authtoken.models import Token

from apps.accounts.models import Level, Module, Role, User, create_default_roles
from apps.api import views_incidents
from apps.core.workdays import add_work_days
from apps.equipment.models import Asset, AssetStatus, Department, DeviceModel, RiskClass
from apps.incidents import services as inc
from apps.incidents.models import Affected, Basis, DecidedBy, Incident, Outcome, Status
from apps.tenants.context import tenant_context
from apps.workorders.models import Priority, Urgency, WorkOrder, WoStatus, WoType
from apps.workorders.services import create_service_request, create_work_order

API = "/api/v1/"
INCIDENTS = f"{API}incidents/"
SEND_AS_TEXT = {"note": ["Send this as text."]}


# --- helpers (as tests/test_incidents_api.py's) ----------------------------------------------------------------------------------

def post(client, address, body=None, token=None):
    extra = {"HTTP_AUTHORIZATION": f"Token {token.key}"} if token else {}
    return client.post(address, body if body is not None else {}, content_type="application/json", **extra)


def patch(client, address, body):
    return client.patch(address, body, content_type="application/json")


def ok(r, code=200):
    assert r.status_code == code, (r.status_code, r.content)
    return r.json()


def bad(r, field, match=""):
    """A 400 keyed by `field`, with `match` in its words (never a list's brackets)."""
    assert r.status_code == 400, (r.status_code, r.content)
    data = r.json()
    assert field in data, data
    text = " ".join(data[field]) if isinstance(data[field], list) else data[field]
    assert match in text and "['" not in text, (match, text)
    return text


def url(incident, act=""):
    key = incident["id"] if isinstance(incident, dict) else incident.pk
    return f"{INCIDENTS}{key}/" + (f"{act}/" if act else "")


def wo_url(wo, act=""):
    key = wo["id"] if isinstance(wo, dict) else wo.pk
    return f"{API}work-orders/{key}/" + (f"{act}/" if act else "")


def recorded(client, device, **body):
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


def custom_role(slug, levels):
    role = Role.objects.create(name=slug.title(), slug=slug)
    role.set_levels(levels)
    return role


# --- [14] the investigation stays on its device, a repair ------------------------------------------------------------------------

def test_an_incidents_investigation_never_moves_to_another_device_or_becomes_a_pm(client, signed_in, vent, pump, dept):
    signed_in("technician")
    incident = recorded(client, vent)
    wo = WorkOrder.objects.get(pk=incident["work_order"])
    number = incident["number"]
    stays = f"{wo.number} is the investigation of incident {number}: it stays with CE-10001, the device the incident was recorded on."
    assert bad(patch(client, wo_url(wo), {"asset": str(pump.pk)}), "asset", stays).startswith(stays)
    bad(patch(client, wo_url(wo), {"type": WoType.PM}), "type", f"{wo.number} is the investigation of incident {number}: it stays a corrective repair.")
    both = patch(client, wo_url(wo), {"asset": str(pump.pk), "type": WoType.PM, "priority": Priority.LOW})
    assert both.status_code == 400 and set(both.json()) == {"asset", "type"}
    # What else an edit changes stays allowed, and sending back the device and type it has is no change
    data = ok(patch(client, wo_url(wo), {"asset": str(vent.pk), "type": WoType.REPAIR, "priority": Priority.CRITICAL}))
    assert (data["asset"], data["type"], data["priority"]) == (str(vent.pk), WoType.REPAIR, Priority.CRITICAL)
    # An adopted request likewise (the incident points to it), and the words without the number for a user without Incidents View
    request = create_service_request(asset=pump, department=dept, problem="Alarm sounded", urgency=Urgency.HIGH)
    adopted = recorded(client, pump, outcome=Outcome.NO_HARM, affected=Affected.NONE, hold=False, work_order=request.work_order.number)
    signed_in(custom_role("wo-editor", {Module.WORKORDERS: Level.EDIT, Module.EQUIPMENT: Level.VIEW}))
    words = bad(patch(client, wo_url(request.work_order), {"asset": str(vent.pk)}), "asset",
                f"{request.work_order.number} is the investigation of an incident: it stays with CE-10002")
    assert adopted["number"] not in words
    # A work order no incident points to moves as before
    plain = create_work_order(asset=pump, type=WoType.REPAIR, priority=Priority.NORMAL, problem="Door latch")
    assert ok(patch(client, wo_url(plain), {"type": WoType.PM}))["type"] == WoType.PM
    wo.refresh_from_db()
    request.work_order.refresh_from_db()
    vent.refresh_from_db()
    assert (wo.asset_id, wo.type, request.work_order.asset_id) == (vent.pk, WoType.REPAIR, pump.pk) and vent.incident_hold


# --- [15] the url names the facility ---------------------------------------------------------------------------------------------

def test_the_incidents_url_names_its_facility(client, signed_in, vent, other_tenant, make_user):
    create_default_roles(other_tenant)
    with tenant_context(other_tenant):
        d = Department.objects.create(name="ICU")
        m = DeviceModel.objects.create(manufacturer="GE", model="B650", description="Monitor", category="Monitors", risk_class=RiskClass.MEDIUM)
        theirs = inc.record_incident(asset=Asset.objects.create(tag="CE-10001", device_model=m, department=d), outcome=Outcome.UNKNOWN,
                                     affected=Affected.PATIENT)
    their_token = Token.objects.create(user=make_user("director", other_tenant, username="their-director@other.example"))
    signed_in("director")
    mine = recorded(client, vent)
    assert mine["number"] == theirs.number  # the same number in both facilities
    assert mine["url"] == f"/incidents/{mine['number']}/?facility=riverside"
    assert ok(client.get(INCIDENTS))["results"][0]["url"] == mine["url"]
    data = ok(Client().get(url(theirs), HTTP_AUTHORIZATION=f"Token {their_token.key}"))  # no session: the token's facility
    assert (data["id"], data["url"]) == (str(theirs.pk), f"/incidents/{theirs.number}/?facility=other")


# --- [17] a report date left out keeps the locked row's ----------------------------------------------------------------------------

def test_a_report_date_left_out_keeps_what_the_locked_row_has(client, signed_in, pump, today, monkeypatch):
    """Another request records the FDA date after this one's get_object read the incident and before it locks the row: the date this
    one leaves out is the one recorded, never the empty one its first copy had."""
    signed_in("manager")
    incident = recorded(client, pump, outcome=Outcome.DEATH, hold=False, event_reference="EV-1")
    number = f"0123456789-{today.year}-0001"
    read = views_incidents.IncidentViewSet.get_object

    def read_before_the_other_commits(self):
        found = read(self)
        Incident.objects.filter(pk=found.pk).update(fda_reported_on=today, report_number=number)
        return found

    monkeypatch.setattr(views_incidents.IncidentViewSet, "get_object", read_before_the_other_commits)
    data = ok(post(client, url(incident, "reports"), {"manufacturer_reported_on": today.isoformat()}))
    assert (data["fda_reported_on"], data["manufacturer_reported_on"], data["report_number"], data["reports_missing"]) == (
        today.isoformat(), today.isoformat(), number, [])


@needs_postgres
@pytest.mark.django_db(transaction=True)
def test_two_report_dates_sent_at_once_both_stay(tenant, make_user):
    """On PostgreSQL: the FDA date's write holds the incident's row while the manufacturer's date arrives by the API with the FDA date
    left out. The API's request waits for the row and keeps the FDA date (read before the lock, it wrote back the empty one)."""
    with tenant_context(tenant):
        today = timezone.localdate()
        model = DeviceModel.objects.create(manufacturer="BD", model="Alaris 8015 PCU", description="Infusion pump", category="Infusion pumps",
                                           risk_class=RiskClass.HIGH, oem_pm_interval_months=12)
        device = Asset.objects.create(tag="CE-20001", device_model=model, department=Department.objects.create(name="ICU"))
        incident = inc.record_incident(asset=device, outcome=Outcome.DEATH, affected=Affected.PATIENT, event_reference="EV-1", hold=False)
    token = Token.objects.create(user=make_user("manager"))
    number = f"0123456789-{today.year}-0001"
    locked, errors, answers = threading.Event(), [], []

    def fda():
        try:
            with tenant_context(tenant), transaction.atomic():
                inc.record_reports(Incident.objects.get(pk=incident.pk), fda_reported_on=today, report_number=number)
                locked.set()
                time.sleep(1)  # the row held: the API's request arrives meanwhile
        except Exception as e:  # noqa: BLE001 - reported below
            errors.append(e)
        finally:
            locked.set()
            connection.close()

    def manufacturer():
        try:
            assert locked.wait(20)
            body = {"manufacturer_reported_on": today.isoformat(), "report_number": number}  # one number for both copies
            answers.append(post(Client(), url(incident, "reports"), body, token=token))
        except Exception as e:  # noqa: BLE001 - reported below
            errors.append(e)
        finally:
            connection.close()

    threads = [threading.Thread(target=f) for f in (fda, manufacturer)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not errors, errors
    [answer] = answers
    data = ok(answer)
    assert (data["fda_reported_on"], data["manufacturer_reported_on"], data["report_number"]) == (today.isoformat(), today.isoformat(), number)
    with tenant_context(tenant):
        incident.refresh_from_db()
        assert (incident.fda_reported_on, incident.manufacturer_reported_on) == (today, today)


# --- [18] a transition's note is text ------------------------------------------------------------------------------------------------

def test_a_transitions_note_that_is_not_text_is_a_400(client, signed_in, ctx, pump):
    repair = create_work_order(asset=pump, type=WoType.REPAIR, priority=Priority.NORMAL, problem="Door latch")
    signed_in("director")
    for status, note in ((WoStatus.CANCELLED, ["x"]), (WoStatus.CANCELLED, 5), (WoStatus.AWAITING_PARTS, {"a": 1}), (WoStatus.CANCELLED, True)):
        r = post(client, wo_url(repair, "transition"), {"status": status, "note": note})
        assert (r.status_code, r.json()) == (400, SEND_AS_TEXT), note
    repair.refresh_from_db()
    assert repair.status == WoStatus.OPEN
    assert ok(post(client, wo_url(repair, "transition"), {"status": WoStatus.CANCELLED, "note": None}))["status"] == WoStatus.CANCELLED


# --- decide takes who was affected -----------------------------------------------------------------------------------------------

def test_decide_takes_who_was_affected_as_it_is_now_known(client, signed_in, pump, today):
    """A visitor's serious injury, decided not reportable (not a patient of the facility); then the person turns out to have been a
    staff member on duty (803.3(t)): decided again with who was affected, it is reportable and its clock runs."""
    signed_in("manager")
    incident = recorded(client, pump, outcome=Outcome.SERIOUS_INJURY, affected=Affected.OTHER, hold=False, event_reference="EV-1")
    decided = {"outcome": Outcome.SERIOUS_INJURY, "basis": Basis.NOT_PATIENT, "decided_on": today.isoformat(), "decided_by": DecidedBy.RISK}
    data = ok(post(client, url(incident, "decide"), decided))
    assert (data["reportable"], data["affected"], data["clock"]) == (False, Affected.OTHER, None)
    bad(post(client, url(incident, "facts"), {"affected": Affected.STAFF}), "affected", "Decide again takes both")
    reportable = decided | {"basis": Basis.MAY_HAVE}
    bad(post(client, url(incident, "decide"), reportable), "basis", "patient of the facility or a staff member on duty")
    bad(post(client, url(incident, "decide"), reportable | {"affected": 5}), "affected", "Send this as text")
    bad(post(client, url(incident, "decide"), reportable | {"affected": ""}), "affected", "Choose who was affected")
    bad(post(client, url(incident, "decide"), reportable | {"affected": Affected.NONE}), "affected", "choose who")
    data = ok(post(client, url(incident, "decide"), reportable | {"affected": Affected.STAFF}))
    due = add_work_days(today, 10)
    assert (data["affected"], data["affected_label"], data["reportable"], data["basis"], data["report_due_on"]) == (
        Affected.STAFF, "Staff member on duty", True, Basis.MAY_HAVE, due.isoformat())
    assert data["required_recipients"] == ["manufacturer"] and data["reports_missing"] == ["manufacturer"] and data["clock"]["pending"]
    data = ok(post(client, url(incident, "decide"), reportable | {"affected": None, "decided_by": DecidedBy.COMMITTEE}))  # null: as recorded
    assert (data["affected"], data["decided_by"], data["reportable"]) == (Affected.STAFF, DecidedBy.COMMITTEE, True)


# --- recorded in error: the service's refusals as a 403 ----------------------------------------------------------------------------

def test_recorded_in_error_answers_the_services_refusals_in_its_words(client, signed_in, ctx, vent):
    incident = inc.record_incident(asset=vent, outcome=Outcome.UNKNOWN, affected=Affected.PATIENT)
    opened = incident.work_order
    signed_in(custom_role("risk", {Module.INCIDENTS: Level.APPROVE, Module.EQUIPMENT: Level.VIEW, Module.WORKORDERS: Level.VIEW}))
    r = post(client, url(incident, "in-error"))
    assert (r.status_code, r.json()) == (403, {"detail": f"Marking {incident.number} recorded in error puts CE-10001 back in use: that needs "
                                                         "Incidents Approve and Equipment Edit."})
    signed_in(custom_role("risk-equipment", {Module.INCIDENTS: Level.APPROVE, Module.EQUIPMENT: Level.EDIT, Module.WORKORDERS: Level.VIEW}))
    r = post(client, url(incident, "in-error"))
    assert (r.status_code, r.json()) == (403, {"detail": f"Marking {incident.number} recorded in error cancels {opened.number}: that needs "
                                                         "Work orders Edit."})
    incident.refresh_from_db()
    opened.refresh_from_db()
    vent.refresh_from_db()
    assert (incident.status, opened.status, vent.incident_hold, vent.status) == (Status.OPEN, WoStatus.OPEN, True, AssetStatus.OUT_OF_SERVICE)
    assert incident.holds.get().active
    signed_in("manager")
    data = ok(post(client, url(incident, "in-error")))
    assert (data["status"], data["work_order_status"]) == (Status.IN_ERROR, WoStatus.CANCELLED)
    vent.refresh_from_db()
    assert (vent.incident_hold, vent.status) == (False, AssetStatus.IN_SERVICE)
