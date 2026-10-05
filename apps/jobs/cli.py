"""
The options the facility-by-facility daily commands share (slice 21): `--tenant` (one facility) and `--date` (a missed day).
`generate_pm`, `send_report_emails`, and `send_staff_notifications` take both; the daily job (apps.jobs.services) runs each of them
for one facility and that facility's local day, and an operator runs them for every facility or one, today or a past day.

Without --date each facility works as of its own today (its time zone, Tenant.timezone). A --date is a day that has already come:
refused when it is still in the future on a clock it would run on (each facility's listed, or the server's when there is none).
These read only the Tenant table, a system table, before any facility's tenant_context (CLAUDE.md, non-negotiable 2).
"""
from collections.abc import Callable
from datetime import date

from django.core.management.base import CommandError
from django.utils import timezone

from apps.tenants.context import zone_override
from apps.tenants.models import Tenant


def parse_day(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as e:
        raise CommandError(f"--date must be YYYY-MM-DD; got {value!r}") from e


def facilities(slug: str | None = None) -> list[Tenant]:
    """The active facilities a command works through, by slug: every one, or the one `slug` names (CommandError when no active
    facility has it, so a typo is not a silent run that did nothing)."""
    tenants = Tenant.objects.filter(is_active=True).order_by("slug")  # a system table: read before any tenant is set
    if slug:
        tenants = tenants.filter(slug=slug)
        if not tenants:
            raise CommandError(f"No active facility has the slug {slug!r}.")
    return list(tenants)


def today_at(tenant: Tenant | None, today: Callable[[], date] = timezone.localdate) -> date:
    """`today()` on `tenant`'s clock (its time zone), or on the server's (TIME_ZONE) for None."""
    with zone_override(tenant):
        return today()


def refuse_future(day: date, tenants: list[Tenant], today: Callable[[], date] = timezone.localdate) -> None:
    """CommandError when `day` has not come yet at one of `tenants` (each on its own clock), or, with none, on the server's.
    `today` is the command's clock (the emails' local_today, which their tests pin)."""
    for tenant in tenants or [None]:
        now_day = today_at(tenant, today)
        if day > now_day:
            where = f": it is still {now_day:%Y-%m-%d} at {tenant.slug}" if tenant is not None else ""
            raise CommandError(f"--date cannot be in the future{where}.")


def days_worded(summary: dict, today: Callable[[], date] = timezone.localdate) -> str:
    """The day a sending command's summary line names: the --date asked for, or each facility's own today (one date when they all
    agree, the server's today when there was no facility). `summary` is send_due's: {"day", "tenants": [{"day", ...}]}."""
    days = [summary["day"]] if summary["day"] is not None else sorted({c["day"] for c in summary["tenants"] if c["day"]})
    if len(days) > 1:
        return "each facility's today (" + ", ".join(f"{d:%Y-%m-%d}" for d in days) + ")"
    return f"{(days[0] if days else today()):%Y-%m-%d}"
