"""Slice 20, part C: assignment emails (apps/notifications/assignments.py).

Every way work reaches a technician (the drawer's Assign, a new work order opened with an assignee on the screen and over the API,
the API's assign, Create on the PM schedule for a day, Auto-assign week) emails that technician's account exactly once, after the
transaction commits, and a batch sends each technician one email listing all their new work orders. Never the problem text, the
requester, the callback, or the location. No repeats (the same technician again, or back to them), the new technician on a
reassignment, nothing for vendor service, the assigner, an opted-out, inactive, addressless, scoped, or other-facility account, a
deactivated facility, or a rolled-back assignment. A failed send frees its claim; a database error never reaches the caller. On
PostgreSQL the sender works under the policies, whatever tenant the caller left set.
"""
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.conf import settings
from django.db import transaction
from django.urls import reverse
from pg_helpers import as_app_role, needs_postgres

from apps.accounts.models import Level, Role, User
from apps.equipment.models import Asset
from apps.notifications import assignments
from apps.notifications.models import NotificationPreference, NotificationSent
from apps.notifications.services import set_preferences
from apps.pm.models import PmProcedure
from apps.pm.services import assign_week, create_pm_work_orders_for_day
from apps.tenants.context import tenant_context
from apps.workorders.models import WorkOrder, WoStatus, WoType
from apps.workorders.services import assign, change_status, create_work_order

HX = {"HTTP_HX_REQUEST": "true"}
PROBLEM = "Alarm keeps sounding for the patient in bed 12, J. Doe"
REQUESTER, CALLBACK, LOCATION = "RN Lee Patel, ICU", "x4410", "ICU bed 12 by the window"
TODAY = date(2026, 9, 29)  # a Tuesday: the PM week is Sep 29 through Oct 5
SEP30 = date(2026, 9, 30)


@pytest.fixture
def staff(ctx, techs, make_user):
    """Dana and Tom (tests/conftest.py's technicians) linked to their own technician accounts, and Kim, the director who assigns."""
    out = {}
    for key, first in (("dana", "Dana"), ("tom", "Tom")):
        user = make_user("technician", username=f"{key}@riverside.example")
        user.email, user.first_name = user.username, first
        user.save(update_fields=["email", "first_name"])
        techs[key].user = user
        techs[key].save(update_fields=["user", "updated_at"])
        out[key] = user
    out["kim"] = make_user("director")
    return out


@pytest.fixture
def wo(ctx, vent):
    return create_work_order(asset=vent, type=WoType.REPAIR, priority="high", problem=PROBLEM, requester=REQUESTER, callback=CALLBACK,
                             reported_location=LOCATION)


@pytest.fixture
def committed(django_capture_on_commit_callbacks):
    """Run the block, then what it left for after the commit (as a real commit would)."""
    return lambda: django_capture_on_commit_callbacks(execute=True)


def to(mailoutbox, address) -> list:
    return [m for m in mailoutbox if m.to == [address]]


def numbers_in(message) -> list:
    return sorted(w.number for w in WorkOrder.objects.all() if w.number in message.body)


def assert_no_free_text(message):
    for text in (PROBLEM, "J. Doe", "bed 12", REQUESTER, CALLBACK, LOCATION, "Scheduled", "HA-G5-PM6"):
        assert text not in message.subject and text not in message.body, text


# --- one assignment -------------------------------------------------------------------------------------------------------------

def test_assigning_emails_the_technician_once_the_change_commits(staff, techs, wo, vent, committed, mailoutbox):
    with committed():
        assign(wo, technician=techs["dana"], by=staff["kim"])
        assert mailoutbox == []  # nothing before the commit
    assert len(mailoutbox) == 1
    m = mailoutbox[0]
    assert m.to == ["dana@riverside.example"] and m.subject == f"Assigned to you: {wo.number} · CE-10001, ICU ventilator · due {wo.due_on:%b} {wo.due_on.day}"
    link = f"{settings.APP_BASE_URL}/work-orders/{wo.number}/"
    for text in ("Hello Dana,", "A work order at Riverside Regional is assigned to you", f"{wo.number} · Corrective repair · High priority",
                 "Device: CE-10001 · ICU ventilator", "Department: ICU", f"Due: {wo.due_on:%A}, {wo.due_on:%b} {wo.due_on.day}, {wo.due_on.year}",
                 f"Open it: {link}", f"{settings.APP_BASE_URL}{reverse('web:notifications')}", "emailed to dana@riverside.example"):
        assert text in m.body, text
    assert_no_free_text(m)
    sent = NotificationSent.objects.get()
    assert (sent.kind, sent.key, sent.user) == ("assignment", f"work_order:{wo.pk}", staff["dana"])


def test_the_same_technician_again_is_not_told_twice_and_a_new_one_is(staff, techs, wo, committed, mailoutbox):
    with committed():
        assign(wo, technician=techs["dana"], by=staff["kim"])
    with committed():
        assign(wo, technician=techs["dana"], by=staff["kim"])  # again: already told
    assert [m.to for m in mailoutbox] == [["dana@riverside.example"]]
    with committed():
        assign(wo, technician=techs["tom"], by=staff["kim"])  # reassigned: Tom hears of it
    with committed():
        assign(wo, technician=techs["dana"], by=staff["kim"])  # back to Dana, who was told already
    assert [m.to for m in mailoutbox] == [["dana@riverside.example"], ["tom@riverside.example"]]
    assert NotificationSent.objects.count() == 2


def test_vendor_service_and_a_technician_without_an_account_send_nothing(staff, techs, wo, committed, mailoutbox):
    techs["tom"].user = None
    techs["tom"].save(update_fields=["user", "updated_at"])
    with committed():
        assign(wo, vendor_name="Hamilton Medical field service", by=staff["kim"])
        assign(wo, technician=techs["tom"], by=staff["kim"])
    assert mailoutbox == [] and not NotificationSent.objects.exists()


@pytest.mark.parametrize("change", ["opted_out", "inactive", "no_email", "assigner", "scoped", "no_wo_view", "other_facility", "facility_off"])
def test_who_is_never_emailed(staff, techs, wo, committed, mailoutbox, change, other_tenant, tenant):
    dana = staff["dana"]
    by = staff["kim"]
    if change == "opted_out":
        set_preferences(dana, assignments=False)
    elif change == "inactive":
        dana.is_active = False
    elif change == "no_email":
        dana.email = ""
    elif change == "assigner":
        by = dana  # she took it herself
    elif change == "scoped":
        dana.role = Role.objects.get(slug="vendor")
    elif change == "no_wo_view":
        role = Role.objects.create(name="Contracts only", slug="contracts-only")
        role.set_levels({"contracts": Level.EDIT})
        dana.role = role
    elif change == "other_facility":
        dana.tenant = other_tenant
    elif change == "facility_off":
        tenant.is_active = False
        tenant.save(update_fields=["is_active"])
    dana.save()
    with committed():
        assign(wo, technician=techs["dana"], by=by)
    assert mailoutbox == [] and not NotificationSent.objects.exists()


def test_turning_assignment_emails_back_on_brings_the_next_one(staff, techs, wo, vent, committed, mailoutbox):
    set_preferences(staff["dana"], assignments=False)
    with committed():
        assign(wo, technician=techs["dana"], by=staff["kim"])
    set_preferences(staff["dana"], assignments=True)
    second = create_work_order(asset=vent, type=WoType.REPAIR, priority="normal", problem="Fan noise")
    with committed():
        assign(second, technician=techs["dana"], by=staff["kim"])
    assert len(mailoutbox) == 1 and numbers_in(mailoutbox[0]) == [second.number]


# --- after commit only ----------------------------------------------------------------------------------------------------------

def test_a_rolled_back_assignment_sends_nothing(staff, techs, wo, committed, mailoutbox):
    with committed() as callbacks:
        with pytest.raises(RuntimeError), transaction.atomic():
            assign(wo, technician=techs["dana"], by=staff["kim"])
            raise RuntimeError("the request failed after assigning")
    assert callbacks == [] and mailoutbox == []
    wo.refresh_from_db()
    assert wo.assigned_to is None and not NotificationSent.objects.exists()


def test_a_rolled_back_batch_sends_nothing(staff, techs, committed, mailoutbox, monkeypatch, fleet):
    real = assign
    calls = []

    def failing(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("the second assignment failed")
        return real(*args, **kwargs)

    monkeypatch.setattr("apps.workorders.services.assign", failing)
    with committed() as callbacks:
        with pytest.raises(RuntimeError):
            create_pm_work_orders_for_day(SEP30, by=staff["kim"], today=TODAY)
    assert callbacks == [] and mailoutbox == [] and not WorkOrder.objects.exists()


def test_work_closed_or_moved_before_the_email_goes_is_not_announced(staff, techs, wo, vent, committed, mailoutbox):
    other = create_work_order(asset=vent, type=WoType.REPAIR, priority="normal", problem="Fan noise")
    with committed():
        with assignments.batch():
            assign(wo, technician=techs["dana"], by=staff["kim"])
            change_status(wo, WoStatus.CANCELLED, by=staff["kim"])  # cancelled in the same transaction
            assign(other, technician=techs["dana"], by=staff["kim"])
            assign(other, technician=techs["tom"], by=staff["kim"])  # the last announcement counts
    assert [m.to for m in mailoutbox] == [["tom@riverside.example"]] and numbers_in(mailoutbox[0]) == [other.number]


def test_a_failed_send_frees_its_claim_so_the_next_assignment_tries_again(staff, techs, wo, committed, mailoutbox, monkeypatch):
    monkeypatch.setattr("apps.accounts.emails.EmailMessage.send", lambda self, fail_silently=False: (_ for _ in ()).throw(OSError("SMTP down")))
    with committed():
        assign(wo, technician=techs["dana"], by=staff["kim"])
    assert mailoutbox == [] and not NotificationSent.objects.exists()
    monkeypatch.undo()
    with committed():
        assign(wo, technician=techs["dana"], by=staff["kim"])
    assert len(mailoutbox) == 1 and NotificationSent.objects.count() == 1


def test_a_database_error_while_sending_never_reaches_the_caller(staff, techs, wo, committed, mailoutbox, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("database went away")

    monkeypatch.setattr(assignments, "_send_in", broken)
    with committed():
        assign(wo, technician=techs["dana"], by=staff["kim"])
    assert mailoutbox == []
    item = assignments.Announcement(wo.tenant_id, wo.pk, techs["dana"].pk, staff["kim"].pk)
    assert assignments.send([item]) == 0


def test_quiet_announces_nothing(staff, techs, wo, committed, mailoutbox):
    with committed():
        with assignments.quiet():
            assign(wo, technician=techs["dana"], by=staff["kim"])
    assert mailoutbox == []


# --- each path ------------------------------------------------------------------------------------------------------------------

def test_the_drawers_assign(client, staff, techs, wo, committed, mailoutbox):
    client.force_login(staff["kim"])
    with committed():
        r = client.post(f"/work-orders/{wo.number}/assign/", {"assignee": str(techs["dana"].id)}, **HX)
    assert r.status_code == 200 and [m.to for m in mailoutbox] == [["dana@riverside.example"]]
    with committed():
        client.post(f"/work-orders/{wo.number}/assign/", {"assignee": "vendor"}, **HX)
    assert len(mailoutbox) == 1


def test_a_new_work_order_opened_with_an_assignee(client, staff, techs, vent, committed, mailoutbox):
    client.force_login(staff["kim"])
    with committed():
        r = client.post("/work-orders/new/", {"asset": vent.tag, "type": "repair", "priority": "critical", "problem": PROBLEM, "requester": REQUESTER,
                                              "assignee": str(techs["dana"].id)}, **HX)
    assert r.status_code == 200
    wo = WorkOrder.objects.get()
    assert len(mailoutbox) == 1 and numbers_in(mailoutbox[0]) == [wo.number] and "Critical priority" in mailoutbox[0].body
    assert_no_free_text(mailoutbox[0])


def test_the_api_create_with_an_assignee_and_the_api_assign(client, staff, techs, vent, wo, committed, mailoutbox):
    client.force_login(staff["kim"])
    with committed():
        r = client.post("/api/v1/work-orders/", {"asset": str(vent.id), "type": "repair", "priority": "high", "problem": PROBLEM,
                                                 "due_on": "2026-10-09", "assigned_to": str(techs["tom"].id)}, content_type="application/json")
    assert r.status_code == 201, r.json()
    created = WorkOrder.objects.get(pk=r.json()["id"])
    assert [m.to for m in mailoutbox] == [["tom@riverside.example"]] and numbers_in(mailoutbox[0]) == [created.number]
    with committed():
        r = client.post(f"/api/v1/work-orders/{wo.id}/assign/", {"technician": str(techs["dana"].id)}, content_type="application/json")
    assert r.status_code == 200 and [m.to for m in mailoutbox][1:] == [["dana@riverside.example"]]
    for m in mailoutbox:
        assert_no_free_text(m)


def test_the_service_create_announces_too_but_not_to_its_own_creator(staff, techs, vent, committed, mailoutbox):
    with committed():
        create_work_order(asset=vent, type=WoType.REPAIR, priority="normal", problem=PROBLEM, assigned_to=techs["dana"], created_by=staff["dana"])
        create_work_order(asset=vent, type=WoType.REPAIR, priority="normal", problem=PROBLEM, assigned_to=techs["tom"], created_by=staff["kim"])
        create_work_order(asset=vent, type=WoType.REPAIR, priority="normal", problem=PROBLEM, assigned_to=None, vendor_service=True,
                          vendor_name="Hamilton Medical field service")
    assert [m.to for m in mailoutbox] == [["tom@riverside.example"]]


# --- batches: a day's PMs, a week -----------------------------------------------------------------------------------------------

def dev(tag, model, dept, day):
    return Asset.objects.create(tag=tag, device_model=model, department=dept, next_pm_on=day)


@pytest.fixture
def fleet(ctx, dept, vent_model, pump_model):
    """Due Sep 30: two ventilators (only Dana is credentialed) and a pump (Dana or Tom: Tom's plate is lighter)."""
    vent_model.pm_procedure = PmProcedure.objects.create(code="HA-G5-PM6", name="G5 6-month PM", estimated_hours=Decimal("1.5"), checklist=["Inspect"])
    vent_model.save()
    return {"v1": dev("CE-V1", vent_model, dept, SEP30), "v2": dev("CE-V2", vent_model, dept, SEP30), "p1": dev("CE-P1", pump_model, dept, SEP30)}


def pm_numbers(*tags) -> list:
    return sorted(WorkOrder.objects.filter(type=WoType.PM, asset__tag__in=tags).values_list("number", flat=True))


def test_a_days_pms_send_each_technician_one_email(staff, techs, fleet, committed, mailoutbox):
    with committed():
        batch = create_pm_work_orders_for_day(SEP30, by=staff["kim"], today=TODAY)
    assert (batch.created, batch.assigned) == (3, 3)
    dana, tom = to(mailoutbox, "dana@riverside.example"), to(mailoutbox, "tom@riverside.example")
    assert len(mailoutbox) == 2 and len(dana) == 1 and len(tom) == 1
    assert numbers_in(dana[0]) == pm_numbers("CE-V1", "CE-V2") and numbers_in(tom[0]) == pm_numbers("CE-P1")
    assert dana[0].subject == "Assigned to you: 2 work orders at Riverside Regional" and "2 work orders at Riverside Regional are assigned" in dana[0].body
    assert "Preventive maintenance · High priority" in dana[0].body and "Due: Wednesday, Sep 30, 2026" in dana[0].body
    for m in mailoutbox:
        assert_no_free_text(m)
    assert NotificationSent.objects.count() == 3


def test_the_pm_screens_create_button(client, staff, techs, fleet, committed, mailoutbox, monkeypatch):
    monkeypatch.setattr("apps.web.views_pm._today", lambda: TODAY)
    client.force_login(staff["kim"])
    with committed():
        r = client.post(reverse("web:pm_create", args=["2026-09-30"]), **HX, HTTP_HX_TARGET="pm-body")
    assert r.status_code == 200 and sorted(m.to[0] for m in mailoutbox) == ["dana@riverside.example", "tom@riverside.example"]


def test_auto_assign_week_sends_each_technician_one_email_and_a_second_run_none(staff, techs, fleet, committed, mailoutbox):
    with committed():
        done = assign_week(by=staff["kim"], today=TODAY)
    assert done.assigned == 3 and len(mailoutbox) == 2
    assert numbers_in(to(mailoutbox, "dana@riverside.example")[0]) == pm_numbers("CE-V1", "CE-V2")
    assert numbers_in(to(mailoutbox, "tom@riverside.example")[0]) == pm_numbers("CE-P1")
    with committed():
        assign_week(by=staff["kim"], today=TODAY)
    assert len(mailoutbox) == 2


def test_auto_assign_week_announces_open_pms_it_puts_on_a_plate(client, staff, techs, fleet, committed, mailoutbox, monkeypatch):
    """PMs the nightly job opened, on nobody's plate, are assigned by Auto-assign week and announced; the screen's button too."""
    monkeypatch.setattr("apps.web.views_pm._today", lambda: TODAY)
    for a in fleet.values():
        create_work_order(asset=a, type=WoType.PM, priority="normal", problem="Scheduled preventive maintenance", opened_on=TODAY - timedelta(days=1),
                          due_on=SEP30)
    client.force_login(staff["kim"])
    with committed():
        r = client.post("/pm/week/assign/", **HX)
    assert r.status_code == 200 and len(mailoutbox) == 2
    assert numbers_in(to(mailoutbox, "dana@riverside.example")[0]) == pm_numbers("CE-V1", "CE-V2")


def test_a_batch_leaves_out_what_the_assigner_took_and_what_was_told_before(staff, techs, fleet, committed, mailoutbox):
    with committed():
        create_pm_work_orders_for_day(SEP30, by=staff["dana"], today=TODAY)  # Dana creates the day herself: only Tom hears
    assert [m.to for m in mailoutbox] == [["tom@riverside.example"]]


def test_an_opted_out_technician_in_a_batch_and_the_others_still_hear(staff, techs, fleet, committed, mailoutbox):
    set_preferences(staff["tom"], assignments=False)
    with committed():
        create_pm_work_orders_for_day(SEP30, by=staff["kim"], today=TODAY)
    assert [m.to for m in mailoutbox] == [["dana@riverside.example"]] and len(numbers_in(mailoutbox[0])) == 2


def test_nested_batches_join_the_outer_one(staff, techs, wo, vent, committed, mailoutbox):
    second = create_work_order(asset=vent, type=WoType.REPAIR, priority="normal", problem="Fan noise")
    with committed():
        with assignments.batch():
            assign(wo, technician=techs["dana"], by=staff["kim"])
            with assignments.batch():
                assign(second, technician=techs["dana"], by=staff["kim"])
    assert len(mailoutbox) == 1 and numbers_in(mailoutbox[0]) == sorted([wo.number, second.number])


# --- tenant isolation of what the emails remember ---------------------------------------------------------------------------------

def test_what_was_sent_stays_in_its_facility(staff, techs, wo, committed, mailoutbox, other_tenant):
    with committed():
        assign(wo, technician=techs["dana"], by=staff["kim"])
    assert NotificationSent.objects.count() == 1
    with tenant_context(other_tenant):
        assert not NotificationSent.objects.exists() and not NotificationPreference.objects.exists()
    assert NotificationSent.unscoped.count() == 1  # unscoped: the row exists, only in its own facility


# --- PostgreSQL: the sender under the policies ------------------------------------------------------------------------------------

@needs_postgres
def test_the_emails_under_row_level_security(staff, techs, wo, fleet, django_capture_on_commit_callbacks, mailoutbox, tenant):
    """As the runtime role: an assignment's email reads the work order, the device, the account, its preferences, and its role, and
    writes what it sent, inside the work order's facility, even when the caller left no tenant set when the commit ran."""
    set_preferences(staff["tom"], assignments=False)
    as_app_role()
    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        assign(wo, technician=techs["dana"], by=staff["kim"])
        create_pm_work_orders_for_day(SEP30, by=staff["kim"], today=TODAY)
    with tenant_context(None):  # as a job's commit with no tenant set: the sender enters the facility itself
        assert NotificationSent.unscoped.count() == 0  # unscoped and no tenant: the policy shows nothing
        for callback in callbacks:
            callback()
    assert [m.to for m in mailoutbox] == [["dana@riverside.example"], ["dana@riverside.example"]]  # Tom turned them off
    assert numbers_in(mailoutbox[0]) == [wo.number] and numbers_in(mailoutbox[1]) == pm_numbers("CE-V1", "CE-V2")
    with tenant_context(tenant):
        assert NotificationSent.objects.filter(user=staff["dana"]).count() == 3
    assert User.objects.get(pk=staff["dana"].pk).email == "dana@riverside.example"
