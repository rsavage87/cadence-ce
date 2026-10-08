"""
The daily emails to staff (slice 20, part D): the daily digest and the contract reminders. The `send_staff_notifications` command
sends them once a day (apps.jobs: staff_notifications, after the report emails), facility by facility.

The daily digest goes to each person who chose it on their Notifications page (apps.notifications.services.wants) and who works the
facility's work orders as a technician: an active Technician record linked to their account, Work orders View, and a role that sees
the whole facility. It lists their work due today or overdue, and their open PMs due in the next seven days; a day with nothing to
list sends nothing. At most one a day (NotificationSent: digest, the day). Slice 24: their work is My work's, and "due" is My work's
one definition (apps.workorders.my_work.mine and due: open or in progress, due today or before, not waiting on parts), so the email,
the page, and its nav badge count the same work; its "all your open work" link opens My work.

Contract reminders go to each person with Contracts Edit (who renews or replaces a contract) who has not turned them off, about each
contract that still covers devices in use and ends in 90, 30, or 7 days, or has just ended. Each contract and stage is reminded once
per person (NotificationSent: contract, "<contract id>:<end date>:<stage>"; the end date is in the key so a renewed or re-dated
contract is reminded again before its new end). A missed day catches up with the stage the contract is in now, never the stages it
has passed: a contract found 28 days from its end that was never reminded at 30 gets the 30-day reminder, once, not the 90-day one
too. A person gets one email a day listing every contract due a reminder.

Slice 28: the incident reminders (apps.incidents.notify.send_reminders: an incident whose report may be due in a few work days, or is
past due, to the people who decide whether it is reportable) go in the same run, facility by facility, on the facility's day.

Claim, then send: the NotificationSent row is inserted before the email goes (the unique constraint is the lock, so two runs at once
send each email once) and removed when the send fails, so the next run tries again. One person or facility failing is logged and
counted, and the rest still go. Deactivated accounts are never emailed and not counted; someone who chose an email but cannot have it
is counted as skipped, with the reason (SKIP_REASONS).

What the emails say: work order numbers, types, priorities, statuses, and due dates, device tags and descriptions, departments;
contract references, vendors, types, coverage, end dates, and the devices covered; and links that start with settings.APP_BASE_URL
and ask the reader to sign in. Never a work order's problem, requester, callback, or reported location, nor a contract's notes: the
templates get plain values built here, never the records (CLAUDE.md, "No PHI"). Slice 22: every link names its facility
(`facility=<slug>`, apps.accounts.people.with_facility), as every subject does: a person may work in several facilities, where
record numbers repeat, and apps.web.decorators offers the switch when the browser is in another.

send_due starts with no tenant (the daily job): it reads only the Tenant table (a system table) before it enters each facility's
tenant_context, which row-level security on PostgreSQL requires. Slice 21: each facility's day is its own (its time zone): the
daily job sends one facility's emails on that facility's day, so "due today" and "the next seven days" are the facility's.
"""
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta

from django.conf import settings
from django.db import IntegrityError, connection, transaction
from django.db.models import Count, Q
from django.urls import reverse
from django.utils import timezone

from apps.accounts import emails, people
from apps.accounts.models import Level, Module, User
from apps.contracts.models import Contract
from apps.credentials.models import Technician
from apps.equipment.models import Asset
from apps.incidents import notify as incident_notify
from apps.tenants.context import tenant_context
from apps.tenants.models import Tenant
from apps.workorders import my_work, scoping
from apps.workorders.models import WoType
from apps.workorders.services import PRIORITY_RANK

from .models import NotificationSent
from .services import preferences_for, wants

log = logging.getLogger(__name__)

Kind = NotificationSent.Kind
JOB = "staff_notifications"  # the key of this job in apps.jobs.services.DAILY_JOBS
DIGEST_TEMPLATE = "notifications/email/digest"
CONTRACT_TEMPLATE = "notifications/email/contract_reminder"

# Who may have each email, beside their own choice: the digest lists work orders (Work orders View); the reminders ask for a
# decision about a contract (Contracts Edit: who renews, edits, or replaces one).
DIGEST_MODULE, DIGEST_LEVEL = Module.WORKORDERS, Level.VIEW
REMINDER_MODULE, REMINDER_LEVEL = Module.CONTRACTS, Level.EDIT

PM_DAYS = 7  # the digest's PMs: due after today, up to this many days ahead
LIST_CAP = 20  # the digest lists at most this many work orders in each part, then "and N more" with a link to the list

# Contract reminder stages, the most urgent first: (days to the end, at most; the stage's key). A contract is in the first stage
# whose days it is within; the day after it ends it is ENDED, for ENDED_CATCH_UP_DAYS (a missed run still says so, once).
STAGES = ((7, "7"), (30, "30"), (90, "90"))
ENDED = "ended"
ENDED_CATCH_UP_DAYS = 7
MODELS_SHOWN = 3  # the device models named under each contract, the largest groups first

# Why someone who chose an email did not get it: the stable key in send_due's summary, and the words the command prints.
SKIP_REASONS = {
    "no_email": "no email address",
    "scoped": "sees only part of the facility",
    "no_access": "no Work orders View",
    "no_technician": "no active technician record",
}
KIND_WORDS = {Kind.DIGEST.value: "digest", Kind.CONTRACT.value: "contract reminder"}  # the summary counts by the plain slug


def local_today() -> date:
    """Today on the clock in effect: inside a facility (tenant_context) the facility's today in its time zone, the day its daily job
    runs for (apps.jobs); outside any, the server's (TIME_ZONE)."""
    return timezone.localdate()


def _url(tenant, name: str, *args, query: str = "") -> str:
    """A link to one of `tenant`'s pages: from APP_BASE_URL (never a request's Host), naming the facility (people.with_facility)."""
    return people.with_facility(settings.APP_BASE_URL + reverse(name, args=args) + (f"?{query}" if query else ""), tenant)


def _name(user) -> str:
    return user.first_name or user.email


# --- who --------------------------------------------------------------------------------------------------------------------

def _chose(user, kind: str) -> bool:
    """Whether `user` chose the emails of `kind` (a NotificationPreference field): wants() decides; a missing address alone still
    counts as chosen, so the summary can say why they got nothing."""
    return wants(user, kind) or (not user.email and bool(getattr(preferences_for(user), kind)))


def digest_refusal(user, technician) -> str | None:
    """Why `user`, who chose the digest, cannot have it (a key of SKIP_REASONS), or None. Inside the facility's context."""
    if not user.email:
        return "no_email"
    if scoping.is_scoped(user):  # the digest is the facility's work, which a scoped role never sees
        return "scoped"
    if not user.has_level(DIGEST_MODULE, DIGEST_LEVEL):
        return "no_access"
    if technician is None:
        return "no_technician"
    return None


def reminder_refusal(user) -> str | None:
    """Why `user`, who has Contracts Edit and wants contract reminders, cannot have them, or None. Inside the facility's context."""
    if not user.email:
        return "no_email"
    if scoping.is_scoped(user):  # the Contracts screen refuses a scoped role, whatever its levels
        return "scoped"
    return None


# --- what: the digest ------------------------------------------------------------------------------------------------------

def _wo_line(wo, tenant, today: date) -> dict:
    """One work order as the digest shows it. Plain values only: never its problem, requester, callback, or reported location."""
    asset = wo.asset
    return {"number": wo.number, "type": wo.get_type_display(), "priority": wo.get_priority_display(), "status": wo.get_status_display(),
            "due_on": wo.due_on, "late_days": max((today - wo.due_on).days, 0),
            "tag": asset.tag, "device": asset.device_model.description, "department": asset.department.name, "url": _url(tenant, "web:wo", wo.number)}


def digest_for(user, tenant, today: date) -> dict:
    """What `user`'s digest for `today` lists: {"due": [...], "due_total", "pms": [...], "pms_total"}, each list at most LIST_CAP
    lines (_wo_line). Their work is My work's (apps.workorders.my_work.mine: the work orders assigned to their own active technician
    profile; digest_refusal has refused a scoped user and one without a profile). Due: my_work.due, the one "due" My work and its
    badge show (open or in progress, due today or before; work waiting on parts is not due). PMs: their open PM work orders due in
    the next PM_DAYS, whatever their status."""
    related = ("asset", "asset__device_model", "asset__department")
    due = my_work.due(user, today).select_related(*related).order_by(PRIORITY_RANK, "due_on", "number")
    pms = (my_work.mine(user).filter(type=WoType.PM, due_on__gt=today, due_on__lte=today + timedelta(days=PM_DAYS)).select_related(*related)
           .order_by("due_on", "asset__tag"))
    return {"due": [_wo_line(wo, tenant, today) for wo in due[:LIST_CAP]], "due_total": due.count(),
            "pms": [_wo_line(wo, tenant, today) for wo in pms[:LIST_CAP]], "pms_total": pms.count()}


# --- what: the contract reminders ------------------------------------------------------------------------------------------

def reminder_stage(end_on: date, today: date) -> str | None:
    """The reminder stage a contract ending `end_on` is in on `today`: "7", "30", or "90" (ends within that many days, today
    included), ENDED (ended in the last ENDED_CATCH_UP_DAYS), or None."""
    left = (end_on - today).days
    if left < 0:
        return ENDED if -left <= ENDED_CATCH_UP_DAYS else None
    for days, stage in STAGES:
        if left <= days:
            return stage
    return None


def _when(left: int) -> str:
    """How far the end is, after "ends" or "ended": "in 30 days", "tomorrow", "today", "yesterday", "3 days ago"."""
    if left > 1:
        return f"in {left} days"
    return {1: "tomorrow", 0: "today", -1: "yesterday"}.get(left) or f"{-left} days ago"


@dataclass
class Reminder:
    """A contract due a reminder today, with what the email says about it (plain values)."""
    contract: Contract
    stage: str
    devices: int
    models: list = field(default_factory=list)  # [("Philips IntelliVue MX750", 10), ...], the largest groups first

    @property
    def key(self) -> str:
        return f"{self.contract.id}:{self.contract.end_on.isoformat()}:{self.stage}"

    def line(self, tenant, today: date) -> dict:
        c = self.contract
        left = (c.end_on - today).days
        return {"reference": c.reference, "vendor": c.vendor, "type": c.get_type_display(), "coverage": c.get_coverage_display(),
                "end_on": c.end_on, "ended": left < 0, "when": _when(left), "devices": self.devices,
                "models": self.models[:MODELS_SHOWN], "more_models": max(len(self.models) - MODELS_SHOWN, 0),
                "url": _url(tenant, "web:contract", c.pk)}


def reminders_due(today: date) -> list[Reminder]:
    """The facility's contracts in a reminder stage on `today` that still cover devices in use (Asset.ACTIVE_STATUSES), by end
    date. Whether a person has had a stage's reminder is NotificationSent's; this is the same for everyone."""
    longest = max(days for days, _stage in STAGES)
    contracts = (Contract.objects.filter(end_on__gte=today - timedelta(days=ENDED_CATCH_UP_DAYS), end_on__lte=today + timedelta(days=longest))
                 .annotate(in_use=Count("assets", filter=Q(assets__status__in=Asset.ACTIVE_STATUSES))).filter(in_use__gt=0).order_by("end_on", "reference"))
    due = [Reminder(c, stage, c.in_use) for c in contracts if (stage := reminder_stage(c.end_on, today))]
    if due:
        by_id = {r.contract.id: r for r in due}
        rows = (Asset.objects.filter(contract_id__in=list(by_id), status__in=Asset.ACTIVE_STATUSES).order_by()
                .values("contract_id", "device_model__manufacturer", "device_model__model").annotate(n=Count("id"))
                .order_by("contract_id", "-n", "device_model__manufacturer", "device_model__model"))
        for row in rows:
            by_id[row["contract_id"]].models.append((f"{row['device_model__manufacturer']} {row['device_model__model']}", row["n"]))
    return due


# --- sending ---------------------------------------------------------------------------------------------------------------

class AlreadySent(Exception):
    """Another run claimed this email while this one was getting ready."""


def _claim(kind: str, key: str, user) -> NotificationSent | None:
    """Insert the row that says this email went; None when it is there already (another run has it). Its own savepoint, so a
    refused insert never spoils the run's transaction."""
    try:
        with transaction.atomic():
            return NotificationSent.objects.create(kind=kind, key=key, user=user)
    except IntegrityError:
        return None


def _release(claims: list[NotificationSent]) -> None:
    NotificationSent.objects.filter(pk__in=[c.pk for c in claims]).delete()


def _send_claimed(user, template: str, context: dict, claims: list[NotificationSent]) -> bool:
    """Send one email whose claims are held; give the claims back when it does not go (a mail outage, a template error), so the
    next run tries again. True once the mail backend took it."""
    try:
        sent = emails.send(user.email, template, context)  # a mail failure is logged there and returns False
    except Exception:
        _release(claims)
        raise
    if not sent:
        _release(claims)
    return sent


def send_digest(user, technician, tenant, today: date) -> str:
    """Email `user` their digest for `today`, if they may have it and it has something to say. Returns "sent", "failed", "quiet"
    (nothing to list: no email), or a SKIP_REASONS key; raises AlreadySent when today's digest is another run's. Inside the
    facility's context; the caller has checked that they chose it."""
    reason = digest_refusal(user, technician)
    if reason:
        return reason
    items = digest_for(user, tenant, today)
    if not items["due_total"] and not items["pms_total"]:
        return "quiet"
    claim = _claim(Kind.DIGEST, today.isoformat(), user)
    if claim is None:
        raise AlreadySent
    context = {**items, "name": _name(user), "facility": tenant.name, "today": today, "pm_days": PM_DAYS,
               "due_more": items["due_total"] - len(items["due"]), "pms_more": items["pms_total"] - len(items["pms"]),
               "my_work_url": _url(tenant, "web:my_work"), "preferences_url": _url(tenant, "web:notifications")}
    return "sent" if _send_claimed(user, DIGEST_TEMPLATE, context, [claim]) else "failed"


def send_reminders(user, reminders: list[Reminder], tenant, today: date) -> tuple[str, int]:
    """Email `user` one reminder listing each of `reminders` (the ones they have not had) they can still claim. Returns
    ("sent" | "failed" | a SKIP_REASONS key, how many contracts the email listed); raises AlreadySent when another run has them all.
    Inside the facility's context; the caller has checked Contracts Edit and that they want them."""
    reason = reminder_refusal(user)
    if reason:
        return reason, 0
    claims, listed = [], []
    try:
        for r in reminders:
            claim = _claim(Kind.CONTRACT, r.key, user)
            if claim is not None:
                claims.append(claim)
                listed.append(r)
    except Exception:  # a database error midway: nothing is sent, so nothing stays claimed
        _release(claims)
        raise
    if not claims:
        raise AlreadySent
    context = {"name": _name(user), "facility": tenant.name, "today": today, "items": [r.line(tenant, today) for r in listed],
               "stages": [days for days, _stage in reversed(STAGES)], "contracts_url": _url(tenant, "web:contracts"),
               "preferences_url": _url(tenant, "web:notifications")}
    sent = _send_claimed(user, CONTRACT_TEMPLATE, context, claims)
    return ("sent" if sent else "failed"), len(listed)


def _attempt(kind: str, user, tenant, send) -> tuple[str, int]:
    """Run `send` (one person's email of `kind`: its checks and its sending) and return its (outcome, contracts listed). Another run
    having it is ("", 0); an error (a bad row, a database error, a template that will not render) is logged and fails this email
    only, so the rest still go."""
    try:
        return send()
    except AlreadySent:
        return "", 0
    except Exception:
        log.exception("The %s for user %s at %s failed", KIND_WORDS[kind], user.pk, tenant.slug)
        return "failed", 0


def _tally(counts: dict, kind: str, outcome: str, sent_key: str) -> None:
    if outcome in SKIP_REASONS:
        counts["skipped"][(kind, outcome)] += 1
    elif outcome == "sent":
        counts[sent_key] += 1
    elif outcome in ("failed", "quiet"):
        counts[outcome] += 1


def _send_facility(tenant, today: date, counts: dict) -> None:
    """Send `tenant`'s digests, contract reminders, and incident reminders for `today`, counting into `counts`. Inside the tenant's
    context. One person's email failing is logged and counted, and the rest still go (_attempt; the incident reminders' own)."""
    people = list(User.objects.filter(tenant=tenant, is_active=True).select_related("role").order_by("username"))  # deactivated: never
    technicians = {t.user_id: t for t in Technician.objects.filter(is_active=True, user__isnull=False)}
    digested = set(NotificationSent.objects.filter(kind=Kind.DIGEST, key=today.isoformat()).values_list("user_id", flat=True))
    due = reminders_due(today)
    counts["contracts"] = len(due)
    reminded = (set(NotificationSent.objects.filter(kind=Kind.CONTRACT, key__in=[r.key for r in due]).values_list("user_id", "key"))
                if due else set())

    for user in people:
        def digest():
            if user.id in digested or not _chose(user, "daily_digest"):
                return "", 0
            return send_digest(user, technicians.get(user.id), tenant, today), 0

        def contract_reminders():
            unsent = [r for r in due if (user.id, r.key) not in reminded]
            if not unsent or not user.has_level(REMINDER_MODULE, REMINDER_LEVEL) or not _chose(user, "contract_reminders"):
                return "", 0
            return send_reminders(user, unsent, tenant, today)

        outcome, _listed = _attempt(Kind.DIGEST.value, user, tenant, digest)
        _tally(counts, Kind.DIGEST.value, outcome, "digests")
        outcome, listed = _attempt(Kind.CONTRACT.value, user, tenant, contract_reminders)
        _tally(counts, Kind.CONTRACT.value, outcome, "reminders")
        counts["reminded"] += listed if outcome == "sent" else 0

    try:
        incidents = incident_notify.send_reminders(today)  # slice 28: to those who decide, each incident and stage once
    except Exception:  # reading the incidents failed: counted, and the digests and contract reminders above stand
        log.exception("The incident reminders at %s failed", tenant.slug)
        counts["failed"] += 1
        return
    counts["incident_reminders"] += incidents["sent"]
    counts["incidents"] += incidents["incidents"]
    counts["failed"] += incidents["failed"]


def send_due(today: date | None = None, facility=None) -> dict:
    """Send the digests, contract reminders, and incident reminders due `today`, facility by facility (active facilities only; only
    `facility` when given, as the daily job does). With no `today`, each facility's own today (its time zone). Returns what happened:
    {"day" (`today`, or None), "sent", "failed", "skipped", "tenants": [{"slug", "name", "day" (the facility's), "digests" (sent),
    "quiet" (chosen, nothing to list), "reminders" (emails sent), "reminded" (contracts they listed), "contracts" (in a reminder
    stage), "incident_reminders" (emails sent), "incidents" (incidents they listed), "failed", "skipped": Counter of (kind,
    SKIP_REASONS key), "error"}]}; "error" is set when a facility could not be worked through at all (one failure)."""
    summary = {"day": today, "sent": 0, "failed": 0, "skipped": 0, "tenants": []}
    tenants = Tenant.objects.filter(is_active=True).order_by("slug")  # a system table: read before any tenant is set
    if facility is not None:
        tenants = tenants.filter(pk=facility.pk)
    for tenant in tenants:
        counts = {"slug": tenant.slug, "name": tenant.name, "day": today, "digests": 0, "quiet": 0, "reminders": 0, "reminded": 0,
                  "contracts": 0, "incident_reminders": 0, "incidents": 0, "failed": 0, "skipped": Counter(), "error": ""}
        try:
            with tenant_context(tenant):
                counts["day"] = today or local_today()  # inside the facility's context: its today
                _send_facility(tenant, counts["day"], counts)
        except Exception as e:  # e.g. the database went away mid-run: the next facility still gets its emails
            log.exception("Staff notifications for %s failed", tenant.slug)
            counts["error"] = f"{type(e).__name__}: {e}"
            counts["failed"] += 1
            if not connection.is_usable():  # a connection that broke is not reopened on its own outside a request: the next facility needs one
                connection.close()
        summary["tenants"].append(counts)
        summary["sent"] += counts["digests"] + counts["reminders"] + counts["incident_reminders"]
        summary["failed"] += counts["failed"]
        summary["skipped"] += sum(counts["skipped"].values())
    return summary
