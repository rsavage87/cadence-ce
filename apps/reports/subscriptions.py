"""
Report emails (slice 13): the Reports screen's Schedule button. A user asks for one report by email every Monday or on the
first Monday of each month, and the daily jobs (apps.jobs, the `send_report_emails` command) send what is due.

Self-service only. A subscription always goes to its own user's account email, and only while that user is active, belongs to
the facility, the facility is active, and the user can still view Reports; nothing here takes another recipient. Turning one
off always works.

Each email carries the report's table as an attachment, byte for byte the CSV the screen downloads (apps.web.exports), and a
link to the printable report, which asks the reader to sign in. Links start with settings.APP_BASE_URL, never a request's Host, and
(slice 22) name their facility (`facility=<slug>`, apps.accounts.people.with_facility), as the subject does: a person may work in
several facilities, whose reports share their keys, and apps.web.decorators offers the switch when the browser is in another.
Reports hold device, cost, and staff figures, never the free text a requester typed, so the email cannot repeat patient details.

Slice 18: a facility's custom reports (apps/reports/custom.py) are scheduled and emailed like the eight standard ones: every report
is found and run by key through apps.reports.services.find_report and run_any. A custom report that lists work orders or devices is
sent only to someone who can see them (apps/reports/permissions.py); deleting one deletes its subscriptions.

send_due starts with no tenant (the daily job): it reads only the Tenant table (a system table) before it enters each facility's
tenant_context, which is what row-level security on PostgreSQL requires. Slice 21: each facility's day is its own (its time zone):
the daily job sends one facility's emails on that facility's day, so its Mondays are the facility's Mondays.
"""
import logging
from collections import Counter
from datetime import date, timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from apps.accounts import emails, people
from apps.accounts.models import Level, Module
from apps.jobs.models import JobRun
from apps.tenants.context import get_current_tenant, tenant_context
from apps.tenants.models import Tenant
from apps.web.exports import csv_line
from apps.workorders.scoping import is_scoped

from . import permissions as perms
from .models import ReportSubscription
from .services import csv_filename, find_report, run_any

log = logging.getLogger(__name__)

Frequency = ReportSubscription.Frequency
JOB = "report_emails"  # the key of this job in apps.jobs.services.DAILY_JOBS
TEMPLATE = "reports/email/report"
# How often, as the toast and the email say it ("Scheduled: <report>, every Monday to ...").
WHEN = {Frequency.WEEKLY: "every Monday", Frequency.MONTHLY: "first Monday of each month"}
SHORT = {Frequency.WEEKLY: "weekly", Frequency.MONTHLY: "monthly"}  # the Schedule button: "Scheduled weekly"
# Why a due subscription was not sent: the stable key in send_due's summary, and the words the command prints.
SKIP_REASONS = {
    "inactive": "account deactivated",
    "no_email": "no email address",
    "no_access": "no Reports View",
    "scoped": "sees only part of the facility",
    "other_facility": "not in this facility",
    "unknown_report": "report no longer offered",
    "no_source_access": "cannot see what the report lists",
}


def local_today() -> date:
    """Today on the clock in effect: inside a facility (tenant_context, a signed-in request) the facility's today in its time zone,
    the day its daily job runs for (apps.jobs); outside any, the server's (TIME_ZONE)."""
    return timezone.localdate()


# --- who may have a report emailed, and setting it ---------------------------------------------------------------------

def _refusal(user) -> str | None:
    """Why `user` cannot have reports emailed in the current facility, in plain words; None when they can."""
    tenant = get_current_tenant()
    if tenant is None or user.tenant_id != tenant.id:
        return "Only people in this facility can have its reports emailed."
    if not user.is_active:
        return "This account is deactivated."
    if not user.email:
        return "Your account has no email address, so there is nowhere to send the report."
    if not user.has_level(Module.REPORTS, Level.VIEW):
        return "You need Reports View to have reports emailed to you."
    if is_scoped(user):  # slice 16: every report is the whole facility's, which a scoped role never sees (closed by default)
        return "Your role sees only part of the facility, and every report covers all of it."
    return None


def can_schedule(user) -> bool:
    """Whether the Reports screen offers `user` the Schedule button (the service checks again on save)."""
    return user.is_authenticated and _refusal(user) is None


def subscription_for(user, report_key: str) -> ReportSubscription | None:
    """`user`'s subscription to one report in the current facility, or None."""
    return ReportSubscription.objects.filter(user=user, report=report_key).first()


def set_subscription(user, report_key: str, frequency: str | None) -> ReportSubscription | None:
    """Email `report_key` to `user` weekly or monthly, or stop it (None or ""). Returns the subscription, or None once it is off.
    Raises ValidationError for an unknown report or frequency, and (when turning one on) for a user who cannot receive it: not in
    this facility, deactivated, without an email address, without Reports View, or (a custom report) without View on what it
    lists. A changed frequency keeps last_sent_on, so a report already sent today is not sent again."""
    meta = find_report(report_key)
    if meta is None:
        raise ValidationError("There is no such report.")
    report_key = meta["key"]  # a custom report's key as it is stored, however its id was spelled
    frequency = frequency or ""
    if frequency and frequency not in Frequency.values:
        raise ValidationError("Choose Off, Every Monday, or First Monday of each month.")
    if not frequency:
        ReportSubscription.objects.filter(user=user, report=report_key).delete()
        return None
    refusal = _refusal(user) or (perms.email_refusal(user, meta["source"]) if meta["custom"] else "")
    if refusal:
        raise ValidationError(refusal)
    sub = ReportSubscription.objects.filter(user=user, report=report_key).first()
    if sub is not None and sub.frequency == frequency:
        return sub  # nothing changes: keep its start day and what it has sent
    today = local_today()
    start_on = first_send_on(frequency, today, sub.last_sent_on if sub else None)
    if sub is None:
        return ReportSubscription.objects.create(user=user, report=report_key, frequency=frequency, start_on=start_on)
    sub.frequency, sub.start_on = frequency, start_on
    sub.save(update_fields=["frequency", "start_on"])
    return sub


# --- when ---------------------------------------------------------------------------------------------------------------

def is_due_on(frequency: str, day: date) -> bool:
    """Weekly: every Monday. Monthly: the first Monday of the month (a Monday in the month's first seven days)."""
    if day.weekday() != 0:
        return False
    return frequency == Frequency.WEEKLY or (frequency == Frequency.MONTHLY and day.day <= 7)


def latest_sending_day(frequency: str, today: date) -> date:
    """The most recent day on or before `today` that `frequency` sends on (this Monday, or this month's first Monday)."""
    day = today
    while not is_due_on(frequency, day):  # at most five weeks back
        day -= timedelta(days=1)
    return day


def is_due(sub: ReportSubscription, today: date) -> bool:
    """Due when its latest sending day has come (on or after the day it was turned on for) and nothing has gone out since. So a
    Monday whose run failed or never happened (a mail outage, the scheduler down, a run cut short) is caught up by the next daily
    run, never twice on one day, and a schedule turned on after a Monday's run starts with the next sending day."""
    due_day = latest_sending_day(sub.frequency, today)
    if sub.start_on is not None and due_day < sub.start_on:
        return False
    return sub.last_sent_on is None or sub.last_sent_on < due_day


def next_due_on(frequency: str, start: date) -> date:
    """The first day on or after `start` that `frequency` sends on."""
    day = start
    while not is_due_on(frequency, day):  # at most five weeks: the first Monday of the next month
        day += timedelta(days=1)
    return day


def first_send_on(frequency: str, today: date, last_sent_on: date | None = None) -> date:
    """When the next email for `frequency` arrives: today if today is a sending day and today's run has not happened yet (and it
    was not sent today already), otherwise the next sending day."""
    sent_today = last_sent_on is not None and last_sent_on >= today
    # This facility's run for its day (slice 21: one run per facility and local day); a system table, readable with no tenant set.
    ran_today = JobRun.objects.filter(job=JOB, facility=get_current_tenant(), run_on=today).exists()
    return next_due_on(frequency, today + timedelta(days=1) if sent_today or ran_today else today)


# --- sending ------------------------------------------------------------------------------------------------------------

def report_csv(data: dict) -> bytes:
    """A report's table as the screen's CSV download streams it (web:report_csv): UTF-8 with the byte-order mark, CRLF lines."""
    return ("\ufeff" + csv_line(data["columns"]) + "".join(csv_line(row) for row in data["rows"])).encode("utf-8")


def skip_reason(sub: ReportSubscription, tenant) -> str | None:
    """Why `sub` must not be sent in `tenant` now (a key of SKIP_REASONS), or None. Run inside the tenant's context: Reports View
    is read from the user's role, a tenant-scoped row."""
    user = sub.user
    meta = find_report(sub.report)
    if meta is None:
        return "unknown_report"
    if user.tenant_id != tenant.id:
        return "other_facility"
    if not user.is_active:
        return "inactive"
    if not user.email:
        return "no_email"
    if not user.has_level(Module.REPORTS, Level.VIEW):
        return "no_access"
    if is_scoped(user):  # a subscription made before their role was narrowed: never sent, as the web refuses them every report
        return "scoped"
    if perms.meta_refusal(user, meta):  # a custom report listing work orders or devices they can no longer see
        return "no_source_access"
    return None


def _send(sub: ReportSubscription, tenant, today: date, data: dict) -> bool:
    """Email one report (already computed as of `today`) to its subscriber and stamp the day. True once the mail backend took it."""
    meta = find_report(sub.report)
    filename = csv_filename(meta, today)
    context = {
        "user": sub.user, "facility": tenant.name, "report": meta, "today": today, "rows": len(data["rows"]), "filename": filename,
        "total": data.get("total"), "truncated": bool(data.get("truncated")),  # a custom report lists at most custom.MAX_ROWS
        "frequency": sub.frequency, "next_on": next_due_on(sub.frequency, today + timedelta(days=1)),
        "print_url": people.with_facility(settings.APP_BASE_URL + reverse("web:report_print", args=[sub.report]), tenant),
        "report_url": people.with_facility(settings.APP_BASE_URL + reverse("web:report", args=[sub.report]), tenant),
    }
    # Claim it first: two runs at once (the scheduler and an operator) must not both send it. The claim holds only if nothing has
    # been sent for this sending day yet; a failed send gives it back, so the next run tries again.
    previous = sub.last_sent_on
    due_day = latest_sending_day(sub.frequency, today)
    claimed = (ReportSubscription.objects.filter(pk=sub.pk).filter(Q(last_sent_on__isnull=True) | Q(last_sent_on__lt=due_day))
               .update(last_sent_on=today))
    if not claimed:
        raise AlreadySent
    if not emails.send(sub.user.email, TEMPLATE, context, attachments=[(filename, report_csv(data), "text/csv")]):
        ReportSubscription.objects.filter(pk=sub.pk, last_sent_on=today).update(last_sent_on=previous)  # emails.send logged why
        return False
    sub.last_sent_on = today
    return True


class AlreadySent(Exception):
    """Another run sent this subscription for its sending day while this one was getting ready."""


def send_report_email(sub: ReportSubscription, today: date) -> bool:
    """Email `sub`'s report as of `today` to its user now, inside the subscription's facility, computed the way the Reports
    screen computes it. Stamps last_sent_on only when the email was sent. Raises ValidationError when the user may not receive
    it (see SKIP_REASONS); False when the email could not be sent."""
    tenant = Tenant.objects.get(pk=sub.tenant_id)  # a system table, readable before the tenant is set
    if not tenant.is_active:
        raise ValidationError("Not sent: the facility is inactive.")
    with tenant_context(tenant):
        reason = skip_reason(sub, tenant)
        if reason:
            raise ValidationError(f"Not sent: {SKIP_REASONS[reason]}.")
        return _send(sub, tenant, today, run_any(sub.report, today))


def _send_facility(tenant, today: date, counts: dict) -> None:
    """Send `tenant`'s subscriptions due `today`, counting into `counts`. Inside the tenant's context. One subscription failing
    (a report error, a mail outage) is logged and counted, and the rest still go."""
    subs = (ReportSubscription.objects.filter(Q(last_sent_on__isnull=True) | Q(last_sent_on__lt=today))
            .select_related("user").order_by("report", "user__username"))  # due-ness (a catch-up any day of the week) is is_due's
    reports = {}  # every subscriber gets the same table: compute each report once per facility and day
    for sub in subs:
        if not is_due(sub, today):
            continue
        counts["due"] += 1
        reason = skip_reason(sub, tenant)
        if reason:
            counts["skipped"][reason] += 1
            continue
        try:
            if sub.report not in reports:
                reports[sub.report] = run_any(sub.report, today)
            sent = _send(sub, tenant, today, reports[sub.report])
        except AlreadySent:
            counts["due"] -= 1  # another run has it: neither sent nor failed here
            continue
        except Exception:  # a report that cannot be computed, or a database error, fails this email only
            log.exception("Report email %s for user %s at %s failed", sub.report, sub.user_id, tenant.slug)
            sent = False
        counts["sent" if sent else "failed"] += 1


def send_due(today: date | None = None, facility=None) -> dict:
    """Send every report email due `today`, facility by facility (active facilities only; only `facility` when given, as the daily
    job does). With no `today`, each facility's own today (its time zone): a facility's Monday is its own. Returns what happened:
    {"day" (`today`, or None), "sent", "failed", "skipped", "tenants": [{"slug", "name", "day" (the facility's), "due", "sent",
    "failed", "skipped": Counter of SKIP_REASONS keys, "error"}]}; "error" is set when a facility could not be worked through at
    all (its emails count as one failure)."""
    summary = {"day": today, "sent": 0, "failed": 0, "skipped": 0, "tenants": []}
    tenants = Tenant.objects.filter(is_active=True).order_by("slug")  # a system table: read before any tenant is set
    if facility is not None:
        tenants = tenants.filter(pk=facility.pk)
    for tenant in tenants:
        counts = {"slug": tenant.slug, "name": tenant.name, "day": today, "due": 0, "sent": 0, "failed": 0, "skipped": Counter(),
                  "error": ""}
        try:
            with tenant_context(tenant):
                counts["day"] = today or local_today()  # inside the facility's context: its today
                _send_facility(tenant, counts["day"], counts)
        except Exception as e:  # e.g. the database went away mid-run: the next facility still gets its emails
            log.exception("Report emails for %s failed", tenant.slug)
            counts["error"] = f"{type(e).__name__}: {e}"
            counts["failed"] += 1
        summary["tenants"].append(counts)
        summary["sent"] += counts["sent"]
        summary["failed"] += counts["failed"]
        summary["skipped"] += sum(counts["skipped"].values())
    return summary
