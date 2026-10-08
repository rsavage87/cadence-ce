"""
Emails about device incidents (slice 28) to the people who decide whether one was reportable: Incidents Approve holders who see the
whole facility and keep NotificationPreference.incidents on (apps.notifications.services offers it to them, and only them, on the
Notifications page).

- recorded(incident): after commit, when an incident with a clock is recorded (apps.incidents.services.record_incident), or a change
  starts its clock (update_facts raising the outcome, record_finding clearing a decision). Once per incident and person, however often
  the clock starts again (NotificationSent: kind incident, key "<number>:recorded").
- send_reminders(today): the daily job (apps.notifications.daily runs it in each facility, on the facility's day): every open incident
  whose clock is running (services.needing_action) with SOON_WORK_DAYS work days or fewer left ("<number>:soon"), and once it is past
  due ("<number>:overdue"). Each stage once per incident and person. A missed day catches up with the stage the incident is in now,
  never one it has passed: an incident first found past due gets "overdue" only. One email per person listing every incident due a
  reminder they have not had.

What they say: the incident's number, the device's tag, and the report's due date (with the work days left), and a link from
APP_BASE_URL naming the facility (people.with_facility). Never the outcome, who was affected, the day it happened, the device's unit,
or anything typed (CLAUDE.md non-negotiable 6): every email says an incident "may need a report to the FDA or the manufacturer",
since who must get it follows from the outcome.

Claim, then send (as apps.notifications.daily does): the NotificationSent row is inserted before the email goes (the unique constraint
is the lock, so two runs at once send each email once) and removed when the send fails, so the next run, or the next change that starts
the clock, tries again. recorded() never raises: a failure is logged, and the incident stays recorded.

quiet(): the incidents recorded inside announce nothing (a data load: the demo seed).
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar

from django.conf import settings
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

log = logging.getLogger(__name__)

RECORDED, SOON, OVERDUE = "recorded", "soon", "overdue"  # the stages, each once per incident and person
RECORDED_TEMPLATE = "notifications/email/incident_recorded"
REMINDER_TEMPLATE = "notifications/email/incident_reminder"
PREFERENCE = "incidents"  # NotificationPreference's field (apps.notifications.services.KINDS)

_quiet: ContextVar = ContextVar("cadence_incident_quiet", default=0)


def key_for(number: str, stage: str) -> str:
    """NotificationSent.key of an incident's email at `stage`."""
    return f"{number}:{stage}"


# --- quiet --------------------------------------------------------------------------------------------------------------------

def _hush(step: int) -> None:
    _quiet.set(_quiet.get() + step)


@contextmanager
def quiet():
    """Announce nothing recorded inside (a data load, such as the demo seed). The emails go after commit, so the silence goes with
    them: it starts in a commit callback taken before anything inside and ends in one taken after, so the announcements made inside
    run silenced whenever their transaction commits. A rollback runs none of them, and nothing stays silenced."""
    transaction.on_commit(lambda: _hush(1))  # at once outside a transaction
    try:
        yield
    finally:
        transaction.on_commit(lambda: _hush(-1))


# --- who ----------------------------------------------------------------------------------------------------------------------

def recipients(tenant) -> list:
    """The people of the current facility (`tenant`) who get incident emails: active, with an address, Incidents Approve and the
    whole facility (permissions.can_decide), and incidents on (services.wants). Inside the facility's context."""
    from apps.accounts.models import User
    from apps.notifications.services import wants

    from . import permissions as perms

    users = User.objects.filter(tenant=tenant, is_active=True).exclude(email="").select_related("role").order_by("username")
    return [u for u in users if perms.can_decide(u) and wants(u, PREFERENCE)]


# --- what -------------------------------------------------------------------------------------------------------------------------

def _link(tenant, name: str, *args) -> str:
    """A link to one of `tenant`'s pages for an email: from APP_BASE_URL (never a request's Host), naming the facility."""
    from apps.accounts import people

    return people.with_facility(settings.APP_BASE_URL + reverse(name, args=args), tenant)


def _line(incident, clock, tenant) -> dict:
    """One incident as an email shows it: its number, the device's tag, the due date, and how it stands. Plain values only."""
    return {"number": incident.number, "tag": incident.asset.tag, "due_on": clock.due, "left": clock.left, "overdue": clock.overdue,
            "url": _link(tenant, "web:incident", incident.number)}


def _context(tenant, user, items: list) -> dict:
    return {"facility": tenant.name, "name": user.first_name or user.email, "email": user.email, "items": items, "count": len(items),
            "incidents_url": _link(tenant, "web:incidents"), "preferences_url": _link(tenant, "web:notifications")}


# --- sending ----------------------------------------------------------------------------------------------------------------------

def _claim(key: str, user):
    """Insert the row that says this email went; None when it is there already. Its own savepoint, so a refused insert never spoils
    the caller's transaction."""
    from apps.notifications.models import NotificationSent

    try:
        with transaction.atomic():
            return NotificationSent.objects.create(kind=NotificationSent.Kind.INCIDENT, key=key, user=user)
    except IntegrityError:
        return None


def _send(user, template: str, context: dict, claims: list) -> bool:
    """Send one email whose claims are held; give the claims back when it does not go, so the next try sends it."""
    from apps.accounts import emails
    from apps.notifications.models import NotificationSent

    try:
        sent = emails.send(user.email, template, context)  # a mail failure is logged there and returns False
    except Exception:
        NotificationSent.objects.filter(pk__in=[c.pk for c in claims]).delete()
        raise
    if not sent:
        NotificationSent.objects.filter(pk__in=[c.pk for c in claims]).delete()
    return sent


def recorded(incident) -> int:
    """After commit: `incident` was recorded with a clock, or a change started its clock. Each recipient hears once per incident
    (RECORDED), while it is open and its clock is running. Read again inside its own facility, whatever context committed it. Returns
    how many emails went out; never raises."""
    from apps.tenants.context import tenant_context
    from apps.tenants.models import Tenant

    if _quiet.get() > 0:
        return 0
    try:
        tenant = Tenant.objects.filter(pk=incident.tenant_id, is_active=True).first()  # a system table: no tenant needed to read it
        if tenant is None:
            return 0  # a deactivated facility emails nobody
        with tenant_context(tenant):
            return _send_recorded(tenant, incident.pk)
    except Exception:  # the incident is saved already: an email must never undo it or reach the caller
        log.exception("Could not send the recorded email for incident %s", getattr(incident, "pk", None))
        return 0


def _send_recorded(tenant, pk) -> int:
    from . import services
    from .models import Incident, Status

    incident = Incident.objects.select_related("asset").filter(pk=pk, status=Status.OPEN).first()
    if incident is None:
        return 0
    clock = services.clock(incident, timezone.localdate())  # the facility's today (tenant_context)
    if not clock.pending:
        return 0  # decided, or reported, before the email went
    key = key_for(incident.number, RECORDED)
    sent = 0
    for user in recipients(tenant):
        claim = _claim(key, user)
        if claim is None:
            continue  # told already (an earlier start of this incident's clock)
        try:
            if _send(user, RECORDED_TEMPLATE, _context(tenant, user, [_line(incident, clock, tenant)]), [claim]):
                sent += 1
        except Exception:  # its claim is given back; the others still hear
            log.exception("The recorded email for incident %s to user %s failed", incident.number, user.pk)
    return sent


def stage_of(clock) -> str | None:
    """The reminder an incident's clock is due today: OVERDUE once past due, SOON with SOON_WORK_DAYS work days or fewer left, else
    None (also once it is decided not reportable, or its reports are recorded: nothing is pending)."""
    if clock.overdue:
        return OVERDUE
    if clock.soon:
        return SOON
    return None


def reminders_due(today) -> list[tuple]:
    """[(incident, clock, stage)] for the current facility's incidents due a reminder `today`, by due date. Whether a person has had
    it is NotificationSent's; this is the same for everyone. One query."""
    from . import services

    out = []
    for incident in services.needing_action().select_related("asset").order_by("report_due_on", "number"):
        clock = services.clock(incident, today)
        stage = stage_of(clock)
        if stage is not None:
            out.append((incident, clock, stage))
    return out


def send_reminders(today) -> dict:
    """The daily reminders in the current facility (apps.notifications.daily runs it inside each facility's context, on its day):
    each recipient one email listing every incident due a reminder today that they have not had. Returns {"sent": emails sent,
    "failed": emails that did not go (their claims released for the next run), "incidents": incidents reminded about}. One person's
    email failing is logged and counted, and the rest still go."""
    from apps.notifications.models import NotificationSent
    from apps.tenants.context import get_current_tenant

    from .services import SOON_WORK_DAYS

    tenant = get_current_tenant()
    counts = {"sent": 0, "failed": 0, "incidents": 0}
    due = reminders_due(today) if tenant is not None else []
    if not due:
        return counts
    keys = {key_for(incident.number, stage): (incident, clock) for incident, clock, stage in due}
    had = set(NotificationSent.objects.filter(kind=NotificationSent.Kind.INCIDENT, key__in=list(keys)).values_list("user_id", "key"))
    reminded = set()
    for user in recipients(tenant):
        unsent = [key for key in keys if (user.pk, key) not in had]
        if not unsent:
            continue
        claims = []
        try:
            for key in unsent:
                claim = _claim(key, user)
                if claim is not None:
                    claims.append(claim)
            if not claims:
                continue  # another run has them
            items = [_line(*keys[c.key], tenant) for c in claims]
            if _send(user, REMINDER_TEMPLATE, {**_context(tenant, user, items), "soon_days": SOON_WORK_DAYS}, claims):
                counts["sent"] += 1
                reminded.update(keys[c.key][0].pk for c in claims)
            else:
                counts["failed"] += 1
        except Exception:  # a bad row or a template that will not render: nothing stays claimed, and the rest still go
            log.exception("The incident reminder for user %s at %s failed", user.pk, tenant.slug)
            NotificationSent.objects.filter(pk__in=[c.pk for c in claims]).delete()
            counts["failed"] += 1
    counts["incidents"] = len(reminded)
    return counts
