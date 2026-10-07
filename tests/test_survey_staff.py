"""
The survey binder's "Technician qualifications" section (slice 25, part 3): technicians and credentials as of today, and the work
completed in the period by someone not credentialed for the device that day, read from each credential's history as it stood at the end
of that day, by the one rule qualification() uses (apps.credentials.services.credential_standing).
"""
import uuid
from datetime import date, datetime, time, timedelta
from datetime import timezone as dt_timezone
from zoneinfo import ZoneInfo

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from pg_helpers import as_app_role, needs_postgres
from survey_helpers import gaps_of, period, rows_of

from apps.credentials import services as cred
from apps.credentials.models import Credential, Scope, Technician
from apps.reports.survey import DEVICE, FINDING, WORK_ORDER, staff
from apps.tenants.context import tenant_context
from apps.workorders.models import LaborLine, Source, WorkOrder, WoStatus, WoType

TODAY = date(2026, 10, 7)
P = period(date(2026, 1, 1), TODAY)
H = Credential.history.model
WORK = "uncredentialed_work"


def at(day: date, hour: int = 12, minute: int = 0, zone=None):
    """A moment on `day` in `zone` (the active one, the facility's, by default)."""
    moment = datetime.combine(day, time(hour, minute))
    return moment.replace(tzinfo=zone) if zone else timezone.make_aware(moment)


def saved(credential, *moments):
    """Date the credential's history rows, oldest first, to `moments` (one per row). `credential` is one, or its id (a removed one's
    instance has lost it)."""
    rows = list(H.objects.filter(id=getattr(credential, "id", credential)).order_by("history_date", "history_id"))
    assert len(rows) == len(moments), [r.history_type for r in rows]
    for rec, moment in zip(rows, moments):
        H.objects.filter(history_id=rec.history_id).update(history_date=moment)


def done(asset, day, tech=None, labor=(), kind=WoType.REPAIR, **extra):
    """A work order completed on `day`, assigned to `tech`, with a labor line for each of `labor`."""
    wo = WorkOrder.objects.create(asset=asset, type=kind, status=WoStatus.COMPLETED, opened_on=day - timedelta(days=2), due_on=day,
                                  completed_on=day, problem="Alarm", assigned_to=tech, **extra)
    for t in labor:
        LaborLine.objects.create(work_order=wo, technician=t, worked_on=day, hours=1, rate=60)
    return wo


def flagged(section) -> list[tuple]:
    """(work order, technician, what they had) for each row of the work done without a credential."""
    return [(r[0], r[5], r[6]) for r in rows_of(section, WORK)]


@pytest.fixture
def dana(ctx):
    return Technician.objects.create(name="Dana Whitfield", title="Lead BMET", certification="CBET")


@pytest.fixture
def tom(ctx):
    return Technician.objects.create(name="Tom Okafor", title="BMET I")


def g5(technician, **kw):
    """A model credential for the Hamilton-G5 (the `vent` fixture's model)."""
    return cred.add_credential(technician, scope=Scope.MODEL, value="Hamilton-G5", **kw)


# --- credentials as they stood on the day ------------------------------------------------------------------------------------

def test_work_during_a_lapse_is_flagged_and_work_after_renewal_is_not(vent, dana):
    c = g5(dana, issued_on=date(2025, 1, 1), expires_on=date(2026, 3, 31))
    cred.renew_credential(c, today=date(2026, 4, 20))
    saved(c, at(date(2025, 1, 2)), at(date(2026, 4, 20)))
    before, lapse, after = done(vent, date(2026, 3, 15), dana), done(vent, date(2026, 4, 10), dana), done(vent, date(2026, 4, 25), dana)
    s = staff.build(P, None)
    assert flagged(s) == [(lapse.number, "Dana Whitfield", "Expired on Mar 31, 2026")]
    assert {before.number, after.number}.isdisjoint(r[0] for r in rows_of(s, WORK))
    [finding] = gaps_of(s)
    assert finding.kind == FINDING and finding.record == "Dana Whitfield"
    assert finding.text == "Dana Whitfield completed 1 work order on Hamilton Medical Hamilton-G5 without a credential for it on the day"
    assert finding.url == f"/users/credentials/?technician={dana.pk}"
    assert s.table(WORK).links == {0: WORK_ORDER, 3: DEVICE}
    assert rows_of(s, WORK)[0][:5] == [lapse.number, "Corrective repair", date(2026, 4, 10), vent.tag, "Hamilton Medical Hamilton-G5"]


def test_work_before_a_credential_was_removed_is_not_flagged(vent, dana):
    c = g5(dana, issued_on=date(2025, 1, 1))
    cid = c.id
    cred.remove_credential(c)
    saved(cid, at(date(2025, 1, 2)), at(date(2026, 6, 1)))
    before = done(vent, date(2026, 5, 20), dana)
    on_the_day, after = done(vent, date(2026, 6, 1), dana), done(vent, date(2026, 6, 10), dana)
    got = flagged(staff.build(P, None))
    assert before.number not in [n for n, _, _ in got]
    # Read at the end of each day: on the day it was removed, it no longer stood.
    assert sorted(got) == sorted([(on_the_day.number, "Dana Whitfield", "None matching"), (after.number, "Dana Whitfield", "None matching")])


def test_a_credential_typed_in_later_than_it_was_issued_covers_the_days_since(vent, dana):
    c = g5(dana, issued_on=date(2026, 2, 1))
    saved(c, at(date(2026, 6, 1)))  # entered in Cadence four months after it was issued
    covered, too_early = done(vent, date(2026, 3, 1), dana), done(vent, date(2026, 1, 15), dana)
    assert flagged(staff.build(P, None)) == [(too_early.number, "Dana Whitfield", "None matching")]
    assert covered.number


def test_work_while_in_training_is_flagged_with_that_reason(vent, dana):
    c = g5(dana, status=Credential.Status.IN_TRAINING, issued_on=date(2026, 1, 2))
    cred.sign_off_credential(c, today=date(2026, 4, 1))
    saved(c, at(date(2026, 1, 2)), at(date(2026, 4, 1)))
    training, signed_off = done(vent, date(2026, 3, 1), dana), done(vent, date(2026, 4, 5), dana)
    assert flagged(staff.build(P, None)) == [(training.number, "Dana Whitfield", "In training")]
    assert signed_off.number


def test_an_expired_and_an_in_training_credential_are_both_named(vent, dana):
    c = cred.add_credential(dana, scope=Scope.CATEGORY, value="Ventilators", issued_on=date(2024, 1, 1), expires_on=date(2026, 2, 1))
    t = g5(dana, status=Credential.Status.IN_TRAINING, issued_on=date(2026, 1, 1))
    saved(c, at(date(2024, 1, 2)))
    saved(t, at(date(2026, 1, 2)))
    wo = done(vent, date(2026, 3, 1), dana)
    assert flagged(staff.build(P, None)) == [(wo.number, "Dana Whitfield", "Expired on Feb 1, 2026; In training")]


def test_the_technicians_on_the_labor_lines_did_the_work_else_the_assignee(vent, dana, tom):
    c = g5(dana, issued_on=date(2025, 1, 1))
    saved(c, at(date(2025, 1, 2)))
    by_tom = done(vent, date(2026, 5, 1), dana, labor=[tom])  # assigned to Dana, but Tom logged the time
    both = done(vent, date(2026, 5, 2), tom, labor=[dana, tom])
    dana_alone = done(vent, date(2026, 5, 3), tom, labor=[dana])  # Dana logged it all: Tom, the assignee, is not asked
    tom_assigned = done(vent, date(2026, 5, 4), tom)  # nobody logged time: the assignee did it
    nobody = done(vent, date(2026, 5, 5))
    s = staff.build(P, None)
    assert sorted(flagged(s)) == sorted([(by_tom.number, "Tom Okafor", "None matching"), (both.number, "Tom Okafor", "None matching"),
                                         (tom_assigned.number, "Tom Okafor", "None matching")])
    assert dana_alone.number and nobody.number
    figures = {f.label: f for f in s.figures}
    assert figures["Work orders checked"].value == 4 and "1 named no technician" in figures["Work orders checked"].hint
    [finding] = gaps_of(s)
    assert finding.text == "Tom Okafor completed 3 work orders on Hamilton Medical Hamilton-G5 without a credential for it on the day"


def test_imported_and_vendor_work_is_never_checked(vent, dana):
    imported = done(vent, date(2026, 5, 1), dana, source=Source.IMPORTED)
    vendor = done(vent, date(2026, 5, 2), dana, vendor_service=True, vendor_name="Hamilton service")
    cancelled = done(vent, date(2026, 5, 3), dana)
    WorkOrder.objects.filter(pk=cancelled.pk).update(status=WoStatus.CANCELLED)
    s = staff.build(P, None)
    assert rows_of(s, WORK) == [] and gaps_of(s) == []
    figures = {f.label: f.value for f in s.figures}
    assert figures["Imported work orders not checked"] == 1 and figures["Work orders checked"] == 0
    assert any("imported from the previous system (1 in the period) and vendor service (1)" in n for n in s.notes)
    assert imported.number and vendor.number


def test_work_outside_the_period_is_not_checked(vent, dana):
    done(vent, date(2025, 12, 31), dana)
    open_wo = done(vent, date(2026, 5, 1), dana)
    WorkOrder.objects.filter(pk=open_wo.pk).update(status=WoStatus.IN_PROGRESS, completed_on=None)
    closed = done(vent, date(2026, 5, 2), dana)
    WorkOrder.objects.filter(pk=closed.pk).update(status=WoStatus.CLOSED)
    assert [n for n, _, _ in flagged(staff.build(P, None))] == [closed.number]


# --- one rule ----------------------------------------------------------------------------------------------------------------

def test_qualification_today_is_the_binders_rule_for_today(vent, pump, dana, tom):
    today = timezone.localdate()
    ana = Technician.objects.create(name="Ana Diaz")
    lee = Technician.objects.create(name="Lee Park")
    g5(dana)  # model, no expiry
    cred.add_credential(tom, scope=Scope.CATEGORY, value="Infusion pumps", issued_on=today - timedelta(days=800),
                        expires_on=today - timedelta(days=1))  # expired yesterday
    cred.add_credential(ana, scope=Scope.MANUFACTURER, value="BD", status=Credential.Status.IN_TRAINING)
    cred.add_credential(ana, scope=Scope.CATEGORY, value="Ventilators", expires_on=today)  # expires today: still covers today
    cred.add_credential(lee, scope=Scope.MANUFACTURER, value="Hamilton Medical", expires_on=today + timedelta(days=10))
    techs = [dana, tom, ana, lee]
    timelines = cred.credential_versions([t.id for t in techs])
    expected = set()
    for t in techs:
        for asset in (vent, pump):
            q = cred.qualification(t, asset, today).ok
            binder = any(s == cred.COVERS for _, s in cred.standings_on(timelines.get(t.id, []), asset.device_model, today))
            assert q == binder, (t.name, asset.tag)
            done(asset, today, t)
            if not q:
                expected.add((t.name, asset.tag))
    assert len(expected) == 5  # dana on the pump, tom on both, ana on the pump, lee on the pump
    s = staff.build(period(today - timedelta(days=30), today), None)
    assert {(r[5], r[3]) for r in rows_of(s, WORK)} == expected


def test_the_standing_rule_itself(vent_model):
    c = Credential(scope=Scope.CATEGORY, value="Ventilators", status=Credential.Status.ACTIVE, expires_on=date(2026, 5, 1))
    assert cred.credential_standing(c, vent_model, date(2026, 5, 1)) == cred.COVERS
    assert cred.credential_standing(c, vent_model, date(2026, 5, 2)) == cred.EXPIRED
    c.status = Credential.Status.IN_TRAINING
    assert cred.credential_standing(c, vent_model, date(2026, 5, 1)) == cred.IN_TRAINING
    c.value = "Infusion pumps"
    assert cred.credential_standing(c, vent_model, date(2026, 5, 1)) == ""


# --- the facility's days ---------------------------------------------------------------------------------------------------

def inside(tenant, zone):
    tenant.timezone = zone
    tenant.save(update_fields=["timezone"])
    return tenant_context(tenant)


def test_a_renewal_late_in_the_evening_counts_on_the_facilitys_day(tenant, vent, dana):
    chicago = ZoneInfo("America/Chicago")
    with inside(tenant, "America/Chicago"):
        c = g5(dana, issued_on=date(2025, 1, 1), expires_on=date(2026, 9, 30))
        cred.renew_credential(c, today=date(2026, 10, 5))
        renewed = at(date(2026, 10, 5), 23, 30, chicago)
        assert renewed.astimezone(dt_timezone.utc).date() == date(2026, 10, 6)  # UTC's day is the next one
        saved(c, at(date(2025, 1, 2), zone=chicago), renewed)
        lapse, renewal_day = done(vent, date(2026, 10, 4), dana), done(vent, date(2026, 10, 5), dana)
        got = flagged(staff.build(period(date(2026, 9, 1), TODAY), None))
    assert got == [(lapse.number, "Dana Whitfield", "Expired on Sep 30, 2026")] and renewal_day.number


def test_a_removal_just_after_midnight_counts_on_the_facilitys_day(tenant, vent, dana):
    tokyo = ZoneInfo("Asia/Tokyo")
    with inside(tenant, "Asia/Tokyo"):
        c = g5(dana, issued_on=date(2025, 1, 1))
        cid = c.id
        cred.remove_credential(c)
        removed = at(date(2026, 10, 6), 0, 30, tokyo)
        assert removed.astimezone(dt_timezone.utc).date() == date(2026, 10, 5)  # UTC's day is the one before
        saved(cid, at(date(2025, 1, 2), zone=tokyo), removed)
        last_day, after = done(vent, date(2026, 10, 5), dana), done(vent, date(2026, 10, 6), dana)
        got = flagged(staff.build(period(date(2026, 9, 1), TODAY), None))
    assert got == [(after.number, "Dana Whitfield", "None matching")] and last_day.number


# --- this facility only ------------------------------------------------------------------------------------------------------

def test_another_facilitys_history_is_never_read(tenant, other_tenant, vent, dana):
    """A credential history row of another facility that names our technician (planted: it cannot happen through the app) never
    covers their work: the history is read through core.history._rows."""
    with tenant_context(other_tenant):
        theirs = Technician.objects.create(name="Dana Whitfield")
        cred.add_credential(theirs, scope=Scope.MODEL, value="Hamilton-G5", issued_on=date(2025, 1, 1))
    H.objects.create(id=uuid.uuid4(), tenant_id=other_tenant.id, technician_id=dana.id, scope=Scope.MODEL, value="Hamilton-G5",
                     status=Credential.Status.ACTIVE, source="", issued_on=date(2025, 1, 1), created_at=at(date(2025, 1, 2)),
                     updated_at=at(date(2025, 1, 2)), history_date=at(date(2025, 1, 2)), history_type="+")
    wo = done(vent, date(2026, 5, 1), dana)
    s = staff.build(P, None)
    assert flagged(s) == [(wo.number, "Dana Whitfield", "None matching")]
    assert [r[0] for r in rows_of(s, "technicians")] == ["Dana Whitfield"]  # theirs is not ours
    assert s.table("credentials").columns == ["Technician", "Scope", "Covers", "Source", "Issued", "Expires", "Status", "Today"]
    assert rows_of(s, "credentials") == []


# --- technicians and credentials as of today ---------------------------------------------------------------------------------

def test_technicians_and_credentials_tables_and_figures(vent, dana, tom, settings):
    settings.CREDENTIAL_EXPIRY_WARNING_DAYS = 60
    g5(dana, source="OEM training", issued_on=date(2025, 1, 1), expires_on=TODAY + timedelta(days=30))
    cred.add_credential(dana, scope=Scope.CATEGORY, value="Infusion pumps", source="In-house sign-off", issued_on=date(2024, 1, 1),
                        expires_on=TODAY + timedelta(days=400))
    cred.add_credential(tom, scope=Scope.MANUFACTURER, value="BD", issued_on=date(2023, 1, 1), expires_on=date(2026, 1, 31))
    cred.add_credential(tom, scope=Scope.MODEL, value="Alaris 8015 PCU", status=Credential.Status.IN_TRAINING)
    gone = Technician.objects.create(name="Former Tech", is_active=False)
    cred.add_credential(gone, scope=Scope.MODEL, value="Hamilton-G5")
    s = staff.build(P, None)
    assert rows_of(s, "technicians") == [["Dana Whitfield", "Lead BMET", "CBET", 2, TODAY + timedelta(days=30)],
                                         ["Tom Okafor", "BMET I", "", 2, None]]
    assert s.table("credentials").columns == ["Technician", "Scope", "Covers", "Source", "Issued", "Expires", "Status", "Today"]
    assert rows_of(s, "credentials") == [
        ["Dana Whitfield", "Category", "Infusion pumps", "In-house sign-off", date(2024, 1, 1), TODAY + timedelta(days=400), "Active",
         "Current"],
        ["Dana Whitfield", "Model", "Hamilton-G5", "OEM training", date(2025, 1, 1), TODAY + timedelta(days=30), "Active",
         "Expiring within 60 days"],
        ["Tom Okafor", "Manufacturer", "BD", "", date(2023, 1, 1), date(2026, 1, 31), "Active", "Expired"],
        ["Tom Okafor", "Model", "Alaris 8015 PCU", "", None, None, "In training", "In training"],
    ]
    figures = {f.label: f.value for f in s.figures}
    assert figures["Active technicians"] == 2 and figures["Credentials expiring soon"] == 1 and figures["Expired credentials"] == 1
    assert gaps_of(s) == []  # an expired credential is a figure, never a gap
    assert s.key == "staff" and s.title == "Technician qualifications" and "as of today, Oct 7, 2026" in s.covers


def test_no_requester_text_reaches_the_section(vent, dana):
    wo = done(vent, date(2026, 5, 1), dana)
    WorkOrder.objects.filter(pk=wo.pk).update(problem="CANARY-1", resolution="CANARY-2", requester="CANARY-3", callback="CANARY-4",
                                              reported_location="CANARY-5")
    s = staff.build(P, None)
    text = repr([s.figures, s.gaps, s.notes, s.topic, s.covers] + [list(t.rows()) for t in s.tables])
    assert wo.number in text and "CANARY" not in text


# --- a fixed number of queries -------------------------------------------------------------------------------------------------

def _populate(vent, pump, start: int, n: int):
    for i in range(start, start + n):
        t = Technician.objects.create(name=f"Tech {i:03d}")
        c = g5(t, issued_on=date(2025, 1, 1), expires_on=date(2026, 3, 1) if i % 2 else None)
        if i % 3 == 0 and c.expires_on:
            cred.renew_credential(c, today=date(2026, 4, 1))
        elif i % 3 == 0:
            cred.remove_credential(c)
        lead = Technician.objects.create(name=f"Lead {i:03d}")
        done(vent if i % 2 else pump, date(2026, 5, 1) + timedelta(days=i % 60), lead, labor=[t])
        done(vent, date(2026, 6, 1), t, source=Source.IMPORTED if i % 4 == 0 else Source.MANUAL)


def _queries(section_build) -> int:
    with CaptureQueriesContext(connection) as q:
        s = section_build()
        for t in s.tables:
            list(t.rows())
    return len(q.captured_queries)


def test_a_fixed_number_of_queries(vent, pump):
    _populate(vent, pump, 0, 5)
    small = _queries(lambda: staff.build(P, None))
    _populate(vent, pump, 5, 45)
    s = staff.build(P, None)
    assert len(rows_of(s, WORK)) > 20 and len(rows_of(s, "technicians")) == 100
    assert _queries(lambda: staff.build(P, None)) == small <= 8


# --- under row-level security ------------------------------------------------------------------------------------------------

@needs_postgres
def test_the_section_reads_history_as_the_runtime_role(tenant, other_tenant, vent, dana):
    c = g5(dana, issued_on=date(2025, 1, 1), expires_on=date(2026, 3, 31))
    cred.renew_credential(c, today=date(2026, 4, 20))
    saved(c, at(date(2025, 1, 2)), at(date(2026, 4, 20)))
    lapse, after = done(vent, date(2026, 4, 10), dana), done(vent, date(2026, 4, 25), dana)
    with tenant_context(other_tenant):
        theirs = Technician.objects.create(name="Their Tech")
        cred.add_credential(theirs, scope=Scope.MODEL, value="Hamilton-G5", issued_on=date(2025, 1, 1))
    as_app_role()
    with tenant_context(tenant):
        s = staff.build(P, None)
    assert flagged(s) == [(lapse.number, "Dana Whitfield", "Expired on Mar 31, 2026")] and after.number
    assert [r[0] for r in rows_of(s, "technicians")] == ["Dana Whitfield"]
