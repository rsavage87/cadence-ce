"""
Slice 28, wave 1a: the incident write services (apps.incidents.services). Recording (the number, the hold, the investigation adopted or
opened, the 10-work-day clock), the facts and who may change them, the decision and its rules, the reports, the finding, custody at the
manufacturer, the release and what it leaves the device, the late PMs' reason, closing and reopening, and "recorded in error"; each
refusal in words keyed by field, and each level (technician Edit, manager Approve, director, analyst View refused, requester and
vendor refused). The guards that keep a held device's other work from starting are wave 1b's (tests/test_incident_guards.py).
"""
from datetime import date, timedelta

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone
from incoming_fixtures import waiting_device
from pg_helpers import as_app_role, needs_postgres

from apps.accounts.models import Level, Module, Role
from apps.core.workdays import add_work_days
from apps.equipment import services as eq
from apps.equipment.models import Asset, AssetStatus, UseBeforeInspection
from apps.incidents import notify
from apps.incidents import services as inc
from apps.incidents.models import Affected, AwareChange, Basis, DecidedBy, Finding, Incident, IncidentHold, Outcome, Release, Status
from apps.tenants.context import tenant_context
from apps.workorders.costs import add_labor
from apps.workorders.models import LateReason, Priority, Urgency, WorkOrder, WoStatus, WoType
from apps.workorders.services import assign, change_status, create_service_request, create_work_order, set_late_reason

DAY = timedelta(days=1)


@pytest.fixture
def today(ctx):
    return timezone.localdate()  # the facility's day, as the services read it


@pytest.fixture
def users(make_user):
    return {slug: make_user(slug) for slug in ("director", "manager", "technician", "analyst", "requester", "vendor")}


@pytest.fixture
def heard(monkeypatch):
    """The incidents recorded with a clock that notify.recorded heard about (wave 2 sends the email), in order."""
    calls = []
    monkeypatch.setattr(notify, "recorded", lambda incident: calls.append(incident.number))
    return calls


def record(asset, **kwargs):
    kwargs.setdefault("outcome", Outcome.UNKNOWN)
    kwargs.setdefault("affected", Affected.PATIENT)
    return inc.record_incident(asset=asset, **kwargs)


def role_user(make_user, slug, levels):
    """An account with a custom role holding only `levels` (every other module None)."""
    Role.objects.create(name=slug.title(), slug=slug).set_levels(levels)
    return make_user(slug)


def done(wo):
    """The investigation completed as the screens do it: started, then completed."""
    change_status(wo, WoStatus.IN_PROGRESS)
    change_status(wo, WoStatus.COMPLETED)
    wo.refresh_from_db()


def refused(field, write, *args, match=None, **kwargs):
    with pytest.raises(ValidationError, match=match) as e:
        write(*args, **kwargs)
    assert field in e.value.message_dict, e.value.message_dict


def reasons(rec) -> list[str]:
    return [r.history_change_reason for r in rec.history.order_by("history_date")]


# --- recording -------------------------------------------------------------------------------------------------------------------

def test_recording_numbers_the_incident_holds_the_device_and_opens_its_investigation(vent, pump, users, today, heard,
                                                                                      django_capture_on_commit_callbacks):
    tech = users["technician"]
    with django_capture_on_commit_callbacks(execute=True):
        incident = record(vent, by=tech, accessories="kept", event_log="saved")
    yy = today.year % 100
    assert incident.number == f"IN-{yy:02d}-0001"
    assert (incident.occurred_on, incident.aware_on, incident.created_by, incident.status) == (today, today, tech, Status.OPEN)
    assert (incident.accessories, incident.event_log, incident.reportable) == ("kept", "saved", None)
    assert incident.report_due_on == add_work_days(today, 10)
    assert heard == [incident.number]  # after commit, because it has a clock
    wo = incident.work_order
    assert incident.opened_work_order
    assert (wo.type, wo.priority, wo.problem, wo.tagged_out, wo.assigned_to, wo.opened_on, wo.created_by, wo.status) == (
        WoType.REPAIR, Priority.HIGH, inc.INVESTIGATION_PROBLEM, True, None, today, tech, WoStatus.OPEN)
    hold = incident.holds.get()
    assert (hold.asset, hold.held_on, hold.status_before, hold.released_on) == (vent, today, AssetStatus.IN_SERVICE, None)
    vent.refresh_from_db()
    assert vent.incident_hold and vent.status == AssetStatus.OUT_OF_SERVICE
    assert inc.investigation_of(vent) == {wo.pk}
    with django_capture_on_commit_callbacks(execute=True):
        quiet = record(pump, outcome=Outcome.NO_HARM, affected=Affected.NONE, hold=False)
    assert quiet.number == f"IN-{yy:02d}-0002" and heard == [incident.number]  # no clock, nobody hears
    assert quiet.work_order is None and not quiet.holds.exists() and quiet.report_due_on is None
    last_year = date(today.year - 1, 12, 30)
    assert record(pump, occurred_on=last_year, hold=False).number == f"IN-{(today.year - 1) % 100:02d}-0001"  # the year it happened


def test_the_clock_counts_ten_work_days_from_the_day_clinical_staff_knew(vent, pump):
    oct8, oct9 = date(2026, 10, 8), date(2026, 10, 9)
    serious = record(vent, occurred_on=oct8, outcome=Outcome.SERIOUS_INJURY, today=oct9)
    assert serious.aware_on == oct8 and serious.report_due_on == date(2026, 10, 23)  # Columbus Day (Oct 12) is not a work day
    assert serious.holds.get().held_on == oct9 and serious.work_order.opened_on == oct9
    staff = record(pump, occurred_on=oct8, outcome=Outcome.DEATH, affected=Affected.STAFF, hold=False, today=oct9)
    assert staff.report_due_on == date(2026, 10, 23)  # staff on duty are patients of the facility (803.3(t))
    assert record(pump, occurred_on=oct8, outcome=Outcome.DEATH, affected=Affected.OTHER, hold=False, today=oct9).report_due_on is None
    assert record(pump, occurred_on=oct8, outcome=Outcome.INJURY, hold=False, today=oct9).report_due_on is None


def test_recording_refuses_facts_that_cannot_be(vent, today):
    for kwargs, field in ((dict(occurred_on=today + DAY), "occurred_on"),
                          (dict(occurred_on=today - 2 * DAY, aware_on=today - 3 * DAY), "aware_on"),
                          (dict(aware_on=today + DAY), "aware_on"),
                          (dict(occurred_on="2026-10-08"), "occurred_on"),
                          (dict(outcome=Outcome.INJURY, affected=Affected.NONE), "affected"),
                          (dict(outcome="bad"), "outcome"),
                          (dict(affected="everyone"), "affected"),
                          (dict(event_reference="MRN 12345"), "event_reference"),  # no spaces: never a name
                          (dict(event_reference="E" * 41), "event_reference"),
                          (dict(accessories="thrown_out"), "accessories"),
                          (dict(event_log="wiped"), "event_log")):
        refused(field, record, vent, **kwargs)
    vent.refresh_from_db()
    assert not Incident.objects.exists() and not WorkOrder.objects.exists() and not vent.incident_hold


@pytest.mark.parametrize("status", [AssetStatus.MISSING, AssetStatus.RETIRED])
def test_a_missing_or_retired_device_is_recorded_without_a_hold(pump, status):
    Asset.objects.filter(pk=pump.pk).update(status=status)
    refused("hold", record, pump, match="record the incident without holding it")
    incident = record(pump, hold=False)
    assert incident.work_order is None and not incident.holds.exists()


def test_adopting_takes_only_an_open_repair_on_the_device(vent, pump):
    def repair(asset):
        return create_work_order(asset=asset, type=WoType.REPAIR, priority=Priority.NORMAL, problem="Alarm")

    pm = create_work_order(asset=vent, type=WoType.PM, priority=Priority.NORMAL, problem="PM")
    theirs, finished, taken = repair(pump), repair(vent), repair(vent)
    done(finished)
    first = record(vent, work_order=taken, hold=False)
    for wo, words in ((pm, "not a repair"), (theirs, "is on CE-10002, not CE-10001"), (finished, "is completed"),
                      (taken, f"already the investigation of incident {first.number}")):
        refused("work_order", record, vent, work_order=wo, match=words)
    assert Incident.objects.count() == 1


def test_adopting_the_request_keeps_its_assignee_time_and_tag_out(vent, dept, techs, today):
    request = create_service_request(asset=vent, department=dept, problem="Alarm sounded, then stopped", urgency=Urgency.HIGH, tagged_out=True)
    wo = request.work_order
    assign(wo, technician=techs["dana"])
    add_labor(wo, hours="1.5", worked_on=today, technician=techs["dana"], by=None)
    count = WorkOrder.objects.count()
    incident = record(vent, work_order=wo)
    wo.refresh_from_db()
    assert WorkOrder.objects.count() == count and incident.work_order == wo and not incident.opened_work_order
    assert (wo.assigned_to, wo.tagged_out, wo.priority, wo.status, wo.labor_lines.count()) == (techs["dana"], True, Priority.HIGH, WoStatus.OPEN, 1)
    assert incident.occurred_on == wo.opened_on  # the day it was reported, unless the form says otherwise
    assert incident.holds.get().status_before == AssetStatus.OUT_OF_SERVICE  # tagged out with the request already
    assert inc.investigation_of(vent) == {wo.pk}


def test_recording_beside_an_open_tagged_out_request_opens_its_own_investigation(vent, dept):
    """The lock order: the incident's number, its work order (numbering), then the device's inspections and row, as a tagged-out
    portal request takes the numbering, then the device."""
    request = create_service_request(asset=vent, department=dept, problem="Screen flickers", urgency=Urgency.NORMAL, tagged_out=True)
    incident = record(vent)
    vent.refresh_from_db()
    assert incident.work_order != request.work_order and incident.opened_work_order
    assert vent.incident_hold and vent.status == AssetStatus.OUT_OF_SERVICE
    request.work_order.refresh_from_db()
    assert request.work_order.status == WoStatus.OPEN and request.work_order.tagged_out


def test_adopting_a_request_that_did_not_tag_the_device_out_holds_it_all_the_same(vent):
    wo = create_work_order(asset=vent, type=WoType.REPAIR, priority=Priority.NORMAL, problem="Intermittent alarm")
    incident = record(vent, work_order=wo)
    vent.refresh_from_db()
    wo.refresh_from_db()
    assert vent.status == AssetStatus.OUT_OF_SERVICE and vent.incident_hold and not wo.tagged_out
    assert incident.holds.get().status_before == AssetStatus.IN_SERVICE


def test_without_a_hold_an_investigation_only_when_asked(pump):
    assert record(pump, outcome=Outcome.NO_HARM, affected=Affected.NONE, hold=False).work_order is None
    probe = record(pump, outcome=Outcome.NO_HARM, affected=Affected.NONE, hold=False, open_work_order=True)
    wo = probe.work_order
    assert probe.opened_work_order and wo.tagged_out and wo.problem == inc.NOT_HELD_PROBLEM
    pump.refresh_from_db()
    assert not pump.incident_hold and pump.status == AssetStatus.OUT_OF_SERVICE  # tagged out, not held
    assert not IncidentHold.objects.exists()


def test_who_may_record_hold_and_open_a_work_order(vent, pump, make_user, users):
    for slug in ("analyst", "requester", "vendor"):
        with pytest.raises(PermissionDenied):
            record(pump, hold=False, by=users[slug])
    clerk = role_user(make_user, "safety", {Module.INCIDENTS: Level.EDIT, Module.EQUIPMENT: Level.VIEW, Module.WORKORDERS: Level.VIEW})
    with pytest.raises(PermissionDenied, match="Equipment Edit"):
        record(vent, by=clerk)
    with pytest.raises(PermissionDenied, match="Work orders Edit"):
        record(vent, hold=False, open_work_order=True, by=clerk)
    assert record(vent, hold=False, by=clerk).created_by == clerk
    tagger = role_user(make_user, "tagger", {Module.INCIDENTS: Level.EDIT, Module.EQUIPMENT: Level.EDIT, Module.WORKORDERS: Level.VIEW})
    with pytest.raises(PermissionDenied, match="Work orders Edit"):
        record(vent, by=tagger)  # holding opens a work order
    wo = create_work_order(asset=vent, type=WoType.REPAIR, priority=Priority.NORMAL, problem="Alarm")
    assert record(vent, work_order=wo, by=tagger).holds.exists()  # adopting opens nothing
    assert record(pump, by=users["technician"]).holds.exists()
    assert record(pump, by=users["director"]).holds.exists()


# --- holding another device ------------------------------------------------------------------------------------------------------

def test_another_part_of_the_system_is_held_under_the_suspects_investigation(vent, pump, users):
    tech = users["technician"]
    incident = record(vent, by=tech)
    hold = inc.hold_device(incident, pump, by=tech)
    pump.refresh_from_db()
    assert pump.incident_hold and pump.status == AssetStatus.OUT_OF_SERVICE and hold.status_before == AssetStatus.IN_SERVICE
    assert inc.investigation_of(pump) == {incident.work_order_id}
    assert WorkOrder.objects.count() == 1
    refused("asset", inc.hold_device, incident, pump, match="already holds CE-10002")
    with pytest.raises(PermissionDenied):
        inc.hold_device(incident, pump, by=users["analyst"])
    inc.release(hold, Release.KEEP_OUT)
    again = inc.hold_device(incident, pump)
    pump.refresh_from_db()
    assert again.pk == hold.pk and again.active and again.release == "" and again.status_before == AssetStatus.OUT_OF_SERVICE
    assert pump.incident_hold and reasons(again) == ["Held", "Kept out of service", "Held again"]


def test_a_missing_device_is_not_held_for_an_incident(vent, pump):
    incident = record(vent)
    Asset.objects.filter(pk=pump.pk).update(status=AssetStatus.MISSING)
    refused("asset", inc.hold_device, incident, pump, match="is missing: it cannot be held")
    pump.refresh_from_db()
    assert not pump.incident_hold and incident.holds.count() == 1


def test_holding_a_device_opens_the_investigation_an_incident_lacks(vent, make_user):
    incident = record(vent, outcome=Outcome.NO_HARM, affected=Affected.NONE, hold=False)
    tagger = role_user(make_user, "tagger", {Module.INCIDENTS: Level.EDIT, Module.EQUIPMENT: Level.EDIT, Module.WORKORDERS: Level.VIEW})
    with pytest.raises(PermissionDenied, match="Work orders Edit"):
        inc.hold_device(incident, vent, by=tagger)
    hold = inc.hold_device(incident, vent)
    assert incident.opened_work_order and incident.work_order.problem == inc.INVESTIGATION_PROBLEM and incident.work_order.tagged_out
    assert hold.status_before == AssetStatus.IN_SERVICE  # read before anything moved the device


def test_two_incidents_hold_one_device_until_both_let_go(vent):
    first, second = record(vent), record(vent)
    vent.refresh_from_db()
    assert vent.incident_hold and set(inc.holding_incidents(vent)) == {first, second}
    h1, h2 = first.holds.get(), second.holds.get()
    assert h2.status_before == AssetStatus.OUT_OF_SERVICE  # what the first hold made it
    inc.release(h1, Release.KEEP_OUT)
    vent.refresh_from_db()
    assert vent.incident_hold and vent.status == AssetStatus.OUT_OF_SERVICE
    assert inc.release_note(h1) == f"CE-10001 stays held: incident {second.number} still holds it."
    inc.release(h2, Release.KEEP_OUT)
    vent.refresh_from_db()
    assert not vent.incident_hold and vent.status == AssetStatus.OUT_OF_SERVICE
    assert inc.release_note(h2) == "" and inc.release_note(h1) == ""


# --- the facts -------------------------------------------------------------------------------------------------------------------

def test_a_technician_raises_the_outcome_and_moves_first_knew_earlier(pump, users, today, heard, django_capture_on_commit_callbacks):
    tech = users["technician"]
    incident = record(pump, outcome=Outcome.NO_HARM, hold=False, occurred_on=today - 3 * DAY, aware_on=today - DAY)
    assert incident.report_due_on is None
    with django_capture_on_commit_callbacks(execute=True):
        inc.update_facts(incident, outcome=Outcome.UNKNOWN, aware_on=today - 3 * DAY, event_reference="EV-2026-0412", by=tech)
    assert (incident.outcome, incident.aware_on, incident.event_reference) == (Outcome.UNKNOWN, today - 3 * DAY, "EV-2026-0412")
    assert incident.report_due_on == add_work_days(today - 3 * DAY, 10)
    assert heard == [incident.number]  # the clock started: the managers hear, as for one recorded with it
    with pytest.raises(PermissionDenied, match="Lowering"):
        inc.update_facts(incident, outcome=Outcome.INJURY, by=tech)
    with pytest.raises(PermissionDenied, match="later"):
        inc.update_facts(incident, aware_on=today - 2 * DAY, aware_reason=AwareChange.CORRECTION, by=tech)
    with pytest.raises(PermissionDenied, match="stops the report clock"):
        inc.update_facts(incident, affected=Affected.OTHER, by=tech)
    inc.update_facts(incident, affected=Affected.STAFF, accessories="kept", by=tech)  # still a patient of the facility: Edit
    assert incident.report_due_on == add_work_days(today - 3 * DAY, 10)
    for slug in ("analyst", "requester", "vendor"):
        with pytest.raises(PermissionDenied):
            inc.update_facts(incident, event_log="saved", by=users[slug])


def test_a_manager_lowers_the_outcome_and_moves_first_knew_later_with_a_reason(pump, users, today):
    mgr = users["manager"]
    incident = record(pump, outcome=Outcome.SERIOUS_INJURY, hold=False, occurred_on=today - 5 * DAY)
    refused("aware_reason", inc.update_facts, incident, aware_on=today - 2 * DAY, by=mgr)
    refused("aware_reason", inc.update_facts, incident, aware_on=today - 2 * DAY, aware_reason="felt like it", by=mgr)
    inc.update_facts(incident, aware_on=today - 2 * DAY, aware_reason=AwareChange.SERIOUS_LATER, by=mgr)
    assert incident.report_due_on == add_work_days(today - 2 * DAY, 10)
    latest = incident.history.order_by("-history_date").first()
    assert latest.history_change_reason == AwareChange.SERIOUS_LATER.label and latest.history_user == mgr
    inc.update_facts(incident, outcome=Outcome.NO_HARM, by=mgr)
    assert incident.report_due_on is None and not inc.needing_action().exists()


def test_once_decided_the_outcome_and_who_change_only_by_deciding_again(pump, today):
    incident = record(pump, outcome=Outcome.INJURY, hold=False, event_reference="EV-1", occurred_on=today - 4 * DAY)
    inc.decide(incident, outcome=Outcome.INJURY, basis=Basis.NOT_SERIOUS, decided_on=today - DAY, decided_by=DecidedBy.RISK)
    for fields, field in ((dict(outcome=Outcome.SERIOUS_INJURY), "outcome"), (dict(affected=Affected.STAFF), "affected"),
                          (dict(event_reference=""), "event_reference"), (dict(occurred_on=today), "occurred_on")):
        refused(field, inc.update_facts, incident, **fields)
    inc.update_facts(incident, event_reference="EV-2")  # corrected, never cleared
    saves = incident.history.count()
    inc.update_facts(incident, event_reference=" EV-2 ")
    assert incident.history.count() == saves  # nothing changed, nothing saved
    for field in ("finding", "report_due_on", "status"):
        with pytest.raises(ValidationError, match="cannot be changed here"):
            inc.update_facts(incident, **{field: "x"})


# --- the decision ----------------------------------------------------------------------------------------------------------------

def test_the_decision_and_its_rules(pump, users, today):
    mgr = users["manager"]
    incident = record(pump, hold=False, occurred_on=today - 3 * DAY)  # not known yet, a patient: the clock runs
    due = incident.report_due_on

    def refuse(field, **kwargs):
        args = dict(outcome=Outcome.SERIOUS_INJURY, basis=Basis.MAY_HAVE, decided_on=today, decided_by=DecidedBy.RISK) | kwargs
        refused(field, inc.decide, incident, by=mgr, **args)

    refuse("event_reference")  # the pointer to the deliberations first
    inc.update_facts(incident, event_reference="EV-77")
    refuse("outcome", outcome=Outcome.UNKNOWN)
    refuse("basis", outcome=Outcome.INJURY)  # a report only for a death or serious injury
    refuse("basis", basis=Basis.NOT_SERIOUS)
    refuse("basis", basis=Basis.NOT_PATIENT)  # the person was a patient
    refuse("basis", basis="hunch")
    refuse("decided_by", decided_by="nurse")
    refuse("decided_on", decided_on=today - 4 * DAY)
    refuse("decided_on", decided_on=today + DAY)
    refuse("decided_on", decided_on=None)
    for slug in ("technician", "analyst", "requester", "vendor"):
        with pytest.raises(PermissionDenied):
            inc.decide(incident, outcome=Outcome.SERIOUS_INJURY, basis=Basis.MAY_HAVE, decided_on=today, decided_by=DecidedBy.RISK, by=users[slug])
    inc.decide(incident, outcome=Outcome.SERIOUS_INJURY, basis=Basis.MAY_HAVE, decided_on=today, decided_by=DecidedBy.RISK, by=mgr)
    assert (incident.reportable, incident.basis, incident.recorded_by, incident.outcome, incident.report_due_on) == (
        True, Basis.MAY_HAVE, mgr, Outcome.SERIOUS_INJURY, due)
    assert incident.history.order_by("-history_date").first().history_change_reason == "Decided: reportable"
    inc.record_finding(incident, Finding.DEVICE_FAILURE)
    refuse("basis", outcome=Outcome.NO_HARM, basis=Basis.NO_SUGGESTION)  # a failure found suggests the device
    inc.decide(incident, outcome=Outcome.INJURY, basis=Basis.NOT_SERIOUS, decided_on=today, decided_by=DecidedBy.COMMITTEE, by=users["director"])
    assert incident.reportable is False and incident.report_due_on is None and not inc.needing_action().exists()


def test_a_visitor_is_not_a_patient_and_a_harmless_incident_decides_freely(pump, today):
    visitor = record(pump, outcome=Outcome.SERIOUS_INJURY, affected=Affected.OTHER, hold=False, event_reference="EV-9")
    assert visitor.report_due_on is None and inc.needs_decision(visitor)
    refused("basis", inc.decide, visitor, outcome=Outcome.SERIOUS_INJURY, basis=Basis.MAY_HAVE, decided_on=today, decided_by=DecidedBy.RISK)
    inc.decide(visitor, outcome=Outcome.SERIOUS_INJURY, basis=Basis.NOT_PATIENT, decided_on=today, decided_by=DecidedBy.RISK)
    assert visitor.reportable is False and not inc.needs_decision(visitor)
    quiet = record(pump, outcome=Outcome.NO_HARM, affected=Affected.NONE, hold=False)  # optional, and no event report needed
    assert not inc.needs_decision(quiet)
    inc.decide(quiet, outcome=Outcome.NO_HARM, basis=Basis.NOT_SERIOUS, decided_on=today, decided_by=DecidedBy.CE)
    assert quiet.reportable is False and quiet.decided_by == DecidedBy.CE


# --- the reports -----------------------------------------------------------------------------------------------------------------

def test_recording_the_reports(vent, pump, users, today):
    mgr = users["manager"]
    occurred = today - 6 * DAY
    death = record(pump, outcome=Outcome.DEATH, hold=False, occurred_on=occurred, event_reference="EV-1")
    inc.decide(death, outcome=Outcome.DEATH, basis=Basis.MAY_HAVE, decided_on=today, decided_by=DecidedBy.RISK)
    number = f"0123456789-{today.year}-0001"
    refused("report_number", inc.record_reports, death, fda_reported_on=today, report_number="")
    refused("report_number", inc.record_reports, death, fda_reported_on=today, report_number="12345-2026-1")
    refused("report_number", inc.record_reports, death, fda_reported_on=today, report_number=f"0123456789-{today.year - 1}-0001",
            match="the year the report went out")
    refused("fda_reported_on", inc.record_reports, death, report_number=number)  # neither date
    refused("fda_reported_on", inc.record_reports, death, fda_reported_on=occurred - DAY, report_number=number)
    refused("manufacturer_reported_on", inc.record_reports, death, manufacturer_reported_on=today + DAY, report_number=number)
    with pytest.raises(PermissionDenied):
        inc.record_reports(death, fda_reported_on=today, report_number=number, by=users["technician"])
    inc.record_reports(death, manufacturer_reported_on=today, report_number=number, by=mgr)
    assert inc.reports_missing(death) == ("fda",) and set(inc.needing_action()) == {death}
    inc.record_reports(death, fda_reported_on=today, manufacturer_reported_on=today, report_number=number, by=mgr)
    assert inc.reports_missing(death) == () and not inc.needing_action().exists()
    assert reasons(death)[-2:] == ["Reports recorded", "Reports corrected"]
    other = record(vent, hold=False, outcome=Outcome.SERIOUS_INJURY)
    refused("report_number", inc.record_reports, other, manufacturer_reported_on=today, report_number=number, match=death.number)


# --- the finding -----------------------------------------------------------------------------------------------------------------

def test_a_failure_found_clears_a_decision_that_the_device_was_not_suggested(pump, users, today, heard,
                                                                              django_capture_on_commit_callbacks):
    incident = record(pump, hold=False, occurred_on=today - 2 * DAY, event_reference="EV-5")
    inc.decide(incident, outcome=Outcome.SERIOUS_INJURY, basis=Basis.NO_SUGGESTION, decided_on=today, decided_by=DecidedBy.RISK)
    assert incident.reportable is False and incident.report_due_on is None
    for slug in ("analyst", "requester", "vendor"):
        with pytest.raises(PermissionDenied):
            inc.record_finding(incident, Finding.DEVICE_FAILURE, by=users[slug])
    refused("finding", inc.record_finding, incident, "broken")
    with django_capture_on_commit_callbacks(execute=True):
        result = inc.record_finding(incident, Finding.ACCESSORY_FAILURE, by=users["technician"])
    assert result.decision_cleared and incident.reportable is None
    assert (incident.basis, incident.decided_on, incident.decided_by, incident.recorded_by) == ("", None, "", None)
    assert incident.report_due_on == add_work_days(today - 2 * DAY, 10) and inc.needs_decision(incident)
    assert reasons(incident)[-1] == inc.DECIDE_AGAIN and heard == [incident.number]
    inc.decide(incident, outcome=Outcome.SERIOUS_INJURY, basis=Basis.MAY_HAVE, decided_on=today, decided_by=DecidedBy.RISK)
    again = inc.record_finding(incident, Finding.DEVICE_FAILURE)
    assert not again.decision_cleared and incident.reportable is True  # a decision that it may have contributed stands


# --- the investigation -----------------------------------------------------------------------------------------------------------

def test_opening_an_investigation_when_there_is_none_or_it_was_cancelled(vent, pump, make_user, users):
    incident = record(pump, outcome=Outcome.NO_HARM, affected=Affected.NONE, hold=False)
    clerk = role_user(make_user, "safety", {Module.INCIDENTS: Level.EDIT, Module.WORKORDERS: Level.VIEW})
    with pytest.raises(PermissionDenied, match="Work orders Edit"):
        inc.open_investigation(incident, by=clerk)
    with pytest.raises(PermissionDenied):
        inc.open_investigation(incident, by=users["analyst"])
    wo = inc.open_investigation(incident, by=users["technician"])
    assert incident.work_order == wo and incident.opened_work_order and wo.tagged_out and wo.problem == inc.NOT_HELD_PROBLEM
    refused("work_order", inc.open_investigation, incident, match=wo.number)
    change_status(wo, WoStatus.CANCELLED)
    second = inc.open_investigation(incident)
    assert second != wo and incident.work_order == second
    held = record(vent)
    change_status(held.work_order, WoStatus.CANCELLED)
    assert inc.open_investigation(held).problem == inc.INVESTIGATION_PROBLEM  # the device is held: the work order says so


# --- custody at the manufacturer -------------------------------------------------------------------------------------------------

def test_custody_at_the_manufacturer(vent, users, today):
    mgr = users["manager"]
    incident = record(vent, occurred_on=today - 5 * DAY, today=today - 3 * DAY)
    hold = incident.holds.get()
    with pytest.raises(PermissionDenied):
        inc.sent_to_manufacturer(hold, on=today, by=users["technician"])
    refused("back_on", inc.back_from_manufacturer, hold, on=today, match="not with the manufacturer")
    for on in (today - 4 * DAY, today + DAY, None):
        refused("sent_on", inc.sent_to_manufacturer, hold, on=on, by=mgr)
    inc.sent_to_manufacturer(hold, on=today - DAY, by=mgr)
    assert hold.with_manufacturer and hold.sent_on == today - DAY
    refused("sent_on", inc.sent_to_manufacturer, hold, on=today, match="record it back first")
    for kind in (Release.RETURN_TO_USE, Release.KEEP_OUT):
        refused("release", inc.release, hold, kind, by=mgr, match="with the manufacturer")
    refused("back_on", inc.back_from_manufacturer, hold, on=today - 2 * DAY)
    with pytest.raises(PermissionDenied):
        inc.back_from_manufacturer(hold, on=today, by=users["technician"])
    inc.back_from_manufacturer(hold, on=today, by=mgr)
    assert hold.back_on == today and not hold.with_manufacturer
    refused("release", inc.release, hold, Release.KEPT_BY_MANUFACTURER, match="went to the manufacturer first")
    inc.sent_to_manufacturer(hold, on=today)  # a second trip
    inc.release(hold, Release.KEPT_BY_MANUFACTURER, by=mgr)
    vent.refresh_from_db()
    assert not vent.incident_hold and vent.status == AssetStatus.OUT_OF_SERVICE
    assert reasons(hold) == ["Held", "Sent to the manufacturer", "Back from the manufacturer", "Sent to the manufacturer",
                             "Kept by the manufacturer"]


# --- the release -----------------------------------------------------------------------------------------------------------------

def test_return_to_use_waits_for_the_investigation_the_decision_the_reports_and_the_pm(vent, users, make_user, today):
    mgr = users["manager"]
    incident = record(vent, outcome=Outcome.SERIOUS_INJURY, event_reference="EV-3", occurred_on=today - 2 * DAY)
    hold = incident.holds.get()
    for slug in ("technician", "analyst"):
        with pytest.raises(PermissionDenied):
            inc.release(hold, Release.RETURN_TO_USE, by=users[slug])
    risk = role_user(make_user, "risk", {Module.INCIDENTS: Level.APPROVE, Module.EQUIPMENT: Level.VIEW})
    with pytest.raises(PermissionDenied, match="Equipment Edit"):
        inc.release(hold, Release.RETURN_TO_USE, by=risk)
    refused("release", inc.release, hold, "lost")
    refused("release", inc.release, hold, Release.IN_ERROR)  # only with "recorded in error"
    refused("release", inc.release, hold, Release.RETURN_TO_USE, by=mgr, match=f"Complete investigation {incident.work_order.number}")
    done(incident.work_order)
    refused("release", inc.release, hold, Release.RETURN_TO_USE, by=mgr, match=f"Decide whether {incident.number} was reportable")
    inc.decide(incident, outcome=Outcome.SERIOUS_INJURY, basis=Basis.MAY_HAVE, decided_on=today, decided_by=DecidedBy.RISK)
    refused("release", inc.release, hold, Release.RETURN_TO_USE, by=mgr, match="Record the report to the manufacturer")
    inc.record_reports(incident, fda_reported_on=today, report_number=f"0123456789-{today.year}-0007")  # the FDA's copy counts
    Asset.objects.filter(pk=vent.pk).update(next_pm_on=today - DAY)
    refused("release", inc.release, hold, Release.RETURN_TO_USE, by=mgr, match="release it kept out of service, do the PM")
    Asset.objects.filter(pk=vent.pk).update(next_pm_on=today)
    inc.release(hold, Release.RETURN_TO_USE, by=mgr)
    vent.refresh_from_db()
    assert not vent.incident_hold and vent.status == AssetStatus.IN_SERVICE and inc.release_note(hold) == ""
    assert (hold.released_on, hold.release, hold.released_by) == (today, Release.RETURN_TO_USE, mgr)
    with pytest.raises(ValidationError, match="released CE-10001"):
        inc.release(hold, Release.KEEP_OUT)


def test_a_cancelled_investigation_keeps_the_suspect_out_but_not_another_part(vent, pump):
    incident = record(vent, outcome=Outcome.NO_HARM, affected=Affected.NONE)
    part = inc.hold_device(incident, pump)
    change_status(incident.work_order, WoStatus.CANCELLED)
    refused("release", inc.release, incident.holds.get(asset=vent), Release.RETURN_TO_USE, match="was cancelled: open another")
    inc.release(part, Release.RETURN_TO_USE)  # the channel module is not what the investigation is about
    pump.refresh_from_db()
    assert not pump.incident_hold and pump.status == AssetStatus.IN_SERVICE


def test_returned_to_use_but_held_out_by_its_incoming_inspection_or_a_repair(vent, vent_model, today):
    new = waiting_device(vent_model, "NEW-9", today=today)
    incident = record(new, outcome=Outcome.NO_HARM, affected=Affected.NONE)
    done(incident.work_order)
    hold = inc.release(incident.holds.get(), Release.RETURN_TO_USE)
    new.refresh_from_db()
    assert not new.incident_hold and new.status == AssetStatus.OUT_OF_SERVICE and new.awaiting_inspection
    assert inc.release_note(hold) == "NEW-9 stays out of service until it passes its incoming inspection."
    second = record(vent, outcome=Outcome.NO_HARM, affected=Affected.NONE)
    repair = create_work_order(asset=vent, type=WoType.REPAIR, priority=Priority.NORMAL, problem="Cracked housing", tag_out=True)
    done(second.work_order)
    hold = inc.release(second.holds.get(), Release.RETURN_TO_USE)
    vent.refresh_from_db()
    assert not vent.incident_hold and vent.status == AssetStatus.OUT_OF_SERVICE
    assert inc.release_note(hold) == f"CE-10001 stays out of service until repair {repair.number} is completed."


def test_the_pms_missed_while_held_take_the_incident_hold_reason(vent, today):
    held_on = today - 20 * DAY

    def pm(due):
        return create_work_order(asset=vent, type=WoType.PM, priority=Priority.NORMAL, problem="PM", opened_on=today - 40 * DAY, due_on=due)

    before, missed, explained, upcoming = pm(today - 25 * DAY), pm(today - 5 * DAY), pm(today - 4 * DAY), pm(today + 3 * DAY)
    set_late_reason(explained, LateReason.STAFFING, today=today)
    incident = record(vent, outcome=Outcome.NO_HARM, affected=Affected.NONE, occurred_on=held_on, today=held_on)
    inc.release(incident.holds.get(), Release.KEEP_OUT, today=today)
    assert {wo.number: WorkOrder.objects.get(pk=wo.pk).late_reason for wo in (before, missed, explained, upcoming)} == {
        before.number: "", missed.number: LateReason.INCIDENT_HOLD, explained.number: LateReason.STAFFING, upcoming.number: ""}


# --- closing ---------------------------------------------------------------------------------------------------------------------

def test_closing_needs_everything_done_and_reopening_opens_it(vent, users, today):
    mgr = users["manager"]
    incident = record(vent, outcome=Outcome.DEATH, occurred_on=today - 3 * DAY)
    with pytest.raises(PermissionDenied):
        inc.close(incident, by=users["technician"])
    with pytest.raises(ValidationError) as e:
        inc.close(incident, by=mgr)
    assert set(e.value.message_dict) == {"holds", "work_order", "finding", "reportable"}
    inc.update_facts(incident, event_reference="EV-4")
    inc.decide(incident, outcome=Outcome.DEATH, basis=Basis.MAY_HAVE, decided_on=today, decided_by=DecidedBy.COMMITTEE)
    with pytest.raises(ValidationError) as e:
        inc.close(incident, by=mgr)
    assert {"fda_reported_on", "manufacturer_reported_on"} <= set(e.value.message_dict)
    inc.record_reports(incident, fda_reported_on=today, manufacturer_reported_on=today, report_number=f"0123456789-{today.year}-0002")
    inc.record_finding(incident, Finding.MET_SPECS)
    done(incident.work_order)
    inc.release(incident.holds.get(), Release.KEEP_OUT)
    inc.close(incident, by=mgr)
    assert (incident.status, incident.closed_on, incident.closed_by) == (Status.CLOSED, today, mgr)
    for write in (lambda: inc.update_facts(incident, event_log="saved"), lambda: inc.record_finding(incident, Finding.UTILITY),
                  lambda: inc.decide(incident, outcome=Outcome.DEATH, basis=Basis.MAY_HAVE, decided_on=today, decided_by=DecidedBy.RISK),
                  lambda: inc.close(incident), lambda: inc.hold_device(incident, vent), lambda: inc.open_investigation(incident),
                  lambda: inc.recorded_in_error(incident)):
        with pytest.raises(ValidationError, match="closed: reopen it first"):
            write()
    with pytest.raises(PermissionDenied):
        inc.reopen(incident, by=users["technician"])
    inc.reopen(incident, by=mgr)
    assert (incident.status, incident.closed_on, incident.closed_by) == (Status.OPEN, None, None)
    with pytest.raises(ValidationError, match="is open"):
        inc.reopen(incident)
    assert reasons(incident)[-2:] == ["Closed", "Reopened"]


# --- recorded in error -----------------------------------------------------------------------------------------------------------

def test_recorded_in_error_restores_the_devices_and_cancels_only_what_it_opened(vent, pump, dept, users):
    mgr = users["manager"]
    incident = record(vent)
    inc.hold_device(incident, pump)
    change_status(incident.work_order, WoStatus.IN_PROGRESS)  # the investigation had started
    with pytest.raises(PermissionDenied):
        inc.recorded_in_error(incident, by=users["technician"])
    inc.recorded_in_error(incident, by=mgr)
    assert incident.status == Status.IN_ERROR
    assert incident.work_order.status == WoStatus.CANCELLED
    for device in (vent, pump):
        device.refresh_from_db()
        assert not device.incident_hold and device.status == AssetStatus.IN_SERVICE
    assert {h.release for h in incident.holds.all()} == {Release.IN_ERROR}
    with pytest.raises(ValidationError, match="recorded in error"):
        inc.reopen(incident)
    with pytest.raises(ValidationError, match="recorded in error"):
        inc.recorded_in_error(incident)
    request = create_service_request(asset=vent, department=dept, problem="Leak", urgency=Urgency.NORMAL, tagged_out=True)
    wrong = record(vent, work_order=request.work_order)
    inc.recorded_in_error(wrong)
    request.work_order.refresh_from_db()
    vent.refresh_from_db()
    assert request.work_order.status == WoStatus.OPEN  # the request goes on
    assert vent.status == AssetStatus.OUT_OF_SERVICE and not vent.incident_hold  # tagged out with it, as before the hold
    assert record(vent, work_order=request.work_order).work_order == request.work_order  # free for the right incident


def test_recorded_in_error_never_puts_a_waiting_device_in_use_nor_ends_another_incidents_hold(vent, vent_model, today):
    new = waiting_device(vent_model, "NEW-7", today=today)
    eq.use_before_inspection(new, UseBeforeInspection.EMERGENCY, today=today)
    on_new = record(new)
    assert on_new.holds.get().status_before == AssetStatus.IN_SERVICE
    inc.recorded_in_error(on_new)
    new.refresh_from_db()
    assert not new.incident_hold and new.status == AssetStatus.OUT_OF_SERVICE  # still waiting: out until it passes
    first, second = record(vent), record(vent)
    inc.recorded_in_error(second)
    vent.refresh_from_db()
    assert vent.incident_hold and vent.status == AssetStatus.OUT_OF_SERVICE  # the first still holds it
    inc.recorded_in_error(first)
    vent.refresh_from_db()
    assert not vent.incident_hold and vent.status == AssetStatus.IN_SERVICE


# --- history and levels ----------------------------------------------------------------------------------------------------------

def test_the_incidents_history_has_its_reasons_and_the_devices_never_its_number(vent, users, today):
    tech, mgr = users["technician"], users["manager"]
    incident = record(vent, by=tech, occurred_on=today - DAY, event_reference="EV-8")
    inc.record_finding(incident, Finding.MET_SPECS, by=tech)
    inc.decide(incident, outcome=Outcome.NO_HARM, basis=Basis.NOT_SERIOUS, decided_on=today, decided_by=DecidedBy.RISK, by=mgr)
    done(incident.work_order)
    hold = inc.release(incident.holds.get(), Release.RETURN_TO_USE, by=mgr)
    inc.close(incident, by=mgr)
    rows = list(incident.history.order_by("history_date"))
    assert [r.history_change_reason for r in rows] == ["Recorded", "Finding recorded", "Decided: not reportable", "Closed"]
    assert [r.history_user for r in rows] == [tech, tech, mgr, mgr]
    assert reasons(hold) == ["Held", "Returned to use"]
    device_reasons = [r.history_change_reason or "" for r in vent.history.all()]
    assert {"Held for an incident investigation", "Hold released"} <= set(device_reasons)
    assert not any("IN-" in reason for reason in device_reasons)


@pytest.mark.parametrize("slug", ["technician", "analyst", "requester", "vendor"])
def test_only_approve_decides_releases_and_closes(vent, pump, users, today, slug):
    user = users[slug]
    incident = record(vent, event_reference="EV-1")
    hold = incident.holds.get()
    writes = [lambda: inc.decide(incident, outcome=Outcome.NO_HARM, basis=Basis.NOT_SERIOUS, decided_on=today, decided_by=DecidedBy.CE, by=user),
              lambda: inc.record_reports(incident, fda_reported_on=today, report_number=f"0123456789-{today.year}-0001", by=user),
              lambda: inc.sent_to_manufacturer(hold, on=today, by=user), lambda: inc.back_from_manufacturer(hold, on=today, by=user),
              lambda: inc.release(hold, Release.KEEP_OUT, by=user), lambda: inc.close(incident, by=user), lambda: inc.reopen(incident, by=user),
              lambda: inc.recorded_in_error(incident, by=user)]
    if slug != "technician":  # and View or a scoped account records nothing either
        writes += [lambda: record(pump, hold=False, by=user), lambda: inc.hold_device(incident, pump, by=user),
                   lambda: inc.update_facts(incident, event_log="saved", by=user), lambda: inc.record_finding(incident, Finding.MET_SPECS, by=user),
                   lambda: inc.open_investigation(incident, by=user)]
    for write in writes:
        with pytest.raises(PermissionDenied):
            write()
    incident.refresh_from_db()
    assert incident.status == Status.OPEN and incident.reportable is None and hold.active


@needs_postgres
def test_the_writes_under_the_policies(vent, pump, tenant, users, today):
    """As the runtime role: numbering, the hold, the decision, the release, and closing read and write in the facility the context set."""
    tech, mgr = users["technician"], users["manager"]
    as_app_role()
    with tenant_context(tenant):
        incident = record(vent, by=tech, event_reference="EV-1")
        inc.hold_device(incident, pump, by=tech)
        inc.decide(incident, outcome=Outcome.NO_HARM, basis=Basis.NOT_SERIOUS, decided_on=today, decided_by=DecidedBy.RISK, by=mgr)
        inc.record_finding(incident, Finding.MET_SPECS, by=tech)
        done(incident.work_order)
        for hold in incident.holds.all():
            inc.release(hold, Release.RETURN_TO_USE, by=mgr)
        inc.close(incident, by=mgr)
        assert Incident.objects.get().status == Status.CLOSED
        assert not Asset.objects.filter(incident_hold=True).exists()
