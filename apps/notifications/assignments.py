"""
Assignment emails (slice 20, part C): when a work order is assigned to a technician, the technician's own account (Technician.user)
gets an email saying so, once the change is committed.

- Where they start: apps.workorders.services, the one place work is assigned. `assign` (the drawer's Assign, the API's assign, a new
  work order opened with an assignee, a failed PM's repair) and `create_work_order` with `assigned_to` (the API's create) call
  `announce`. Vendor service, and a technician with no account, send nothing.
- Who gets one: the technician's linked user, only while that account belongs to the work order's facility (an active one), is active
  with an email address, wants assignment emails (apps.notifications.services.wants: on until they turn it off on the Notifications
  page), sees the whole facility and can view work orders (the people the Notifications page is for: a scoped role could not open the
  link), and did not make the assignment themselves.
- Once: each work order is announced to a user at most once, ever (NotificationSent: kind "assignment", key the work order, the
  user). Assigning it to the same technician again, or back to them after someone else had it, does not repeat it; assigning it to
  someone else tells the new technician.
- In batches: inside `batch()` (Create on the PM schedule for a day, Auto-assign week) every announcement waits for the end of the
  batch, and each technician gets one email listing all their new work orders rather than one per work order.
- After commit: the email goes once the transaction that assigned the work commits (transaction.on_commit), so a rolled-back
  assignment sends nothing. The sender reads every work order again, inside its own facility whatever context made the assignment, and
  includes only those still open and still with that technician.
- What it says: for each work order its number, type, priority, due date, device tag and description, department, and a link
  (settings.APP_BASE_URL + the work order's page, which asks the reader to sign in). Never the problem, the requester, the callback,
  or the location: free text someone typed, which can name a patient (CLAUDE.md, "No PHI"). The template gets only those values,
  never the work order itself, so it cannot show more by accident.
- Never raises: a failed send is logged (apps.accounts.emails) and its claims released, so a later assignment of the same work order
  can try again; the assignment itself stands.

`quiet()` turns announcements off for what runs inside it (a data load, such as a demo seed, that assigns work nobody needs to hear
about).
"""
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from django.conf import settings
from django.db import IntegrityError, transaction
from django.urls import reverse

from apps.accounts import emails
from apps.accounts.models import Level, Module
from apps.tenants.context import tenant_context
from apps.tenants.models import Tenant
from apps.workorders.models import OPEN_STATUSES, WorkOrder
from apps.workorders.scoping import is_scoped

from .models import NotificationSent
from .services import wants

log = logging.getLogger(__name__)

TEMPLATE = "notifications/email/assignment"
KIND = NotificationSent.Kind.ASSIGNMENT

_batch: ContextVar = ContextVar("cadence_assignment_batch", default=None)
_quiet: ContextVar = ContextVar("cadence_assignment_quiet", default=False)


@dataclass(frozen=True)
class Announcement:
    """One assignment to announce: ids only, read again when the email is sent."""
    tenant_id: object
    work_order_id: object
    technician_id: object
    by_id: int | None


def key_for(work_order) -> str:
    """NotificationSent.key of a work order's assignment email (one per work order and user)."""
    return f"work_order:{work_order.pk}"


# --- announcing (inside the transaction that assigns) ----------------------------------------------------------------------------

def announce(work_order, technician, by=None) -> None:
    """`technician` now has `work_order` (assigned by `by`, or by nobody: a job). Inside a batch() the announcement waits for the end
    of the batch; otherwise its email is sent once the transaction commits. Does nothing for vendor service or a technician without
    an account, and nothing inside quiet()."""
    if technician is None or work_order.vendor_service or technician.user_id is None or _quiet.get():
        return
    item = Announcement(work_order.tenant_id, work_order.pk, technician.pk, getattr(by, "pk", None))
    pending = _batch.get()
    if pending is not None:
        pending.append(item)
    else:
        _send_after_commit([item])


@contextmanager
def batch():
    """Collect the announcements made inside, and once the transaction commits send each technician one email listing all their
    new work orders. A batch inside a batch joins it. If the block raises, nothing is announced."""
    if _batch.get() is not None:
        yield
        return
    pending: list = []
    token = _batch.set(pending)
    try:
        yield
    finally:
        _batch.reset(token)
    if pending:
        _send_after_commit(pending)


@contextmanager
def quiet():
    """Announce nothing that is assigned inside (a data load nobody needs to hear about)."""
    token = _quiet.set(True)
    try:
        yield
    finally:
        _quiet.reset(token)


def _send_after_commit(items: list) -> None:
    items = list(items)
    transaction.on_commit(lambda: send(items), robust=True)


# --- sending (after commit) ------------------------------------------------------------------------------------------------------

def send(items: list) -> int:
    """Send the emails for `items` (Announcements) now: in each facility, one email per user listing their work orders that are still
    open, still with the technician they were assigned to, and not announced to them before. Returns how many emails went out.
    Never raises."""
    sent = 0
    by_tenant: dict = {}
    for item in items:
        by_tenant.setdefault(item.tenant_id, []).append(item)
    for tenant_id, its in by_tenant.items():
        try:
            tenant = Tenant.objects.filter(pk=tenant_id, is_active=True).first()  # a system table: no tenant needed to read it
            if tenant is None:
                continue  # a deactivated facility emails nobody
            with tenant_context(tenant):
                sent += _send_in(tenant, its)
        except Exception:  # a database error must not reach the caller: the assignment is already saved
            log.exception("Could not send assignment emails in facility %s", tenant_id)
    return sent


def recipient_refusal(user, tenant) -> str | None:
    """Why `user` gets no assignment email in `tenant` (inside its context), or None. Short reasons for the log and the tests."""
    if user is None:
        return "no account"
    if user.tenant_id != tenant.id:
        return "another facility"
    if not wants(user, "assignments"):
        return "deactivated, no email address, or turned off"
    if is_scoped(user):
        return "sees only part of the facility"
    if not user.has_level(Module.WORKORDERS, Level.VIEW):
        return "no Work orders View"
    return None


def _send_in(tenant, items: list) -> int:
    # The last announcement of each work order is the one that counts: assigned to Dana, then to Tom, inside one batch, tells Tom.
    last = {}
    for item in items:
        last[item.work_order_id] = item
    wos = (WorkOrder.objects.filter(pk__in=list(last), status__in=OPEN_STATUSES, vendor_service=False, assigned_to__isnull=False)
           .select_related("asset__device_model", "asset__department", "assigned_to__user"))
    per_user: dict = {}
    for wo in wos:
        item = last[wo.pk]
        if wo.assigned_to_id != item.technician_id:
            continue  # with someone else since (or the assignment was undone): their own announcement covers it
        user = wo.assigned_to.user
        if user is None or user.pk == item.by_id:
            continue  # nobody to tell, or they assigned it to themselves
        per_user.setdefault(user.pk, (user, []))[1].append(wo)
    sent = 0
    for user, user_wos in per_user.values():
        why = recipient_refusal(user, tenant)
        if why:
            log.info("No assignment email to user %s: %s", user.pk, why)
            continue
        sent += _send_to(tenant, user, user_wos)
    return sent


def _claim(user, wo) -> bool:
    """Record that `wo`'s assignment is announced to `user`; False when it already was (the unique constraint is the lock)."""
    try:
        with transaction.atomic():
            NotificationSent.objects.create(kind=KIND, key=key_for(wo), user=user)
        return True
    except IntegrityError:
        return False


def _send_to(tenant, user, wos: list) -> int:
    told = set(NotificationSent.objects.filter(kind=KIND, user=user, key__in=[key_for(w) for w in wos]).values_list("key", flat=True))
    claimed = [w for w in wos if key_for(w) not in told and _claim(user, w)]
    if not claimed:
        return 0
    claimed.sort(key=lambda w: (w.due_on, w.number))
    if emails.send(user.email, TEMPLATE, context(tenant, user, claimed)):
        return 1
    NotificationSent.objects.filter(kind=KIND, user=user, key__in=[key_for(w) for w in claimed]).delete()  # not sent: free to try again
    return 0


def context(tenant, user, wos: list) -> dict:
    """Everything the email shows, and nothing else: no problem, requester, callback, or location; links only from APP_BASE_URL."""
    rows = []
    for wo in wos:
        asset, model = wo.asset, wo.asset.device_model
        rows.append({"number": wo.number, "type": wo.get_type_display(), "priority": wo.get_priority_display(), "due_on": wo.due_on,
                     "tag": asset.tag, "description": model.description or str(model), "department": asset.department.name,
                     "url": f"{settings.APP_BASE_URL}{reverse('web:wo', args=[wo.number])}"})
    return {"facility": tenant.name, "name": user.first_name, "email": user.email, "work_orders": rows, "count": len(rows),
            "preferences_url": f"{settings.APP_BASE_URL}{reverse('web:notifications')}"}
