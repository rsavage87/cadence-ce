"""
Emails to the person who sent a request through the public portal (slice 13): a confirmation once the request is saved, and a
notice when its work order is completed.

- Only while the facility confirms requests by email (Settings) and only to an address at one of its work email domains, both
  checked again at sending time: a request made before email was turned off, or at a domain since removed, gets nothing.
- Nothing the requester typed: not the problem (it can mention a patient), not the room or bed (free text, which would let anyone
  put a line of their own into the hospital's email). The templates get the plain values from _context(), never the request
  itself, so they cannot show it by accident.
- Confirmations are capped per address and per facility each hour (settings.PORTAL_EMAILS_PER_ADDRESS_PER_HOUR, ..._PER_FACILITY_
  PER_HOUR): the per-IP limit on the form can be dodged with a forged X-Forwarded-For, so the mail itself has its own limits.
- Sent after the transaction commits, inside the request's own tenant whatever context completed the work order (the web UI, the
  API, a management command, or none at all), reading only that tenant's rows.
- A sent email is noted in the work order's status history (a note with no status change), which the work order's timeline
  shows, which the portal's confirmation page reads, and which keeps the done notice to one per request however often the work
  order is reopened and completed again.
- Never raises: a failed email leaves the request and the status change as they are, and the sender gets False.
"""
import logging
from urllib.parse import urlencode

from django.conf import settings
from django.db import transaction
from django.urls import reverse

from apps.accounts import emails
from apps.facility import services as fs
from apps.tenants.context import tenant_context
from apps.workorders.models import ServiceRequest, WorkOrderStatusHistory, WoStatus

log = logging.getLogger(__name__)

RECEIVED_NOTE = "Confirmation emailed to the requester"
DONE_NOTE = "Completion notice emailed to the requester"
RECEIVED_TEMPLATE = "portal/email/request_received"
DONE_TEMPLATE = "portal/email/request_done"
DONE_STATUSES = (WoStatus.COMPLETED, WoStatus.CLOSED)


def request_received_after_commit(service_request) -> None:
    """Send the confirmation once the transaction that saved `service_request` commits (at once outside a transaction)."""
    transaction.on_commit(lambda: send_request_received(service_request), robust=True)


def request_done_after_commit(work_order) -> None:
    """Send the done notice once the transaction that completed `work_order` commits."""
    transaction.on_commit(lambda: send_request_done(work_order), robust=True)


def _within_caps(sr) -> bool:
    """Count this confirmation against the hour's caps; False (and nothing counted further) once either is reached."""
    from django.core.cache import caches

    cache = caches["limits"]
    for key, limit in ((f"portal-mail:addr:{sr.tenant_id}:{sr.requester_email.lower()}", settings.PORTAL_EMAILS_PER_ADDRESS_PER_HOUR),
                       (f"portal-mail:facility:{sr.tenant_id}", settings.PORTAL_EMAILS_PER_FACILITY_PER_HOUR)):
        cache.add(key, 0, 60 * 60)
        try:
            n = cache.incr(key)
        except ValueError:  # the hour ended between add and incr
            cache.set(key, 1, 60 * 60)
            n = 1
        if n > limit:
            log.warning("Portal confirmation not emailed for %s: hourly cap reached", sr.number)
            return False
    return True


def send_request_received(service_request) -> bool:
    """Email the requester the request number, the device, its department, the urgency, and the response target. False when
    nothing was sent: no address, email confirmations off, the address's domain no longer allowed, an hourly cap reached, or the
    send failed."""
    try:
        with tenant_context(service_request.tenant):
            sr = _request(pk=service_request.pk)
            return sr is not None and _send(sr, RECEIVED_TEMPLATE, RECEIVED_NOTE, capped=True)
    except Exception:  # a database error must not reach the portal's response or the caller's status change
        log.exception("Could not email the confirmation for service request %s", service_request.pk)
        return False


def send_request_done(work_order) -> bool:
    """Email the requester that the work on their request is done, with the completed date and how to report the problem again.
    Only for a work order that came from the portal and is still completed or closed, and only once per request."""
    try:
        with tenant_context(work_order.tenant):
            sr = _request(work_order_id=work_order.pk)
            if sr is None or sr.work_order.status not in DONE_STATUSES or sr.work_order.completed_on is None:
                return False
            if WorkOrderStatusHistory.objects.filter(work_order_id=sr.work_order_id, note=DONE_NOTE).exists():
                return False  # already told: a reopened and completed again work order does not email twice
            return _send(sr, DONE_TEMPLATE, DONE_NOTE)
    except Exception:  # a database error must not reach the caller: its status change is already saved
        log.exception("Could not email the done notice for work order %s", work_order.pk)
        return False


def confirmation_sent(sr: ServiceRequest) -> bool:
    """Whether the confirmation email for `sr` went out (the portal's confirmation page says so). Call inside sr's tenant."""
    return WorkOrderStatusHistory.objects.filter(work_order_id=sr.work_order_id, note=RECEIVED_NOTE).exists()


# --- helpers ------------------------------------------------------------------------------------------------

def _request(**lookup) -> ServiceRequest | None:
    """The request, read fresh inside the current tenant, with what the emails show."""
    return ServiceRequest.objects.select_related("tenant", "asset__device_model", "department", "work_order").filter(**lookup).first()


def _send(sr: ServiceRequest, template: str, note: str, capped: bool = False) -> bool:
    """`capped`: count against the hourly caps (the confirmation, which anyone can cause); counted only for an email that would go."""
    s = fs.get_settings()
    if not fs.email_allowed(sr.requester_email, s):
        return False
    if capped and not _within_caps(sr):
        return False
    if not emails.send(sr.requester_email, template, _context(sr, s)):
        return False
    wo = sr.work_order
    WorkOrderStatusHistory.objects.create(tenant_id=wo.tenant_id, work_order=wo, from_status=wo.status, to_status=wo.status, note=note)
    return True


def _context(sr: ServiceRequest, s) -> dict:
    """Everything the two emails show, and nothing else: no problem text, and links only from APP_BASE_URL."""
    from .views import RESPONSE_TARGETS  # the portal's promise, also shown on its confirmation page

    model = sr.asset.device_model
    new_request = reverse("portal:request", args=[sr.tenant.slug])
    return {"facility": sr.tenant.name, "number": sr.number, "name": sr.requester_name, "tag": sr.asset.tag,
            "description": model.description or str(model), "department": sr.department.name,
            "urgency": sr.get_urgency_display(), "target": RESPONSE_TARGETS.get(sr.urgency, ""), "hotline": s.portal_hotline,
            "completed_on": sr.work_order.completed_on,
            "new_request_url": f"{settings.APP_BASE_URL}{new_request}?{urlencode({'asset': sr.asset.tag})}"}
