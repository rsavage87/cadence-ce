"""
Slice 28: incidents for tests that need one before (or without) apps.incidents.services' writers: the rows written directly, the
device held through the flag's real writer (equipment.services.set_incident_hold). Import from any test file:
`from incident_fixtures import held_incident`.
"""
from datetime import timedelta

from django.utils import timezone

from apps.core.models import Sequence
from apps.equipment.services import set_incident_hold
from apps.incidents.models import Affected, Incident, IncidentHold, Outcome, Status
from apps.incidents.services import due_date
from apps.workorders.models import Priority, WoType
from apps.workorders.services import create_work_order


def make_incident(asset, *, outcome=Outcome.UNKNOWN, affected=Affected.PATIENT, occurred_on=None, aware_on=None, work_order=None,
                  open_work_order=True, hold=True, event_reference="", status=Status.OPEN, **fields) -> Incident:
    """An incident on `asset`: with an investigation (the `work_order` given, else a tagged-out repair opened for it when
    open_work_order), and the device held (an IncidentHold row and the flag) when `hold`."""
    today = timezone.localdate()
    occurred_on = occurred_on or today
    aware_on = aware_on or occurred_on
    opened = False
    if work_order is None and open_work_order:
        work_order = create_work_order(asset=asset, type=WoType.REPAIR, priority=Priority.HIGH, problem="Investigation of a reported incident",
                                       opened_on=occurred_on, tag_out=True)
        opened = True
    year = occurred_on.year
    number = f"IN-{year % 100:02d}-{Sequence.next(f'incident-{year}'):04d}"
    incident = Incident.objects.create(number=number, asset=asset, work_order=work_order, opened_work_order=opened, occurred_on=occurred_on,
                                       aware_on=aware_on, outcome=outcome, affected=affected, event_reference=event_reference, status=status,
                                       report_due_on=due_date(outcome=outcome, affected=affected, aware_on=aware_on,
                                                              reportable=fields.get("reportable")),
                                       **fields)
    if hold:
        asset.refresh_from_db()
        IncidentHold.objects.create(incident=incident, asset=asset, held_on=occurred_on, status_before=asset.status)
        set_incident_hold(asset)
    return incident


def held_incident(asset, **kwargs) -> Incident:
    """An open incident holding `asset`, with its own investigation work order (incident.work_order)."""
    return make_incident(asset, **kwargs)


def days_ago(n: int):
    return timezone.localdate() - timedelta(days=n)
