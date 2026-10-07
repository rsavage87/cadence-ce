"""
The survey binder's "Technician qualifications" section (slice 25): the facility's technicians and their credentials as of today, and
the work completed in the period by a technician who was not credentialed for the device on the day it was completed.

Who did the work: the technicians on its labor lines; when none logged time, the technician it is assigned to. Left out: work orders
imported from the previous system (Source.IMPORTED: who did them was not recorded in Cadence's terms) and vendor service (Cadence keeps
no record of a vendor's staff). Credentialed on a day: each credential as it stood at the end of that day, from its history
(apps.credentials.services.credential_versions, version_on), judged by the one rule qualification() uses too (credential_standing): a
credential renewed after it lapsed leaves the days between uncovered, one removed later still covers the days before, and one typed in
after it was issued covers the days since its issue date.

Queries: the technicians, the active technicians' credentials, the period's work order counts, the work orders to check, their labor
lines' technicians, and the credentials' history; whatever the number of work orders. qualification() is never called per row.
"""
from __future__ import annotations

from collections import defaultdict
from typing import NamedTuple

from django.conf import settings
from django.db.models import Count, Q
from django.urls import reverse

from apps.credentials.models import Credential, Technician
from apps.credentials.services import COVERS, EXPIRED, IN_TRAINING, SCOPE_SHORT, credential_state, credential_versions, standings_on
from apps.recalls.services import fmt_date
from apps.workorders.models import LaborLine, Source, WorkOrder, WoStatus, WoType

from . import DEVICE, FINDING, WORK_ORDER, Figure, Gap, Period, Section, Table

KEY, TITLE = "staff", "Technician qualifications"
DONE = (WoStatus.COMPLETED, WoStatus.CLOSED)
NONE_MATCHING = "None matching"
TYPE_LABELS = dict(WoType.choices)


class _Model(NamedTuple):
    """What a credential is matched against (apps.credentials.services.credential_names): the device model's names today."""
    category: str
    manufacturer: str
    model: str

    def __str__(self):
        return f"{self.manufacturer} {self.model}"


def _state_words(credential: Credential, today) -> str:
    key = credential_state(credential, today)["key"]
    return {"ok": "Current", "expiring": f"Expiring within {settings.CREDENTIAL_EXPIRY_WARNING_DAYS} days", "expired": "Expired",
            "in_training": "In training"}[key]


def _what_they_had(standings: list) -> str:
    """Why a technician was not credentialed that day, from the credentials naming the device then: expired (on its date), in
    training, or none naming it at all."""
    words = []
    expired = [v.expires_on for v, s in standings if s == EXPIRED]
    if expired:
        words.append(f"Expired on {fmt_date(max(expired))}")
    if any(s == IN_TRAINING for _, s in standings):
        words.append("In training")
    return "; ".join(words) or NONE_MATCHING


def _credentials_url(technician: Technician | None) -> str:
    """The Users and access credentials tab, for that technician while they are active (the tab lists active technicians only)."""
    url = reverse("web:credentials")
    return f"{url}?technician={technician.pk}" if technician is not None and technician.is_active else url


def _unqualified_work(period: Period, technicians: dict) -> tuple[list, dict]:
    """The work completed in the period by someone not credentialed for the device that day: (rows, counts)."""
    done = WorkOrder.objects.filter(status__in=DONE, completed_on__gte=period.start, completed_on__lte=period.end)
    counts = done.aggregate(imported=Count("id", filter=Q(source=Source.IMPORTED)),
                            vendor=Count("id", filter=Q(vendor_service=True) & ~Q(source=Source.IMPORTED)))
    checked = done.exclude(source=Source.IMPORTED).filter(vendor_service=False)
    work = list(checked.order_by("-completed_on", "number").values(
        "id", "number", "type", "completed_on", "assigned_to_id", "asset__tag", "asset__device_model_id", "asset__device_model__category",
        "asset__device_model__manufacturer", "asset__device_model__model"))
    by_labor = defaultdict(set)
    for wo_id, tech_id in (LaborLine.objects.filter(work_order__in=checked.order_by().values("id"), technician__isnull=False)
                           .order_by().values_list("work_order_id", "technician_id").distinct()):
        by_labor[wo_id].add(tech_id)
    doers = {w["id"]: by_labor.get(w["id"]) or ({w["assigned_to_id"]} if w["assigned_to_id"] else set()) for w in work}
    timelines = credential_versions({t for ids in doers.values() for t in ids})

    rows, unnamed = [], 0
    for w in work:
        if not doers[w["id"]]:
            unnamed += 1
            continue
        model = _Model(w["asset__device_model__category"], w["asset__device_model__manufacturer"], w["asset__device_model__model"])
        day = w["completed_on"]
        for tech_id in sorted(doers[w["id"]], key=lambda t: technicians[t].name if t in technicians else ""):
            standings = standings_on(timelines.get(tech_id, []), model, day)
            if any(s == COVERS for _, s in standings):
                continue
            technician = technicians.get(tech_id)
            rows.append({"number": w["number"], "type": TYPE_LABELS.get(w["type"], w["type"]), "completed_on": day, "tag": w["asset__tag"],
                         "model_id": w["asset__device_model_id"], "model": str(model), "technician": technician, "technician_id": tech_id,
                         "had": _what_they_had(standings)})
    counts.update(checked=len(work) - unnamed, unnamed=unnamed)
    return rows, counts


def _findings(rows: list) -> list[Gap]:
    """One per technician and device model: how many work orders they completed on it without a credential for it that day."""
    groups = defaultdict(set)
    for r in rows:
        groups[(r["technician_id"], r["model_id"])].add(r["number"])
    by_key = {(r["technician_id"], r["model_id"]): r for r in rows}
    out = []
    for key, numbers in groups.items():
        r = by_key[key]
        name = r["technician"].name if r["technician"] is not None else "A former technician"
        n = len(numbers)
        out.append((name, r["model"], Gap(FINDING, f"{name} completed {n} work order{'' if n == 1 else 's'} on {r['model']} without a "
                                                   f"credential for it on the day", url=_credentials_url(r["technician"]), record=name)))
    return [gap for _name, _model, gap in sorted(out, key=lambda item: item[:2])]


def build(period: Period, user) -> Section:
    today = period.today
    technicians = {t.id: t for t in Technician.objects.all()}  # former ones too: they may have done work in the period
    active = sorted((t for t in technicians.values() if t.is_active), key=lambda t: t.name)
    credentials = list(Credential.objects.filter(technician__is_active=True))
    by_technician = defaultdict(list)
    for c in credentials:
        by_technician[c.technician_id].append(c)
    states = {c.id: credential_state(c, today)["key"] for c in credentials}

    def soonest(tech):
        current = [c.expires_on for c in by_technician[tech.id] if c.status == Credential.Status.ACTIVE and c.expires_on and c.expires_on >= today]
        return min(current, default=None)

    tech_rows = [[t.name, t.title, t.certification, len(by_technician[t.id]), soonest(t)] for t in active]
    names = {t.id: t.name for t in active}
    cred_rows = [[names[c.technician_id], SCOPE_SHORT.get(c.scope, c.scope), c.value, c.source, c.issued_on, c.expires_on,
                  c.get_status_display(), _state_words(c, today)]
                 for c in sorted(credentials, key=lambda c: (names[c.technician_id], c.scope, c.value))]

    work, counts = _unqualified_work(period, technicians)
    work_rows = [[r["number"], r["type"], r["completed_on"], r["tag"], r["model"],
                  r["technician"].name if r["technician"] is not None else "", r["had"]] for r in work]
    unnamed = counts["unnamed"]
    figures = [
        Figure("Active technicians", len(active)),
        Figure("Credentials expiring soon", sum(s == "expiring" for s in states.values()),
               f"current today, expiring within {settings.CREDENTIAL_EXPIRY_WARNING_DAYS} days"),
        Figure("Expired credentials", sum(s == "expired" for s in states.values()), "not a gap: the technician may no longer do that work"),
        Figure("Work orders checked", counts["checked"],
               "completed in the period by in-house technicians" + (f"; {unnamed} named no technician and could not be checked" if unnamed else "")),
        Figure("Imported work orders not checked", counts["imported"], "brought over from the previous system"),
    ]
    tables = [
        Table(key="technicians", title="Technicians", columns=["Technician", "Title", "Certification", "Credentials", "Soonest expiry"],
              rows=lambda: iter(tech_rows), count=len(tech_rows), empty="No active technicians."),
        Table(key="credentials", title="Credentials", columns=["Technician", "Scope", "Covers", "Source", "Issued", "Expires", "Status", "Today"],
              rows=lambda: iter(cred_rows), count=len(cred_rows), empty="No credentials recorded for the active technicians."),
        Table(key="uncredentialed_work", title="Work done without a credential that day",
              columns=["Work order", "Type", "Completed on", "Device", "Model", "Technician", "What they had"],
              rows=lambda: iter(work_rows), count=len(work_rows), links={0: WORK_ORDER, 3: DEVICE},
              empty="No work completed in this period was done without a credential for the device that day."),
    ]
    return Section(
        key=KEY, title=TITLE,
        topic="That the technicians who maintain the equipment are qualified for it: their credentials, and work done without one.",
        covers=f"{period.label} for the work done; technicians and credentials {period.as_of_today}",
        figures=figures, gaps=_findings(work), tables=tables,
        notes=[
            "Who did the work: the technicians who logged time on it; when nobody logged time, the technician it is assigned to.",
            f"Left out: work orders imported from the previous system ({counts['imported']} in the period) and vendor service "
            f"({counts['vendor']}): Cadence keeps no record of a vendor's staff or their training.",
            "Credentials are read as they stood at the end of each day work was completed, from their history: a credential renewed after "
            "it lapsed leaves the days between uncovered, one removed later still covers the days before, and one entered in Cadence "
            "after it was issued covers the days since its issue date.",
            "A credential covers a device when it names the device's category, manufacturer, or model (the model's names today), is "
            "active rather than in training, and had not expired that day: the same rule Cadence uses when work is assigned.",
            "Expired credentials are counted, not listed as gaps: a technician may no longer do that work. A technician's soonest "
            "expiry is among the credentials current today.",
        ],
    )
