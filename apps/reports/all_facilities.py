"""
All facilities (slice 22): the Overview's figures for every facility a person has joined, side by side, with totals. The web page
(apps.web.views_all_facilities) and the API (apps.api.views_all_facilities) show these numbers and work out none of their own.

Which facilities: the person's accounts (apps.accounts.people) they have joined and can open now, the one they are signed in to
included; never a pending invitation, a deactivated account, or one in a deactivated facility. An account in one facility (no person)
gets its own facility alone, and so does a superuser working in the facility they picked (Admin, Tenants).

Each facility is read inside itself with the person's account there (people.in_facility): its rows (under row-level security another
facility's are hidden), its time zone, so its own today and current month, and the account's role there, which must give that
facility's Overview as the Overview screen asks (`may_see_overview`). A facility whose Overview the person may not see is listed by
name with no figures ("no access to its Overview") and left out of the totals: it is theirs (the facility menu lists it), its numbers
are not theirs to see. Only plain values leave a facility's block, never a queryset or a model instance, whose lazy reads would run
in whichever facility is current by then.

The figures are the Overview's (apps.reports.services.overview_kpis) for each facility's current month to date, all read at one instant
(`now`), so near midnight two facilities in different time zones may be in different days, or months. The totals add the parts up and
work every rate out again from the sums, never by averaging rates: PM on time is the summed on time over the summed due (100% when
nothing was due, as on the Overview), the mean time to repair is weighted by the repairs closed, uptime is 100 less the summed downtime
days over the summed device-days, and the cost of service ratio is the summed annual cost over the summed acquisition value. Alerts
needing action count each alert once: a recall alert is shared by every facility it matches, so one that needs action in two
facilities is one alert in the totals.

Slice 27, the PM completion window (apps.pm.windows): each facility's PM figures are counted by its own window, read inside it, never
another's (nothing caches a window across facilities). Its row says the window in words (`pm_window`, the Overview's window_words)
and how many PMs due so far are still inside it (`pm_pending`, `life_support_pm_pending`). The totals still add the parts up, and say
when the facilities' windows differ (`pm_windows_differ`): each facility is then judged by its own policy.
"""
from datetime import date, datetime

from django.utils import timezone

from apps.accounts import people
from apps.accounts.models import Level, Module
from apps.pm.windows import windows
from apps.tenants.context import tenant_context
from apps.workorders import scoping

from .services import alerts_needing_action_ids, overview_kpis, window_words

# A facility's figures, in the order the page shows them. Every one is a plain int or float.
FIGURES = ("active_devices", "open_work_orders", "overdue_work_orders", "awaiting_parts",
           "pm_due", "pm_on_time", "pm_rate", "life_support_pm_due", "life_support_pm_on_time", "life_support_pm_rate",
           "repairs_closed", "mttr_days", "repair_spend", "downtime_days", "device_days", "uptime_pct",
           "cost_of_service", "acquisition_value", "cost_of_service_ratio_pct", "alerts_needing_action")
# Slice 27: a facility's PMs due so far still inside its PM window, added up as they are; kept out of FIGURES (the API's figures).
PENDING = ("pm_pending", "life_support_pm_pending")
# The figures that add up across facilities as they are (counts, days, money); the totals work the others out from these.
SUMMED = ("active_devices", "open_work_orders", "overdue_work_orders", "awaiting_parts", "pm_due", "pm_on_time", "life_support_pm_due",
          "life_support_pm_on_time", "repairs_closed", "repair_spend", "downtime_days", "device_days", "cost_of_service", "acquisition_value")


def may_see_overview(account) -> bool:
    """Whether `account` may see its facility's Overview, the Overview screen's own test (apps.web.views.overview): Reports View, and
    a role that sees the whole facility (apps.workorders.scoping). Call it inside the account's facility, on the account loaded there."""
    return account.has_level(Module.REPORTS, Level.VIEW) and not scoping.is_scoped(account)


def _rate(part, whole) -> float:
    """pm_on_time_rate's rule: the share in percent, 100 when nothing was due."""
    return part / whole * 100 if whole else 100.0


def facility_figures(now: datetime) -> dict:
    """The current facility's Overview figures for its current month to date, at the instant `now` in its time zone: plain values,
    plus `today`, `month` (its first day), and `alert_ids` (the alerts needing action, which the totals unite)."""
    today = timezone.localdate(now)
    w = windows()  # this facility's: read inside it, every time (never one facility's window for another)
    k = overview_kpis(today.year, today.month, today, w=w)
    pm, ls, cost = k["pm_on_time"], k["pm_on_time_life_support"], k["cost_of_service"]
    alert_ids = frozenset(alerts_needing_action_ids())
    return {
        "today": today, "month": date(today.year, today.month, 1),
        "active_devices": k["active_devices"], "open_work_orders": k["open_work_orders"],
        "overdue_work_orders": k["overdue_work_orders"], "awaiting_parts": k["awaiting_parts"],
        "pm_due": pm["due"], "pm_on_time": pm["on_time"], "pm_rate": float(pm["rate"]),
        "life_support_pm_due": ls["due"], "life_support_pm_on_time": ls["on_time"], "life_support_pm_rate": float(ls["rate"]),
        "repairs_closed": k["repairs_closed"], "mttr_days": float(k["mttr_days"]), "repair_spend": float(k["repair_spend"]),
        "downtime_days": k["downtime_days"], "device_days": k["active_devices"] * k["period"]["days"], "uptime_pct": float(k["uptime_pct"]),
        "cost_of_service": float(cost["total"]), "acquisition_value": float(cost["acquisition"]),
        "cost_of_service_ratio_pct": float(cost["ratio_pct"]),
        "alert_ids": alert_ids, "alerts_needing_action": len(alert_ids),
        "pm_window": window_words(w), "pm_pending": pm["pending"], "life_support_pm_pending": ls["pending"],
    }


def _row(account, tenant, current: bool, now: datetime) -> dict:
    """`tenant`'s row, read inside it (the caller's block) as `account` loaded there."""
    row = {"account": account.pk, "name": tenant.name, "slug": tenant.slug, "current": current}
    if not may_see_overview(account):
        return {**row, "overview": False}
    return {**row, "overview": True, **facility_figures(now)}


def totals(rows: list[dict]) -> dict | None:
    """The rows with figures added up, every rate worked out from the sums (the module docstring), and each alert counted once.
    None when no row has figures."""
    shown = [r for r in rows if r["overview"]]
    if not shown:
        return None
    t = {key: sum(r[key] for r in shown) for key in SUMMED + PENDING}
    repairs, device_days = t["repairs_closed"], t["device_days"]
    alert_ids = frozenset().union(*(r["alert_ids"] for r in shown))
    return {
        **t, "facilities": len(shown),
        "pm_rate": _rate(t["pm_on_time"], t["pm_due"]),
        "life_support_pm_rate": _rate(t["life_support_pm_on_time"], t["life_support_pm_due"]),
        "mttr_days": sum(r["mttr_days"] * r["repairs_closed"] for r in shown) / repairs if repairs else 0.0,
        "uptime_pct": 100.0 - (t["downtime_days"] / device_days * 100 if device_days else 0.0),
        "cost_of_service_ratio_pct": t["cost_of_service"] / t["acquisition_value"] * 100 if t["acquisition_value"] else 0.0,
        "alert_ids": alert_ids, "alerts_needing_action": len(alert_ids),
        "pm_windows_differ": len({_window_key(r["pm_window"]) for r in shown}) > 1,
    }


def _window_key(words: dict) -> tuple:
    return (words["high"], words["other"])


def joined_accounts(user) -> list:
    """The accounts All facilities lists for `user`'s person: this one, and every other they have joined and can open now (User and
    Tenant only, like people.facility_menu), by facility name."""
    return [a for a in people.accounts_of(user) if a.pk == user.pk or people.is_joined(a)]


def all_facilities(user, tenant=None, now: datetime | None = None) -> dict:
    """{"facilities": one row per facility (`_row`: account, name, slug, current, overview, and the figures when overview), "totals"}.
    `tenant` is the facility the request works in: it names the facility of an account that has none of its own (a superuser who
    picked one in Admin). `now` is the instant every facility is read at (the clock, once)."""
    now = now or timezone.now()
    if not user.tenant_id:
        if tenant is None:
            return {"facilities": [], "totals": None}
        with tenant_context(tenant):  # a superuser: its levels are every module's, in any facility
            rows = [_row(user, tenant, True, now)]
        return {"facilities": rows, "totals": totals(rows)}
    rows = []
    for account in joined_accounts(user):
        with people.in_facility(account) as fresh:
            rows.append(_row(fresh, fresh.tenant, account.pk == user.pk, now))
    return {"facilities": rows, "totals": totals(rows)}
