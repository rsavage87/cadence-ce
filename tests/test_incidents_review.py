"""
Slice 28 review fixes in the incident services (apps.incidents.services, apps.incidents.models, apps.workorders.services.lock_work and
its callers): who was affected is decided again with the outcome, and a decision that starts the clock tells the managers once; a
harm outcome is never decided with no one affected, and a decision on an incident recorded with its clock running always points to
the event report; an incident recorded in error gives up its report number and days (its history keeps them) to the incident
recorded on the right device; recorded in error is never a side door around Equipment Edit (a device back in use) or Work orders Edit
(the work order it opened); a year typed with a slip is refused, and every year that prints alike shares one counter; a report number
takes ASCII digits only. On PostgreSQL, the lock order: every writer of a device's work takes its open work orders in number order
before any one of them (each in its own device's order, and the facility's work order numbering never after them), so two writes on
one device take turns, never wait in a circle.
"""
import threading
import time
from datetime import date, timedelta

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import OperationalError, connection
from django.utils import timezone
from incident_fixtures import make_incident
from pg_helpers import needs_postgres

from apps.accounts.models import Level, Module, Role
from apps.core.models import Sequence
from apps.core.workdays import add_work_days
from apps.equipment.models import Asset, AssetStatus
from apps.incidents import services as inc
from apps.incidents.models import (
    REPORT_NUMBER_VALIDATOR,
    Affected,
    AwareChange,
    Basis,
    DecidedBy,
    Finding,
    Incident,
    IncidentHold,
    Outcome,
    Release,
    Status,
)
from apps.tenants.context import tenant_context
from apps.workorders import completion
from apps.workorders import services as wo_services
from apps.workorders.completion import complete_work_order
from apps.workorders.models import LateReason, PmResult, Priority, Source, Urgency, WorkOrder, WoStatus, WoType
from apps.workorders.services import change_status, create_service_request, create_work_order

DAY = timedelta(days=1)
OCT8 = date(2026, 10, 8)  # a fixed "today" where the year matters (passed to the services, never read from the clock)
DECIDERS = ["director@riverside.example", "manager@riverside.example"]
HELD = "is held as evidence for an incident investigation"


@pytest.fixture
def today(ctx):
    return timezone.localdate()  # the facility's day, as the services read it


@pytest.fixture
def people(ctx, make_user):
    """The people who decide (Incidents Approve), each with an address: they get the incident emails."""
    out = {slug: make_user(slug) for slug in ("director", "manager")}
    for user in out.values():
        user.email = user.username
        user.save()
    return out


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
    with pytest.raises(ValidationError) as e:
        write(*args, **kwargs)
    assert field in e.value.message_dict, e.value.message_dict
    if match is not None:
        assert any(match in m for m in e.value.message_dict[field]), e.value.message_dict
    return e.value.message_dict


def to(mailoutbox) -> list[str]:
    return sorted(m.to[0] for m in mailoutbox)


# --- [1]/[7]: who was affected, decided again --------------------------------------------------------------------------------------

def test_a_visitor_found_to_be_staff_on_duty_is_decided_again_and_the_clock_starts(pump, people, today, mailoutbox,
                                                                                  django_capture_on_commit_callbacks):
    mgr = people["manager"]
    occurred = today - 2 * DAY
    with django_capture_on_commit_callbacks(execute=True):
        incident = record(pump, outcome=Outcome.SERIOUS_INJURY, affected=Affected.OTHER, event_reference="EV-1", hold=False,
                          occurred_on=occurred)
        inc.decide(incident, outcome=Outcome.SERIOUS_INJURY, basis=Basis.NOT_PATIENT, decided_on=today, decided_by=DecidedBy.RISK, by=mgr)
    assert (incident.reportable, incident.report_due_on) == (False, None) and mailoutbox == []  # a visitor: no clock, nobody told
    # The facility learns the person was a staff member on duty (803.3(t)). The facts' edit says where that is done ...
    refused("affected", inc.update_facts, incident, affected=Affected.STAFF, by=mgr, match="Decide again takes both")
    # ... and deciding again without saying so still reads a visitor.
    refused("basis", inc.decide, incident, outcome=Outcome.SERIOUS_INJURY, basis=Basis.MAY_HAVE, decided_on=today, decided_by=DecidedBy.RISK,
            by=mgr, match="was not one")
    with django_capture_on_commit_callbacks(execute=True):
        inc.decide(incident, outcome=Outcome.SERIOUS_INJURY, affected=Affected.STAFF, basis=Basis.MAY_HAVE, decided_on=today,
                   decided_by=DecidedBy.COMMITTEE, by=mgr)
    assert (incident.affected, incident.reportable, incident.basis, incident.recorded_by) == (Affected.STAFF, True, Basis.MAY_HAVE, mgr)
    assert incident.report_due_on == add_work_days(occurred, inc.REPORT_WORK_DAYS)
    assert list(inc.needing_action()) == [incident] and inc.reports_missing(incident) == ("manufacturer",)
    assert inc.clock(incident, today).pending
    assert to(mailoutbox) == DECIDERS and all(incident.number in m.subject for m in mailoutbox)  # the clock started: the managers hear
    # Once: deciding again with the clock running, or stopping it and starting it again, emails nobody a second time.
    with django_capture_on_commit_callbacks(execute=True):
        inc.decide(incident, outcome=Outcome.SERIOUS_INJURY, basis=Basis.MAY_HAVE, decided_on=today, decided_by=DecidedBy.RISK, by=mgr)
        inc.decide(incident, outcome=Outcome.SERIOUS_INJURY, affected=Affected.OTHER, basis=Basis.NOT_PATIENT, decided_on=today,
                   decided_by=DecidedBy.RISK, by=mgr)
        assert incident.report_due_on is None
        inc.decide(incident, outcome=Outcome.SERIOUS_INJURY, affected=Affected.STAFF, basis=Basis.MAY_HAVE, decided_on=today,
                   decided_by=DecidedBy.RISK, by=mgr)
    assert len(mailoutbox) == 2 and incident.report_due_on == add_work_days(occurred, inc.REPORT_WORK_DAYS)
    # The history keeps each decision with who was affected as it then stood.
    rows = list(incident.history.order_by("history_date"))
    assert [(r.history_change_reason, r.affected) for r in rows] == [
        ("Recorded", Affected.OTHER), ("Decided: not reportable", Affected.OTHER), ("Decided: reportable", Affected.STAFF),
        ("Decided: reportable", Affected.STAFF), ("Decided: not reportable", Affected.OTHER), ("Decided: reportable", Affected.STAFF)]


def test_a_near_miss_that_turned_out_to_be_a_death_is_decided_again_with_both(pump, people, today, mailoutbox,
                                                                              django_capture_on_commit_callbacks):
    mgr = people["manager"]
    occurred = today - 3 * DAY
    near_miss = record(pump, outcome=Outcome.NO_HARM, affected=Affected.NONE, hold=False, occurred_on=occurred)
    inc.decide(near_miss, outcome=Outcome.NO_HARM, basis=Basis.NOT_SERIOUS, decided_on=today - 2 * DAY, decided_by=DecidedBy.CE, by=mgr)
    assert (near_miss.reportable, near_miss.report_due_on) == (False, None)
    # A patient's death becomes known: the facts' edit refuses both and says where they change.
    with pytest.raises(ValidationError) as e:
        inc.update_facts(near_miss, outcome=Outcome.DEATH, affected=Affected.PATIENT, by=mgr)
    assert set(e.value.message_dict) == {"outcome", "affected"} and "Decide again takes both" in e.value.message_dict["affected"][0]
    # Who was affected as recorded (no one) never goes with a death; the deliberations' pointer comes first.
    refused("affected", inc.decide, near_miss, outcome=Outcome.DEATH, basis=Basis.MAY_HAVE, decided_on=today, decided_by=DecidedBy.RISK,
            match="choose who")
    refused("event_reference", inc.decide, near_miss, outcome=Outcome.DEATH, affected=Affected.PATIENT, basis=Basis.MAY_HAVE,
            decided_on=today, decided_by=DecidedBy.RISK)
    inc.update_facts(near_miss, event_reference="EV-12", by=mgr)
    inc.update_facts(near_miss, aware_on=today - DAY, aware_reason=AwareChange.SERIOUS_LATER, by=mgr)  # known later: the clock's day
    with django_capture_on_commit_callbacks(execute=True):
        inc.decide(near_miss, outcome=Outcome.DEATH, affected=Affected.PATIENT, basis=Basis.MAY_HAVE, decided_on=today,
                   decided_by=DecidedBy.COMMITTEE, by=mgr)
    assert (near_miss.outcome, near_miss.affected, near_miss.reportable) == (Outcome.DEATH, Affected.PATIENT, True)
    assert near_miss.report_due_on == add_work_days(today - DAY, inc.REPORT_WORK_DAYS)
    assert inc.reports_missing(near_miss) == ("fda", "manufacturer") and list(inc.needing_action()) == [near_miss]
    assert to(mailoutbox) == DECIDERS
    with pytest.raises(ValidationError) as e:
        inc.close(near_miss, by=mgr)
    assert {"fda_reported_on", "manufacturer_reported_on"} <= set(e.value.message_dict)


def test_a_patient_found_to_be_a_visitor_is_decided_again_and_closes_without_a_report(vent, people, make_user, today):
    mgr = people["manager"]
    incident = record(vent, event_reference="EV-1", occurred_on=today - 2 * DAY)
    inc.decide(incident, outcome=Outcome.SERIOUS_INJURY, basis=Basis.MAY_HAVE, decided_on=today, decided_by=DecidedBy.RISK, by=mgr)
    assert inc.reports_missing(incident) == ("manufacturer",)
    refused("affected", inc.update_facts, incident, affected=Affected.OTHER, by=mgr, match="Decide again takes both")
    refused("basis", inc.decide, incident, outcome=Outcome.SERIOUS_INJURY, basis=Basis.NOT_PATIENT, decided_on=today,
            decided_by=DecidedBy.RISK, by=mgr, match="is patient, not a visitor")
    with pytest.raises(PermissionDenied):  # deciding again is the decision's level
        inc.decide(incident, outcome=Outcome.SERIOUS_INJURY, affected=Affected.OTHER, basis=Basis.NOT_PATIENT, decided_on=today,
                   decided_by=DecidedBy.RISK, by=make_user("technician"))
    inc.decide(incident, outcome=Outcome.SERIOUS_INJURY, affected=Affected.OTHER, basis=Basis.NOT_PATIENT, decided_on=today,
               decided_by=DecidedBy.RISK, by=mgr)
    assert (incident.affected, incident.reportable, incident.report_due_on) == (Affected.OTHER, False, None)
    assert inc.reports_missing(incident) == () and not inc.needing_action().exists()
    inc.record_finding(incident, Finding.MET_SPECS, by=mgr)
    done(incident.work_order)
    inc.release(incident.holds.get(), Release.KEEP_OUT, by=mgr)
    inc.close(incident, by=mgr)
    assert incident.status == Status.CLOSED


def test_who_was_affected_is_one_of_the_choices(pump, today):
    incident = record(pump, hold=False, event_reference="EV-1")
    for affected in ("", "everyone", "  "):
        refused("affected", inc.decide, incident, outcome=Outcome.SERIOUS_INJURY, affected=affected, basis=Basis.MAY_HAVE, decided_on=today,
                decided_by=DecidedBy.RISK, match="Choose who was affected")
    incident.refresh_from_db()
    assert (incident.affected, incident.reportable) == (Affected.PATIENT, None)
    inc.decide(incident, outcome=Outcome.SERIOUS_INJURY, affected=f" {Affected.STAFF} ", basis=Basis.MAY_HAVE, decided_on=today,
               decided_by=DecidedBy.RISK)
    assert incident.affected == Affected.STAFF


# --- [2]: a harm outcome needs someone affected ------------------------------------------------------------------------------------

@pytest.mark.parametrize("outcome", [Outcome.DEATH, Outcome.SERIOUS_INJURY, Outcome.INJURY])
def test_a_harm_outcome_is_never_decided_with_no_one_affected(pump, today, outcome):
    incident = record(pump, outcome=Outcome.NO_HARM, affected=Affected.NONE, hold=False)
    for basis in (Basis.NO_SUGGESTION, Basis.MAY_HAVE, Basis.NOT_SERIOUS, Basis.NOT_PATIENT):
        refused("affected", inc.decide, incident, outcome=outcome, basis=basis, decided_on=today, decided_by=DecidedBy.RISK,
                match="Someone was harmed, or may have been: choose who")
    incident.refresh_from_db()
    assert (incident.outcome, incident.affected, incident.reportable, incident.basis) == (Outcome.NO_HARM, Affected.NONE, None, "")
    # With who was affected, it is a harm incident: its event report first (the deliberations), then the decision.
    refused("event_reference", inc.decide, incident, outcome=outcome, affected=Affected.PATIENT, basis=Basis.NO_SUGGESTION,
            decided_on=today, decided_by=DecidedBy.RISK)
    inc.update_facts(incident, event_reference="EV-20")
    inc.decide(incident, outcome=outcome, affected=Affected.PATIENT, basis=Basis.NO_SUGGESTION, decided_on=today, decided_by=DecidedBy.RISK)
    assert (incident.outcome, incident.affected, incident.reportable, incident.report_due_on) == (outcome, Affected.PATIENT, False, None)


def test_a_decision_that_stops_a_running_clock_still_points_to_the_event_report(pump, today):
    """An incident recorded with its clock running (not known yet, a patient) was a harm incident as recorded: whatever the decision
    finds, the deliberations behind it are in the event report (decide's docstring: the recorded or final outcome, with someone
    affected), as they were before decide took who was affected. Saying no one was affected after all does not make it optional."""
    incident = record(pump, hold=False, occurred_on=today - DAY)
    assert incident.report_due_on is not None
    refused("event_reference", inc.decide, incident, outcome=Outcome.NO_HARM, basis=Basis.NOT_SERIOUS, decided_on=today,
            decided_by=DecidedBy.RISK)  # who was affected as recorded
    refused("event_reference", inc.decide, incident, outcome=Outcome.NO_HARM, affected=Affected.NONE, basis=Basis.NOT_SERIOUS,
            decided_on=today, decided_by=DecidedBy.RISK)
    incident.refresh_from_db()
    assert (incident.reportable, incident.affected) == (None, Affected.PATIENT) and incident.report_due_on is not None
    inc.update_facts(incident, event_reference="EV-40")
    inc.decide(incident, outcome=Outcome.NO_HARM, affected=Affected.NONE, basis=Basis.NOT_SERIOUS, decided_on=today, decided_by=DecidedBy.RISK)
    assert (incident.reportable, incident.affected, incident.report_due_on) == (False, Affected.NONE, None)


# --- [3]: recorded in error gives up its report number --------------------------------------------------------------------------

def test_recorded_in_error_gives_its_report_number_to_the_incident_on_the_right_device(vent, pump, people, today):
    mgr = people["manager"]
    occurred = today - 2 * DAY
    number = f"0123456789-{today.year}-0001"
    wrong = record(pump, outcome=Outcome.SERIOUS_INJURY, event_reference="EV-30", occurred_on=occurred)  # the wrong pump
    inc.decide(wrong, outcome=Outcome.SERIOUS_INJURY, basis=Basis.MAY_HAVE, decided_on=today, decided_by=DecidedBy.RISK, by=mgr)
    inc.record_reports(wrong, fda_reported_on=today, manufacturer_reported_on=today, report_number=number, by=mgr)
    inc.recorded_in_error(wrong, by=mgr)
    assert (wrong.status, wrong.report_number, wrong.fda_reported_on, wrong.manufacturer_reported_on) == (Status.IN_ERROR, "", None, None)
    last, before = wrong.history.order_by("-history_date")[:2]
    assert (last.history_change_reason, last.report_number) == ("Recorded in error", "")
    assert (before.report_number, before.fda_reported_on, before.manufacturer_reported_on) == (number, today, today)  # the history keeps them
    right = record(vent, outcome=Outcome.SERIOUS_INJURY, event_reference="EV-30", occurred_on=occurred)
    inc.decide(right, outcome=Outcome.SERIOUS_INJURY, basis=Basis.MAY_HAVE, decided_on=today, decided_by=DecidedBy.RISK, by=mgr)
    inc.record_reports(right, manufacturer_reported_on=today, report_number=number, by=mgr)
    inc.record_finding(right, Finding.DEVICE_FAILURE, by=mgr)
    done(right.work_order)
    inc.release(right.holds.get(), Release.KEEP_OUT, by=mgr)
    inc.close(right, by=mgr)
    assert (right.status, right.report_number) == (Status.CLOSED, number)
    assert Incident.objects.get(report_number=number) == right


# --- [6]: recorded in error is no side door --------------------------------------------------------------------------------------

def _state(incident) -> tuple:
    """Everything recorded in error would change: the incident, its holds, the devices, its work order, and their histories."""
    incident.refresh_from_db()
    holds = list(IncidentHold.objects.filter(incident=incident).order_by("asset__tag").values_list("asset__tag", "released_on", "release"))
    devices = list(Asset.objects.filter(incident_holds__incident=incident).order_by("tag").values_list("tag", "status", "incident_hold"))
    wo = WorkOrder.objects.get(pk=incident.work_order_id)
    return (incident.status, incident.report_number, incident.fda_reported_on, holds, devices, wo.status, incident.history.count(),
            wo.status_history.count())


def test_recorded_in_error_needs_equipment_edit_to_put_a_device_back_in_use_and_work_orders_edit_to_cancel(
        vent, pump, pump_model, dept, make_user, people, today):
    risk = role_user(make_user, "risk", {Module.INCIDENTS: Level.APPROVE, Module.EQUIPMENT: Level.VIEW, Module.WORKORDERS: Level.VIEW})
    tagger = role_user(make_user, "tagger", {Module.INCIDENTS: Level.APPROVE, Module.EQUIPMENT: Level.EDIT, Module.WORKORDERS: Level.VIEW})
    in_use = record(vent)  # in service when held; the incident opened its investigation
    inc.record_reports(in_use, fda_reported_on=today, report_number=f"0123456789-{today.year}-0003")
    Asset.objects.filter(pk=pump.pk).update(status=AssetStatus.ON_LOAN)
    on_loan = record(pump)
    spare = Asset.objects.create(tag="CE-10003", device_model=pump_model, department=dept, status=AssetStatus.OUT_OF_SERVICE)
    out = record(spare)  # out of service when held: nothing goes back in use, but its work order would be cancelled
    before = {i.pk: _state(i) for i in (in_use, on_loan, out)}
    for incident, user, words in (
            (in_use, risk, "puts CE-10001 back in use: that needs Incidents Approve and Equipment Edit."),
            (on_loan, risk, "puts CE-10002 back in use: that needs Incidents Approve and Equipment Edit."),
            (out, risk, f"cancels {out.work_order.number}: that needs Work orders Edit."),
            (in_use, tagger, f"cancels {in_use.work_order.number}: that needs Work orders Edit."),
            (out, tagger, f"cancels {out.work_order.number}: that needs Work orders Edit.")):
        with pytest.raises(PermissionDenied) as e:
            inc.recorded_in_error(incident, by=user)
        assert str(e.value) == f"Marking {incident.number} recorded in error {words}"
    assert {i.pk: _state(i) for i in (in_use, on_loan, out)} == before  # nothing changed
    # The manager has Equipment Edit and Work orders Approve.
    inc.recorded_in_error(in_use, by=people["manager"])
    vent.refresh_from_db()
    in_use.work_order.refresh_from_db()
    assert (in_use.status, in_use.report_number, vent.status, vent.incident_hold, in_use.work_order.status) == (
        Status.IN_ERROR, "", AssetStatus.IN_SERVICE, False, WoStatus.CANCELLED)
    # Nothing back in use and nothing to cancel (an adopted request on a tagged-out device): Incidents Approve is enough.
    request = create_service_request(asset=vent, department=dept, problem="Leak", urgency=Urgency.NORMAL, tagged_out=True)
    adopted = record(vent, work_order=request.work_order)
    assert adopted.holds.get().status_before == AssetStatus.OUT_OF_SERVICE
    inc.recorded_in_error(adopted, by=risk)
    vent.refresh_from_db()
    request.work_order.refresh_from_db()
    assert (adopted.status, vent.status, vent.incident_hold, request.work_order.status) == (
        Status.IN_ERROR, AssetStatus.OUT_OF_SERVICE, False, WoStatus.OPEN)


# --- [13]: the year ----------------------------------------------------------------------------------------------------------------

def test_a_slip_of_the_year_is_refused_and_years_that_print_alike_share_one_counter(vent, pump):
    for typo in (date(1926, 10, 8), date(26, 10, 8), date(1999, 12, 31)):
        refused("occurred_on", record, pump, occurred_on=typo, aware_on=OCT8, hold=False, today=OCT8, match="from 2000 on")
        refused("occurred_on", record, pump, occurred_on=typo, hold=False, today=OCT8, match="from 2000 on")
    refused("occurred_on", record, pump, occurred_on=date(2099, 10, 8), hold=False, today=OCT8, match="not a day to come")
    assert not Incident.objects.exists() and not Sequence.objects.filter(key__startswith="incident").exists()
    numbers = [record(pump, occurred_on=day, hold=False, today=OCT8).number
               for day in (date(2025, 12, 31), date(2026, 1, 2), inc.EARLIEST, OCT8)]
    assert numbers == ["IN-25-0001", "IN-26-0001", "IN-00-0001", "IN-26-0002"]
    assert make_incident(pump, occurred_on=OCT8, hold=False, open_work_order=False).number == "IN-26-0003"  # the fixture's too
    assert record(vent, occurred_on=OCT8, today=OCT8).number == "IN-26-0004"
    assert sorted(Sequence.objects.filter(key__startswith="incident").values_list("key", "value")) == [
        ("incident-00", 1), ("incident-25", 1), ("incident-26", 4)]
    incident = Incident.objects.get(number="IN-26-0001")
    refused("occurred_on", inc.update_facts, incident, occurred_on=date(1926, 1, 2), today=OCT8, match="from 2000 on")


def test_the_api_answers_a_slip_of_the_year_with_a_400(client, pump, make_user):
    client.force_login(make_user("director"))
    first = client.post("/api/v1/incidents/", {"asset": "CE-10002", "outcome": "no_harm", "affected": "none", "hold": False},
                        content_type="application/json")
    assert first.status_code == 201, first.content
    for day in ("1926-10-08", "0026-01-02"):
        r = client.post("/api/v1/incidents/", {"asset": "CE-10002", "outcome": "no_harm", "affected": "none", "hold": False, "occurred_on": day},
                        content_type="application/json")
        assert r.status_code == 400 and "occurred_on" in r.json(), (day, r.status_code, r.content)
    assert Incident.objects.count() == 1


# --- [16]: the report number's digits ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("number", ["٠١٢٣٤٥٦٧٨٩-٢٠٢٦-٠٠٠١",  # Arabic-Indic
                                    "０１２３４５６７８９-２０２６-０００１",  # fullwidth, as an IME types them
                                    "0123456789-２０２６-0001",  # one part
                                    "०१२३४५६७८९-2026-0001"])  # Devanagari
def test_a_report_number_takes_ascii_digits_only(pump, number):
    incident = record(pump, outcome=Outcome.DEATH, hold=False, event_reference="EV-1", occurred_on=OCT8 - 7 * DAY, today=OCT8)
    inc.decide(incident, outcome=Outcome.DEATH, basis=Basis.MAY_HAVE, decided_on=OCT8, decided_by=DecidedBy.RISK, today=OCT8)
    refused("report_number", inc.record_reports, incident, fda_reported_on=OCT8, report_number=number, today=OCT8, match="10-digit number")
    with pytest.raises(ValidationError):
        REPORT_NUMBER_VALIDATOR(number)  # the field's own validator (forms, the admin)
    incident.refresh_from_db()
    assert (incident.report_number, incident.fda_reported_on) == ("", None)
    inc.record_reports(incident, fda_reported_on=OCT8, report_number="0123456789-2026-0001", today=OCT8)
    assert incident.report_number == "0123456789-2026-0001"


# --- [4]/[5]: the lock order, on PostgreSQL --------------------------------------------------------------------------------------
#
# Each race runs two writes on one device, each in its own thread and connection. The first stops at a point inside its write (a
# Pause: after it has taken its first work order locks, the point where, before the fix, it held one work order and went on to wait
# for a lower number); the second then runs until PostgreSQL shows it waiting on a lock; then the first goes on. Taking the device's
# open work orders in number order first, the second waits for the first and both finish. Out of order, PostgreSQL finds the circle
# and one of them fails with "deadlock detected" (OperationalError) after its deadlock_timeout (1 s).

WAIT = 15  # seconds: never reached when the order is right


class Pause:
    """Stops the thread named "first" the first time it is called, until `go` is set."""

    def __init__(self):
        self.reached, self.go, self._used = threading.Event(), threading.Event(), False

    def __call__(self):
        if threading.current_thread().name == "first" and not self._used:
            self._used = True
            self.reached.set()
            self.go.wait(WAIT)


def pause_at(monkeypatch, module, name, *, after=False) -> Pause:
    """A Pause in `module.name`: before it runs, or after it returns."""
    pause, original = Pause(), getattr(module, name)

    def paused(*args, **kwargs):
        if not after:
            pause()
        result = original(*args, **kwargs)
        if after:
            pause()
        return result

    monkeypatch.setattr(module, name, paused)
    return pause


def _waits_on_a_lock(thread) -> bool:
    """Whether a session of the test database waits on a lock (PostgreSQL's pg_stat_activity) before `thread` ends."""
    deadline = time.monotonic() + WAIT
    with connection.cursor() as cur:
        while thread.is_alive() and time.monotonic() < deadline:
            cur.execute("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND wait_event_type = 'Lock'")
            if cur.fetchone()[0]:
                return True
            time.sleep(0.01)
    return False


def race(tenant, pause: Pause, first, second, *, must_wait=True) -> dict:
    """Run `first` to `pause`, then `second` until it waits on a lock (or ends: only when not `must_wait`), then let `first` go on.
    Returns {"first" / "second": the exception it raised} (empty when both finished)."""
    errors = {}

    def run(call):
        try:
            with connection.cursor() as cur:
                cur.execute("SET lock_timeout = '20s'")  # a wrong order fails, never hangs the suite
            with tenant_context(tenant):
                call()
        except Exception as e:  # noqa: BLE001 - reported to the test
            errors[threading.current_thread().name] = e
        finally:
            connection.close()

    one = threading.Thread(target=run, args=(first,), name="first")
    two = threading.Thread(target=run, args=(second,), name="second")
    one.start()
    while one.is_alive() and not pause.reached.wait(0.01):
        pass
    assert pause.reached.is_set(), f"the first write ended before its pause: {errors}"
    two.start()
    waited = _waits_on_a_lock(two)
    pause.go.set()
    one.join(WAIT)
    two.join(WAIT)
    assert not one.is_alive() and not two.is_alive()
    assert waited or not must_wait, f"the second write never waited for the first, so nothing raced: {errors}"
    assert not [e for e in errors.values() if isinstance(e, OperationalError)], errors
    return errors


def _repair(asset, tech, **extra):
    return create_work_order(asset=asset, type=WoType.REPAIR, priority=Priority.NORMAL, problem="Alarm", assigned_to=tech,
                             opened_on=timezone.localdate() - 7 * DAY, **extra)


@needs_postgres
@pytest.mark.django_db(transaction=True)
def test_two_completions_on_one_device_take_turns(tenant, vent, techs, monkeypatch):
    """[4] Mark completed on two in-progress repairs of one device at once: the first (the higher number) stops once it has its locks;
    the second (the lower number) waits for it; both complete."""
    lower, higher = _repair(vent, techs["dana"]), _repair(vent, techs["dana"])
    for wo in (lower, higher):
        change_status(wo, WoStatus.IN_PROGRESS)
    pause = pause_at(monkeypatch, completion, "blocker")
    errors = race(tenant, pause,
                  lambda: complete_work_order(WorkOrder.objects.get(pk=higher.pk), resolution="Replaced the flow sensor"),
                  lambda: complete_work_order(WorkOrder.objects.get(pk=lower.pk), resolution="Reseated the cable"))
    assert errors == {}
    assert set(WorkOrder.objects.filter(pk__in=[lower.pk, higher.pk]).values_list("status", flat=True)) == {WoStatus.COMPLETED}


@needs_postgres
@pytest.mark.django_db(transaction=True)
def test_recording_an_incident_on_a_request_while_a_lower_numbered_pm_starts(tenant, vent, techs, monkeypatch):
    """[5] An incident recorded from a nurse's tagged-out request (adopting it) while a technician starts the device's PM, numbered
    before it: the start waits for the hold, then is refused in words (only the investigation starts on a held device)."""
    pm = create_work_order(asset=vent, type=WoType.PM, priority=Priority.NORMAL, problem="PM", assigned_to=techs["dana"])
    request = create_work_order(asset=vent, type=WoType.REPAIR, priority=Priority.HIGH, problem="Alarmed and stopped", source=Source.PORTAL,
                                tag_out=True)
    assert pm.number < request.number
    pause = pause_at(monkeypatch, inc, "_adopt_refusal")
    errors = race(tenant, pause,
                  lambda: record(Asset.objects.get(pk=vent.pk), work_order=WorkOrder.objects.get(pk=request.pk)),
                  lambda: change_status(WorkOrder.objects.get(pk=pm.pk), WoStatus.IN_PROGRESS))
    assert list(errors) == ["second"] and isinstance(errors["second"], ValidationError) and HELD in errors["second"].messages[0]
    incident = Incident.objects.get()
    vent.refresh_from_db()
    assert incident.work_order_id == request.pk and vent.incident_hold and vent.status == AssetStatus.OUT_OF_SERVICE
    assert WorkOrder.objects.get(pk=pm.pk).status == WoStatus.OPEN


@needs_postgres
@pytest.mark.django_db(transaction=True)
def test_a_release_giving_a_late_reason_while_the_investigation_starts(tenant, vent, techs, monkeypatch):
    """[5] Releasing (kept out) a device with a PM it missed while held, while a technician starts its investigation (numbered
    before the PM): the release stops after giving the PM its reason; the start waits for it, then starts the investigation."""
    today = timezone.localdate()
    held_on = today - 20 * DAY
    incident = record(vent, outcome=Outcome.NO_HARM, affected=Affected.NONE, occurred_on=held_on, today=held_on)
    investigation = incident.work_order
    missed = create_work_order(asset=vent, type=WoType.PM, priority=Priority.NORMAL, problem="PM", opened_on=today - 6 * DAY,
                               due_on=today - 5 * DAY)
    assert investigation.number < missed.number
    pause = pause_at(monkeypatch, wo_services, "set_late_reason", after=True)
    hold = incident.holds.get()
    errors = race(tenant, pause,
                  lambda: inc.release(IncidentHold.objects.get(pk=hold.pk), Release.KEEP_OUT, today=today),
                  lambda: change_status(WorkOrder.objects.get(pk=investigation.pk), WoStatus.IN_PROGRESS))
    assert errors == {}
    vent.refresh_from_db()
    assert not vent.incident_hold and vent.status == AssetStatus.OUT_OF_SERVICE
    assert WorkOrder.objects.get(pk=missed.pk).late_reason == LateReason.INCIDENT_HOLD
    assert WorkOrder.objects.get(pk=investigation.pk).status == WoStatus.IN_PROGRESS


@needs_postgres
@pytest.mark.django_db(transaction=True)
def test_recorded_in_error_while_a_lower_numbered_repair_starts(tenant, vent, techs, monkeypatch):
    """[5] Marking an incident recorded in error (cancelling the investigation it opened) while a technician starts the device's
    repair, numbered before it: the start waits, then starts the repair on the released device."""
    repair = _repair(vent, techs["dana"])
    incident = record(vent)
    assert repair.number < incident.work_order.number
    pause = pause_at(monkeypatch, wo_services, "change_status")
    errors = race(tenant, pause,
                  lambda: inc.recorded_in_error(Incident.objects.get(pk=incident.pk)),
                  lambda: change_status(WorkOrder.objects.get(pk=repair.pk), WoStatus.IN_PROGRESS))
    assert errors == {}
    vent.refresh_from_db()
    assert not vent.incident_hold and vent.status == AssetStatus.IN_SERVICE
    assert WorkOrder.objects.get(pk=incident.work_order_id).status == WoStatus.CANCELLED
    assert WorkOrder.objects.get(pk=repair.pk).status == WoStatus.IN_PROGRESS


@needs_postgres
@pytest.mark.django_db(transaction=True)
def test_a_failed_pm_completed_while_an_incident_opens_its_investigation_on_the_device(tenant, vent, techs, monkeypatch):
    """A PM completed as failed (which opens its repair: the facility's work order numbering) while an incident is recorded on the
    same device with a hold (which opens its investigation: the same numbering, then the device's work orders). The completion
    stops once it has its locks; the recording takes the numbering and waits for the device's work; the completion then needs the
    numbering. Both must finish, one after the other."""
    pm = create_work_order(asset=vent, type=WoType.PM, priority=Priority.NORMAL, problem="PM", assigned_to=techs["dana"])
    pause = pause_at(monkeypatch, completion, "blocker")
    errors = race(tenant, pause,
                  lambda: complete_work_order(WorkOrder.objects.get(pk=pm.pk), pm_result=PmResult.FAIL, resolution="Flow sensor failed",
                                              tag_out=True),
                  lambda: record(Asset.objects.get(pk=vent.pk)))
    assert errors == {}
    vent.refresh_from_db()
    assert WorkOrder.objects.get(pk=pm.pk).status == WoStatus.COMPLETED and vent.incident_hold


@needs_postgres
@pytest.mark.django_db(transaction=True)
def test_recorded_in_error_of_two_held_devices_while_the_suspects_older_repair_starts(tenant, vent, pump, techs, monkeypatch):
    """Recorded in error of an incident holding two devices, whose opened investigation is on the suspect device, while a technician
    starts the suspect's older open repair (numbered before the investigation). The devices' work orders are taken one device at a
    time, in id order: the other part's first. The investigation belongs to the suspect's number order, so it is never taken with
    the other part's work, ahead of the suspect's older repair: the start and the release take turns."""
    suspect, part = sorted([vent, pump], key=lambda a: str(a.pk), reverse=True)  # the other part's work orders are taken first
    older = _repair(suspect, techs["dana"])
    incident = record(suspect)
    inc.hold_device(incident, part)
    assert older.number < incident.work_order.number
    pause = pause_at(monkeypatch, wo_services, "lock_work", after=True)  # once the first device's work orders are locked
    errors = race(tenant, pause,
                  lambda: inc.recorded_in_error(Incident.objects.get(pk=incident.pk)),
                  lambda: change_status(WorkOrder.objects.get(pk=older.pk), WoStatus.IN_PROGRESS), must_wait=False)
    assert "first" not in errors, errors
    assert "second" not in errors or HELD in errors["second"].messages[0], errors  # refused while held, else started once released
    assert Incident.objects.get(pk=incident.pk).status == Status.IN_ERROR
    assert not Asset.objects.filter(pk__in=[vent.pk, pump.pk], incident_hold=True).exists()
    assert WorkOrder.objects.get(pk=incident.work_order_id).status == WoStatus.CANCELLED
